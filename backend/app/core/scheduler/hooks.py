"""定时任务 Notification Hooks - 任务完成后的回调机制

默认交付 hook:
    1. 在源会话中追加一条 role=user, message_type=scheduled, content=<任务结果> 的消息并持久化
    2. 通过事件总线发送 type=message 事件（维度: conversation:{session_id}），
       前端据此将消息加入对应会话并自动发起 Agent 回复请求
"""
import uuid
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List

import structlog

logger = structlog.get_logger()

CompletionHook = Callable[..., Coroutine[Any, Any, None]]


class ScheduledTaskHookManager:
    """定时任务完成回调管理器"""

    def __init__(self):
        self._completion_hooks: List[CompletionHook] = []

    def register_completion_hook(self, hook: CompletionHook):
        """注册任务完成回调"""
        self._completion_hooks.append(hook)
        logger.info("Scheduled task completion hook registered", hook=getattr(hook, "__name__", str(hook)))

    async def execute_completion_hooks(self, task_state: Dict[str, Any], result: str, success: bool):
        """依次执行所有完成回调"""
        for hook in self._completion_hooks:
            try:
                await hook(task_state=task_state, result=result, success=success)
            except Exception as e:
                logger.error(
                    "Scheduled task completion hook failed",
                    hook=getattr(hook, "__name__", str(hook)),
                    error=str(e),
                    exc_info=True,
                )


# 全局单例
scheduled_task_hook_manager = ScheduledTaskHookManager()


# ===== 默认交付 Hook =====

async def deliver_result_to_conversation(task_state: Dict[str, Any], result: str, success: bool):
    """将任务结果作为 role=user/message_type=scheduled 消息注入源会话，并推送事件通知前端"""
    from app.models.schemas.chat import Message, MessageRole, MessageType
    from app.services.persistence import persistence
    from app.services.task_service import task_service
    from app.core.event_bus import event_bus

    session_id = task_state.get("session_id")
    agent_id = task_state.get("agent_id")
    task_id = task_state.get("id")
    task_name = task_state.get("name") or task_id

    if not session_id:
        logger.warning("Scheduled task has no session_id, skip delivery", task_id=task_id)
        return

    content = (
        f"[Scheduled task '{task_name}' ({task_id}) {'completed' if success else 'failed'}]\n\n"
        f"{result}"
    )

    msg_id = str(uuid.uuid4())
    created_at = datetime.now()

    # 1. 优先从 Agent 内存获取会话（保证与运行中状态一致），否则从数据库加载
    agent = task_service.get_agent(agent_id) if agent_id else None
    conversation = None
    if agent is not None:
        conversation = agent.get_conversation(session_id)
    if conversation is None:
        conversation = await persistence.load_conversation(session_id)
        if conversation is not None and agent is not None:
            agent._conversations[session_id] = conversation

    if conversation is None:
        logger.warning(
            "Scheduled task delivery skipped: conversation not found",
            task_id=task_id,
            session_id=session_id,
        )
        return

    # 2. 追加 scheduled 用户消息并持久化
    message = Message(
        id=msg_id,
        role=MessageRole.USER,
        content=content,
        message_type=MessageType.SCHEDULED,
        created_at=created_at,
    )
    conversation.messages.append(message)
    conversation.updated_at = created_at
    await persistence.save_conversation(conversation)

    # 3. 推送 type=message 事件（high 优先级，支持断线补发）
    await event_bus.emit_event(
        type="message",
        source=session_id,
        data={
            "id": msg_id,
            "role": MessageRole.USER.value,
            "content": content,
            "message_type": MessageType.SCHEDULED.value,
            "conversation_id": session_id,
            "agent_id": agent_id or "",
            "task_id": task_id,
            "created_at": created_at.isoformat(),
        },
        priority="high",
        dimensions=[f"conversation:{session_id}"],
    )

    logger.info(
        "Scheduled task result delivered",
        task_id=task_id,
        session_id=session_id,
        message_id=msg_id,
    )


# 注册默认交付 hook
scheduled_task_hook_manager.register_completion_hook(deliver_result_to_conversation)
