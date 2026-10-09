"""Session Memory - 会话记忆管理器

后台监控会话 token 增长，在自然停顿点增量更新结构化笔记（SESSION.md）。
当 AutoCompact 触发时，若笔记摘要已足够（10K~40K token 且覆盖 ≥5 条消息），
直接复用摘要，跳过 LLM 调用。
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import structlog
from app.config import settings
from app.core.memory_system.store import memory_store
from app.models.schemas.chat import MessageRole

logger = structlog.get_logger()

SESSION_NOTE_FILENAME = "SESSION.md"

# 后台任务集合（防止 fire-and-forget 任务被 GC）
_background_tasks: set = set()


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（1 token ≈ 3 字符）"""
    if not text:
        return 0
    try:
        import tiktoken
        encoding = tiktoken.encoding_for_model("gpt-4")
        return len(encoding.encode(text))
    except Exception:
        return len(text) // 3


@dataclass
class _SessionState:
    """会话级监控状态"""
    total_tokens: int = 0
    baseline_tokens: int = 0            # 上次更新时的累计 token
    tool_calls_since_update: int = 0    # 上次更新以来的工具调用次数
    assistant_pause_turns: int = 0      # 连续无工具调用的 assistant 轮数
    messages_covered: int = 0           # 笔记已覆盖的消息数
    last_processed_msg_id: Optional[str] = None
    updating: bool = False


class SessionMemoryManager:
    """会话记忆管理器（全局单例）"""

    def __init__(self):
        self._states: Dict[str, _SessionState] = {}

    @property
    def enabled(self) -> bool:
        return bool(memory_store.enabled and settings.get("MEMORY_SESSION_MEMORY_ENABLED", True))

    def _get_state(self, conversation_id: str) -> _SessionState:
        if conversation_id not in self._states:
            self._states[conversation_id] = _SessionState()
        return self._states[conversation_id]

    def get_state(self, conversation_id: str) -> Optional[_SessionState]:
        """只读获取会话状态（不存在返回 None，供 API 查询）"""
        return self._states.get(conversation_id)

    # ===== 消息监控 =====

    def on_message_added(self, conversation_id: str, msg) -> None:
        """每条消息加入会话后调用：统计 token / 工具调用 / 停顿轮数"""
        if not self.enabled:
            return
        try:
            state = self._get_state(conversation_id)
            state.total_tokens += _estimate_tokens(msg.content or "")

            if msg.role == MessageRole.ASSISTANT:
                if getattr(msg, "tool_calls", None):
                    state.tool_calls_since_update += len(msg.tool_calls)
                    state.assistant_pause_turns = 0
                else:
                    state.assistant_pause_turns += 1
        except Exception as e:
            logger.warning("Session memory tracking failed", conversation_id=conversation_id, error=str(e))

    # ===== 触发条件判断 =====

    def should_update(self, conversation_id: str) -> Tuple[bool, str]:
        """
        判断是否应触发 Session Memory 更新。

        条件：
        - 首次更新需累计 ≥ first_update_tokens，之后每次需新增 ≥ increment_tokens
        - 或工具调用次数 ≥ tool_calls_threshold
        - 优先在自然停顿点调用（on_turn_end 即停顿点）
        """
        if not self.enabled:
            return False, "disabled"

        state = self._states.get(conversation_id)
        if not state or state.updating:
            return False, "no state or already updating"

        first_tokens = int(settings.get("MEMORY_SESSION_MEMORY_FIRST_UPDATE_TOKENS", 10000))
        increment_tokens = int(settings.get("MEMORY_SESSION_MEMORY_INCREMENT_TOKENS", 5000))
        tool_threshold = int(settings.get("MEMORY_SESSION_MEMORY_TOOL_CALLS_THRESHOLD", 3))

        delta_tokens = state.total_tokens - state.baseline_tokens
        threshold = first_tokens if state.baseline_tokens == 0 else increment_tokens

        if delta_tokens >= threshold:
            return True, f"token delta {delta_tokens} >= {threshold}"
        if state.tool_calls_since_update >= tool_threshold:
            return True, f"tool calls {state.tool_calls_since_update} >= {tool_threshold}"
        return False, f"below thresholds (delta={delta_tokens}, tools={state.tool_calls_since_update})"

    # ===== 更新执行 =====

    def on_turn_end(self, agent, conversation_id: str) -> None:
        """会话轮次结束（自然停顿点）：检查条件并 fire-and-forget 更新"""
        if not self.enabled:
            return
        should, reason = self.should_update(conversation_id)
        if not should:
            logger.debug("Session memory update skipped", conversation_id=conversation_id, reason=reason)
            return

        state = self._get_state(conversation_id)
        state.updating = True
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._run_update(agent, conversation_id))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            logger.info("Session memory update triggered", conversation_id=conversation_id, reason=reason)
        except RuntimeError:
            state.updating = False

    async def _run_update(self, agent, conversation_id: str) -> None:
        """forked agent 执行结构化编辑更新"""
        from app.core.memory_system.fork import create_forked_agent, resolve_memory_llm, run_forked_task
        from app.core.memory_system.prompts import (
            SESSION_MEMORY_TEMPLATE, SESSION_MEMORY_UPDATE_PROMPT,
        )

        state = self._get_state(conversation_id)
        try:
            # 会话记忆写入独立的会话目录（session-memory/<conversation_id>/），
            # 与用户/团队记忆无文件交集，且同一会话的更新已由 updating 状态串行化，
            # 因此不参与统一记忆写入锁。
            # 收集增量消息（上次处理位置之后的原始消息）
            conversation = agent.get_conversation(conversation_id)
            if not conversation:
                return

            messages = conversation.messages
            start_idx = 0
            if state.last_processed_msg_id:
                for i, msg in enumerate(messages):
                    if msg.id == state.last_processed_msg_id:
                        start_idx = i + 1
                        break

            increment = [
                m for m in messages[start_idx:]
                if m.role != MessageRole.SYSTEM and m.content
            ]
            if not increment:
                return

            increment_text = "\n\n".join(
                f"{m.role.value}: {(m.content or '')[:2000]}" for m in increment
            )[:30000]

            # 当前笔记内容
            note_dir = memory_store.session_memory_dir(conversation_id)
            note_path = note_dir / SESSION_NOTE_FILENAME
            if note_path.exists():
                current_note = note_path.read_text(encoding="utf-8", errors="replace")
            else:
                current_note = SESSION_MEMORY_TEMPLATE

            prompt = SESSION_MEMORY_UPDATE_PROMPT.format(
                current_note=current_note,
                increment=increment_text,
                note_path=str(note_path),
                max_rounds=3,
            )

            svc, mem_provider, mem_model = resolve_memory_llm(
                agent.llm_service,
                provider=getattr(agent.config, "llm_provider", None),
                model=getattr(agent.config, "llm_model", None),
            )
            forked = create_forked_agent(
                name="session-memory-writer",
                system_prompt=(
                    "You are a session note-taker. You can only write within the memory directory. "
                    "Update ONLY the changed sections of the structured note. "
                    "Keep the YAML frontmatter at the top and update messages_covered."
                ),
                llm_service=svc,
                max_iterations=3,
                parent_agent_id=agent.id,
                llm_provider=mem_provider,
                llm_model=mem_model,
            )

            await run_forked_task(forked, prompt)

            # 更新状态：基线、游标、覆盖消息数
            state.baseline_tokens = state.total_tokens
            state.tool_calls_since_update = 0
            state.assistant_pause_turns = 0
            state.last_processed_msg_id = increment[-1].id
            state.messages_covered = len([m for m in messages if m.role != MessageRole.SYSTEM][:start_idx + len(increment)])

            # 确保 frontmatter 中的 messages_covered 被维护（兜底写入）
            self._ensure_note_frontmatter(note_path, state.messages_covered)

            logger.info(
                "Session memory updated",
                conversation_id=conversation_id,
                increment_messages=len(increment),
                baseline_tokens=state.baseline_tokens,
            )
        except Exception as e:
            logger.error("Session memory update failed", conversation_id=conversation_id, error=str(e), exc_info=True)
        finally:
            state.updating = False

    def _ensure_note_frontmatter(self, note_path, messages_covered: int) -> None:
        """兜底：确保笔记文件存在且 frontmatter 包含 messages_covered"""
        try:
            from app.core.memory_system.prompts import SESSION_MEMORY_TEMPLATE
            note_path.parent.mkdir(parents=True, exist_ok=True)

            if not note_path.exists():
                frontmatter = (
                    "---\n"
                    f"name: Session Memory\n"
                    f"description: Structured session memory note\n"
                    f"messages_covered: {messages_covered}\n"
                    "---\n\n"
                )
                note_path.write_text(frontmatter + SESSION_MEMORY_TEMPLATE, encoding="utf-8")
                return

            content = note_path.read_text(encoding="utf-8", errors="replace")
            if content.startswith("---"):
                match = re.match(r"^---\n(.*?)\n---\n?(.*)$", content, re.DOTALL)
                if match:
                    fm_text, body = match.group(1), match.group(2)
                    fm_text = re.sub(r"messages_covered:.*\n?", "", fm_text)
                    fm_text += f"\nmessages_covered: {messages_covered}"
                    note_path.write_text(f"---\n{fm_text}\n---\n{body}", encoding="utf-8")
                    return
            # 无 frontmatter，补上
            frontmatter = f"---\nmessages_covered: {messages_covered}\n---\n\n"
            note_path.write_text(frontmatter + content, encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to ensure session note frontmatter", error=str(e))

    # ===== 摘要复用（供 auto compact 使用）=====

    def get_usable_summary(self, conversation_id: str) -> Optional[str]:
        """
        获取可复用的会话摘要（用于替代 auto compact 的 LLM 调用）。

        条件：摘要 token 在 (summary_min_tokens, summary_max_tokens) 区间内，
        且覆盖消息数 ≥ summary_min_messages。
        """
        if not self.enabled:
            return None

        note_path = memory_store.session_memory_dir(conversation_id) / SESSION_NOTE_FILENAME
        if not note_path.exists():
            return None

        try:
            raw = note_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

        # 解析 frontmatter 中的 messages_covered
        body = raw
        messages_covered = 0
        if raw.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.DOTALL)
            if match:
                for line in match.group(1).splitlines():
                    if line.strip().startswith("messages_covered:"):
                        try:
                            messages_covered = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                body = match.group(2)

        summary_tokens = _estimate_tokens(body)
        min_tokens = int(settings.get("MEMORY_SESSION_MEMORY_SUMMARY_MIN_TOKENS", 10000))
        max_tokens = int(settings.get("MEMORY_SESSION_MEMORY_SUMMARY_MAX_TOKENS", 40000))
        min_messages = int(settings.get("MEMORY_SESSION_MEMORY_SUMMARY_MIN_MESSAGES", 5))

        # 笔记内容不足时不用下限拦截（摘要尚在积累中），但必须覆盖足够消息
        if messages_covered < min_messages:
            return None
        if summary_tokens > max_tokens:
            return None
        if summary_tokens < min_tokens:
            # 摘要还不够充实，但消息覆盖足够时仍允许复用（避免 auto compact 额外 LLM 调用）
            state = self._states.get(conversation_id)
            if not state or state.baseline_tokens < min_tokens:
                return None

        logger.info(
            "Session memory summary reusable for auto compact",
            conversation_id=conversation_id,
            summary_tokens=summary_tokens,
            messages_covered=messages_covered,
        )
        return body.strip()

    # ===== 清理 =====

    def clear_conversation(self, conversation_id: str) -> None:
        """清理会话状态与磁盘笔记（会话删除时调用）"""
        self._states.pop(conversation_id, None)
        memory_store.cleanup_session_memory(conversation_id)


# 全局单例
session_memory_manager = SessionMemoryManager()
