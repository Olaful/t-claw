"""文件型任务系统

任务持久化在 .happy-claw/tasks/{HAPPY_CLAW_TASK_LIST_ID}/{task_id}.json，
提供文件锁并发保护、依赖强制执行、任务所有权、生命周期 Hooks、
文件响应式监听与自动清理能力。

导出:
    - task_store: 核心存储单例
    - task_hook_manager: 生命周期 Hook 管理器
    - task_watcher: 文件监听器单例
"""
from app.core.task_system.hooks import task_hook_manager, TASK_EVENTS
from app.core.task_system.store import task_store, TaskStore, TASK_TYPE_PREFIXES, TASK_STATUSES
from app.core.task_system.watcher import task_watcher

__all__ = [
    "task_store",
    "TaskStore",
    "task_hook_manager",
    "task_watcher",
    "TASK_EVENTS",
    "TASK_TYPE_PREFIXES",
    "TASK_STATUSES",
]
