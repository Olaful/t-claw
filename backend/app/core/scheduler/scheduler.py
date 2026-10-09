"""定时任务调度器 - 1秒轮询 + 抖动（防惊群） + 错过补跑（Catch-up）

触发判定:
    task.json 持久化 next_fire_at（毫秒时间戳）。每次轮询发现 now >= next_fire_at
    且任务未在执行中，即触发一次。休眠/关机/应用关闭导致错过时，唤醒后下次轮询
    发现到期只补跑一次。

抖动（可通过 SCHEDULE_JITTER_ENABLED 配置）:
    1. 重复任务: 确定性偏移 = stable_hash(task_id) % min(period * ratio, max_seconds)
       实际触发时间 = 计划时间 + 偏移（同一任务每次偏移固定）
    2. 一次性任务: 计划时间落在 :00 或 :30 时，提前最多 edge_seconds 触发（提前不延后）
"""
import asyncio
import time
import zlib
from datetime import datetime, timedelta
from typing import Optional

import structlog

from app.core.scheduler.cron import parse as parse_cron, CronParseError
from app.core.scheduler.store import schedule_store

logger = structlog.get_logger()


def _setting(name: str, default):
    try:
        from app.config import settings
        val = settings.get(name)
        return default if val is None else val
    except Exception:
        return default


def stable_hash(text: str) -> int:
    """任务ID的确定性哈希（跨进程稳定）"""
    return zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF


def compute_next_fire_ms(state: dict, after_ms: Optional[int] = None) -> Optional[int]:
    """计算任务的下一次触发时间（毫秒时间戳，含抖动）

    - 重复任务: 基于 cron 表达式求 after 之后的下次匹配时间，加确定性抖动偏移
    - 一次性任务: 基于 at 字段（ISO 时间），整点/半点时提前随机抖动
    """
    recurring = state.get("recurring", True)
    task_id = state.get("id", "")
    jitter_enabled = bool(_setting("SCHEDULE_JITTER_ENABLED", True))

    if not recurring:
        at_str = state.get("at")
        if not at_str:
            return None
        try:
            planned = datetime.fromisoformat(at_str)
        except Exception:
            logger.warning("Invalid 'at' time for scheduled task", task_id=task_id, at=at_str)
            return None

        if jitter_enabled:
            # 仅当计划时间落在整点或半点时，提前最多 edge_seconds 触发
            if planned.minute in (0, 30) and planned.second == 0:
                edge_seconds = int(_setting("SCHEDULE_JITTER_ONESHOT_EDGE_SECONDS", 90))
                if edge_seconds > 0:
                    offset = stable_hash(task_id) % edge_seconds
                    planned = planned - timedelta(seconds=offset)

        return int(planned.timestamp() * 1000)

    # 重复任务
    cron_expr = state.get("cron")
    if not cron_expr:
        return None
    try:
        sched = parse_cron(cron_expr)
    except CronParseError as e:
        logger.warning("Invalid cron expression for scheduled task", task_id=task_id, cron=cron_expr, error=str(e))
        return None

    after_dt = datetime.fromtimestamp(after_ms / 1000) if after_ms else datetime.now()
    planned = sched.next_fire(after_dt)
    if planned is None:
        return None

    if jitter_enabled:
        period = sched.period_seconds(planned)
        if period > 0:
            ratio = float(_setting("SCHEDULE_JITTER_RECURRING_RATIO", 0.1))
            max_seconds = int(_setting("SCHEDULE_JITTER_RECURRING_MAX_SECONDS", 900))
            max_offset = min(int(period * ratio), max_seconds)
            if max_offset > 0:
                offset = stable_hash(task_id) % max_offset
                planned = planned + timedelta(seconds=offset)

    return int(planned.timestamp() * 1000)


class Scheduler:
    """定时任务调度器（单例，asyncio 后台轮询）"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running_tasks: set = set()  # 正在执行的 task_id 集合（双重保险）

    @property
    def enabled(self) -> bool:
        return bool(_setting("SCHEDULER_ENABLED", True))

    @property
    def poll_interval(self) -> float:
        return float(_setting("SCHEDULER_POLL_INTERVAL_SECONDS", 1))

    async def start(self):
        """启动调度器（应用生命周期开始时调用）"""
        if not self.enabled:
            logger.info("Scheduler disabled via config")
            return
        if self._task and not self._task.done():
            return

        # 启动时确保所有任务都有 next_fire_at（重启后恢复调度 + 过期清理，文件 I/O 在线程池执行）
        removed = await asyncio.to_thread(schedule_store.cleanup_expired)
        await self._ensure_next_fire_times()

        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started", poll_interval=self.poll_interval)

    async def stop(self):
        """停止调度器"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        logger.info("Scheduler stopped")

    async def _ensure_next_fire_times(self):
        """为缺失 next_fire_at 的任务计算下次触发时间（新创建或重启恢复）"""
        # ✅ 同步文件 I/O 移至线程池，避免阻塞事件循环（影响 FastAPI 请求/SSE 流）
        tasks = await asyncio.to_thread(schedule_store.list_tasks)
        for state in tasks:
            if state.get("next_fire_at"):
                continue
            next_ms = compute_next_fire_ms(state)
            if next_ms is None:
                continue
            await asyncio.to_thread(
                schedule_store.update_state,
                state["session_id"], state["id"], {"next_fire_at": next_ms}
            )

    async def _loop(self):
        """1秒轮询主循环"""
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Scheduler tick error", error=str(e), exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _tick(self):
        now_ms = int(time.time() * 1000)

        # ✅ 同步文件 I/O 移至线程池，避免轮询时阻塞事件循环主流程
        states = await asyncio.to_thread(schedule_store.list_tasks)

        for state in states:
            task_id = state.get("id")
            session_id = state.get("session_id")
            if not task_id or not session_id:
                continue

            # 防并发：文件标记或内存标记
            if state.get("running") or task_id in self._running_tasks:
                continue

            next_fire_at = state.get("next_fire_at")
            if not next_fire_at or now_ms < next_fire_at:
                continue

            # 到期（含错过补跑场景）：标记执行并触发（写盘在线程池执行）
            updated = await asyncio.to_thread(
                schedule_store.update_state, session_id, task_id, {"running": True}
            )
            if updated is None:
                continue  # 任务可能已被删除
            self._running_tasks.add(task_id)

            logger.info(
                "Scheduled task triggered",
                task_id=task_id,
                session_id=session_id,
                scheduled_at=next_fire_at,
                now=now_ms,
                catch_up=(now_ms - next_fire_at) > 120_000,
            )

            from app.core.scheduler.runner import scheduled_task_runner
            asyncio.create_task(self._run_and_finalize(session_id, task_id, scheduled_task_runner))

    async def _run_and_finalize(self, session_id: str, task_id: str, runner):
        """执行任务并在结束后更新调度状态"""
        try:
            await runner.run(session_id, task_id)
        except Exception as e:
            logger.error("Scheduled task execution failed", task_id=task_id, error=str(e), exc_info=True)
        finally:
            self._running_tasks.discard(task_id)
            state = await asyncio.to_thread(schedule_store.get, session_id, task_id)
            if state is None:
                return  # 一次性任务已被删除

            if state.get("recurring"):
                # 重复任务：以上次触发点为基准计算下一次（避免漂移）
                next_ms = compute_next_fire_ms(state, after_ms=int(time.time() * 1000))
                await asyncio.to_thread(
                    schedule_store.update_state,
                    session_id, task_id, {"running": False, "next_fire_at": next_ms}
                )
            else:
                # 一次性任务执行后删除（写盘在线程池执行）
                await asyncio.to_thread(schedule_store.delete, session_id, task_id)


# 全局单例
scheduler = Scheduler()
