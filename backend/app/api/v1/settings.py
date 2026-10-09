"""
LLM配置管理API
"""
from typing import Dict, Any
from fastapi import APIRouter, HTTPException

from app.core.settings_store import update_settings, read_settings
from app.models.schemas.settings import (
    LLMProviderConfig,
    LLMModelConfig,
    LLMProviderResponse,
    LLMProvidersResponse,
    AddProviderRequest,
    UpdateProviderRequest,
    AddModelRequest,
    UpdateModelRequest,
    MCPServerConfigRequest,
    MCPGlobalConfigRequest,
    MCPServerResponse,
    MCPConfigResponse,
    ToolItemResponse,
    ToolToggleRequest,
    ToolTimeoutConfigRequest,
    ToolRateLimitRequest,
    SubagentToolsRequest,
    TeamToolsRequest,
    ToolsConfigResponse,
    SkillItemResponse,
    SkillToggleRequest,
    AgentSkillsRequest,
    SkillsConfigResponse,
    MemoryConfigResponse,
    MemoryConfigRequest,
)

import structlog
logger = structlog.get_logger()

router = APIRouter(prefix="/settings", tags=["settings"])


def _get_llm_providers(data: Dict[str, Any]) -> Dict[str, Any]:
    """从配置数据中获取llm.providers"""
    llm = data.get("agent", {}).get("llm", {})
    return llm.get("providers", {}) or {}


@router.get("/llm/providers", response_model=LLMProvidersResponse)
async def get_llm_providers():
    """获取所有LLM Provider及其Models"""
    data = read_settings()
    providers_dict = _get_llm_providers(data)
    
    providers = []
    for name, config in providers_dict.items():
        if hasattr(config, 'get'):
            # 转换models列表
            models = config.get("models", []) or []
            provider_config = LLMProviderConfig(
                baseUrl=config.get("baseUrl", ""),
                apiKey=config.get("apiKey", ""),
                billingMode=config.get("billingMode"),
                peakTime=config.get("peakTime"),
                models=models,
            )
            providers.append(LLMProviderResponse(name=name, config=provider_config))
    
    return LLMProvidersResponse(providers=providers, total=len(providers))


@router.post("/llm/providers", response_model=LLMProviderResponse)
async def add_llm_provider(request: AddProviderRequest):
    """添加新的LLM Provider"""
    # 检查是否已存在
    data = read_settings()
    providers_dict = _get_llm_providers(data)
    if request.name in providers_dict:
        raise HTTPException(status_code=400, detail=f"Provider '{request.name}' 已存在")
    
    def _mutator(data: Dict[str, Any]) -> None:
        llm = data.setdefault("agent", {}).setdefault("llm", {})
        providers = llm.setdefault("providers", {})
        providers[request.name] = {
            "baseUrl": request.baseUrl,
            "apiKey": request.apiKey,
        }
        if request.billingMode:
            providers[request.name]["billingMode"] = request.billingMode
        if request.peakTime:
            providers[request.name]["peakTime"] = request.peakTime.model_dump()
        providers[request.name]["models"] = []
    
    update_settings(_mutator)
    logger.info("Added LLM provider", name=request.name)
    
    return LLMProviderResponse(
        name=request.name,
        config=LLMProviderConfig(
            baseUrl=request.baseUrl,
            apiKey=request.apiKey,
            billingMode=request.billingMode,
            peakTime=request.peakTime,
            models=[],
        )
    )


@router.put("/llm/providers/{provider_id}", response_model=LLMProviderResponse)
async def update_llm_provider(provider_id: str, request: UpdateProviderRequest):
    """更新LLM Provider配置"""
    data = read_settings()
    providers_dict = _get_llm_providers(data)
    if provider_id not in providers_dict:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' 不存在")
    
    def _mutator(data: Dict[str, Any]) -> None:
        providers = data.get("agent", {}).get("llm", {}).get("providers", {})
        provider = providers.get(provider_id, {})
        if request.baseUrl is not None:
            provider["baseUrl"] = request.baseUrl
        if request.apiKey is not None:
            provider["apiKey"] = request.apiKey
        if request.billingMode is not None:
            provider["billingMode"] = request.billingMode
        if request.peakTime is not None:
            provider["peakTime"] = request.peakTime.model_dump()
        providers[provider_id] = provider
    
    update_settings(_mutator)
    logger.info("Updated LLM provider", name=provider_id)
    
    # 重新读取返回最新数据
    data = read_settings()
    providers_dict = _get_llm_providers(data)
    config = providers_dict.get(provider_id, {})
    
    return LLMProviderResponse(
        name=provider_id,
        config=LLMProviderConfig(
            baseUrl=config.get("baseUrl", ""),
            apiKey=config.get("apiKey", ""),
            billingMode=config.get("billingMode"),
            peakTime=config.get("peakTime"),
            models=config.get("models", []),
        )
    )


@router.delete("/llm/providers/{provider_id}")
async def delete_llm_provider(provider_id: str):
    """删除LLM Provider"""
    data = read_settings()
    providers_dict = _get_llm_providers(data)
    if provider_id not in providers_dict:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' 不存在")
    
    def _mutator(data: Dict[str, Any]) -> None:
        providers = data.get("agent", {}).get("llm", {}).get("providers", {})
        providers.pop(provider_id, None)
    
    update_settings(_mutator)
    logger.info("Deleted LLM provider", name=provider_id)
    
    return {"message": f"Provider '{provider_id}' 已删除"}


@router.post("/llm/providers/{provider_id}/models", response_model=LLMModelConfig)
async def add_llm_model(provider_id: str, request: AddModelRequest):
    """为Provider添加新Model"""
    data = read_settings()
    providers_dict = _get_llm_providers(data)
    if provider_id not in providers_dict:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' 不存在")
    
    # 检查模型是否已存在
    existing_models = providers_dict[provider_id].get("models", []) or []
    for m in existing_models:
        if m.get("id") == request.model.id:
            raise HTTPException(status_code=400, detail=f"Model '{request.model.id}' 已存在")
    
    def _mutator(data: Dict[str, Any]) -> None:
        providers = data.get("agent", {}).get("llm", {}).get("providers", {})
        provider = providers.get(provider_id, {})
        models = provider.get("models", []) or []
        models.append(request.model.model_dump())
        provider["models"] = models
        providers[provider_id] = provider
    
    update_settings(_mutator)
    logger.info("Added LLM model", provider=provider_id, model=request.model.id)
    
    return request.model


@router.put("/llm/providers/{provider_id}/models/{model_id}", response_model=LLMModelConfig)
async def update_llm_model(provider_id: str, model_id: str, request: UpdateModelRequest):
    """更新Provider的Model配置"""
    data = read_settings()
    providers_dict = _get_llm_providers(data)
    if provider_id not in providers_dict:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' 不存在")
    
    # 检查模型是否存在
    existing_models = providers_dict[provider_id].get("models", []) or []
    model_exists = any(m.get("id") == model_id for m in existing_models)
    if not model_exists:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' 不存在")
    
    def _mutator(data: Dict[str, Any]) -> None:
        providers = data.get("agent", {}).get("llm", {}).get("providers", {})
        provider = providers.get(provider_id, {})
        models = provider.get("models", []) or []
        # 找到并更新模型
        for i, m in enumerate(models):
            if m.get("id") == model_id:
                models[i] = request.model.model_dump()
                break
        provider["models"] = models
        providers[provider_id] = provider
    
    update_settings(_mutator)
    logger.info("Updated LLM model", provider=provider_id, model=model_id)
    
    return request.model


@router.delete("/llm/providers/{provider_id}/models/{model_id}")
async def delete_llm_model(provider_id: str, model_id: str):
    """删除Provider的Model"""
    data = read_settings()
    providers_dict = _get_llm_providers(data)
    if provider_id not in providers_dict:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_id}' 不存在")
    
    def _mutator(data: Dict[str, Any]) -> None:
        providers = data.get("agent", {}).get("llm", {}).get("providers", {})
        provider = providers.get(provider_id, {})
        models = provider.get("models", []) or []
        # 过滤掉要删除的模型
        models = [m for m in models if m.get("id") != model_id]
        provider["models"] = models
        providers[provider_id] = provider
    
    update_settings(_mutator)
    logger.info("Deleted LLM model", provider=provider_id, model=model_id)
    
    return {"message": f"Model '{model_id}' 已删除"}


# ===== MCP 配置管理 =====

def _get_mcp_section(data: Dict[str, Any]) -> Dict[str, Any]:
    """从配置数据中获取 mcp 段"""
    return data.get("mcp", {}) or {}


def _build_server_response(name: str, raw: Dict[str, Any], status_info: Dict[str, Any] = None) -> MCPServerResponse:
    """构造服务器响应（合并配置与运行时状态）"""
    status_info = status_info or {}
    return MCPServerResponse(
        name=name,
        type=raw.get("type", "stdio"),
        enabled=bool(raw.get("enabled", True)),
        status=status_info.get("status"),
        tools_count=status_info.get("tools_count", 0),
        last_error=status_info.get("last_error"),
        oauth_required=bool((raw.get("oauth") or {}).get("enabled", False)),
        source=raw.get("source", "settings"),
        description=raw.get("description"),
        command=raw.get("command"),
        args=list(raw.get("args", []) or []),
        url=raw.get("url"),
        headers=dict(raw.get("headers", {}) or {}),
        module=raw.get("module"),
        oauth=raw.get("oauth"),
    )


@router.get("/mcp", response_model=MCPConfigResponse)
async def get_mcp_config():
    """获取完整 MCP 配置（全局参数 + 所有服务器配置）"""
    data = read_settings()
    mcp_section = _get_mcp_section(data)

    # 全局配置（不含 servers）
    global_config = {k: v for k, v in mcp_section.items() if k != "servers"}

    # 服务器配置 + 运行时状态
    from app.tools.mcp import mcp_manager
    runtime_statuses = {s["name"]: s for s in mcp_manager.list_servers()}

    servers = []
    for name, raw in (mcp_section.get("servers", {}) or {}).items():
        status_info = runtime_statuses.get(name, {})
        servers.append(_build_server_response(name, raw, status_info))

    return MCPConfigResponse(
        global_config=global_config,
        servers=servers,
        total=len(servers),
    )


@router.put("/mcp")
async def update_mcp_global_config(request: MCPGlobalConfigRequest):
    """更新 MCP 全局配置（enabled/call_timeout 等）"""
    def _mutator(data: Dict[str, Any]) -> None:
        mcp = data.setdefault("mcp", {})
        if request.enabled is not None:
            mcp["enabled"] = request.enabled
        if request.call_timeout_seconds is not None:
            mcp["call_timeout_seconds"] = request.call_timeout_seconds
        if request.connect_timeout_seconds is not None:
            mcp["connect_timeout_seconds"] = request.connect_timeout_seconds
        if request.max_consecutive_errors is not None:
            mcp["max_consecutive_errors"] = request.max_consecutive_errors
        if request.reconnect_delay_seconds is not None:
            mcp["reconnect_delay_seconds"] = request.reconnect_delay_seconds
        if request.reconnect_backoff_max_seconds is not None:
            mcp["reconnect_backoff_max_seconds"] = request.reconnect_backoff_max_seconds

    update_settings(_mutator)
    logger.info("Updated MCP global config")
    return {"message": "MCP global config updated"}


@router.post("/mcp/servers", response_model=MCPServerResponse)
async def add_mcp_server(request: MCPServerConfigRequest):
    """添加 MCP 服务器"""
    data = read_settings()
    mcp_section = _get_mcp_section(data)
    servers = mcp_section.get("servers", {}) or {}
    if request.name in servers:
        raise HTTPException(status_code=400, detail=f"MCP server '{request.name}' 已存在")

    def _mutator(data: Dict[str, Any]) -> None:
        mcp = data.setdefault("mcp", {})
        svrs = mcp.setdefault("servers", {})
        svrs[request.name] = request.model_dump(exclude_none=True)

    update_settings(_mutator)
    logger.info("Added MCP server", name=request.name)

    # 返回最新配置
    data = read_settings()
    raw = (_get_mcp_section(data).get("servers", {}) or {}).get(request.name, {})
    return _build_server_response(request.name, raw)


@router.put("/mcp/servers/{server_name}", response_model=MCPServerResponse)
async def update_mcp_server(server_name: str, request: MCPServerConfigRequest):
    """更新 MCP 服务器配置"""
    data = read_settings()
    mcp_section = _get_mcp_section(data)
    servers = mcp_section.get("servers", {}) or {}
    if server_name not in servers:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_name}' 不存在")

    def _mutator(data: Dict[str, Any]) -> None:
        mcp = data.setdefault("mcp", {})
        svrs = mcp.setdefault("servers", {})
        svrs[server_name] = request.model_dump(exclude_none=True)

    update_settings(_mutator)
    logger.info("Updated MCP server", name=server_name)

    data = read_settings()
    raw = (_get_mcp_section(data).get("servers", {}) or {}).get(server_name, {})
    return _build_server_response(server_name, raw)


@router.delete("/mcp/servers/{server_name}")
async def delete_mcp_server(server_name: str):
    """删除 MCP 服务器"""
    data = read_settings()
    mcp_section = _get_mcp_section(data)
    servers = mcp_section.get("servers", {}) or {}
    if server_name not in servers:
        raise HTTPException(status_code=404, detail=f"MCP server '{server_name}' 不存在")

    def _mutator(data: Dict[str, Any]) -> None:
        mcp = data.get("mcp", {})
        svrs = mcp.get("servers", {}) or {}
        svrs.pop(server_name, None)

    update_settings(_mutator)
    logger.info("Deleted MCP server", name=server_name)
    return {"message": f"MCP server '{server_name}' 已删除"}


@router.post("/mcp/reload")
async def reload_mcp_config():
    """触发热重载：对比配置文件与运行状态，差量重载 MCP 服务器"""
    from app.tools.mcp import mcp_manager
    result = await mcp_manager.reload_config()
    logger.info("MCP config reloaded", result=result)
    return {"message": "MCP 配置已重载", **result}


# ===== 工具管理 =====

def _get_tools_section(data: Dict[str, Any]) -> Dict[str, Any]:
    """从配置数据中获取 tools 段"""
    return data.get("tools", {}) or {}


@router.get("/tools", response_model=ToolsConfigResponse)
async def get_tools_config():
    """获取工具管理完整配置：全部工具列表（含类型/启用状态）+ 超时/限流/subagent/团队配置"""
    from app.tools.registry import tool_registry, is_tool_globally_disabled

    data = read_settings()
    tools_section = _get_tools_section(data)
    global_disallowed = list(tools_section.get("disallowed", []) or [])

    # 工具列表（按 metadata.source 分类）
    items = []
    for t in tool_registry.list_tools():
        meta = t.metadata or {}
        source = meta.get("source")
        if source == "mcp":
            ttype = "mcp"
        elif source == "extension":
            ttype = "extension"
        else:
            ttype = "builtin"
        items.append(ToolItemResponse(
            name=t.name,
            description=t.description or "",
            type=ttype,
            enabled=not is_tool_globally_disabled(t.name, global_disallowed),
            system_tool=bool(meta.get("system_tool", False)),
            timeout_seconds=t.timeout_seconds,
            mcp_server=meta.get("mcp_server"),
        ))

    # 排序：内置 -> MCP -> 插件，同类按名称
    type_order = {"builtin": 0, "mcp": 1, "extension": 2}
    items.sort(key=lambda x: (type_order.get(x.type, 9), x.name))

    enabled_count = sum(1 for i in items if i.enabled)

    # subagent 分层工具配置
    agent_cfg = data.get("agent", {}) or {}
    subagent_cfg = agent_cfg.get("subagent", {}) or {}
    subagent = {
        "disallowed_tools_per_level": dict(subagent_cfg.get("disallowed_tools_per_level", {}) or {}),
        "allowed_tools_per_level": dict(subagent_cfg.get("allowed_tools_per_level", {}) or {}),
    }

    # 团队工具配置
    teams_cfg = data.get("teams", {}) or {}
    team_tools = teams_cfg.get("tools", {}) or {}
    team = {
        "disallowed": list(team_tools.get("disallowed", []) or []),
        "allowed": list(team_tools.get("allowed", []) or []),
    }

    return ToolsConfigResponse(
        tools=items,
        disallowed=global_disallowed,
        timeout_seconds=dict(tools_section.get("timeout_seconds", {}) or {}),
        rate_limiting=dict(tools_section.get("rate_limiting", {}) or {}),
        subagent=subagent,
        team=team,
        total=len(items),
        enabled_count=enabled_count,
        disabled_count=len(items) - enabled_count,
    )


@router.put("/tools/timeout")
async def update_tool_timeout(request: ToolTimeoutConfigRequest):
    """更新工具超时配置（tools.timeout_seconds），并应用到运行时注册表"""
    from app.tools.registry import reload_tool_runtime

    def _mutator(data: Dict[str, Any]) -> None:
        tools = data.setdefault("tools", {})
        timeout = dict(tools.get("timeout_seconds", {}) or {})
        if request.default is not None:
            timeout["default"] = request.default
        if request.overrides is not None:
            for k, v in request.overrides.items():
                if v is None:
                    timeout.pop(k, None)
                else:
                    timeout[k] = v
        tools["timeout_seconds"] = timeout

    update_settings(_mutator)
    result = reload_tool_runtime()
    logger.info("Updated tool timeout config", result=result)
    return {"message": "工具超时配置已更新", **result}


@router.put("/tools/rate-limit")
async def update_tool_rate_limit(request: ToolRateLimitRequest):
    """更新工具限流配置（tools.rate_limiting）"""
    def _mutator(data: Dict[str, Any]) -> None:
        tools = data.setdefault("tools", {})
        rl = dict(tools.get("rate_limiting", {}) or {})
        if request.enabled is not None:
            rl["enabled"] = request.enabled
        if request.requests_per_minute is not None:
            rl["requests_per_minute"] = request.requests_per_minute
        tools["rate_limiting"] = rl

    update_settings(_mutator)
    logger.info("Updated tool rate limit config")
    return {"message": "限流配置已更新"}


@router.put("/tools/subagent")
async def update_subagent_tools(request: SubagentToolsRequest):
    """更新 subagent 分层工具配置（agent.subagent.*_tools_per_level）"""
    def _mutator(data: Dict[str, Any]) -> None:
        agent = data.setdefault("agent", {})
        subagent = agent.setdefault("subagent", {})
        if request.disallowed_tools_per_level is not None:
            subagent["disallowed_tools_per_level"] = request.disallowed_tools_per_level
        if request.allowed_tools_per_level is not None:
            subagent["allowed_tools_per_level"] = request.allowed_tools_per_level

    update_settings(_mutator)
    logger.info("Updated subagent tools config")
    return {"message": "subagent 分层工具配置已更新"}


@router.put("/tools/team")
async def update_team_tools(request: TeamToolsRequest):
    """更新团队工具配置（teams.tools.disallowed/allowed）"""
    def _mutator(data: Dict[str, Any]) -> None:
        teams = data.setdefault("teams", {})
        tools = teams.setdefault("tools", {})
        if request.disallowed is not None:
            tools["disallowed"] = request.disallowed
        if request.allowed is not None:
            tools["allowed"] = request.allowed

    update_settings(_mutator)
    logger.info("Updated team tools config")
    return {"message": "团队工具配置已更新"}


@router.put("/tools/{tool_name}/toggle")
async def toggle_tool(tool_name: str, request: ToolToggleRequest):
    """启用/禁用单个工具（全局）：写入/移除 tools.disallowed，所有 agent 即时生效"""
    from app.tools.registry import reload_tool_runtime

    def _mutator(data: Dict[str, Any]) -> None:
        tools = data.setdefault("tools", {})
        disallowed = list(tools.get("disallowed", []) or [])
        if request.enabled:
            # 启用：从黑名单移除该工具名
            disallowed = [p for p in disallowed if p != tool_name]
        else:
            # 禁用：加入黑名单（去重）
            if tool_name not in disallowed:
                disallowed.append(tool_name)
        tools["disallowed"] = disallowed

    update_settings(_mutator)
    reload_tool_runtime()
    logger.info("Toggled tool", tool_name=tool_name, enabled=request.enabled)
    return {"message": "已启用" if request.enabled else "已禁用", "tool_name": tool_name, "enabled": request.enabled}


# ===== 技能管理 =====

def _get_skills_section(data: Dict[str, Any]) -> Dict[str, Any]:
    """从配置数据中获取 skills 段"""
    return data.get("skills", {}) or {}


@router.get("/skills", response_model=SkillsConfigResponse)
async def get_skills_config():
    """获取技能管理完整配置：全部技能列表（含类型/启用状态）+ 全局禁用列表 + 各 agent 技能配置"""
    from app.skills.loader import skill_loader, is_skill_globally_disabled, get_skill_type

    data = read_settings()
    skills_section = _get_skills_section(data)
    global_disallowed = list(skills_section.get("disallowed", []) or [])

    items = []
    for s in skill_loader.list_skills():
        globally_disabled = is_skill_globally_disabled(s.name, global_disallowed)
        # 展示真实门控原因（排除全局禁用附加的原因，避免与开关状态重复）
        gate_reasons = [r for r in (s.gate_reasons or []) if "global skills.disallowed" not in r]
        items.append(SkillItemResponse(
            name=s.name,
            description=s.description or "",
            type=get_skill_type(s.source_priority),
            enabled=not globally_disabled,
            context=s.context or "inline",
            version=str(s.version or "1.0.0"),
            gate_reasons=gate_reasons,
        ))

    # 排序：内置 -> 插件 -> 工作区，同类按名称
    type_order = {"builtin": 0, "plugin": 1, "workspace": 2}
    items.sort(key=lambda x: (type_order.get(x.type, 9), x.name))
    enabled_count = sum(1 for i in items if i.enabled)

    # 各 agent 技能配置
    agent_cfg = data.get("agent", {}) or {}
    entries = agent_cfg.get("entries", {}) or {}
    agents: Dict[str, Any] = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        sk = entry.get("skills", {}) or {}
        agents[name] = {
            "disallowed": list(sk.get("disallowed", []) or []),
            "allowed": list(sk.get("allowed", []) or []),
        }

    return SkillsConfigResponse(
        skills=items,
        disallowed=global_disallowed,
        agents=agents,
        total=len(items),
        enabled_count=enabled_count,
        disabled_count=len(items) - enabled_count,
    )


@router.put("/skills/agent/{agent_name}")
async def update_agent_skills(agent_name: str, request: AgentSkillsRequest):
    """更新某个 agent 的技能配置（agent.entries.{name}.skills.disallowed/allowed）"""
    def _mutator(data: Dict[str, Any]) -> None:
        agent = data.setdefault("agent", {})
        entries = agent.setdefault("entries", {})
        entry = entries.setdefault(agent_name, {})
        skills = entry.setdefault("skills", {})
        if request.disallowed is not None:
            skills["disallowed"] = request.disallowed
        if request.allowed is not None:
            skills["allowed"] = request.allowed

    update_settings(_mutator)
    logger.info("Updated agent skills config", agent_name=agent_name)
    return {"message": "agent 技能配置已更新", "agent_name": agent_name}


@router.put("/skills/{skill_name}/toggle")
async def toggle_skill(skill_name: str, request: SkillToggleRequest):
    """启用/禁用单个技能（全局）：写入/移除 skills.disallowed，并重载技能加载器"""
    from app.skills.loader import skill_loader

    def _mutator(data: Dict[str, Any]) -> None:
        skills = data.setdefault("skills", {})
        disallowed = list(skills.get("disallowed", []) or [])
        if request.enabled:
            disallowed = [p for p in disallowed if p != skill_name]
        else:
            if skill_name not in disallowed:
                disallowed.append(skill_name)
        skills["disallowed"] = disallowed

    update_settings(_mutator)
    skill_loader.reload()
    logger.info("Toggled skill", skill_name=skill_name, enabled=request.enabled)
    return {"message": "已启用" if request.enabled else "已禁用", "skill_name": skill_name, "enabled": request.enabled}


# ===== 记忆配置 =====

def _get_memory_section(data: Dict[str, Any]) -> Dict[str, Any]:
    """从配置数据中获取 memory 段"""
    return data.get("memory", {}) or {}


@router.get("/memory", response_model=MemoryConfigResponse)
async def get_memory_config():
    """获取记忆系统完整配置"""
    data = read_settings()
    mem = _get_memory_section(data)

    # 提取 session_memory 子配置
    sm = mem.get("session_memory", {}) or {}

    return MemoryConfigResponse(
        enabled=bool(mem.get("enabled", True)),
        root=str(mem.get("root", ".happy-claw/memory")),
        mode=str(mem.get("mode", "standard")),
        llm={
            "provider": str(mem.get("llm", {}).get("provider", "aliyun")),
            "model": str(mem.get("llm", {}).get("model", "qwen3.8-max")),
        },
        scan_max_files=int(mem.get("scan_max_files", 200)),
        select_max=int(mem.get("select_max", 5)),
        per_file_max_lines=int(mem.get("per_file_max_lines", 200)),
        per_file_max_bytes=int(mem.get("per_file_max_bytes", 4096)),
        session_inject_budget_bytes=int(mem.get("session_inject_budget_bytes", 61440)),
        index_max_lines=int(mem.get("index_max_lines", 200)),
        index_max_bytes=int(mem.get("index_max_bytes", 25600)),
        freshness_warn_days=int(mem.get("freshness_warn_days", 1)),
        extract_min_interval_seconds=int(mem.get("extract_min_interval_seconds", 300)),
        extract_max_rounds=int(mem.get("extract_max_rounds", 5)),
        extract_min_content_chars=int(mem.get("extract_min_content_chars", 200)),
        extract_min_user_messages=int(mem.get("extract_min_user_messages", 5)),
        consolidate_min_interval_hours=int(mem.get("consolidate_min_interval_hours", 24)),
        consolidate_min_new_conversations=int(mem.get("consolidate_min_new_conversations", 5)),
        consolidate_lock_ttl_seconds=int(mem.get("consolidate_lock_ttl_seconds", 3600)),
        lock_ttl_seconds=int(mem.get("lock_ttl_seconds", 600)),
        consolidate_scan_throttle_seconds=int(mem.get("consolidate_scan_throttle_seconds", 600)),
        prefetch_collect_timeout_seconds=int(mem.get("prefetch_collect_timeout_seconds", 8)),
        session_memory={
            "enabled": bool(sm.get("enabled", True)),
            "first_update_tokens": int(sm.get("first_update_tokens", 10000)),
            "increment_tokens": int(sm.get("increment_tokens", 5000)),
            "tool_calls_threshold": int(sm.get("tool_calls_threshold", 3)),
            "natural_pause_turns": int(sm.get("natural_pause_turns", 2)),
            "summary_min_tokens": int(sm.get("summary_min_tokens", 10000)),
            "summary_max_tokens": int(sm.get("summary_max_tokens", 40000)),
            "summary_min_messages": int(sm.get("summary_min_messages", 5)),
        },
    )


@router.put("/memory")
async def update_memory_config(request: MemoryConfigRequest):
    """更新记忆系统配置"""
    def _mutator(data: Dict[str, Any]) -> None:
        mem = data.setdefault("memory", {})
        # 基础字段
        for field in [
            "enabled", "root", "mode", "scan_max_files", "select_max",
            "per_file_max_lines", "per_file_max_bytes", "session_inject_budget_bytes",
            "index_max_lines", "index_max_bytes", "freshness_warn_days",
            "extract_min_interval_seconds", "extract_max_rounds", "extract_min_content_chars",
            "extract_min_user_messages", "consolidate_min_interval_hours",
            "consolidate_min_new_conversations", "consolidate_lock_ttl_seconds",
            "lock_ttl_seconds", "consolidate_scan_throttle_seconds",
            "prefetch_collect_timeout_seconds",
        ]:
            value = getattr(request, field, None)
            if value is not None:
                mem[field] = value
        # LLM 子配置
        if request.llm is not None:
            mem["llm"] = {
                "provider": request.llm.provider,
                "model": request.llm.model,
            }
        # session_memory 子配置
        if request.session_memory is not None:
            sm = mem.setdefault("session_memory", {})
            for field in [
                "enabled", "first_update_tokens", "increment_tokens", "tool_calls_threshold",
                "natural_pause_turns", "summary_min_tokens", "summary_max_tokens",
                "summary_min_messages",
            ]:
                value = getattr(request.session_memory, field, None)
                if value is not None:
                    sm[field] = value

    update_settings(_mutator)
    logger.info("Updated memory config")
    return {"message": "记忆配置已更新"}
