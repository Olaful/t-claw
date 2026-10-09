"""团队协作 - 文件存储层（TeamStore）

目录结构：
    .happy-claw/teams/{session-id}/{teamName}/
    ├── config.json                 # 团队配置
    └── inboxes/
        ├── {leadName}.json         # Lead 收件箱
        └── {teammateName}.json     # 队友收件箱

收件箱格式（JSON 数组）：
    [
      {
        "from": "worker-1",
        "text": "我已完成对 auth.js 模块的初步审查...",
        "timestamp": "2026-08-30T10:15:30Z",
        "read": false,
        "summary": "Auth 模块审查完成",
        "color": "#3498db"
      }
    ]

并发安全：所有写入均通过 FileLock（对齐 proper-lockfile 语义：
读→修改→原子写回，最多重试 TEAMS_INBOX_LOCK_MAX_RETRIES 次）。
"""
import json
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from filelock import FileLock, Timeout

from app.config import _HAPPYCLAW_HOME
from app.core.team_system.models import (
    TEAMMATE_COLOR_NAMES,
    TEAMMATE_COLOR_POOL,
    make_inbox_message,
)

logger = structlog.get_logger()


class TeamLockTimeoutError(RuntimeError):
    """团队文件锁获取超时"""


def _setting(name: str, default: Any = None) -> Any:
    """从全局配置读取团队系统参数"""
    try:
        from app.config import settings
        val = settings.get(name)
        return default if val is None else val
    except Exception:
        return default


def _sanitize(name: str) -> str:
    """清理目录/文件名中的非法字符"""
    return "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "unnamed"


class TeamStore:
    """团队文件存储（配置 + 收件箱，磁盘持久化 + 文件锁并发保护）"""

    def __init__(self):
        self._lock_cache: Dict[str, FileLock] = {}
        self._lock_cache_guard = threading.Lock()

    # ===== 配置 =====

    @property
    def _lock_max_retries(self) -> int:
        return max(1, int(_setting("TEAMS_INBOX_LOCK_MAX_RETRIES", 10)))

    # ===== 路径 =====

    def teams_root(self) -> Path:
        return _HAPPYCLAW_HOME / "teams"

    def session_dir(self, session_id: str) -> Path:
        return self.teams_root() / _sanitize(session_id)

    def team_dir(self, session_id: str, team_name: str) -> Path:
        return self.session_dir(session_id) / _sanitize(team_name)

    def config_path(self, session_id: str, team_name: str) -> Path:
        return self.team_dir(session_id, team_name) / "config.json"

    def inbox_dir(self, session_id: str, team_name: str) -> Path:
        return self.team_dir(session_id, team_name) / "inboxes"

    def inbox_path(self, session_id: str, team_name: str, member_name: str) -> Path:
        return self.inbox_dir(session_id, team_name) / f"{_sanitize(member_name)}.json"

    # ===== 锁 =====

    def _get_lock(self, lock_path: Path) -> FileLock:
        key = str(lock_path)
        with self._lock_cache_guard:
            lock = self._lock_cache.get(key)
            if lock is None:
                lock = FileLock(key)
                self._lock_cache[key] = lock
            return lock

    @contextmanager
    def _locked(self, lock: FileLock):
        """获取锁（最多重试 N 次，指数退避 10-100ms，对齐 proper-lockfile 语义）"""
        max_retries = self._lock_max_retries
        delay = 0.01
        max_delay = 0.1

        acquired = False
        for attempt in range(1, max_retries + 1):
            try:
                lock.acquire(timeout=0)
                acquired = True
                break
            except Timeout:
                if attempt == max_retries:
                    raise TeamLockTimeoutError(
                        f"Failed to acquire team lock after {max_retries} retries: {lock.lock_file}"
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
                    logger.debug("Team lock release skipped", lock=str(lock.lock_file))

    def _team_lock(self, session_id: str, team_name: str) -> FileLock:
        """团队配置锁"""
        return self._get_lock(self.config_path(session_id, team_name).with_suffix(".json.lock"))

    def _inbox_lock(self, session_id: str, team_name: str, member_name: str) -> FileLock:
        """收件箱锁"""
        return self._get_lock(self.inbox_path(session_id, team_name, member_name).with_suffix(".json.lock"))

    # ===== 原子读写 =====

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        """原子写入：临时文件 + os.replace"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        import os
        os.replace(tmp, path)

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read team JSON file", path=str(path), error=str(e))
            return default

    # ===== 团队配置（config.json） =====

    def create_team(
        self,
        session_id: str,
        team_name: str,
        lead_name: str,
        lead_agent_id: str,
    ) -> Dict[str, Any]:
        """创建团队：初始化 config.json 与 Lead 收件箱（幂等，已存在则返回现有配置）"""
        existing = self.load_config(session_id, team_name)
        if existing:
            return existing

        config = {
            "name": team_name,
            "leadAgentId": lead_agent_id,
            "leadName": lead_name,
            "members": [
                {
                    "agentId": lead_agent_id,
                    "name": lead_name,
                    "agentType": "team-lead",
                    "color": TEAMMATE_COLOR_POOL[0],
                    "colorName": TEAMMATE_COLOR_NAMES[0],
                    "isLead": True,
                    "isActive": True,
                }
            ],
        }
        with self._locked(self._team_lock(session_id, team_name)):
            # double-check：并发创建时只保留第一个
            if self.config_path(session_id, team_name).exists():
                return self._read_json(self.config_path(session_id, team_name), config)
            self._atomic_write_json(self.config_path(session_id, team_name), config)
            # 初始化 Lead 收件箱
            self._atomic_write_json(self.inbox_path(session_id, team_name, lead_name), [])
        logger.info("Team created", session_id=session_id, team=team_name, lead=lead_name)
        return config

    def load_config(self, session_id: str, team_name: str) -> Optional[Dict[str, Any]]:
        path = self.config_path(session_id, team_name)
        if not path.exists():
            return None
        return self._read_json(path, None)

    def save_config(self, session_id: str, team_name: str, config: Dict[str, Any]) -> None:
        with self._locked(self._team_lock(session_id, team_name)):
            self._atomic_write_json(self.config_path(session_id, team_name), config)

    def get_member(self, config: Dict[str, Any], member_name: str) -> Optional[Dict[str, Any]]:
        for m in config.get("members", []):
            if m.get("name") == member_name:
                return m
        return None

    def allocate_color(self, config: Dict[str, Any]) -> tuple:
        """分配下一个未使用的颜色，返回 (color, colorName)"""
        used = {m.get("colorName") for m in config.get("members", [])}
        for name, color in zip(TEAMMATE_COLOR_NAMES, TEAMMATE_COLOR_POOL):
            if name not in used:
                return color, name
        # 颜色用尽时循环复用
        idx = len(config.get("members", [])) % len(TEAMMATE_COLOR_POOL)
        return TEAMMATE_COLOR_POOL[idx], TEAMMATE_COLOR_NAMES[idx]

    def add_member(
        self,
        session_id: str,
        team_name: str,
        agent_id: str,
        name: str,
        agent_type: str = "general-purpose",
    ) -> Dict[str, Any]:
        """添加成员到团队配置（含颜色分配与收件箱初始化），返回成员条目"""
        with self._locked(self._team_lock(session_id, team_name)):
            config = self._read_json(self.config_path(session_id, team_name), None)
            if config is None:
                raise ValueError(f"Team not found: {session_id}/{team_name}")
            existing = self.get_member(config, name)
            if existing:
                # 已存在：更新为激活状态
                existing["isActive"] = True
                existing["agentId"] = agent_id
                self._atomic_write_json(self.config_path(session_id, team_name), config)
                return existing
            color, color_name = self.allocate_color(config)
            member = {
                "agentId": agent_id,
                "name": name,
                "agentType": agent_type,
                "color": color,
                "colorName": color_name,
                "isLead": False,
                "isActive": True,
            }
            config["members"].append(member)
            self._atomic_write_json(self.config_path(session_id, team_name), config)
        # 初始化收件箱
        inbox = self.inbox_path(session_id, team_name, name)
        if not inbox.exists():
            self._atomic_write_json(inbox, [])
        logger.info("Team member added", team=team_name, member=name, color=color_name)
        return member

    def remove_member(self, session_id: str, team_name: str, member_name: str) -> bool:
        """从团队配置移除成员"""
        with self._locked(self._team_lock(session_id, team_name)):
            config = self._read_json(self.config_path(session_id, team_name), None)
            if config is None:
                return False
            before = len(config.get("members", []))
            config["members"] = [
                m for m in config.get("members", []) if m.get("name") != member_name
            ]
            removed = len(config["members"]) < before
            if removed:
                self._atomic_write_json(self.config_path(session_id, team_name), config)
        if removed:
            logger.info("Team member removed", team=team_name, member=member_name)
        return removed

    def set_member_active(self, session_id: str, team_name: str, member_name: str, active: bool) -> None:
        with self._locked(self._team_lock(session_id, team_name)):
            config = self._read_json(self.config_path(session_id, team_name), None)
            if config is None:
                return
            member = self.get_member(config, member_name)
            if member:
                member["isActive"] = active
                self._atomic_write_json(self.config_path(session_id, team_name), config)

    def list_members(self, session_id: str, team_name: str) -> List[Dict[str, Any]]:
        config = self.load_config(session_id, team_name)
        return config.get("members", []) if config else []

    def list_teams_for_session(self, session_id: str) -> List[str]:
        """列出会话下的所有团队名称"""
        session_dir = self.session_dir(session_id)
        if not session_dir.exists():
            return []
        return sorted(
            p.name for p in session_dir.iterdir()
            if p.is_dir() and (p / "config.json").exists()
        )

    # ===== 收件箱 =====

    def append_inbox_message(
        self,
        session_id: str,
        team_name: str,
        to_name: str,
        from_name: str,
        message: Any,
        color: str = "",
        summary: str = "",
    ) -> Dict[str, Any]:
        """向指定成员收件箱追加一条消息（锁内 读→追加→写回）"""
        entry = make_inbox_message(from_name, message, color=color, summary=summary)
        lock = self._inbox_lock(session_id, team_name, to_name)
        with self._locked(lock):
            path = self.inbox_path(session_id, team_name, to_name)
            messages = self._read_json(path, [])
            if not isinstance(messages, list):
                messages = []
            messages.append(entry)
            self._atomic_write_json(path, messages)
        logger.info(
            "Inbox message appended",
            team=team_name,
            to=to_name,
            **{"from": from_name},
            summary=entry.get("summary", ""),
        )
        return entry

    def send_team_message(
        self,
        session_id: str,
        team_name: str,
        from_name: str,
        to: str,
        message: Any,
        summary: str = "",
    ) -> Dict[str, Any]:
        """发送团队消息（支持 to="*" 广播，排除发送者自己）

        Returns:
            {"success": True, "delivered_to": [...]} 或 {"success": False, "error": ...}
        """
        config = self.load_config(session_id, team_name)
        if config is None:
            return {"success": False, "error": f"Team not found: {team_name}"}

        sender = self.get_member(config, from_name)
        color = sender.get("color", "") if sender else ""

        members = config.get("members", [])
        if to == "*":
            targets = [m["name"] for m in members if m.get("name") != from_name and m.get("isActive", True)]
        else:
            target = self.get_member(config, to)
            if target is None:
                names = ", ".join(m.get("name", "") for m in members)
                return {
                    "success": False,
                    "error": f"Teammate '{to}' not found in team '{team_name}'. Members: {names}",
                }
            targets = [to]

        for name in targets:
            self.append_inbox_message(
                session_id, team_name, name, from_name, message, color=color, summary=summary
            )
        return {"success": True, "delivered_to": targets}

    def read_unread(
        self,
        session_id: str,
        team_name: str,
        member_name: str,
        mark_read: bool = True,
    ) -> List[Dict[str, Any]]:
        """读取成员收件箱中的未读消息（锁内读取并标记已读，防止重复消费）"""
        lock = self._inbox_lock(session_id, team_name, member_name)
        with self._locked(lock):
            path = self.inbox_path(session_id, team_name, member_name)
            messages = self._read_json(path, [])
            if not isinstance(messages, list):
                return []
            unread = [m for m in messages if not m.get("read", False)]
            if unread and mark_read:
                for m in unread:
                    m["read"] = True
                self._atomic_write_json(path, messages)
        return unread

    def peek_messages(
        self,
        session_id: str,
        team_name: str,
        member_name: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """只读查看最近的消息（不改变已读状态）"""
        path = self.inbox_path(session_id, team_name, member_name)
        messages = self._read_json(path, [])
        if not isinstance(messages, list):
            return []
        return messages[-limit:]

    # ===== 清理 =====

    def remove_team_dir(self, session_id: str, team_name: str) -> bool:
        """删除团队目录（配置 + 收件箱）"""
        team_dir = self.team_dir(session_id, team_name)
        if team_dir.exists():
            shutil.rmtree(team_dir, ignore_errors=True)
            logger.info("Team directory removed", session_id=session_id, team=team_name)
            return True
        return False

    def remove_session_teams(self, session_id: str) -> List[str]:
        """删除会话下的所有团队目录，返回被删除的团队名"""
        removed = []
        for team_name in self.list_teams_for_session(session_id):
            if self.remove_team_dir(session_id, team_name):
                removed.append(team_name)
        session_dir = self.session_dir(session_id)
        if session_dir.exists() and not any(session_dir.iterdir()):
            shutil.rmtree(session_dir, ignore_errors=True)
        return removed


# 全局单例
team_store = TeamStore()
