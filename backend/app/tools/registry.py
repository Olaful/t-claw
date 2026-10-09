"""工具注册表 - 管理所有可用工具"""
import re
import json
import uuid
import time
import contextvars
from datetime import datetime
from typing import Dict, Any, Optional, Callable, List

import structlog
from app.models.schemas.tool import (
    ToolRegistration, FunctionDefinition, ToolCall, ToolResult,
    ToolStatus, ToolPermission as ToolPermissionEnum,
)

logger = structlog.get_logger()

# 当前工具调用的 ID（ContextVar，供 handler 读取，如 bash_command 后台任务需要）
current_tool_call_id: contextvars.ContextVar[str] = contextvars.ContextVar('current_tool_call_id', default='')


class ToolRegistry:
    """工具注册表"""
    
    def __init__(self):
        self._tools: Dict[str, ToolRegistration] = {}
        self._handlers: Dict[str, Callable] = {}
    
    def register(
        self,
        name: str,
        description: str,
        handler: Callable,
        parameters: Dict[str, Any],
        required_role: ToolPermissionEnum = ToolPermissionEnum.PUBLIC,
        timeout_seconds: int = 30,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ToolRegistration:
        """注册工具"""
        tool_id = str(uuid.uuid4())
        
        function_def = FunctionDefinition(
            name=name,
            description=description,
            parameters=parameters,
        )
        
        registration = ToolRegistration(
            id=tool_id,
            name=name,
            description=description,
            function_definition=function_def,
            required_role=required_role,
            status=ToolStatus.ACTIVE,
            timeout_seconds=timeout_seconds,
            metadata=metadata or {},
        )
        
        self._tools[name] = registration
        self._handlers[name] = handler
        
        return registration
    
    def unregister(self, name: str) -> bool:
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            del self._handlers[name]
            logger.info("Tool unregistered", name=name)
            return True
        return False
    
    def get(self, name: str) -> Optional[ToolRegistration]:
        """获取工具注册信息"""
        return self._tools.get(name)
    
    def get_handler(self, name: str) -> Optional[Callable]:
        """获取工具处理函数"""
        return self._handlers.get(name)
    
    def list_tools(
        self,
        status: Optional[ToolStatus] = None,
        required_role: Optional[ToolPermissionEnum] = None,
    ) -> List[ToolRegistration]:
        """列出工具"""
        tools = list(self._tools.values())
        
        if status:
            tools = [t for t in tools if t.status == status]
        if required_role:
            tools = [t for t in tools if t.required_role == required_role]
        
        return tools
    
    async def execute(
        self,
        tool_call: ToolCall,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> ToolResult:
        """执行工具调用"""
        start_time = time.time()
        
        # 获取工具
        registration = self._tools.get(tool_call.name)
        if not registration:
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                success=False,
                error=f"Tool not found: {tool_call.name}",
            )
        
        # 检查状态
        if registration.status != ToolStatus.ACTIVE:
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                success=False,
                error=f"Tool is not active: {tool_call.name}",
            )
        
        # ✅ 执行前置钩子（包括权限检查、安全检查等）
        from app.tools.hooks import hook_manager
        from app.models.schemas.tool import HookContext
        
        hook_context = HookContext(
            tool_name=tool_call.name,
            tool_call_id=tool_call.id,
            user_id=user_id,
            agent_id=agent_id,
            arguments=tool_call.arguments,
        )
        
        try:
            pre_hook_result = await hook_manager.execute_pre_hooks(
                tool_call.name,
                hook_context,
            )
            
            # 如果 prehook 返回错误，直接返回结果，不执行工具
            if not pre_hook_result.should_continue or not pre_hook_result.success:
                execution_time = int((time.time() - start_time) * 1000)
                logger.warning(
                    "Tool execution blocked by pre-hook",
                    tool_name=tool_call.name,
                    tool_call_id=tool_call.id,
                    error=pre_hook_result.error,
                )
                return ToolResult(
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                    success=False,
                    error=pre_hook_result.error or "Tool execution blocked by pre-hook",
                    execution_time_ms=execution_time,
                )
            
            # 如果 prehook 修改了参数，使用修改后的参数
            validated_args = pre_hook_result.modified_arguments or tool_call.arguments
        except Exception as e:
            logger.error("Pre-hook execution failed", tool_name=tool_call.name, error=str(e), exc_info=True)
            # prehook 失败时，继续执行工具（根据策略可调整）
            validated_args = tool_call.arguments
        
        # 获取处理函数
        handler = self._handlers.get(tool_call.name)
        if not handler:
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                success=False,
                error=f"Tool handler not found: {tool_call.name}",
            )
        
        # 执行工具
        try:
            # 验证参数
            validated_args = self._validate_arguments(
                validated_args,
                registration.function_definition.parameters,
            )
            
            # MCP 工具超时监控
            is_mcp_tool = registration.metadata.get("mcp_tool", False)
            mcp_timeout = 0
            if is_mcp_tool:
                try:
                    from app.config import settings
                    mcp_timeout = settings.MCP_TOOL_TO_BACKGROUND_TIMEOUT
                except Exception:
                    mcp_timeout = 0
            
            # 设置当前工具调用 ID（ContextVar，供 handler 读取）
            token = current_tool_call_id.set(tool_call.id)
            
            # 调用处理函数
            import asyncio
            if asyncio.iscoroutinefunction(handler):
                if is_mcp_tool and mcp_timeout > 0:
                    # MCP 工具带超时监控
                    task = asyncio.create_task(handler(**validated_args))
                    try:
                        result = await asyncio.wait_for(task, timeout=mcp_timeout)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "MCP tool exceeded timeout, consider running in background",
                            tool_name=tool_call.name,
                            timeout=mcp_timeout,
                        )
                        # 任务继续在后台运行，返回提示信息
                        result = {
                            "success": False,
                            "error": f"MCP tool '{tool_call.name}' exceeded {mcp_timeout}s timeout. Task continues in background.",
                            "mcp_timeout_exceeded": True,
                        }
                else:
                    result = await handler(**validated_args)
            else:
                result = handler(**validated_args)
            
            # 重置 ContextVar
            current_tool_call_id.reset(token)
            
            execution_time = int((time.time() - start_time) * 1000)
            
            # ✅ 执行后置钩子
            try:
                result = await hook_manager.execute_post_hooks(
                    tool_call.name,
                    hook_context,
                    result,
                )
            except Exception as e:
                logger.error("Post-hook execution failed", tool_name=tool_call.name, error=str(e), exc_info=True)
                # posthook 失败不影响工具执行结果
            
            logger.info(
                "Tool executed successfully",
                tool_name=tool_call.name,
                execution_time_ms=execution_time,
            )
            
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                success=True,
                result=result,
                execution_time_ms=execution_time,
            )
        
        except Exception as e:
            # 重置 ContextVar
            try:
                current_tool_call_id.reset(token)
            except Exception:
                pass
            execution_time = int((time.time() - start_time) * 1000)
            logger.error(
                "Tool execution failed",
                tool_name=tool_call.name,
                error=str(e),
                execution_time_ms=execution_time,
            )
            
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                success=False,
                error=str(e),
                execution_time_ms=execution_time,
            )
    
    def _validate_arguments(
        self,
        arguments: Dict[str, Any],
        schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        """验证工具参数"""
        # 简化的参数验证
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        
        # 检查必填参数
        for req in required:
            if req not in arguments:
                raise ValueError(f"Missing required argument: {req}")
        
        # 类型检查（简化版）
        validated = {}
        for key, value in arguments.items():
            if key in properties:
                prop_schema = properties[key]
                expected_type = prop_schema.get("type")
                
                # ✅ 兼容 LLM 有时将 array/object 参数以 JSON 字符串形式返回
                # （如 "[\"最新新闻\", \"今日热点\"]"），尝试解析为真实类型
                if expected_type in ("array", "object") and isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (json.JSONDecodeError, ValueError):
                        pass  # 解析失败则保留原值，交由下方类型检查报错
                
                if expected_type == "string" and not isinstance(value, str):
                    raise ValueError(f"Argument {key} must be a string")
                elif expected_type == "integer" and not isinstance(value, int):
                    raise ValueError(f"Argument {key} must be an integer")
                elif expected_type == "number" and not isinstance(value, (int, float)):
                    raise ValueError(f"Argument {key} must be a number")
                elif expected_type == "boolean" and not isinstance(value, bool):
                    raise ValueError(f"Argument {key} must be a boolean")
                elif expected_type == "array" and not isinstance(value, list):
                    raise ValueError(f"Argument {key} must be an array")
                elif expected_type == "object" and not isinstance(value, dict):
                    raise ValueError(f"Argument {key} must be an object")
            
            validated[key] = value
        
        return validated
    
    def get_function_definitions(self) -> List[Dict[str, Any]]:
        """获取所有工具的Function Calling定义"""
        definitions = []
        for tool in self._tools.values():
            if tool.status == ToolStatus.ACTIVE:
                definitions.append({
                    "type": "function",
                    "function": {
                        "name": tool.function_definition.name,
                        "description": tool.function_definition.description,
                        "parameters": tool.function_definition.parameters,
                    },
                })
        return definitions
    
    def get_tool_names(self) -> List[str]:
        """获取所有活跃工具名列表"""
        return [t.name for t in self._tools.values() if t.status == ToolStatus.ACTIVE]

    def is_system_tool(self, name: str) -> bool:
        """检查是否是系统工具（不显示在前端的内部工具）"""
        tool = self._tools.get(name)
        if tool:
            return tool.metadata.get("system_tool", False)
        return False
    
    def update_status(self, name: str, status: ToolStatus) -> bool:
        """更新工具状态"""
        if name in self._tools:
            self._tools[name].status = status
            logger.info("Tool status updated", name=name, status=status)
            return True
        return False

    def apply_timeout_config(self, timeout_map: Dict[str, Any]) -> int:
        """按 tools.timeout_seconds 配置更新各注册项的 timeout_seconds。

        Args:
            timeout_map: 形如 {"default": 30, "web_search": 10, ...}，
                         "default" 作为兜底，具名 key 覆盖优先。

        Returns:
            实际更新的工具数量。
        """
        if not isinstance(timeout_map, dict):
            return 0
        default_timeout = timeout_map.get("default")
        updated = 0
        for name, reg in self._tools.items():
            # 具名覆盖优先，其次 default
            if name in timeout_map and isinstance(timeout_map[name], (int, float)):
                reg.timeout_seconds = int(timeout_map[name])
                updated += 1
            elif isinstance(default_timeout, (int, float)):
                reg.timeout_seconds = int(default_timeout)
                updated += 1
        if updated:
            logger.info("Applied tool timeout config", updated=updated)
        return updated


# 全局工具注册表实例
tool_registry = ToolRegistry()


def _match_tool_pattern(pattern: str, tool_name: str) -> bool:
    """判断工具名是否匹配给定模式（与 FilteredToolRegistry 一致的规则）。

    - `*` 匹配所有工具
    - `mcp__<server>` 匹配该服务器所有工具（精确等于或 `mcp__<server>__` 前缀）
    - 其他模式作为正则全匹配，正则非法时退化为精确相等
    """
    if pattern == "*":
        return True
    if pattern.startswith("mcp__"):
        if tool_name == pattern:
            return True
        if tool_name.startswith(f"{pattern}__"):
            return True
        try:
            if re.fullmatch(pattern, tool_name):
                return True
        except re.error:
            pass
        return False
    try:
        return bool(re.fullmatch(pattern, tool_name))
    except re.error:
        return tool_name == pattern


def get_global_disallowed() -> List[str]:
    """读取全局 tools.disallowed 配置列表（所有 agent 都不能使用）。"""
    try:
        from app.config import settings
        tools_cfg = settings._json_config.get("tools", {})
        if hasattr(tools_cfg, "get"):
            disallowed = tools_cfg.get("disallowed", [])
        else:
            disallowed = []
        return list(disallowed) if disallowed else []
    except Exception:
        return []


def is_tool_globally_disabled(tool_name: str, patterns: Optional[List[str]] = None) -> bool:
    """判断工具是否被全局 tools.disallowed 命中（禁用）。"""
    if patterns is None:
        patterns = get_global_disallowed()
    for pattern in patterns:
        if _match_tool_pattern(pattern, tool_name):
            return True
    return False


def reload_tool_runtime() -> Dict[str, Any]:
    """配置变更后刷新运行时注册表。

    - 超时：将 tools.timeout_seconds 应用到各注册项；
    - 禁用：由 create_filtered_tool_registry 在创建时动态合并全局 disallowed，
            无需在此改动注册表（reload 后新会话即时生效）。
    """
    try:
        from app.config import settings
        tools_cfg = settings._json_config.get("tools", {})
        timeout_map = tools_cfg.get("timeout_seconds", {}) if hasattr(tools_cfg, "get") else {}
        if hasattr(timeout_map, "to_dict"):
            timeout_map = timeout_map.to_dict()
        updated = tool_registry.apply_timeout_config(dict(timeout_map or {}))
        return {"timeout_updated": updated}
    except Exception as e:
        logger.warning("Failed to reload tool runtime", error=str(e))
        return {"timeout_updated": 0, "error": str(e)}


class FilteredToolRegistry:
    """
    分层过滤的工具注册表包装器。

    基于全局 tool_registry，按 allowed/disallowed 规则过滤可用工具。
    - disallowed 优先级高于 allowed
    - 支持正则匹配（如 `file$`、`*`）
    - MCP 工具以服务器维度排除：`mcp__demo` 排除 demo 服务器所有工具
    """

    def __init__(
        self,
        base: ToolRegistry,
        allowed: List[str] = None,
        disallowed: List[str] = None,
    ):
        self._base = base
        self._allowed = allowed or []
        self._disallowed = disallowed or []
        # 预编译正则（缓存）
        self._allowed_compiled = [self._compile_pattern(p) for p in self._allowed]
        self._disallowed_compiled = [self._compile_pattern(p) for p in self._disallowed]

    @staticmethod
    def _compile_pattern(pattern: str) -> str:
        """返回原始 pattern（匹配时按需编译）"""
        return pattern

    def _match_tool(self, pattern: str, tool_name: str) -> bool:
        """
        判断工具是否匹配给定模式。

        匹配规则:
        - `*` 匹配所有工具
        - `mcp__<server>` 匹配该服务器所有工具（精确等于或 `mcp__<server>__` 前缀）
        - 其他模式作为正则表达式匹配
        """
        if pattern == "*":
            return True
        # MCP 服务器维度匹配: mcp__<server> 匹配该服务器下所有工具
        if pattern.startswith("mcp__"):
            server_part = pattern  # e.g. "mcp__demo"
            if tool_name == server_part:
                return True
            if tool_name.startswith(f"{server_part}__"):
                return True
            # 也作为正则尝试匹配
            try:
                if re.fullmatch(pattern, tool_name):
                    return True
            except re.error:
                pass
            return False
        # 正则匹配
        try:
            return bool(re.fullmatch(pattern, tool_name))
        except re.error:
            return tool_name == pattern

    def _is_allowed(self, tool_name: str) -> bool:
        """判断工具是否通过过滤（disallowed 优先于 allowed）"""
        # 1. 检查 disallowed（优先级高）
        for pattern in self._disallowed:
            if self._match_tool(pattern, tool_name):
                return False
        # 2. 检查 allowed：空列表表示全部允许
        if not self._allowed:
            return True
        for pattern in self._allowed:
            if self._match_tool(pattern, tool_name):
                return True
        return False

    # ---- 代理方法（带过滤） ----

    def get(self, name: str) -> Optional[ToolRegistration]:
        if not self._is_allowed(name):
            return None
        return self._base.get(name)

    def get_handler(self, name: str) -> Optional[Callable]:
        if not self._is_allowed(name):
            return None
        return self._base.get_handler(name)

    def list_tools(
        self,
        status: Optional[ToolStatus] = None,
        required_role: Optional[ToolPermissionEnum] = None,
    ) -> List[ToolRegistration]:
        tools = self._base.list_tools(status=status, required_role=required_role)
        return [t for t in tools if self._is_allowed(t.name)]

    def get_function_definitions(self) -> List[Dict[str, Any]]:
        definitions = self._base.get_function_definitions()
        return [d for d in definitions if self._is_allowed(d["function"]["name"])]

    def is_system_tool(self, name: str) -> bool:
        return self._base.is_system_tool(name)

    def get_tool_names(self) -> List[str]:
        """获取过滤后的活跃工具名列表"""
        return [n for n in self._base.get_tool_names() if self._is_allowed(n)]

    async def execute(self, tool_call: ToolCall, **kwargs) -> ToolResult:
        """执行工具调用（带过滤检查）"""
        if not self._is_allowed(tool_call.name):
            return ToolResult(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                success=False,
                error=f"Tool not allowed: {tool_call.name}",
            )
        return await self._base.execute(tool_call, **kwargs)


def create_filtered_tool_registry(
    agent_name: Optional[str] = None,
    subagent_level: Optional[int] = None,
    team_mode: bool = False,
) -> FilteredToolRegistry:
    """
    根据配置创建过滤后的工具注册表。

    配置路径:
    - 主agent:     agent.entries.{agent_name}.tools.disallowed/allowed
    - 子agent:     agent.subagent.disallowed_tools_per_level/allowed_tools_per_level.{level}
    - 团队成员:    teams.tools.disallowed/allowed

    Args:
        agent_name: 主agent名称（用于查找 agent.entries 配置）
        subagent_level: 子agent层级（0, 1, 2...）
        team_mode: 是否为团队成员模式

    Returns:
        FilteredToolRegistry 实例
    """
    from app.config import settings

    allowed: List[str] = []
    disallowed: List[str] = []

    if subagent_level is not None:
        # 子agent：按层级配置
        subagent_cfg = settings._json_config.get("agent", {})
        subagent_cfg = subagent_cfg.get("subagent", {}) if hasattr(subagent_cfg, 'get') else {}
        level_key = str(subagent_level)

        # disallowed_tools_per_level
        disallowed_per_level = subagent_cfg.get("disallowed_tools_per_level", {})
        if hasattr(disallowed_per_level, 'get'):
            disallowed = disallowed_per_level.get(level_key, [])
            if hasattr(disallowed, 'to_dict'):
                disallowed = []
            elif not isinstance(disallowed, list):
                disallowed = list(disallowed) if disallowed else []

        # allowed_tools_per_level
        allowed_per_level = subagent_cfg.get("allowed_tools_per_level", {})
        if hasattr(allowed_per_level, 'get'):
            allowed = allowed_per_level.get(level_key, [])
            if hasattr(allowed, 'to_dict'):
                allowed = []
            elif not isinstance(allowed, list):
                allowed = list(allowed) if allowed else []

        logger.info(
            "Subagent tool filter",
            level=level_key,
            allowed=allowed,
            disallowed=disallowed,
        )

    elif team_mode:
        # 团队成员：teams.tools 配置
        teams_cfg = settings._json_config.get("teams", {})
        tools_cfg = teams_cfg.get("tools", {}) if hasattr(teams_cfg, 'get') else {}
        disallowed = list(tools_cfg.get("disallowed", []))
        allowed = list(tools_cfg.get("allowed", []))
        logger.info("Team tool filter", allowed=allowed, disallowed=disallowed)

    elif agent_name is not None:
        # 主agent：agent.entries.{agent_name}.tools 配置
        agent_cfg = settings._json_config.get("agent", {})
        entries = agent_cfg.get("entries", {}) if hasattr(agent_cfg, 'get') else {}
        entry = entries.get(agent_name, {}) if hasattr(entries, 'get') else {}
        tools_cfg = entry.get("tools", {}) if hasattr(entry, 'get') else {}
        disallowed = list(tools_cfg.get("disallowed", []))
        allowed = list(tools_cfg.get("allowed", []))
        logger.info(
            "Agent tool filter",
            agent_name=agent_name,
            allowed=allowed,
            disallowed=disallowed,
        )

    # ✅ image_generate 工具条件注册：仅当 agent 配置了 mediaModels 时可用
    if agent_name is not None:
        try:
            _agent_cfg = settings._json_config.get("agent", {})
            _entries = _agent_cfg.get("entries", {}) if hasattr(_agent_cfg, 'get') else {}
            _entry = _entries.get(agent_name, {}) if hasattr(_entries, 'get') else {}
            _media_models = _entry.get("mediaModels") if hasattr(_entry, 'get') else None
            _image_models = _media_models.get("image") if hasattr(_media_models, 'get') else None
            if not _image_models:
                if "image_generate" not in disallowed:
                    disallowed.append("image_generate")
        except Exception as _e:
            logger.debug("Failed to check mediaModels for tool filter", error=str(_e))
            if "image_generate" not in disallowed:
                disallowed.append("image_generate")
    else:
        # 子agent / 团队成员模式：默认不允许 image_generate
        if "image_generate" not in disallowed:
            disallowed.append("image_generate")

    # ✅ music_generate 工具条件注册：仅当 agent 配置了 mediaModels.music 时可用
    if agent_name is not None:
        try:
            _agent_cfg = settings._json_config.get("agent", {})
            _entries = _agent_cfg.get("entries", {}) if hasattr(_agent_cfg, 'get') else {}
            _entry = _entries.get(agent_name, {}) if hasattr(_entries, 'get') else {}
            _media_models = _entry.get("mediaModels") if hasattr(_entry, 'get') else None
            _music_models = _media_models.get("music") if hasattr(_media_models, 'get') else None
            if not _music_models:
                if "music_generate" not in disallowed:
                    disallowed.append("music_generate")
        except Exception as _e:
            logger.debug("Failed to check mediaModels.music for tool filter", error=str(_e))
            if "music_generate" not in disallowed:
                disallowed.append("music_generate")
    else:
        # 子agent / 团队成员模式：默认不允许 music_generate
        if "music_generate" not in disallowed:
            disallowed.append("music_generate")

    # ✅ video_generate 工具条件注册：仅当 agent 配置了 mediaModels.video 时可用
    if agent_name is not None:
        try:
            _agent_cfg = settings._json_config.get("agent", {})
            _entries = _agent_cfg.get("entries", {}) if hasattr(_agent_cfg, 'get') else {}
            _entry = _entries.get(agent_name, {}) if hasattr(_entries, 'get') else {}
            _media_models = _entry.get("mediaModels") if hasattr(_entry, 'get') else None
            _video_models = _media_models.get("video") if hasattr(_media_models, 'get') else None
            if not _video_models:
                if "video_generate" not in disallowed:
                    disallowed.append("video_generate")
        except Exception as _e:
            logger.debug("Failed to check mediaModels.video for tool filter", error=str(_e))
            if "video_generate" not in disallowed:
                disallowed.append("video_generate")
    else:
        # 子agent / 团队成员模式：默认不允许 video_generate
        if "video_generate" not in disallowed:
            disallowed.append("video_generate")

    # ✅ pdf_parse 工具条件注册：仅当 agent 配置了 pdf 时可用
    if agent_name is not None:
        try:
            _agent_cfg = settings._json_config.get("agent", {})
            _entries = _agent_cfg.get("entries", {}) if hasattr(_agent_cfg, 'get') else {}
            _entry = _entries.get(agent_name, {}) if hasattr(_entries, 'get') else {}
            _pdf_cfg = _entry.get("pdf") if hasattr(_entry, 'get') else None
            if not _pdf_cfg:
                if "pdf_parse" not in disallowed:
                    disallowed.append("pdf_parse")
        except Exception as _e:
            logger.debug("Failed to check pdf config for tool filter", error=str(_e))
            if "pdf_parse" not in disallowed:
                disallowed.append("pdf_parse")
    else:
        # 子agent / 团队成员模式：默认不允许 pdf_parse
        if "pdf_parse" not in disallowed:
            disallowed.append("pdf_parse")

    # 合并全局 tools.disallowed（全局禁用的工具，所有 agent/subagent/team 都不能使用）
    global_disallowed = get_global_disallowed()
    if global_disallowed:
        merged = list(disallowed)
        for pattern in global_disallowed:
            if pattern not in merged:
                merged.append(pattern)
        disallowed = merged
        logger.info("Merged global tools.disallowed", global_disallowed=global_disallowed)

    return FilteredToolRegistry(
        base=tool_registry,
        allowed=allowed,
        disallowed=disallowed,
    )


# 装饰器：注册工具
def tool(
    name: Optional[str] = None,
    description: str = "",
    parameters: Optional[Dict[str, Any]] = None,
    required_role: ToolPermissionEnum = ToolPermissionEnum.PUBLIC,
    timeout_seconds: int = 30,
):
    """工具注册装饰器"""
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        tool_params = parameters or {
            "type": "object",
            "properties": {},
            "required": [],
        }
        
        tool_registry.register(
            name=tool_name,
            description=description or func.__doc__ or "",
            handler=func,
            parameters=tool_params,
            required_role=required_role,
            timeout_seconds=timeout_seconds,
        )
        
        return func
    return decorator
