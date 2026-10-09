"""定时任务存储 - 文件型持久化

目录结构:
    .happy-claw/scheduled-tasks/{session-id}/{task-id}/
        SKILL.md    定时任务触发时 agent 需要执行的指令提示（带 frontmatter）
        task.json   任务元数据 {id, cron, at, recurring, durable, agent_id, session_id,
                    created_at, last_run_at, next_fire_at, running, name, description}
        .output     定时任务执行结果（recurring=true 时覆盖写）

任务 ID: 8 位随机字符（如 abc12345）
"""
import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from app.config import _HAPPYCLAW_HOME

logger = structlog.get_logger()

_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
_ID_LEN = 8

SKILL_FILE = "SKILL.md"
STATE_FILE = "task.json"
OUTPUT_FILE = ".output"


def _setting(name: str, default: Any = None) -> Any:
    """从全局配置读取调度器参数"""
    try:
        from app.config import settings
        val = settings.get(name)
        return default if val is None else val
    except Exception:
        return default


def _now_ms() -> int:
    return int(time.time() * 1000)


class MaxScheduleExceededError(RuntimeError):
    """定时任务数量超过上限"""

    def __init__(self, max_schedule: int):
        super().__init__(f"Too many scheduled jobs (max {max_schedule}). Cancel one first.")
        self.max_schedule = max_schedule


class ScheduledTaskStore:
    """定时任务文件存储"""

    # ===== 路径 =====

    def root(self) -> Path:
        return _HAPPYCLAW_HOME / "scheduled-tasks"

    def session_dir(self, session_id: str) -> Path:
        return self.root() / session_id

    def task_dir(self, session_id: str, task_id: str) -> Path:
        return self.session_dir(session_id) / task_id

    # ===== CRUD =====

    def create(
        self,
        session_id: str,
        agent_id: str,
        skill_md: str,
        cron: Optional[str] = None,
        at: Optional[str] = None,
        recurring: bool = True,
        durable: bool = True,
        name: str = "",
        description: str = "",
    ) -> Dict[str, Any]:
        """创建定时任务（全局数量上限检查）"""
        max_schedule = int(_setting("MAX_SCHEDULE", 50))
        current = self.count()
        if current >= max_schedule:
            raise MaxScheduleExceededError(max_schedule)

        task_id = "".join(secrets.choice(_ID_CHARS) for _ in range(_ID_LEN))
        tdir = self.task_dir(session_id, task_id)
        tdir.mkdir(parents=True, exist_ok=True)

        # SKILL.md
        (tdir / SKILL_FILE).write_text(skill_md, encoding="utf-8")

        # task.json
        state = {
            "id": task_id,
            "cron": cron,
            "at": at,
            "recurring": recurring,
            "durable": durable,
            "agent_id": agent_id,
            "session_id": session_id,
            "name": name,
            "description": description,
            "created_at": _now_ms(),
            "last_run_at": None,
            "next_fire_at": None,
            "running": False,
        }
        self._write_state(tdir, state)

        logger.info(
            "Scheduled task created",
            task_id=task_id,
            session_id=session_id,
            cron=cron,
            at=at,
            recurring=recurring,
        )
        return state

    def delete(self, session_id: str, task_id: str) -> bool:
        """删除任务目录"""
        tdir = self.task_dir(session_id, task_id)
        if not tdir.exists():
            return False
        shutil.rmtree(tdir, ignore_errors=True)
        logger.info("Scheduled task deleted", task_id=task_id, session_id=session_id)
        return True

    def get(self, session_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        tdir = self.task_dir(session_id, task_id)
        return self._read_state(tdir)

    def list_tasks(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出任务（可按会话过滤）"""
        root = self.root()
        if not root.exists():
            return []

        tasks: List[Dict[str, Any]] = []
        session_dirs = [root / session_id] if session_id else list(root.iterdir())
        for sdir in session_dirs:
            if not sdir.is_dir():
                continue
            for tdir in sdir.iterdir():
                if not tdir.is_dir():
                    continue
                state = self._read_state(tdir)
                if state:
                    tasks.append(state)
        return tasks

    def count(self) -> int:
        """全局任务数量"""
        root = self.root()
        if not root.exists():
            return 0
        n = 0
        for sdir in root.iterdir():
            if not sdir.is_dir():
                continue
            for tdir in sdir.iterdir():
                if tdir.is_dir() and (tdir / STATE_FILE).exists():
                    n += 1
        return n

    # ===== 内容读写 =====

    def read_skill(self, session_id: str, task_id: str) -> Optional[str]:
        path = self.task_dir(session_id, task_id) / SKILL_FILE
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def write_output(self, session_id: str, task_id: str, content: str):
        """写入任务结果（覆盖）"""
        tdir = self.task_dir(session_id, task_id)
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / OUTPUT_FILE).write_text(content, encoding="utf-8")

    def update_state(self, session_id: str, task_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """合并式更新 task.json"""
        tdir = self.task_dir(session_id, task_id)
        state = self._read_state(tdir)
        if state is None:
            return None
        state.update(patch)
        self._write_state(tdir, state)
        return state

    # ===== 清理 =====

    def cleanup_session(self, session_id: str):
        """会话删除后清理其全部定时任务"""
        sdir = self.session_dir(session_id)
        if sdir.exists():
            shutil.rmtree(sdir, ignore_errors=True)
            logger.info("Scheduled tasks cleaned for session", session_id=session_id)

    def cleanup_expired(self, retention_days: Optional[int] = None) -> int:
        """启动清理：
        1. 删除超过 N 天的一次性任务（recurring=false 的残留）
        2. 删除所有非持久化任务（durable=false，仅存活于单次运行期）
        """
        days = int(retention_days if retention_days is not None else _setting("SCHEDULED_TASKS_RETENTION_DAYS", 7))
        root = self.root()
        if not root.exists():
            return 0

        now_ms = _now_ms()
        expire_ms = days * 86400 * 1000
        removed = 0

        for sdir in list(root.iterdir()):
            if not sdir.is_dir():
                continue
            for tdir in list(sdir.iterdir()):
                if not tdir.is_dir():
                    continue
                state = self._read_state(tdir)
                if state is None:
                    continue
                created_at = state.get("created_at") or 0
                expired_once = (not state.get("recurring")) and (now_ms - created_at > expire_ms)
                non_durable = not state.get("durable", True)
                if expired_once or non_durable:
                    shutil.rmtree(tdir, ignore_errors=True)
                    removed += 1

        if removed:
            logger.info("Expired scheduled tasks cleaned", removed=removed, retention_days=days)
        return removed

    # ===== 内部 =====

    @staticmethod
    def _read_state(tdir: Path) -> Optional[Dict[str, Any]]:
        path = tdir / STATE_FILE
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to read scheduled task state", path=str(path), error=str(e))
            return None

    @staticmethod
    def _write_state(tdir: Path, state: Dict[str, Any]):
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / STATE_FILE).write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# 全局单例
schedule_store = ScheduledTaskStore()
