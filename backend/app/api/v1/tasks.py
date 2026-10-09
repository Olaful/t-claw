"""任务与后台任务API"""
from fastapi import APIRouter, HTTPException

from app.models.schemas.task import AsyncTask, ScheduledTask, CronTrigger
from app.services.task_service import task_service

router = APIRouter()


@router.get("/")
async def list_tasks(status: str = None):
    """列出任务"""
    tasks = task_service.list_tasks(status=status)
    return {
        "tasks": [
            {
                "id": t.id,
                "task_type": t.task_type,
                "status": t.status,
                "progress": t.progress,
                "result": t.result,
                "error": t.error,
                "created_at": t.created_at.isoformat(),
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in tasks
        ],
        "total": len(tasks),
    }


@router.get("/{task_id}")
async def get_task(task_id: str):
    """获取任务详情"""
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "result": task.result,
        "error": task.error,
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消任务"""
    task = task_service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if task.status == "running":
        task.status = "cancelled"
        return {"message": "Task cancelled", "task_id": task_id}
    
    raise HTTPException(status_code=400, detail="Task cannot be cancelled")


# ===== 定时任务 =====

@router.get("/scheduled/list")
async def list_scheduled_tasks():
    """列出定时任务"""
    tasks = task_service.list_scheduled_tasks()
    return {
        "tasks": [
            {
                "id": t.id,
                "name": t.name,
                "task_type": t.task_type,
                "cron_expression": t.cron_expression,
                "enabled": t.enabled,
                "last_run": t.last_run.isoformat() if t.last_run else None,
                "next_run": t.next_run.isoformat() if t.next_run else None,
            }
            for t in tasks
        ],
        "total": len(tasks),
    }


@router.post("/scheduled/")
async def create_scheduled_task(
    name: str,
    task_type: str,
    cron_expression: str,
    params: dict = None,
):
    """创建定时任务"""
    import uuid
    
    task = ScheduledTask(
        id=str(uuid.uuid4()),
        name=name,
        task_type=task_type,
        cron_expression=cron_expression,
        params=params or {},
    )
    
    task_service.register_scheduled_task(task)
    
    return {
        "id": task.id,
        "name": task.name,
        "cron_expression": task.cron_expression,
        "message": "Scheduled task created",
    }


@router.delete("/scheduled/{task_id}")
async def delete_scheduled_task(task_id: str):
    """删除定时任务"""
    tasks = task_service.list_scheduled_tasks()
    task = next((t for t in tasks if t.id == task_id), None)
    
    if not task:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    
    # 从列表中移除
    task_service._scheduled_tasks.pop(task_id, None)
    return {"message": "Scheduled task deleted", "task_id": task_id}


# ===== 系统状态 =====

@router.get("/system/status")
async def system_status():
    """系统状态"""
    agents = task_service.list_agents()
    tasks = task_service.list_tasks()
    
    return {
        "agents": {
            "total": len(agents),
            "running": len([a for a in agents if a.get("status") == "running"]),
            "idle": len([a for a in agents if a.get("status") == "idle"]),
        },
        "tasks": {
            "total": len(tasks),
            "running": len([t for t in tasks if t.status == "running"]),
            "completed": len([t for t in tasks if t.status == "completed"]),
            "failed": len([t for t in tasks if t.status == "failed"]),
        },
        "scheduled_tasks": len(task_service.list_scheduled_tasks()),
    }
