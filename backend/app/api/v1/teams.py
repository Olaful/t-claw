"""团队协作API"""
from fastapi import APIRouter, HTTPException

from app.models.schemas.team import (
    CreateTeamRequest, AddMemberRequest, CreateTaskRequest, ClaimTaskRequest,
    TeamResponse, TeamListResponse, TaskItemResponse, TaskListResponse,
    TeamConfig, TaskPriority, TeamStatus,
)
from app.agents.multi import create_team
from app.services.llm_service import create_llm_service
from app.tools.registry import tool_registry
from app.core.memory import create_memory_manager
from app.core.prompt_engine import create_prompt_engine
from app.services.persistence import persistence

router = APIRouter()

# 团队存储（内存缓存，数据已持久化到数据库）
_teams = {}


@router.post("/", response_model=TeamResponse)
async def create_team_api(request: CreateTeamRequest):
    """创建团队"""
    llm_service = create_llm_service()
    memory_manager = create_memory_manager()
    prompt_engine = create_prompt_engine()
    
    team = create_team(
        config=request.config,
        llm_service=llm_service,
        tool_registry=tool_registry,
        memory_manager=memory_manager,
        prompt_engine=prompt_engine,
    )
    
    _teams[team.id] = team
    
    # 保存到数据库
    await persistence.save_team(team.id, team.config, team.status)
    
    return TeamResponse(
        id=team.id,
        config=team.config,
        status=team.status,
        member_count=len(team._members),
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


@router.get("/", response_model=TeamListResponse)
async def list_teams():
    """列出所有团队"""
    teams = list(_teams.values())
    return TeamListResponse(
        teams=[
            TeamResponse(
                id=t.id,
                config=t.config,
                status=t.status,
                member_count=len(t._members),
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
            for t in teams
        ],
        total=len(teams),
    )


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(team_id: str):
    """获取团队详情"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    return TeamResponse(
        id=team.id,
        config=team.config,
        status=team.status,
        member_count=len(team._members),
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


@router.delete("/{team_id}")
async def delete_team(team_id: str):
    """删除团队"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    await team.stop()
    del _teams[team_id]
    
    # 从数据库删除
    await persistence.delete_team(team_id)
    
    return {"message": "Team deleted", "team_id": team_id}


@router.post("/{team_id}/members")
async def add_member(team_id: str, request: AddMemberRequest):
    """添加团队成员"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    from app.models.schemas.agent import AgentConfig
    config = AgentConfig(name=f"Agent-{request.agent_id[:8]}")
    
    member = await team.add_member(
        agent_config=config,
        role=request.role,
    )
    
    return {
        "agent_id": member.agent_id,
        "role": member.role,
        "status": member.status,
    }


@router.delete("/{team_id}/members/{agent_id}")
async def remove_member(team_id: str, agent_id: str):
    """移除团队成员"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    success = await team.remove_member(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Member not found")
    
    return {"message": "Member removed", "agent_id": agent_id}


@router.get("/{team_id}/members")
async def list_members(team_id: str):
    """列出团队成员"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    return {
        "members": [
            {
                "agent_id": m.agent_id,
                "role": m.role,
                "status": m.status,
                "capabilities": m.capabilities,
                "current_task": m.current_task,
            }
            for m in team._members.values()
        ],
        "total": len(team._members),
    }


# ===== 白板任务 =====

@router.post("/{team_id}/tasks")
async def create_task(team_id: str, request: CreateTaskRequest):
    """创建白板任务"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    task = await team.create_task(
        title=request.title,
        description=request.description,
        priority=request.priority,
        assigned_to=request.assigned_to
    )
    
    return TaskItemResponse(
        id=task.id,
        team_id=task.team_id,
        title=task.title,
        description=task.description,
        priority=task.priority,
        status=task.status,
        assigned_to=task.assigned_to,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get("/{team_id}/tasks", response_model=TaskListResponse)
async def list_tasks(team_id: str, status: str = None):
    """列出团队任务"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    tasks = list(team._tasks.values())
    if status:
        tasks = [t for t in tasks if t.status.value == status]
    
    return TaskListResponse(
        tasks=[
            TaskItemResponse(
                id=t.id,
                team_id=t.team_id,
                title=t.title,
                description=t.description,
                priority=t.priority,
                status=t.status,
                assigned_to=t.assigned_to,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
            for t in tasks
        ],
        total=len(tasks),
    )


@router.post("/{team_id}/tasks/{task_id}/claim")
async def claim_task(team_id: str, task_id: str, request: ClaimTaskRequest):
    """认领任务"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    success = await team.claim_task(task_id, request.agent_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot claim task")
    
    return {"message": "Task claimed", "task_id": task_id, "agent_id": request.agent_id}


@router.post("/{team_id}/tasks/{task_id}/complete")
async def complete_task(team_id: str, task_id: str, result: str = None):
    """完成任务"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    await team.complete_task(task_id, result)
    return {"message": "Task completed", "task_id": task_id}


@router.post("/{team_id}/execute")
async def execute_team_task(team_id: str, goal: str):
    """协作执行目标"""
    team = _teams.get(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    result = await team.assign_and_execute(goal)
    return result
