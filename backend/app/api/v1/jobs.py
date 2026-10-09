"""后台作业管理 API"""
from fastapi import APIRouter, HTTPException

import structlog

from app.core.background_jobs import background_job_manager

logger = structlog.get_logger()

router = APIRouter()


@router.get("/{session_id}")
async def list_jobs(session_id: str):
    """列出会话的所有后台任务"""
    jobs = background_job_manager.list_jobs(session_id)
    return {
        "session_id": session_id,
        "total": len(jobs),
        "jobs": [job.to_dict() for job in jobs],
    }


@router.get("/{session_id}/{job_id}")
async def get_job(session_id: str, job_id: str):
    """获取任务详情"""
    job = background_job_manager.get_job(job_id)
    if not job:
        # 尝试从磁盘加载
        from app.config import _HAPPYCLAW_HOME
        import json
        state_file = _HAPPYCLAW_HOME / "jobs" / session_id / job_id / "state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                return {"job": state}
            except Exception:
                pass
        raise HTTPException(status_code=404, detail="Job not found")
    
    return {"job": job.to_dict()}


@router.post("/{session_id}/{job_id}/cancel")
async def cancel_job(session_id: str, job_id: str):
    """取消任务"""
    job = background_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.session_id != session_id:
        raise HTTPException(status_code=400, detail="Job does not belong to this session")
    
    success = await background_job_manager.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=400, detail="Job cannot be cancelled (already finished or not found)")
    
    return {"message": "Job cancelled", "job_id": job_id}


@router.get("/{session_id}/{job_id}/output")
async def get_job_output(session_id: str, job_id: str, tail: int = 0):
    """获取任务输出内容"""
    job = background_job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.session_id != session_id:
        raise HTTPException(status_code=400, detail="Job does not belong to this session")
    
    output = background_job_manager.get_job_output(job_id, tail_chars=tail)
    if output is None:
        raise HTTPException(status_code=404, detail="Output file not found")
    
    return {
        "job_id": job_id,
        "output": output,
        "output_file": str(job.output_file),
    }
