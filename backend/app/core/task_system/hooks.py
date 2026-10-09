"""任务生命周期 Hooks - TaskCreated / TaskCompleted 等生命周期节点钩子

允许在任务生命周期的关键节点插入自定义逻辑（如自动验证、审计、通知）。
事件类型:
    - task_created:    任务创建后
    - task_started:    任务状态变为 in_progress（被认领/开始执行）
    - task_updated:    任务字段更新（非状态迁移类）
    - task_completed:  任务状态变为 completed
    - task_deleted:    任务被删除（软删除或物理删除）
"""
import inspect
from typing import Any, Callable, Dict, List

import structlog

logger = structlog.get_logger()

# 支持的任务生命周期事件
TASK_EVENTS = (
    "task_created",
    "task_started",
    "task_updated",
    "task_completed",
    "task_deleted",
)


class TaskHookManager:
    """任务生命周期 Hook 管理器

    用法:
        from app.core.task_system.hooks import task_hook_manager

        async def on_completed(task: dict):
            ...

        task_hook_manager.register("task_completed", on_completed)
    """

    def __init__(self):
        self._hooks: Dict[str, List[Callable]] = {event: [] for event in TASK_EVENTS}

    def register(self, event: str, handler: Callable) -> None:
        """注册指定事件的钩子

        Args:
            event: 事件名（必须是 TASK_EVENTS 之一）
            handler: 同步或异步回调，接收任务字典作为唯一参数
        """
        if event not in TASK_EVENTS:
            raise ValueError(f"Unknown task event: '{event}'. Valid: {', '.join(TASK_EVENTS)}")
        if handler not in self._hooks[event]:
            self._hooks[event].append(handler)
            logger.debug("Task hook registered", task_event=event, handler=getattr(handler, "__name__", repr(handler)))

    def unregister(self, event: str, handler: Callable) -> bool:
        """注销指定事件的钩子"""
        if event in self._hooks and handler in self._hooks[event]:
            self._hooks[event].remove(handler)
            return True
        return False

    def list_handlers(self, event: str) -> List[Callable]:
        """列出指定事件的钩子（副本）"""
        return list(self._hooks.get(event, []))

    async def emit(self, event: str, task_data: Dict[str, Any]) -> None:
        """触发指定事件的所有钩子

        单个钩子异常仅记录日志（含完整堆栈），不中断后续钩子执行。
        """
        handlers = self._hooks.get(event, [])
        if not handlers:
            return

        for handler in handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    await handler(task_data)
                else:
                    result = handler(task_data)
                    if inspect.isawaitable(result):
                        await result
            except Exception as e:
                logger.error(
                    "Task hook failed",
                    task_event=event,
                    handler=getattr(handler, "__name__", repr(handler)),
                    task_id=task_data.get("id"),
                    error=str(e),
                    exc_info=True,
                )


# 全局单例
task_hook_manager = TaskHookManager()
