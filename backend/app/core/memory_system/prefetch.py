"""记忆预取 - 每轮对话异步启动记忆预取，非阻塞收集结果，注入预算控制"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import structlog
from app.config import settings
from app.core.memory_system.scanner import MemoryEntry, memory_scanner
from app.core.memory_system.selector import select_relevant_memories
from app.core.memory_system.store import memory_store

logger = structlog.get_logger()


@dataclass
class _ConversationPrefetchState:
    """会话级预取状态"""
    task: Optional[asyncio.Task] = None
    result: Optional[str] = None           # 构建好的 system-reminder 文本
    result_entries: List[MemoryEntry] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    already_surfaced: Set[str] = field(default_factory=set)
    injected_bytes: int = 0                # 本会话累计注入字节数（预算）


class MemoryPrefetchManager:
    """
    记忆预取管理器（全局单例）。

    流程：
    1. 用户每轮发言后 start_relevant_memory_prefetch() 即发即弃启动后台任务
    2. 后台：扫描 Manifest → LLM side-query 选出 ≤5 个 → 读取完整内容（受预算限制）
    3. 主流程在 LLM 调用前 collect_prefetch() 非阻塞收集结果
    """

    def __init__(self):
        self._states: Dict[str, _ConversationPrefetchState] = {}

    def _get_state(self, conversation_id: str) -> _ConversationPrefetchState:
        if conversation_id not in self._states:
            # 从会话级 meta 恢复持久化状态（应用重启后避免重复注入）
            meta = memory_store.load_session_meta(conversation_id)
            self._states[conversation_id] = _ConversationPrefetchState(
                already_surfaced=set(meta.get("prefetch_already_surfaced", [])),
                injected_bytes=int(meta.get("prefetch_injected_bytes", 0)),
            )
        return self._states[conversation_id]

    def _persist_state(self, conversation_id: str) -> None:
        """将预取状态持久化到会话级 meta（排除运行时临时标记）"""
        state = self._states.get(conversation_id)
        if state is None:
            return
        # __agent_wrote_memory__ 是运行时标记，不持久化
        surfaced = [s for s in state.already_surfaced if s != "__agent_wrote_memory__"]
        memory_store.update_session_meta(
            conversation_id,
            prefetch_already_surfaced=surfaced,
            prefetch_injected_bytes=state.injected_bytes,
        )

    def start_relevant_memory_prefetch(
        self,
        conversation_id: str,
        user_query: str,
        llm_service,
        active_tools: Optional[List[str]] = None,
    ) -> None:
        """启动后台记忆预取（即发即弃，非阻塞）"""
        if not memory_store.enabled:
            return

        state = self._get_state(conversation_id)

        # 如果上一轮预取还在跑，先取消
        if state.task and not state.task.done():
            state.task.cancel()

        state.result = None
        state.result_entries = []
        state.ready.clear()

        state.task = asyncio.create_task(
            self._prefetch_worker(conversation_id, user_query, llm_service, active_tools or [])
        )
        logger.debug("Memory prefetch started", conversation_id=conversation_id)

    async def _prefetch_worker(
        self,
        conversation_id: str,
        user_query: str,
        llm_service,
        active_tools: List[str],
    ) -> None:
        """后台预取工作协程"""
        state = self._get_state(conversation_id)
        try:
            # 1. 扫描记忆清单（personal；TEAMMEM 模式下同时扫描 team）
            # ✅ 同步文件 I/O 移到线程池，避免阻塞事件循环
            entries = await asyncio.to_thread(memory_scanner.scan_manifest, "personal")
            if memory_store.mode == "teammem":
                team_entries = await asyncio.to_thread(memory_scanner.scan_manifest, "team")
                entries = entries + team_entries
                # 合并后按 mtime 降序
                entries.sort(key=lambda e: e.mtime, reverse=True)
                max_files = int(settings.get("MEMORY_SCAN_MAX_FILES", 200))
                entries = entries[:max_files]

            if not entries:
                logger.debug("No memory files to prefetch", conversation_id=conversation_id)
                return

            # 2. LLM side-query 选择相关记忆（使用 memory.prefetch_llm 独立配置）
            from app.core.memory_system.fork import resolve_memory_llm
            svc, _prov, mem_model = resolve_memory_llm(
                llm_service, settings_prefix="MEMORY_PREFETCH_LLM",
            )
            selected = await select_relevant_memories(
                llm_service=svc,
                user_query=user_query,
                entries=entries,
                already_surfaced=state.already_surfaced,
                active_tools=active_tools,
                model=mem_model,
            )
            if not selected:
                return

            # 3. 读取完整内容并构建 system-reminder（受预算限制）
            # ✅ 同步文件读取移到线程池
            reminder = await asyncio.to_thread(self._build_reminder, conversation_id, selected)
            if reminder:
                state.result = reminder
                state.result_entries = selected
                logger.info(
                    "Memory prefetch completed",
                    conversation_id=conversation_id,
                    injected=[e.filename for e in selected],
                )
        except asyncio.CancelledError:
            logger.debug("Memory prefetch cancelled", conversation_id=conversation_id)
        except Exception as e:
            logger.warning("Memory prefetch failed", conversation_id=conversation_id, error=str(e), exc_info=True)
        finally:
            state.ready.set()

    def _build_reminder(self, conversation_id: str, selected: List[MemoryEntry]) -> str:
        """读取选中记忆的完整内容，构建 system-reminder 文本（受注入预算限制）"""
        state = self._get_state(conversation_id)
        budget_bytes = int(settings.get("MEMORY_SESSION_INJECT_BUDGET_BYTES", 61440))
        max_lines = int(settings.get("MEMORY_PER_FILE_MAX_LINES", 200))
        max_bytes = int(settings.get("MEMORY_PER_FILE_MAX_BYTES", 4096))
        warn_days = float(settings.get("MEMORY_FRESHNESS_WARN_DAYS", 1))

        parts = []
        injected = []
        for entry in selected:
            # session 总预算检查
            if state.injected_bytes >= budget_bytes:
                logger.info("Session memory inject budget exhausted", conversation_id=conversation_id)
                break

            _, body, truncated = memory_store.read_memory(entry.path, max_lines, max_bytes)
            if not body.strip():
                continue

            block = f"### Memory: {entry.name} ({entry.filename})\n{body.strip()}"
            if truncated:
                block += "\n[内容已截断]"

            # 新鲜度警告：超过 warn_days 天的记忆标注
            age_days = (time.time() - entry.mtime) / 86400
            if age_days > warn_days:
                block += f"\n[新鲜度警告：该记忆已更新超过 {warn_days:.0f} 天（约 {age_days:.0f} 天前），内容可能过期，请注意甄别]"

            block_bytes = len(block.encode("utf-8"))
            if state.injected_bytes + block_bytes > budget_bytes:
                logger.info("Skipping memory due to session budget", filename=entry.filename)
                break

            state.injected_bytes += block_bytes
            parts.append(block)
            injected.append(entry)

        if not parts:
            return ""

        # 记录已展示（注入后才真正标记）并持久化
        for entry in injected:
            state.already_surfaced.add(entry.filename)
        self._persist_state(conversation_id)

        header = "以下记忆可能与当前任务相关，来源于持久化记忆库，请结合当前情况甄别使用："
        return f"<system-reminder>\n{header}\n\n" + "\n\n".join(parts) + "\n</system-reminder>"

    async def collect_prefetch(
        self,
        conversation_id: str,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """
        非阻塞收集预取结果。主流程在 LLM 调用前调用。
        超时即放弃（返回 None），不阻塞主流程。
        """
        if not memory_store.enabled:
            return None

        state = self._states.get(conversation_id)
        if not state or not state.task:
            return None

        timeout = timeout if timeout is not None else float(
            settings.get("MEMORY_PREFETCH_COLLECT_TIMEOUT_SECONDS", 8)
        )

        try:
            await asyncio.wait_for(state.ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Memory prefetch collect timed out", conversation_id=conversation_id)
            return None

        result = state.result
        # 结果只消费一次
        state.result = None
        state.task = None
        return result

    def mark_active_memory_write(self, conversation_id: str) -> None:
        """标记主 Agent 在本轮主动写过记忆（用于提取门控，轮次级别一次性标记）"""
        state = self._get_state(conversation_id)
        state.already_surfaced.add("__agent_wrote_memory__")
        # 运行时临时标记，不持久化

    def agent_wrote_memory(self, conversation_id: str) -> bool:
        """检查并消费标记：本轮主 agent 是否主动写过记忆（一次性读取）"""
        state = self._states.get(conversation_id)
        if not state or "__agent_wrote_memory__" not in state.already_surfaced:
            return False
        state.already_surfaced.discard("__agent_wrote_memory__")
        return True

    def clear_conversation(self, conversation_id: str) -> None:
        """清理会话内存状态（会话删除时调用）。会话目录由 session_memory_manager 统一删除。"""
        state = self._states.pop(conversation_id, None)
        if state and state.task and not state.task.done():
            state.task.cancel()


# 全局单例
memory_prefetch_manager = MemoryPrefetchManager()
