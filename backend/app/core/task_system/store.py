"""文件型任务系统 - 核心存储（TaskStore）

每个任务作为独立 JSON 文件持久化在:
    .happy-claw/tasks/{HAPPY_CLAW_TASK_LIST_ID}/{task_id}.json

能力:
    1. 文件锁与并发保护: 每个任务文件独立加锁（FileLock），防止多 Agent 同时修改同一任务；
       列表级锁（.lock）用于认领时的 agent busy 原子扫描。
    2. 依赖强制执行: blocks / blocked_by 双向依赖，认领时检查上游未完成则拒绝认领。
    3. 任务所有权: owner 字段标记认领者，认领检查防重复工作。
    4. 磁盘持久化: 跨会话、跨进程存活。

任务 ID 格式: 1 个字母的类型前缀 + 8 个随机字符，例如 b1a2b3c4d
    b: 本地Shell命令  a: 本地子代理  r: 远程代理
    d: 后台daemon任务  t: 进程内队友  n: 普通
"""
import asyncio
import json
import os
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
from filelock import FileLock, Timeout

from app.config import _HAPPYCLAW_HOME
from app.core.task_system.hooks import task_hook_manager

logger = structlog.get_logger()

# ===== 常量 =====

# 任务类型 -> ID 前缀
TASK_TYPE_PREFIXES = {
    "shell": "b",      # 本地Shell命令
    "subagent": "a",   # 本地子代理
    "remote": "r",     # 远程代理
    "daemon": "d",     # 后台daemon任务
    "teammate": "t",   # 进程内队友
    "normal": "n",     # 普通
}
VALID_PREFIXES = set(TASK_TYPE_PREFIXES.values())

TASK_STATUSES = ("pending", "in_progress", "completed", "deleted")

# 可认领状态之外的"已解决"状态
_RESOLVED_STATUSES = ("completed", "deleted")

# 环境变量: 任务列表 ID（未设置时默认为会话 id）
ENV_TASK_LIST_ID = "HAPPY_CLAW_TASK_LIST_ID"

_ID_RANDOM_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
_ID_RANDOM_LEN = 8

# 认领时视为"open"（agent 占用中）的状态
_OPEN_STATUSES = ("pending", "in_progress")


class TaskLockTimeoutError(RuntimeError):
    """任务文件锁获取超时"""


def _setting(name: str, default: Any = None) -> Any:
    """从全局配置读取任务系统参数（支持 settings.json tasks 段与内置默认值）"""
    try:
        from app.config import settings
        val = settings.get(name)
        return default if val is None else val
    except Exception:
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


class TaskStore:
    """文件型任务存储（磁盘持久化 + 文件锁并发保护）"""

    def __init__(self):
        self._lock_cache: Dict[str, FileLock] = {}
        self._lock_cache_guard = threading.Lock()

    # ===== 配置 =====

    @property
    def _lock_max_retries(self) -> int:
        return int(_setting("TASKS_LOCK_MAX_RETRIES", 30))

    @property
    def _lock_backoff_min_ms(self) -> int:
        return int(_setting("TASKS_LOCK_BACKOFF_MIN_MS", 5))

    @property
    def _lock_backoff_max_ms(self) -> int:
        return int(_setting("TASKS_LOCK_BACKOFF_MAX_MS", 100))

    @property
    def _retention_days(self) -> int:
        return int(_setting("TASKS_RETENTION_DAYS", 7))

    # ===== 路径 =====

    def tasks_root(self) -> Path:
        return _HAPPYCLAW_HOME / "tasks"

    def resolve_task_list_id(self, explicit: Optional[str] = None) -> str:
        """解析任务列表 ID: 显式参数 > 环境变量 > 'default'

        工具调用场景下由调用方传入会话 id 作为 explicit。
        """
        if explicit:
            return explicit
        env_val = os.environ.get(ENV_TASK_LIST_ID, "").strip()
        if env_val:
            return env_val
        return "default"

    def list_dir(self, task_list_id: str) -> Path:
        return self.tasks_root() / task_list_id

    def task_path(self, task_list_id: str, task_id: str) -> Path:
        return self.list_dir(task_list_id) / f"{task_id}.json"

    def list_task_lists(self) -> List[str]:
        """列出所有任务列表目录名"""
        root = self.tasks_root()
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    # ===== 锁 =====

    def _get_lock(self, lock_path: Path) -> FileLock:
        key = str(lock_path)
        with self._lock_cache_guard:
            lock = self._lock_cache.get(key)
            if lock is None:
                lock = FileLock(key)
                self._lock_cache[key] = lock
            return lock

    def _task_lock(self, task_list_id: str, task_id: str) -> FileLock:
        """任务级锁: {task_id}.json.lock"""
        return self._get_lock(self.task_path(task_list_id, task_id).with_suffix(".json.lock"))

    def _list_lock(self, task_list_id: str) -> FileLock:
        """列表级锁: {task_list_id}/.lock"""
        return self._get_lock(self.list_dir(task_list_id) / ".lock")

    @contextmanager
    def _locked(self, lock: FileLock):
        """获取锁（最多重试 N 次，指数退避 5-100ms，对齐 proper-lockfile 语义）"""
        max_retries = max(1, self._lock_max_retries)
        delay = max(1, self._lock_backoff_min_ms) / 1000.0
        max_delay = max(delay, self._lock_backoff_max_ms / 1000.0)

        acquired = False
        for attempt in range(1, max_retries + 1):
            try:
                lock.acquire(timeout=0)  # 非阻塞尝试
                acquired = True
                break
            except Timeout:
                if attempt == max_retries:
                    raise TaskLockTimeoutError(
                        f"Failed to acquire task lock after {max_retries} retries: {lock.lock_file}"
                    )
                time.sleep(delay)
                delay = min(delay * 2, max_delay)

        try:
            yield
        finally:
            if acquired:
                try:
                    lock.release()
                except Exception:
                    logger.debug("Task lock release skipped", lock=str(lock.lock_file))

    # ===== 基础读写 =====

    def _atomic_write(self, path: Path, data: Dict[str, Any]) -> None:
        """原子写入: 临时文件 + os.replace"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def _read_task_file(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning("Failed to read task file", path=str(path), error=str(e), exc_info=True)
            return None

    def _read_task(self, task_list_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        return self._read_task_file(self.task_path(task_list_id, task_id))

    def _scan_tasks(self, task_list_id: str) -> List[Dict[str, Any]]:
        """扫描任务列表目录下所有任务文件"""
        tasks = []
        dir_path = self.list_dir(task_list_id)
        if not dir_path.exists():
            return tasks
        for f in sorted(dir_path.glob("*.json")):
            if f.name.endswith(".tmp"):
                continue
            data = self._read_task_file(f)
            if data:
                tasks.append(data)
        return tasks

    def _generate_task_id(self, task_list_id: str, task_type: str) -> str:
        """生成任务 ID: 1个字母类型前缀 + 8个随机字符，碰撞则重生成"""
        prefix = TASK_TYPE_PREFIXES.get(task_type)
        if prefix is None:
            # 兼容直接传前缀字母
            if task_type in VALID_PREFIXES:
                prefix = task_type
            else:
                prefix = TASK_TYPE_PREFIXES["normal"]
        for _ in range(10):
            suffix = "".join(secrets.choice(_ID_RANDOM_CHARS) for _ in range(_ID_RANDOM_LEN))
            task_id = f"{prefix}{suffix}"
            if not self.task_path(task_list_id, task_id).exists():
                return task_id
        raise RuntimeError("Failed to generate unique task id after 10 attempts")

    # ===== 依赖双向同步 =====

    def _add_edge(self, task_list_id: str, task_id: str, field: str, value: str) -> None:
        """给任务 task_id 的 field 列表追加 value（锁内读改写）"""
        with self._locked(self._task_lock(task_list_id, task_id)):
            task = self._read_task(task_list_id, task_id)
            if not task or task.get("status") == "deleted":
                return
            edges = task.get(field) or []
            if value not in edges:
                edges.append(value)
                task[field] = edges
                task["updated_at"] = _now_iso()
                self._atomic_write(self.task_path(task_list_id, task_id), task)

    def _remove_edge(self, task_list_id: str, task_id: str, field: str, value: str) -> None:
        """从任务 task_id 的 field 列表中移除 value（锁内读改写）"""
        with self._locked(self._task_lock(task_list_id, task_id)):
            task = self._read_task(task_list_id, task_id)
            if not task:
                return
            edges = task.get(field) or []
            if value in edges:
                edges = [e for e in edges if e != value]
                task[field] = edges
                task["updated_at"] = _now_iso()
                self._atomic_write(self.task_path(task_list_id, task_id), task)

    def _sync_dependency_add(self, task_list_id: str, task_id: str,
                             blocks: List[str], blocked_by: List[str]) -> None:
        """创建任务后建立双向依赖边"""
        for downstream in blocks:
            if downstream != task_id:
                self._add_edge(task_list_id, downstream, "blocked_by", task_id)
        for upstream in blocked_by:
            if upstream != task_id:
                self._add_edge(task_list_id, upstream, "blocks", task_id)

    def _sync_dependency_change(self, task_list_id: str, task_id: str,
                                old_blocks: List[str], new_blocks: List[str],
                                old_blocked_by: List[str], new_blocked_by: List[str]) -> None:
        """更新任务依赖后同步双向边（仅处理差量）"""
        for downstream in set(new_blocks) - set(old_blocks):
            if downstream != task_id:
                self._add_edge(task_list_id, downstream, "blocked_by", task_id)
        for downstream in set(old_blocks) - set(new_blocks):
            if downstream != task_id:
                self._remove_edge(task_list_id, downstream, "blocked_by", task_id)
        for upstream in set(new_blocked_by) - set(old_blocked_by):
            if upstream != task_id:
                self._add_edge(task_list_id, upstream, "blocks", task_id)
        for upstream in set(old_blocked_by) - set(new_blocked_by):
            if upstream != task_id:
                self._remove_edge(task_list_id, upstream, "blocks", task_id)

    # ===== 同步核心操作（在线程中执行） =====

    def _create_task_sync(
        self,
        task_list_id: str,
        subject: str,
        description: str,
        active_form: str,
        task_type: str,
        blocks: List[str],
        blocked_by: List[str],
        metadata: Dict[str, Any],
        owner: Optional[str],
    ) -> Dict[str, Any]:
        task_id = self._generate_task_id(task_list_id, task_type)
        now = _now_iso()
        task = {
            "id": task_id,
            "subject": subject,
            "description": description,
            "active_form": active_form,
            "owner": owner,
            "status": "pending",
            "blocks": list(blocks or []),
            "blocked_by": list(blocked_by or []),
            "metadata": dict(metadata or {}),
            "created_at": now,
            "updated_at": now,
        }
        with self._locked(self._task_lock(task_list_id, task_id)):
            self._atomic_write(self.task_path(task_list_id, task_id), task)

        # 双向依赖同步（在本任务锁外逐个处理，避免锁嵌套）
        try:
            self._sync_dependency_add(task_list_id, task_id, task["blocks"], task["blocked_by"])
        except Exception as e:
            logger.warning("Dependency sync on create failed", task_id=task_id, error=str(e), exc_info=True)

        return task

    def _update_task_sync(
        self,
        task_list_id: str,
        task_id: str,
        patch: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """锁内更新任务，返回 (任务, 触发的hook事件名)"""
        with self._locked(self._task_lock(task_list_id, task_id)):
            task = self._read_task(task_list_id, task_id)
            if task is None:
                return None, None

            old_status = task.get("status")
            old_blocks = list(task.get("blocks") or [])
            old_blocked_by = list(task.get("blocked_by") or [])

            for field in ("subject", "description", "active_form", "owner", "status", "blocks", "blocked_by"):
                if field in patch and patch[field] is not None:
                    task[field] = patch[field]
            if patch.get("metadata") is not None:
                merged = dict(task.get("metadata") or {})
                merged.update(patch["metadata"])
                task["metadata"] = merged

            task["updated_at"] = _now_iso()
            self._atomic_write(self.task_path(task_list_id, task_id), task)

            new_status = task.get("status")
            new_blocks = list(task.get("blocks") or [])
            new_blocked_by = list(task.get("blocked_by") or [])

        # 依赖差量同步（锁外处理）
        if "blocks" in patch or "blocked_by" in patch:
            try:
                self._sync_dependency_change(
                    task_list_id, task_id, old_blocks, new_blocks, old_blocked_by, new_blocked_by
                )
            except Exception as e:
                logger.warning("Dependency sync on update failed", task_id=task_id, error=str(e), exc_info=True)

        # 确定生命周期事件
        if new_status != old_status:
            if new_status == "completed":
                event = "task_completed"
            elif new_status == "deleted":
                event = "task_deleted"
            elif new_status == "in_progress":
                event = "task_started"
            else:
                event = "task_updated"
        else:
            event = "task_updated"

        return task, event

    def _claim_task_sync(self, task_list_id: str, task_id: str, owner: str) -> Dict[str, Any]:
        """认领任务: 任务级锁内执行 5 步检查（防 TOCTOU）"""
        with self._locked(self._task_lock(task_list_id, task_id)):
            # 1. 重新读取任务（防 TOCTOU）
            task = self._read_task(task_list_id, task_id)
            if task is None or task.get("status") == "deleted":
                return {"ok": False, "reason": "not_found", "task_id": task_id}

            # 2. 检查被他人认领
            current_owner = task.get("owner")
            if current_owner and current_owner != owner:
                return {
                    "ok": False,
                    "reason": "already_claimed",
                    "task_id": task_id,
                    "current_owner": current_owner,
                }

            # 3. 检查已完成
            if task.get("status") == "completed":
                return {"ok": False, "reason": "already_resolved", "task_id": task_id}

            # 4. 检查上游未完成
            unfinished = []
            for upstream_id in task.get("blocked_by") or []:
                upstream = self._read_task(task_list_id, upstream_id)
                if upstream and upstream.get("status") not in _RESOLVED_STATUSES:
                    unfinished.append(upstream_id)
            if unfinished:
                return {"ok": False, "reason": "blocked", "task_id": task_id, "blocked_by": unfinished}

            # 5. 列表级锁: 原子扫描检查该 agent 是否已有其他 open task
            with self._locked(self._list_lock(task_list_id)):
                for other in self._scan_tasks(task_list_id):
                    if (
                        other.get("id") != task_id
                        and other.get("owner") == owner
                        and other.get("status") in _OPEN_STATUSES
                    ):
                        return {
                            "ok": False,
                            "reason": "agent_busy",
                            "task_id": task_id,
                            "busy_task": other.get("id"),
                        }

            old_status = task.get("status")
            task["owner"] = owner
            task["status"] = "in_progress"
            task["updated_at"] = _now_iso()
            self._atomic_write(self.task_path(task_list_id, task_id), task)

        return {"ok": True, "reason": "claimed", "task": task, "previous_status": old_status}

    def _hard_delete_sync(self, task_list_id: str, task_id: str) -> bool:
        """物理删除任务文件及其锁文件"""
        path = self.task_path(task_list_id, task_id)
        lock_path = path.with_suffix(".json.lock")
        deleted = False
        for p in (path, lock_path):
            try:
                if p.exists():
                    p.unlink()
                    deleted = True
            except Exception as e:
                logger.warning("Failed to delete task file", path=str(p), error=str(e), exc_info=True)
        return deleted

    def _cleanup_expired_sync(self) -> int:
        """启动清理: 删除创建时间超过保留天数的任务文件"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        removed = 0
        root = self.tasks_root()
        if not root.exists():
            return 0

        for list_dir in root.iterdir():
            if not list_dir.is_dir():
                continue
            task_list_id = list_dir.name
            for f in list(list_dir.glob("*.json")):
                data = self._read_task_file(f)
                created_at = _parse_iso((data or {}).get("created_at")) if data else None
                if created_at is None:
                    # 无 created_at 时回退为文件修改时间
                    try:
                        created_at = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                    except Exception:
                        continue
                if created_at < cutoff:
                    task_id = f.stem
                    if self._hard_delete_sync(task_list_id, task_id):
                        removed += 1
                        logger.info("Expired task cleaned", task_id=task_id, task_list_id=task_list_id)
        return removed

    # ===== 异步公开 API =====

    async def create_task(
        self,
        subject: str,
        description: str = "",
        active_form: str = "",
        task_type: str = "normal",
        blocks: Optional[List[str]] = None,
        blocked_by: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        owner: Optional[str] = None,
        task_list_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建任务并持久化，返回完整任务对象"""
        list_id = self.resolve_task_list_id(task_list_id)
        task = await asyncio.to_thread(
            self._create_task_sync,
            list_id, subject, description, active_form, task_type,
            list(blocks or []), list(blocked_by or []), dict(metadata or {}), owner,
        )
        await task_hook_manager.emit("task_created", task)
        logger.info("Task created", task_id=task["id"], task_list_id=list_id, subject=subject)
        return task

    async def get_task(self, task_id: str, task_list_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取任务详情"""
        list_id = self.resolve_task_list_id(task_list_id)
        return await asyncio.to_thread(self._read_task, list_id, task_id)

    async def list_tasks(
        self,
        status: Optional[str] = None,
        owner: Optional[str] = None,
        task_list_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出任务（可按状态/所有者过滤）"""
        list_id = self.resolve_task_list_id(task_list_id)
        tasks = await asyncio.to_thread(self._scan_tasks, list_id)
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        if owner:
            tasks = [t for t in tasks if t.get("owner") == owner]
        return tasks

    async def update_task(
        self,
        task_id: str,
        patch: Dict[str, Any],
        task_list_id: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """更新任务字段，返回 (任务, 触发的事件名)。任务不存在时返回 (None, None)"""
        list_id = self.resolve_task_list_id(task_list_id)
        task, event = await asyncio.to_thread(self._update_task_sync, list_id, task_id, dict(patch))
        if task is not None and event:
            await task_hook_manager.emit(event, task)
            logger.info("Task updated", task_id=task_id, task_list_id=list_id, task_event=event)
        return task, event

    async def claim_task(
        self,
        task_id: str,
        owner: str,
        task_list_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """认领任务（并发安全的 5 步检查）

        Returns:
            {"ok": True, "task": {...}} 或
            {"ok": False, "reason": "not_found|already_claimed|already_resolved|blocked|agent_busy", ...}
        """
        list_id = self.resolve_task_list_id(task_list_id)
        result = await asyncio.to_thread(self._claim_task_sync, list_id, task_id, owner)
        if result.get("ok"):
            await task_hook_manager.emit("task_started", result["task"])
            logger.info("Task claimed", task_id=task_id, owner=owner, task_list_id=list_id)
        else:
            logger.info("Task claim rejected", task_id=task_id, owner=owner, reason=result.get("reason"))
        return result

    async def delete_task(
        self,
        task_id: str,
        hard: bool = False,
        task_list_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """删除任务。默认软删除（置 status=deleted 保留文件），hard=True 时物理删除文件"""
        list_id = self.resolve_task_list_id(task_list_id)
        if hard:
            deleted = await asyncio.to_thread(self._hard_delete_sync, list_id, task_id)
            if deleted:
                await task_hook_manager.emit("task_deleted", {"id": task_id})
            return {"ok": deleted, "task_id": task_id, "hard": True}

        task, event = await self.update_task(task_id, {"status": "deleted"}, task_list_id=list_id)
        if task is None:
            return {"ok": False, "task_id": task_id, "error": "Task not found"}
        return {"ok": True, "task_id": task_id, "task": task}

    async def cleanup_expired(self) -> int:
        """启动清理: 删除创建时间超过保留天数的任务文件，返回删除数量"""
        return await asyncio.to_thread(self._cleanup_expired_sync)

    def remove_task_list(self, task_list_id: str) -> bool:
        """整体删除某个任务列表目录（会话删除后清理）"""
        dir_path = self.list_dir(task_list_id)
        if not dir_path.exists():
            return False
        try:
            shutil.rmtree(dir_path)
            logger.info("Task list directory removed", task_list_id=task_list_id, path=str(dir_path))
            return True
        except Exception as e:
            logger.error("Failed to remove task list directory",
                         task_list_id=task_list_id, error=str(e), exc_info=True)
            return False

    def remove_task_lists_by_prefix(self, prefix: str) -> int:
        """按前缀批量删除任务列表目录（如 team-{session_id}-*），返回删除数量"""
        root = self.tasks_root()
        if not root.exists():
            return 0
        removed = 0
        for p in root.iterdir():
            if p.is_dir() and p.name.startswith(prefix):
                try:
                    shutil.rmtree(p)
                    logger.info("Task list directory removed by prefix", prefix=prefix, dir_name=p.name)
                    removed += 1
                except Exception as e:
                    logger.error("Failed to remove task list directory by prefix",
                                 prefix=prefix, dir_name=p.name, error=str(e), exc_info=True)
        return removed


# 全局单例
task_store = TaskStore()
