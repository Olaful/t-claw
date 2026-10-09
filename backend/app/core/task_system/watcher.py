"""任务文件监听器 - fs.watch 等价的响应式监听

由于任务存储在文件系统中，本监听器通过轮询扫描任务目录
（文件名集合 + mtime）实时感知任务文件变更，并通过事件总线
发布 `task_update` 事件，使 UI 或 Agent 能响应式地感知任务状态更新。
"""
import asyncio
from typing import Dict, Optional

import structlog

from app.core.event_bus import event_bus
from app.core.task_system.store import task_store

logger = structlog.get_logger()


class TaskWatcher:
    """任务目录轮询监听器（后台 asyncio 任务）"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # 快照: task_list_id -> {文件名: mtime}
        self._snapshot: Dict[str, Dict[str, float]] = {}

    @property
    def running(self) -> bool:
        return self._running

    def _scan(self) -> Dict[str, Dict[str, float]]:
        """扫描所有任务列表目录，返回 {task_list_id: {文件名: mtime}}"""
        snapshot: Dict[str, Dict[str, float]] = {}
        try:
            for task_list_id in task_store.list_task_lists():
                files: Dict[str, float] = {}
                dir_path = task_store.list_dir(task_list_id)
                for f in dir_path.iterdir():
                    if not f.is_file():
                        continue
                    # 忽略锁文件与临时文件
                    if f.name.endswith(".lock") or f.name.endswith(".tmp"):
                        continue
                    try:
                        files[f.name] = f.stat().st_mtime
                    except OSError:
                        continue
                snapshot[task_list_id] = files
        except Exception as e:
            logger.warning("Task watcher scan failed", error=str(e), exc_info=True)
        return snapshot

    async def _emit_change(self, task_list_id: str, filename: str, change: str) -> None:
        task_id = filename[:-5] if filename.endswith(".json") else filename
        try:
            await event_bus.emit_event(
                type="task_update",
                source=f"task_list:{task_list_id}",
                data={
                    "task_list_id": task_list_id,
                    "task_id": task_id,
                    "change": change,  # added / removed / modified
                },
                priority="normal",
                dimensions=[f"task_list:{task_list_id}"],
            )
        except Exception as e:
            logger.warning("Task update event emit failed",
                           task_id=task_id, change=change, error=str(e), exc_info=True)

    async def _run(self, interval: float) -> None:
        logger.info("Task watcher started", interval_seconds=interval)
        while self._running:
            try:
                current = await asyncio.to_thread(self._scan)
                old = self._snapshot

                all_lists = set(current) | set(old)
                for task_list_id in all_lists:
                    old_files = old.get(task_list_id, {})
                    new_files = current.get(task_list_id, {})

                    for name in new_files:
                        if name not in old_files:
                            await self._emit_change(task_list_id, name, "added")
                        elif new_files[name] != old_files[name]:
                            await self._emit_change(task_list_id, name, "modified")
                    for name in old_files:
                        if name not in new_files:
                            await self._emit_change(task_list_id, name, "removed")

                self._snapshot = current
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Task watcher loop error", error=str(e), exc_info=True)

            await asyncio.sleep(interval)

    async def start(self) -> None:
        """启动监听器（幂等）"""
        if self._running:
            return
        try:
            from app.config import settings
            enabled = settings.get("TASKS_WATCHER_ENABLED", True)
            interval = float(settings.get("TASKS_WATCHER_INTERVAL_SECONDS", 2))
        except Exception:
            enabled, interval = True, 2.0

        if not enabled:
            logger.info("Task watcher disabled by config")
            return

        self._running = True
        # 初始化快照，避免启动瞬间把已有任务全部当作 added 上报
        self._snapshot = await asyncio.to_thread(self._scan)
        self._task = asyncio.create_task(self._run(max(0.5, interval)))

    async def stop(self) -> None:
        """停止监听器（幂等）"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
            logger.info("Task watcher stopped")


# 全局单例
task_watcher = TaskWatcher()
