"""记忆提取 - 会话结束后 forked agent 异步提取新记忆（fire-and-forget）"""
import asyncio
import time
from typing import List, Optional

import structlog
from app.config import settings
from app.core.memory_system.fork import create_forked_agent, resolve_memory_llm, run_forked_task
from app.core.memory_system.prompts import EXTRACTION_PROMPT
from app.core.memory_system.scanner import memory_scanner
from app.core.memory_system.store import memory_store
from app.models.schemas.chat import MessageRole, MessageType

logger = structlog.get_logger()

# 压缩标记消息类型（提取时需过滤）
_MARKER_TYPES = (
    "compress_budget_boundary",
    "compress_snip_compact_boundary",
    "compress_micro_compact_boundary",
    "compress_auto_compact_boundary",
    "active_compact_boundary",
    MessageType.SLASH_COMMAND_REQUEST.value,
    MessageType.SLASH_COMMAND_RESPONSE.value,
)

# 每条消息内容截断长度（构建提取 prompt 时）
_PER_MESSAGE_CHARS = 2000
# 增量总长度上限
_MAX_INCREMENT_CHARS = 30000


def _message_type_value(msg) -> str:
    mt = getattr(msg, "message_type", None)
    return getattr(mt, "value", str(mt or "")).lower()


def _collect_increment(conversation, cursor_uuid: Optional[str]) -> List:
    """从游标之后取增量消息（过滤压缩标记与 system 消息）"""
    messages = conversation.messages if conversation else []
    start_idx = 0
    if cursor_uuid:
        for i, msg in enumerate(messages):
            if msg.id == cursor_uuid:
                start_idx = i + 1
                break

    increment = []
    for msg in messages[start_idx:]:
        if _message_type_value(msg) in _MARKER_TYPES:
            continue
        if msg.role == MessageRole.SYSTEM:
            continue
        increment.append(msg)
    return increment


def _format_increment(increment: List) -> str:
    """将增量消息格式化为文本"""
    role_labels = {
        MessageRole.USER: "User",
        MessageRole.ASSISTANT: "Assistant",
        MessageRole.TOOL: "Tool",
        MessageRole.SYSTEM: "System",
    }
    parts = []
    total = 0
    for msg in increment:
        label = role_labels.get(msg.role, msg.role.value)
        content = (msg.content or "")[:_PER_MESSAGE_CHARS]
        if msg.role == MessageRole.TOOL and getattr(msg, "name", None):
            label = f"Tool({msg.name})"
        line = f"{label}: {content}"
        if total + len(line) > _MAX_INCREMENT_CHARS:
            parts.append("... (increment truncated)")
            break
        parts.append(line)
        total += len(line)
    return "\n\n".join(parts)


async def extract_memories(agent, conversation_id: str) -> bool:
    """
    记忆提取主流程（由 stop hook fire-and-forget 触发）。

    门控顺序：
    1. 时间节流：距上次提取 < extract_min_interval_seconds 跳过
    2. 主 Agent 主动写过记忆则跳过
    3. 游标：无增量消息则跳过
    4. 内容检查：增量过短/无实质内容跳过

    Returns:
        是否实际执行了提取
    """
    if not memory_store.enabled:
        return False

    try:
        # 1. 时间节流（按会话维护，避免跨会话互相阻塞）
        # ✅ 同步文件 I/O 移到线程池，避免阻塞事件循环
        meta = await asyncio.to_thread(memory_store.load_session_meta, conversation_id)
        min_interval = float(settings.get("MEMORY_EXTRACT_MIN_INTERVAL_SECONDS", 300))
        last_extracted = meta.get("last_extracted_at") or 0
        if time.time() - last_extracted < min_interval:
            logger.debug("Memory extraction throttled", conversation_id=conversation_id)
            return False

        # 2. 主 Agent 主动写入记忆时不触发提取
        from app.core.memory_system.prefetch import memory_prefetch_manager
        if memory_prefetch_manager.agent_wrote_memory(conversation_id):
            logger.info("Skip extraction: main agent wrote memory itself", conversation_id=conversation_id)
            return False

        # 3. 游标增量
        conversation = agent.get_conversation(conversation_id)
        if not conversation:
            return False
        cursor = meta.get("last_memory_message_uuid")  # 会话级游标（消息 ID 仅在本会话内有意义）
        increment = _collect_increment(conversation, cursor)
        if not increment:
            logger.debug("No increment messages for extraction", conversation_id=conversation_id)
            return False

        # 4. 内容检查：过短内容（如纯寒暄）跳过
        min_chars = int(settings.get("MEMORY_EXTRACT_MIN_CONTENT_CHARS", 200))
        min_user_msgs = int(settings.get("MEMORY_EXTRACT_MIN_USER_MESSAGES", 2))
        total_chars = sum(len(msg.content or "") for msg in increment)
        user_msg_count = sum(1 for msg in increment if msg.role == MessageRole.USER)
        has_tool_activity = any(msg.role == MessageRole.TOOL for msg in increment)
        if total_chars < min_chars and not has_tool_activity:
            logger.info("Skip extraction: increment too small", conversation_id=conversation_id, chars=total_chars)
            return False
        if user_msg_count < min_user_msgs:
            logger.info("Skip extraction: too few user messages", conversation_id=conversation_id, user_msgs=user_msg_count, min_required=min_user_msgs)
            return False

        # 5. 组装提取 prompt 并运行 forked agent
        # ✅ 同步文件 I/O 移到线程池
        entries = await asyncio.to_thread(memory_scanner.scan_manifest, "personal", True)
        manifest_text = memory_scanner.build_manifest_text(entries) or "(no existing memories)"
        increment_text = _format_increment(increment)

        max_rounds = int(settings.get("MEMORY_EXTRACT_MAX_ROUNDS", 5))
        prompt = EXTRACTION_PROMPT.format(
            manifest=manifest_text,
            increment=increment_text,
            max_rounds=max_rounds,
        )

        memory_root = str(memory_store.memory_root("personal"))
        team_note = ""
        if memory_store.mode == "teammem":
            team_note = f" Team memories go under: {memory_store.memory_root('team')} with scope: team."

        system_prompt = (
            "You are a memory extraction agent. You can only read/write files inside the memory "
            f"directory: {memory_root}.{team_note} "
            "Follow the task instructions strictly. If nothing is worth recording, respond with SKIP."
        )

        svc, mem_provider, mem_model = resolve_memory_llm(
            agent.llm_service,
            provider=getattr(agent.config, "llm_provider", None),
            model=getattr(agent.config, "llm_model", None),
        )
        forked = create_forked_agent(
            name="memory-extractor",
            system_prompt=system_prompt,
            llm_service=svc,
            max_iterations=max_rounds,
            parent_agent_id=agent.id,
            llm_provider=mem_provider,
            llm_model=mem_model,
        )

        # 统一记忆写入锁：与主 Agent / 整理任务互斥（非阻塞，冲突时跳过本次提取）
        # ✅ 同步文件 I/O 移到线程池
        lock_acquired = await asyncio.to_thread(memory_store.try_acquire_memory_lock, "extractor")
        if not lock_acquired:
            logger.info(
                "Skip extraction: memory write lock held",
                conversation_id=conversation_id,
                holder=memory_store.get_memory_lock_holder(),
            )
            return False

        try:
            result = await run_forked_task(forked, prompt, lock_holder="extractor")
        finally:
            memory_store.release_memory_lock("extractor")

        # 6. 更新游标与时间戳（会话级）
        # ✅ 同步文件 I/O 移到线程池
        await asyncio.to_thread(
            memory_store.update_session_meta,
            conversation_id,
            last_extracted_at=time.time(),
            last_memory_message_uuid=increment[-1].id,
        )

        # 7. 若产生了新记忆文件，刷新索引（强制重扫）
        new_entries = await asyncio.to_thread(memory_scanner.scan_manifest, "personal", True)
        if len(new_entries) != len(entries) or result.strip().upper() != "SKIP":
            await asyncio.to_thread(memory_store.update_index, "personal")
            if memory_store.mode == "teammem":
                await asyncio.to_thread(memory_store.update_index, "team")

        logger.info(
            "Memory extraction completed",
            conversation_id=conversation_id,
            increment_messages=len(increment),
            skipped=result.strip().upper() == "SKIP",
        )
        return True

    except Exception as e:
        logger.error("Memory extraction failed", conversation_id=conversation_id, error=str(e), exc_info=True)
        return False


def extract_memories_fire_and_forget(agent, conversation_id: str) -> None:
    """即发即弃触发记忆提取，不影响主流程"""
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(extract_memories(agent, conversation_id))
        # 防止任务被 GC 回收
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        logger.warning("No running loop, skip memory extraction", conversation_id=conversation_id)


# 后台任务集合（防止 fire-and-forget 任务被 GC）
_background_tasks: set = set()
