"""记忆存储层 - 记忆文件读写、frontmatter 解析、索引维护、状态文件管理"""
import json
import os
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
from app.config import settings

logger = structlog.get_logger()

# 四种记忆类型
MEMORY_TYPES = ("user", "feedback", "reference", "project")
# 作用域
MEMORY_SCOPES = ("personal", "team")

INDEX_FILENAME = "MEMORY.md"
META_FILENAME = ".memory-meta.json"
CONSOLIDATE_LOCK_FILENAME = ".consolidate-lock"
MEMORY_LOCK_FILENAME = ".memory-lock"
TEAM_DIRNAME = "team"
SESSION_MEMORY_DIRNAME = "session-memory"

# 锁持有者标识（主 Agent / 提取 / 整理三方共用统一写入锁）
LOCK_HOLDER_MAIN_AGENT = "main-agent"
LOCK_HOLDER_EXTRACTOR = "extractor"
LOCK_HOLDER_CONSOLIDATOR = "consolidator"

# 当前任务的锁持有者上下文（forked 任务设置，builtin write_file 读取）
current_memory_lock_holder: ContextVar[Optional[str]] = ContextVar(
    "current_memory_lock_holder", default=None
)

from app.config import _HAPPYCLAW_HOME


def _parse_frontmatter_text(text: str) -> Dict[str, Any]:
    """轻量解析 YAML frontmatter（仅支持 key: value 单行格式）"""
    result: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def _serialize_frontmatter(meta: Dict[str, Any]) -> str:
    """序列化 frontmatter 为 YAML 文本（key: value 格式）"""
    lines = ["---"]
    for key, value in meta.items():
        if value is None:
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


class MemoryStore:
    """记忆存储层"""

    def __init__(self):
        self._root_override: Optional[Path] = None

    # ===== 基础属性 =====

    @property
    def enabled(self) -> bool:
        return bool(settings.get("MEMORY_ENABLED", True))

    @property
    def mode(self) -> str:
        """standard | teammem"""
        return str(settings.get("MEMORY_MODE", "standard")).lower()

    def memory_root(self, scope: str = "personal") -> Path:
        """获取记忆根目录（personal 为根，team 为根下 team/ 子目录）"""
        root_setting = str(str(_HAPPYCLAW_HOME / "memory"))
        root_path = Path(root_setting)
        if not root_path.is_absolute():
            root_path = (_HAPPYCLAW_HOME / root_path).resolve()
        if self._root_override:
            root_path = self._root_override
        if scope == "team":
            return root_path / TEAM_DIRNAME
        return root_path

    def set_root_override(self, path: Optional[Path]) -> None:
        """测试用：覆盖记忆根目录"""
        self._root_override = Path(path).resolve() if path else None

    def session_memory_dir(self, conversation_id: str) -> Path:
        """会话记忆目录: memory/session-memory/<conversation_id>/"""
        safe_id = re.sub(r"[^\w\-.]", "_", conversation_id)
        return self.memory_root("personal") / SESSION_MEMORY_DIRNAME / safe_id

    def ensure_dirs(self) -> None:
        """确保目录结构存在（memory/、team/、session-memory/、MEMORY.md）"""
        root = self.memory_root("personal")
        team_root = self.memory_root("team")
        session_root = root / SESSION_MEMORY_DIRNAME
        for d in (root, team_root, session_root):
            d.mkdir(parents=True, exist_ok=True)

        # 个人索引文件
        index_file = root / INDEX_FILENAME
        if not index_file.exists():
            index_file.write_text(
                "# Memory Index\n\n", encoding="utf-8"
            )
        logger.debug("Memory dirs ensured", root=str(root))

    # ===== frontmatter 读写 =====

    def read_frontmatter(self, path: Path) -> Dict[str, Any]:
        """轻量解析：只读文件头部 frontmatter，不读全文"""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline()
                if first_line.strip() != "---":
                    return {}
                fm_lines = []
                for line in f:
                    if line.strip() == "---":
                        break
                    fm_lines.append(line)
                    if len(fm_lines) > 50:  # frontmatter 不应过长
                        break
            return _parse_frontmatter_text("".join(fm_lines))
        except Exception as e:
            logger.warning("Failed to read frontmatter", path=str(path), error=str(e))
            return {}

    def read_memory(
        self,
        path: Path,
        max_lines: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], str, bool]:
        """完整读取记忆文件，返回 (frontmatter, body, truncated)"""
        max_lines = max_lines or int(settings.get("MEMORY_PER_FILE_MAX_LINES", 200))
        max_bytes = max_bytes or int(settings.get("MEMORY_PER_FILE_MAX_BYTES", 4096))

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read(max_bytes + 1)
        except Exception as e:
            logger.warning("Failed to read memory file", path=str(path), error=str(e))
            return {}, "", False

        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]

        # 按行数截断
        lines = raw.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated = True
        raw = "\n".join(lines)

        # 拆分 frontmatter 与正文
        frontmatter: Dict[str, Any] = {}
        body = raw
        if raw.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.DOTALL)
            if match:
                frontmatter = _parse_frontmatter_text(match.group(1))
                body = match.group(2)

        return frontmatter, body, truncated

    def write_memory(
        self,
        path: Path,
        frontmatter: Dict[str, Any],
        body: str,
    ) -> None:
        """原子写入记忆文件（tmp + rename）"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 自动补充时间字段
        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        if path.exists():
            existing_fm = self.read_frontmatter(path)
            frontmatter.setdefault("created_at", existing_fm.get("created_at", now_iso))
        else:
            frontmatter.setdefault("created_at", now_iso)
        frontmatter["updated_at"] = now_iso

        content = _serialize_frontmatter(frontmatter) + "\n\n" + body.strip() + "\n"

        tmp_path = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex[:8]}")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, path)
            logger.info("Memory file written", path=str(path))
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def is_memory_file(self, path: Path) -> bool:
        """判断是否为记忆文件（排除索引、点文件、会话记忆等）"""
        name = path.name
        if name.startswith("."):
            return False
        if name == INDEX_FILENAME:
            return False
        return path.suffix == ".md"

    def iter_memory_files(self, scope: str = "personal") -> List[Path]:
        """列出指定作用域下的所有记忆文件（不递归进 session-memory）"""
        root = self.memory_root(scope)
        if not root.exists():
            return []
        files = []
        for entry in root.iterdir():
            if entry.is_file() and self.is_memory_file(entry):
                files.append(entry)
            elif entry.is_dir() and scope == "personal":
                # personal 根下不递归 team/ 与 session-memory/
                continue
        return files

    def is_within_memory_root(self, path: str, include_team: bool = True) -> bool:
        """校验路径是否在记忆目录内（用于 forked agent 工具权限）"""
        try:
            abs_path = Path(path).resolve()
        except Exception:
            return False
        roots = [self.memory_root("personal")]
        if include_team:
            roots.append(self.memory_root("team"))
        for root in roots:
            try:
                abs_path.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    # ===== 索引 =====

    def update_index(self, scope: str = "personal") -> None:
        """重建 MEMORY.md 索引：- [Title](file.md) — one-line description"""
        root = self.memory_root(scope)
        if not root.exists():
            return

        entries = []
        for path in sorted(self.iter_memory_files(scope)):
            fm = self.read_frontmatter(path)
            title = fm.get("name") or path.stem
            description = fm.get("description") or ""
            entries.append(f"- [{title}]({path.name}) — {description}")

        header = "# Memory Index\n"
        if scope == "team":
            header = "# Team Memory Index\n"
        content = header + "\n" + "\n".join(entries) + ("\n" if entries else "")

        index_path = root / INDEX_FILENAME
        try:
            tmp_path = index_path.with_suffix(".md.tmp")
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, index_path)
            logger.info("Memory index updated", scope=scope, entries=len(entries))
        except Exception as e:
            logger.error("Failed to update memory index", scope=scope, error=str(e), exc_info=True)

    def load_index_content(self, scope: str = "personal") -> str:
        """加载索引内容供 system prompt 注入（截断：200行/25KB，先到为准）"""
        index_path = self.memory_root(scope) / INDEX_FILENAME
        if not index_path.exists():
            return ""

        max_lines = int(settings.get("MEMORY_INDEX_MAX_LINES", 200))
        max_bytes = int(settings.get("MEMORY_INDEX_MAX_BYTES", 25600))

        try:
            with open(index_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read(max_bytes + 1)
        except Exception as e:
            logger.warning("Failed to read memory index", error=str(e))
            return ""

        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        lines = raw.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated = True
        if truncated:
            lines.append("... (index truncated)")
        return "\n".join(lines)

    # ===== 状态文件 .memory-meta.json =====

    def _meta_path(self) -> Path:
        return self.memory_root("personal") / META_FILENAME

    def load_meta(self) -> Dict[str, Any]:
        """加载状态文件"""
        meta_path = self._meta_path()
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load memory meta", error=str(e))
            return {}

    def save_meta(self, meta: Dict[str, Any]) -> None:
        """保存状态文件"""
        meta_path = self._meta_path()
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.error("Failed to save memory meta", error=str(e), exc_info=True)

    def update_meta(self, **fields) -> Dict[str, Any]:
        """增量更新状态字段"""
        meta = self.load_meta()
        meta.update(fields)
        self.save_meta(meta)
        return meta

    # ===== 会话级状态文件 session-memory/<conversation_id>/.memory-meta.json =====

    def _session_meta_path(self, conversation_id: str) -> Path:
        return self.session_memory_dir(conversation_id) / META_FILENAME

    def load_session_meta(self, conversation_id: str) -> Dict[str, Any]:
        """加载会话级状态（提取游标/节流等按会话维护的字段）"""
        meta_path = self._session_meta_path(conversation_id)
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to load session meta", conversation_id=conversation_id, error=str(e))
            return {}

    def save_session_meta(self, conversation_id: str, meta: Dict[str, Any]) -> None:
        """保存会话级状态"""
        try:
            meta_path = self._session_meta_path(conversation_id)
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.error("Failed to save session meta", conversation_id=conversation_id, error=str(e), exc_info=True)

    def update_session_meta(self, conversation_id: str, **fields) -> Dict[str, Any]:
        """增量更新会话级状态字段"""
        meta = self.load_session_meta(conversation_id)
        meta.update(fields)
        self.save_session_meta(conversation_id, meta)
        return meta

    # ===== 整理锁 =====

    def consolidate_lock_path(self) -> Path:
        return self.memory_root("personal") / CONSOLIDATE_LOCK_FILENAME

    def try_acquire_consolidate_lock(self) -> bool:
        """原子创建锁文件；锁存在且未过期时返回 False"""
        lock_path = self.consolidate_lock_path()
        ttl = int(settings.get("MEMORY_CONSOLIDATE_LOCK_TTL_SECONDS", 3600))

        if lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            if age < ttl:
                return False
            # 锁已过期，抢占：先删除
            try:
                lock_path.unlink()
            except OSError:
                return False

        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(f"locked_at={datetime.utcnow().isoformat()}\n")
            return True
        except FileExistsError:
            return False
        except Exception as e:
            logger.warning("Failed to acquire consolidate lock", error=str(e))
            return False

    def release_consolidate_lock(self) -> None:
        """释放锁（删除锁文件）"""
        lock_path = self.consolidate_lock_path()
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception as e:
            logger.warning("Failed to release consolidate lock", error=str(e))

    def get_last_consolidated_at(self) -> Optional[float]:
        """锁文件的 mtime 即 lastConsolidatedAt（Unix 时间戳）"""
        lock_path = self.consolidate_lock_path()
        if lock_path.exists():
            return lock_path.stat().st_mtime
        meta = self.load_meta()
        return meta.get("last_consolidated_at")

    # ===== 统一记忆写入锁（主 Agent / 提取 / 整理三方共用） =====

    def memory_lock_path(self) -> Path:
        return self.memory_root("personal") / MEMORY_LOCK_FILENAME

    def get_memory_lock_holder(self) -> Optional[str]:
        """当前锁持有者（无锁时返回 None）"""
        lock_path = self.memory_lock_path()
        if not lock_path.exists():
            return None
        try:
            text = lock_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("holder="):
                return line[len("holder="):].strip()
        return None

    def try_acquire_memory_lock(self, holder: str, ttl_seconds: Optional[int] = None) -> bool:
        """
        非阻塞获取统一记忆写入锁（可重入）。

        - 无锁 → 获取成功（O_EXCL 原子创建）
        - 锁持有者 == holder → 重入成功
        - 锁被他人持有且未过期 → 失败
        - 锁被他人持有但已过期（mtime 超 TTL） → 抢占
        """
        ttl = int(ttl_seconds or settings.get("MEMORY_LOCK_TTL_SECONDS", 600))
        lock_path = self.memory_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if lock_path.exists():
            if self.get_memory_lock_holder() == holder:
                return True  # 可重入
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = ttl + 1  # 无法读取状态视为过期
            if age < ttl:
                return False
            # 锁已过期，删除后抢占
            try:
                lock_path.unlink()
            except OSError:
                return False

        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(f"holder={holder}\nlocked_at={datetime.utcnow().isoformat()}\n")
            return True
        except FileExistsError:
            # 并发竞争失败：再检查一次是否为自身（重入场景）
            return self.get_memory_lock_holder() == holder
        except Exception as e:
            logger.warning("Failed to acquire memory lock", holder=holder, error=str(e))
            return False

    def force_acquire_memory_lock(self, holder: str) -> bool:
        """强制获取锁（仅主 Agent 使用：直接原子覆盖锁文件，优先级最高）"""
        lock_path = self.memory_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = lock_path.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
        try:
            tmp_path.write_text(
                f"holder={holder}\nlocked_at={datetime.utcnow().isoformat()}\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, lock_path)
            logger.warning("Memory lock force-acquired", holder=holder)
            return True
        except Exception as e:
            logger.warning("Failed to force-acquire memory lock", holder=holder, error=str(e))
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            return False

    def release_memory_lock(self, holder: Optional[str] = None) -> None:
        """释放锁；指定 holder 时仅当持有者匹配才释放（防止误删他人锁）"""
        lock_path = self.memory_lock_path()
        try:
            if not lock_path.exists():
                return
            if holder is not None and self.get_memory_lock_holder() != holder:
                return
            lock_path.unlink()
        except Exception as e:
            logger.warning("Failed to release memory lock", holder=holder, error=str(e))

    # ===== 会话记忆清理 =====

    def cleanup_session_memory(self, conversation_id: str) -> None:
        """删除会话记忆目录"""
        session_dir = self.session_memory_dir(conversation_id)
        if session_dir.exists():
            import shutil
            try:
                shutil.rmtree(session_dir)
                logger.info("Session memory cleaned up", conversation_id=conversation_id)
            except Exception as e:
                logger.warning("Failed to cleanup session memory", error=str(e))


# 全局单例
memory_store = MemoryStore()
