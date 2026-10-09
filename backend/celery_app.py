"""Celery异步任务队列配置"""
from celery import Celery
from app.config import settings


# 创建Celery应用
celery_app = Celery(
    "agent_framework",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

# Celery配置
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=settings.AGENT_TIMEOUT_SECONDS,
    task_soft_time_limit=settings.AGENT_TIMEOUT_SECONDS - 60,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    beat_schedule={},  # 定时任务调度
)

# 自动发现任务模块
celery_app.autodiscover_tasks(["app.services"])


@celery_app.task(bind=True, name="app.tasks.health_check")
def health_check_task(self):
    """健康检查任务"""
    return {"status": "healthy", "worker": self.request.hostname}


@celery_app.task(bind=True, name="app.tasks.run_agent_task")
def run_agent_task(self, agent_id: str, task_data: dict):
    """运行Agent任务（异步）"""
    # 这里会调用Agent执行逻辑
    # 实际实现需要在任务中导入异步运行器
    import asyncio
    from app.services.task_service import TaskService
    
    async def _run():
        service = TaskService()
        return await service.execute_agent_task(agent_id, task_data)
    
    return asyncio.run(_run())


@celery_app.task(bind=True, name="app.tasks.scheduled_task")
def scheduled_task(self, task_name: str, params: dict):
    """定时任务执行器"""
    import asyncio
    from app.services.task_service import TaskService
    
    async def _run():
        service = TaskService()
        return await service.run_scheduled_task(task_name, params)
    
    return asyncio.run(_run())
