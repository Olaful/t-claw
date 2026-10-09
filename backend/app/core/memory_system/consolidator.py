"""记忆整理 - 合并/剪枝（四层门控 + 锁文件），会话结束后 fire-and-forget 触发"""
import asyncio
import time
from typing import Optional

import structlog
from app.config import settings
from app.core.memory_system.fork import create_forked_agent, resolve_memory_llm, run_forked_task
from app.core.memory_system.prompts import CONSOLIDATION_PROMPT
from app.core.memory_system.scanner import memory_scanner
from app.core.memory_system.store import memory_store

logger = structlog.get_logger()

# 后台任务集合（防止 fire-and-forget 任务被 GC）
_background_tasks: set = set()


def _check_gates() -> Optional[str]:
    """
    同步门控检查（时间+扫描节流），返回 None 表示通过，否则返回拒绝原因。
    会话门控和锁门控在 auto_dream 中异步/后续检查。
    """
    meta = memory_store.load_meta()
    now = time.time()

    # 1. 时间门控
    min_interval_hours = float(settings.get("MEMORY_CONSOLIDATE_MIN_INTERVAL_HOURS", 24))
    last_consolidated = memory_store.get_last_consolidated_at() or 0
    if last_consolidated:
        # max(0, ...) 防止文件 mtime 与 time.time() 的精度偏差导致负值
        elapsed = max(0.0, now - last_consolidated)
        if elapsed < min_interval_hours * 3600:
            return f"interval gate: only {elapsed / 3600:.1f}h since last consolidation"

    # 2. 扫描节流门控
    scan_throttle = float(settings.get("MEMORY_CONSOLIDATE_SCAN_THROTTLE_SECONDS", 600))
    last_scan_attempt = meta.get("last_consolidate_scan_attempt") or 0
    if last_scan_attempt and (now - last_scan_attempt) < scan_throttle:
        return "scan throttle gate: scanned too recently"

    return None


async def _check_conversation_gate() -> Optional[str]:
    """
    异步会话门控检查：统计上次整理后创建的非子 agent 会话数量（基于内存数据）。
    遍历所有非子 agent，汇总其 created_at > last_consolidated_at 的会话数量。
    返回 None 表示通过，否则返回拒绝原因。
    """
    min_conversations = int(settings.get("MEMORY_CONSOLIDATE_MIN_NEW_CONVERSATIONS", 5))
    last_consolidated = memory_store.get_last_consolidated_at() or 0
    
    # 转换为 datetime
    from datetime import datetime
    if last_consolidated > 0:
        since_dt = datetime.fromtimestamp(last_consolidated)
    else:
        # 如果从未整理过，统计所有会话
        since_dt = datetime.fromtimestamp(0)
    
    # 从所有非子 agent 中统计新会话数量
    from app.services.task_service import task_service
    new_conversations = 0
    for agent in task_service._agents.values():
        if getattr(agent, "is_subagent", False):
            continue
        for conv in agent._conversations.values():
            if conv.created_at > since_dt:
                new_conversations += 1
    
    if new_conversations < min_conversations:
        return f"conversation gate: only {new_conversations} new conversations (< {min_conversations})"
    
    return None


async def auto_dream(agent, conversation_id: str) -> bool:
    """
    记忆整理主流程：四层门控通过后，forked agent 执行合并/剪枝。

    Returns:
        是否实际执行了整理
    """
    if not memory_store.enabled:
        return False

    # 1. 同步门控（时间+扫描节流）
    # ✅ 同步文件 I/O 移到线程池，避免阻塞事件循环
    reject_reason = await asyncio.to_thread(_check_gates)
    if reject_reason:
        logger.debug("Consolidation gated", conversation_id=conversation_id, reason=reject_reason)
        return False

    # 2. 异步会话门控（统计所有非子 agent 的新会话）
    reject_reason = await _check_conversation_gate()
    if reject_reason:
        logger.debug("Consolidation gated", conversation_id=conversation_id, reason=reject_reason)
        return False

    # 3. 锁门控（原子获取锁）
    # ✅ 同步文件 I/O 移到线程池
    lock_acquired = await asyncio.to_thread(memory_store.try_acquire_consolidate_lock)
    if not lock_acquired:
        logger.debug("Consolidation gated", conversation_id=conversation_id, reason="lock gate: another consolidation is running")
        return False

    # 记录本次扫描尝试时间
    await asyncio.to_thread(memory_store.update_meta, last_consolidate_scan_attempt=time.time())

    try:
        # 1. 定位：扫描记忆清单
        entries = await asyncio.to_thread(memory_scanner.scan_manifest, "personal", True)
        if len(entries) < 2:
            logger.info("Consolidation skipped: fewer than 2 memory files")
            return False

        # 统一记忆写入锁：与主 Agent / 提取任务互斥（非阻塞，冲突时跳过本次整理）
        mem_lock_acquired = await asyncio.to_thread(memory_store.try_acquire_memory_lock, "consolidator")
        if not mem_lock_acquired:
            logger.info(
                "Skip consolidation: memory write lock held",
                conversation_id=conversation_id,
                holder=memory_store.get_memory_lock_holder(),
            )
            return False

        manifest_text = memory_scanner.build_manifest_text(entries)

        # 2. 收集近期会话信号（最近 5 个会话，每个取最后 10 条消息）
        recent_signals = _collect_recent_signals()

        # 3. forked agent 执行合并/剪枝
        prompt = CONSOLIDATION_PROMPT.format(
            manifest=manifest_text,
            recent_signals=recent_signals or "(no recent signals)",
        )

        memory_root = str(memory_store.memory_root("personal"))
        team_note = ""
        if memory_store.mode == "teammem":
            team_note = f" Team memories are under: {memory_store.memory_root('team')}."

        system_prompt = (
            "You are a memory consolidation agent. You can only read/write files inside the memory "
            f"directory: {memory_root}.{team_note} "
            "Merge overlapping memories, prune stale content, and keep files focused. "
            "If nothing needs consolidation, respond with SKIP."
        )

        svc, mem_provider, mem_model = resolve_memory_llm(
            agent.llm_service,
            provider=getattr(agent.config, "llm_provider", None),
            model=getattr(agent.config, "llm_model", None),
        )
        forked = create_forked_agent(
            name="memory-consolidator",
            system_prompt=system_prompt,
            llm_service=svc,
            max_iterations=int(settings.get("MEMORY_EXTRACT_MAX_ROUNDS", 5)),
            parent_agent_id=agent.id,
            llm_provider=mem_provider,
            llm_model=mem_model,
        )

        result = await run_forked_task(forked, prompt, lock_holder="consolidator")

        # 4. 剪枝后更新索引（锁文件 mtime 即 lastConsolidatedAt，touch 一下确保为当前时间）
        # ✅ 同步文件 I/O 移到线程池
        await asyncio.to_thread(memory_store.consolidate_lock_path().touch)
        await asyncio.to_thread(memory_store.update_meta, last_consolidated_at=time.time())
        await asyncio.to_thread(memory_store.update_index, "personal")
        if memory_store.mode == "teammem":
            await asyncio.to_thread(memory_store.update_index, "team")

        logger.info(
            "Memory consolidation completed",
            conversation_id=conversation_id,
            files=len(entries),
            skipped=result.strip().upper() == "SKIP",
        )
        return True

    except Exception as e:
        logger.error("Memory consolidation failed", conversation_id=conversation_id, error=str(e), exc_info=True)
        return False
    finally:
        # 无论成功失败都释放锁（崩溃场景靠 TTL 过期兜底）
        memory_store.release_memory_lock("consolidator")
        memory_store.release_consolidate_lock()


def _collect_recent_signals(max_conversations: int = 5, max_messages: int = 10) -> str:
    """从最近创建的 N 个会话中各取最后 M 条消息作为近期信号
    
    Args:
        max_conversations: 取最近创建的多少个会话
        max_messages: 每个会话取最后多少条消息
    """
    try:
        from app.services.task_service import task_service
        
        # 收集所有非子 agent 的会话
        all_conversations = []
        for agent in task_service._agents.values():
            if getattr(agent, "is_subagent", False):
                continue
            for conv in agent._conversations.values():
                all_conversations.append(conv)
        
        if not all_conversations:
            return ""
        
        # 按创建时间倒序，取最近 N 个会话
        all_conversations.sort(key=lambda c: c.created_at, reverse=True)
        recent_conversations = all_conversations[:max_conversations]
        
        lines = []
        for conv in recent_conversations:
            if not conv.messages:
                continue
            lines.append(f"--- conversation: {conv.title or conv.id} ---")
            for msg in conv.messages[-max_messages:]:
                content = (msg.content or "")[:300]
                if not content.strip():
                    continue
                lines.append(f"{msg.role.value}: {content}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to collect recent signals", error=str(e))
        return ""


def consolidate_fire_and_forget(agent, conversation_id: str) -> None:
    """即发即弃触发记忆整理，不影响主流程"""
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(auto_dream(agent, conversation_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        logger.warning("No running loop, skip memory consolidation", conversation_id=conversation_id)
