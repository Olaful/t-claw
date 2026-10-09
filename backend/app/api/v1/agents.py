"""Agent管理API"""
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query

from app.models.schemas.agent import (
    CreateAgentRequest, UpdateAgentRequest, AgentResponse, AgentListResponse, AgentStatus, LLMProvider
)
from app.services.task_service import task_service
from app.services.persistence import persistence
from app.core.settings_store import (
    set_agent_models, remove_agent_entry, get_agent_models,
)
from app.config import settings
import structlog

logger = structlog.get_logger()

router = APIRouter()


def _fill_model_config(config) -> None:
    """从 settings.json 的 agent.entries.{name} 填充 config.models / config.primary_model。

    模型配置不入库，统一从 settings.json 读取，供前端展示当前选择。
    """
    if config is None or not config.name:
        return
    try:
        models, primary = get_agent_models(config.name)
        config.models = models
        config.primary_model = primary
    except Exception as e:
        logger.warning("Failed to load agent model config", name=config.name, error=str(e))


@router.post("/", response_model=AgentResponse)
async def create_agent(request: CreateAgentRequest):
    """创建Agent"""
    # 检查名称是否已存在
    existing_agents = task_service.list_agents()
    for existing in existing_agents:
        if existing.get("name") == request.config.name:
            raise HTTPException(
                status_code=400,
                detail=f"Agent名称 '{request.config.name}' 已存在，请使用其他名称"
            )
    
    # 先将模型配置写入 settings.json（agent.entries.{name}.models/model），
    # 以便 create_agent 能根据 model.primary 解析并应用默认模型与参数
    try:
        if request.config.models is not None:
            set_agent_models(
                agent_name=request.config.name,
                models=request.config.models,
                primary=request.config.primary_model,
            )
    except Exception as e:
        logger.error("Failed to persist agent model config", name=request.config.name, error=str(e))
    
    agent = task_service.create_agent(request.config, save_to_db=False)
    
    # 同步保存到数据库（确保数据持久化）
    await persistence.save_agent(request.config, agent.status)
    
    # 确保 name 和 description 不为 None
    name = agent.config.name if agent.config and agent.config.name else "未命名Agent"
    description = agent.config.description if agent.config and agent.config.description else ""
    
    return AgentResponse(
        id=agent.id,
        name=name,
        description=description,
        config=agent.config,
        status=agent.status,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


@router.get("/", response_model=AgentListResponse)
async def list_agents():
    """列出所有Agent（包含禁用的agent，禁用的agent不会加载运行）"""
    # 从数据库加载所有agent（包括禁用的）
    all_configs = await persistence.load_all_agents(include_disabled=True)
    
    agents_response = []
    for config in all_configs:
        _fill_model_config(config)
        is_enabled = config.metadata.get('is_enabled', True) if config.metadata else True
        
        # 从 metadata 获取 DB 时间戳（准确值）
        db_created_at = config.metadata.get('db_created_at') if config.metadata else None
        db_updated_at = config.metadata.get('db_updated_at') if config.metadata else None
        created_at = datetime.fromisoformat(db_created_at) if db_created_at else datetime.utcnow()
        updated_at = datetime.fromisoformat(db_updated_at) if db_updated_at else datetime.utcnow()
        
        # 检查是否在 task_service 中运行
        runtime_agent = task_service.get_agent(config.id)
        status = runtime_agent.status if runtime_agent else AgentStatus.IDLE
        
        agents_response.append(AgentResponse(
            id=config.id or "",
            name=config.name or "未命名Agent",
            description=config.description or "",
            config=config,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            is_enabled=is_enabled,
        ))
    
    # 排序：先按启用状态（启用在前），再按更新时间（最新在前）
    agents_response.sort(key=lambda x: (not x.is_enabled, -x.updated_at.timestamp()))
    
    return AgentListResponse(
        agents=agents_response,
        total=len(agents_response),
    )


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str):
    """获取Agent详情（支持已禁用的agent）"""
    # 先尝试从内存获取
    agent = task_service.get_agent(agent_id)
    if agent:
        name = agent.config.name if agent.config and agent.config.name else "未命名Agent"
        description = agent.config.description if agent.config and agent.config.description else ""
        _fill_model_config(agent.config)
        is_enabled = True
        metadata = agent.config.metadata if agent.config else None
        if metadata:
            is_enabled = metadata.get('is_enabled', True)
        # 从 metadata 获取 DB 时间戳
        db_created_at = metadata.get('db_created_at') if metadata else None
        db_updated_at = metadata.get('db_updated_at') if metadata else None
        created_at = datetime.fromisoformat(db_created_at) if db_created_at else agent.created_at
        updated_at = datetime.fromisoformat(db_updated_at) if db_updated_at else agent.updated_at
        return AgentResponse(
            id=agent.id,
            name=name,
            description=description,
            config=agent.config,
            status=agent.status,
            created_at=created_at,
            updated_at=updated_at,
            current_task=agent.current_task,
            is_enabled=is_enabled,
        )
    
    # 内存中没有，从DB加载（可能是禁用的agent）
    config = await persistence.load_agent(agent_id)
    if not config:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    _fill_model_config(config)
    is_enabled = config.metadata.get('is_enabled', True) if config.metadata else True
    db_created_at = config.metadata.get('db_created_at') if config.metadata else None
    db_updated_at = config.metadata.get('db_updated_at') if config.metadata else None
    created_at = datetime.fromisoformat(db_created_at) if db_created_at else datetime.utcnow()
    updated_at = datetime.fromisoformat(db_updated_at) if db_updated_at else datetime.utcnow()
    return AgentResponse(
        id=config.id or agent_id,
        name=config.name or "未命名Agent",
        description=config.description or "",
        config=config,
        status=AgentStatus.IDLE,
        created_at=created_at,
        updated_at=updated_at,
        is_enabled=is_enabled,
    )


@router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: str, request: UpdateAgentRequest):
    """更新Agent配置"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # 更新配置
    old_name = agent.config.name
    if request.name is not None:
        agent.config.name = request.name
    if request.description is not None:
        agent.config.description = request.description
    if request.system_prompt is not None:
        agent.config.system_prompt = request.system_prompt
    if request.llm_model is not None:
        agent.config.llm_model = request.llm_model
    if request.temperature is not None:
        agent.config.temperature = request.temperature
    if request.max_tokens is not None:
        agent.config.max_tokens = request.max_tokens
    if request.primary_model is not None:
        llm_provider, agent.config.llm_model = request.primary_model.split("/")
        agent.config.llm_provider = (LLMProvider(llm_provider))
    
    # 同步模型配置到 settings.json（支持重命名时迁移旧条目）
    try:
        if request.models is not None:
            set_agent_models(
                agent_name=agent.config.name,
                models=request.models,
                primary=request.primary_model,
                old_name=old_name,
            )
        elif old_name and old_name != agent.config.name:
            # 仅重命名、未改模型：迁移旧条目到新名称
            models, primary = get_agent_models(old_name)
            set_agent_models(
                agent_name=agent.config.name,
                models=models,
                primary=primary,
                old_name=old_name,
            )
    except Exception as e:
        logger.error("Failed to persist agent model config", name=agent.config.name, error=str(e))
    
    # ✅ 根据 model.primary 重新解析并应用到运行中的 agent（使模型切换即时生效）
    if not agent.config.is_subagent:
        task_service._apply_primary_model_config(agent.config)
    
    # 保存到数据库（持久化解析后的 llm_model / 参数）
    await persistence.save_agent(agent.config, agent.status)
    
    # 回填模型配置供响应展示
    _fill_model_config(agent.config)
    
    # 从 metadata 获取 is_enabled
    is_enabled = True
    if agent.config and agent.config.metadata:
        is_enabled = agent.config.metadata.get('is_enabled', True)
    
    return AgentResponse(
        id=agent.id,
        config=agent.config,
        status=agent.status,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        is_enabled=is_enabled,
    )


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str):
    """删除Agent（支持内存中和DB中的agent）"""
    # 0. 先获取 agent 名称（用于清理 settings.json 条目）
    agent_name = None
    # 1. 如果在内存中运行，先停止并从内存移除
    runtime_agent = task_service.get_agent(agent_id)
    if runtime_agent:
        agent_name = runtime_agent.config.name if runtime_agent.config else None
        await runtime_agent.stop()
        await runtime_agent.cleanup()
        del task_service._agents[agent_id]
    else:
        db_config = await persistence.load_agent(agent_id)
        if db_config:
            agent_name = db_config.name
    
    # 2. 从数据库删除（包括关联的会话和消息）
    try:
        await persistence.delete_agent(agent_id)
    except Exception as e:
        logger.error("Failed to delete agent from DB", agent_id=agent_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to delete agent: {str(e)}")
    
    # 3. 清理 settings.json 中的 agent.entries.{name} 条目
    if agent_name:
        try:
            remove_agent_entry(agent_name)
        except Exception as e:
            logger.warning("Failed to remove agent entry from settings.json", name=agent_name, error=str(e))
    
    return {"message": "Agent deleted", "agent_id": agent_id}


@router.post("/{agent_id}/start")
async def start_agent(agent_id: str):
    """启动Agent"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    await agent.start()
    return {"message": "Agent started", "agent_id": agent_id}


@router.post("/{agent_id}/stop")
async def stop_agent(agent_id: str):
    """停止Agent"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    await agent.stop()
    return {"message": "Agent stopped", "agent_id": agent_id}


@router.post("/{agent_id}/pause")
async def pause_agent(agent_id: str):
    """暂停Agent"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    await agent.pause()
    return {"message": "Agent paused", "agent_id": agent_id}


@router.get("/{agent_id}/conversations")
async def list_conversations(agent_id: str):
    """列出Agent的会话"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    conversations = agent.list_conversations()
    return {
        "conversations": [
            {
                "id": c.id,
                "title": c.title,
                "status": c.status,
                "message_count": len(c.messages),
                "context_tokens": c.context_tokens,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }
            for c in conversations
        ],
        "total": len(conversations),
    }


@router.post("/{agent_id}/enable")
async def enable_agent(agent_id: str):
    """启用Agent"""
    success = await persistence.enable_agent(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    # 同步更新内存中的agent metadata
    runtime_agent = task_service.get_agent(agent_id)
    if runtime_agent and runtime_agent.config:
        if not runtime_agent.config.metadata:
            runtime_agent.config.metadata = {}
        runtime_agent.config.metadata['is_enabled'] = True
    return {"message": "Agent enabled", "agent_id": agent_id}


@router.post("/{agent_id}/disable")
async def disable_agent(agent_id: str):
    """禁用Agent"""
    success = await persistence.disable_agent(agent_id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    # 同步更新内存中的agent metadata
    runtime_agent = task_service.get_agent(agent_id)
    if runtime_agent and runtime_agent.config:
        if not runtime_agent.config.metadata:
            runtime_agent.config.metadata = {}
        runtime_agent.config.metadata['is_enabled'] = False
    return {"message": "Agent disabled", "agent_id": agent_id}


@router.get("/llm/models")
async def get_llm_models():
    """获取所有可用的LLM模型列表（从settings.json读取）"""
    try:
        # 从settings.json中读取 llm.providers 配置
        llm_config = settings.agent.llm if hasattr(settings, 'agent') and hasattr(settings.agent, 'llm') else None
        
        if not llm_config or not hasattr(llm_config, 'providers'):
            logger.warning("No LLM providers configured in settings.json")
            return {"providers": {}, "models": []}
        
        providers_data = {}
        all_models = []
        
        # 遍历所有提供商
        for provider_name, provider_config in llm_config.providers.items():
            # 支持 DotDict 和 dict 两种类型
            config_dict = provider_config.to_dict() if hasattr(provider_config, 'to_dict') else provider_config
            
            models = config_dict.get('models', [])
            base_url = config_dict.get('baseUrl', '')
            
            # 构建提供商信息
            providers_data[provider_name] = {
                'baseUrl': base_url,
                'modelCount': len(models)
            }
            
            # 收集所有模型，并标记所属提供商
            for model in models:
                model_info = {
                    **model,
                    'provider': provider_name,
                    'displayName': f"{model.get('name', model.get('id'))} ({provider_name})"
                }
                all_models.append(model_info)
        
        logger.info(f"Loaded {len(all_models)} LLM models from {len(providers_data)} providers")
        
        return {
            "providers": providers_data,
            "models": all_models,
            "total": len(all_models)
        }
        
    except Exception as e:
        logger.error("Failed to load LLM models", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load LLM models: {str(e)}"
        )
