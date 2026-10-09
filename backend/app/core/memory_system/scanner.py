"""记忆扫描 - 根据索引目录扫描记忆文件，生成紧凑清单 Manifest"""
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import structlog
from app.config import settings
from app.core.memory_system.store import MemoryStore, memory_store

logger = structlog.get_logger()


@dataclass
class MemoryEntry:
    """记忆清单条目（仅元信息）"""
    filename: str
    path: Path
    name: str
    description: str
    memory_type: str = ""
    scope: str = "personal"
    mtime: float = 0.0

    def to_manifest_line(self) -> str:
        return f"- {self.filename} | name={self.name} | description={self.description}"


class MemoryScanner:
    """记忆扫描器：扫描记忆目录生成 Manifest，带缓存与节流"""

    def __init__(self, store: Optional[MemoryStore] = None):
        self.store = store or memory_store
        # 缓存: scope -> (fingerprint, entries, scanned_at)
        self._cache: Dict[str, tuple] = {}

    def _compute_fingerprint(self, files: List[Path]) -> tuple:
        """以 (文件名, mtime_ns) 组合作为缓存指纹"""
        fingerprint = []
        for f in files:
            try:
                fingerprint.append((f.name, f.stat().st_mtime_ns))
            except OSError:
                continue
        return tuple(sorted(fingerprint))

    def scan_manifest(
        self,
        scope: str = "personal",
        force: bool = False,
    ) -> List[MemoryEntry]:
        """
        扫描记忆目录，读取每个文件的 frontmatter（name/description），
        按 mtime 降序排序，上限 settings.MEMORY_SCAN_MAX_FILES。
        缓存命中（指纹未变）时直接复用。
        """
        max_files = int(settings.get("MEMORY_SCAN_MAX_FILES", 200))

        files = self.store.iter_memory_files(scope)
        fingerprint = self._compute_fingerprint(files)

        if not force:
            cached = self._cache.get(scope)
            if cached and cached[0] == fingerprint:
                logger.debug("Scanner cache hit", scope=scope, entries=len(cached[1]))
                return cached[1]

        entries: List[MemoryEntry] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            fm = self.store.read_frontmatter(path)
            entries.append(MemoryEntry(
                filename=path.name,
                path=path,
                name=fm.get("name") or path.stem,
                description=fm.get("description") or "",
                memory_type=fm.get("type") or "",
                scope=scope,
                mtime=stat.st_mtime,
            ))

        # 按 mtime 降序
        entries.sort(key=lambda e: e.mtime, reverse=True)
        entries = entries[:max_files]

        self._cache[scope] = (fingerprint, entries, time.time())
        logger.info("Memory manifest scanned", scope=scope, entries=len(entries))
        return entries

    def get_last_scan_at(self, scope: str = "personal") -> float:
        """上次扫描时间（用于整理门控的扫描节流）"""
        cached = self._cache.get(scope)
        return cached[2] if cached else 0.0

    def build_manifest_text(self, entries: List[MemoryEntry]) -> str:
        """将清单转换为紧凑文本（供 LLM side-query 使用）"""
        return "\n".join(e.to_manifest_line() for e in entries)


# 全局单例
memory_scanner = MemoryScanner()
