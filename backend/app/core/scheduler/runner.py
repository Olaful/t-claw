"""定时任务执行器 - 触发时启动临时 Agent，按 SKILL.md 指令完成任务"""
import asyncio
import re
import time
import uuid
from typing import Optional, Tuple

import structlog

from app.core.scheduler.store import schedule_store

logger = structlog.get_logger()

_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)


def strip_frontmatter(skill_md: str) -> Tuple[str, str]:
    """剥离 SKILL.md 的 frontmatter，返回 (正文, frontmatter原文)"""
    m = _FRONTMATTER_RE.match(skill_md)
    if m:
        return skill_md[m.end():].strip(), m.group(0)
    return skill_md.strip(), ""


class ScheduledTaskRunner:
    """定时任务执行器"""

    async def run(self, session_id: str, task_id: str):
        """执行一个定时任务：
        1. 读取 SKILL.md 指令正文
        2. 复制源 Agent 配置创建临时 Agent（避免与用户会话冲突）
        3. 完整消费 process_message 流，捕获最终回复作为结果
        4. 写入 .output，更新 last_run_at
        5. 执行 notification hooks（结果注入源会话）
        """
        # ✅ 同步文件 I/O 移至线程池，避免阻塞事件循环主流程
        state = await asyncio.to_thread(schedule_store.get, session_id, task_id)
        if state is None:
            logger.warning("Scheduled task not found", task_id=task_id)
            return

        skill_md = await asyncio.to_thread(schedule_store.read_skill, session_id, task_id) or ""
        prompt, _frontmatter = strip_frontmatter(skill_md)
        if not prompt:
            prompt = "(SKILL.md is empty)"

        logger.info("Running scheduled task", task_id=task_id, session_id=session_id)

        success = False
        result = ""
        try:
            success, result = await self._execute_with_agent(state, prompt)
        except Exception as e:
            logger.error("Scheduled task runner error", task_id=task_id, error=str(e), exc_info=True)
            result = f"[Scheduled task failed] {str(e)}"

        # 写入 .output（覆盖）
        try:
            await asyncio.to_thread(schedule_store.write_output, session_id, task_id, result)
        except Exception as e:
            logger.warning("Failed to write scheduled task output", task_id=task_id, error=str(e))

        # 更新 last_run_at（一次性任务的删除由调度器负责）
        await asyncio.to_thread(
            schedule_store.update_state, session_id, task_id, {"last_run_at": int(time.time() * 1000)}
        )

        # notification hooks（任务完成交付）
        try:
            from app.core.scheduler.hooks import scheduled_task_hook_manager
            await scheduled_task_hook_manager.execute_completion_hooks(
                task_state=state,
                result=result,
                success=success,
            )
        except Exception as e:
            logger.error("Scheduled task completion hooks failed", task_id=task_id, error=str(e), exc_info=True)

        logger.info("Scheduled task finished", task_id=task_id, success=success, result_chars=len(result))

    async def _execute_with_agent(self, state: dict, prompt: str) -> Tuple[bool, str]:
        """创建临时 Agent 执行 SKILL.md 指令，返回 (是否成功, 最终结果文本)"""
        from app.services.task_service import task_service

        agent_id = state.get("agent_id") or ""
        source_agent = task_service.get_agent(agent_id) if agent_id else None
        if source_agent is None:
            return False, f"[Scheduled task failed] Source agent '{agent_id}' not found."

        from app.agents.single import create_single_agent
        from app.core.memory import create_memory_manager
        from app.core.prompt_engine import create_prompt_engine
        from app.core.reflector import create_reflector
        from app.core.compressor import create_compressor
        from app.services.llm_service import create_llm_service
        from app.tools.registry import tool_registry

        # 复制源 Agent 配置（新 id，避免与正在进行的会话冲突）
        temp_config = source_agent.config.model_copy()
        temp_config.id = f"scheduled-{state.get('id')}-{uuid.uuid4().hex[:8]}"
        temp_config.name = f"scheduled-runner-{state.get('id')}"
        # ✅ 临时 Agent 未注册到 agents 表，会话/消息禁止入库（否则 conversations.agent_id 外键违约）；
        # 复用子agent 的"跳过持久化"机制，仅存活于内存，执行后清理。
        temp_config.is_subagent = True
        temp_config.parent_agent_id = source_agent.id
        temp_config.parent_agent_name = source_agent.config.name

        llm_service = create_llm_service(
            provider=temp_config.llm_provider.value,
            model=temp_config.llm_model,
        )
        temp_agent = create_single_agent(
            config=temp_config,
            llm_service=llm_service,
            tool_registry=tool_registry,
            memory_manager=create_memory_manager(),
            prompt_engine=create_prompt_engine(),
            reflector=create_reflector(llm_service),
            compressor=create_compressor(llm_service),
        )

        conversation_id: Optional[str] = None
        final_content = ""
        try:
            async for msg in temp_agent.process_message(message=prompt):
                # 捕获最终 assistant 回复（跳过工具调用中间消息）
                if msg.role.value == "assistant" and msg.content and not msg.tool_calls:
                    final_content = msg.content
                if msg.role.value == "assistant" and msg.content:
                    # 保底：任何带内容的 assistant 消息都记录
                    final_content = final_content or msg.content
                # 记录临时会话ID（process_message 内部自动创建）
                if conversation_id is None:
                    conv = temp_agent.get_current_conversation()
                    if conv:
                        conversation_id = conv.id

            success = bool(final_content)
            return success, final_content or "[Scheduled task produced no output]"
        finally:
            # 清理临时会话（仅内存；临时会话从未入库，无需删 DB），避免污染会话列表
            try:
                if conversation_id:
                    temp_agent._conversations.pop(conversation_id, None)
                await temp_agent.stop()
            except Exception as e:
                logger.debug("Temp scheduled agent cleanup skipped", error=str(e))


# 全局单例
scheduled_task_runner = ScheduledTaskRunner()
