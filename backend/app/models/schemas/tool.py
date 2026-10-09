"""Pydantic模型 - 工具相关"""
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable
from enum import Enum
from pydantic import BaseModel, Field


class ToolPermission(str, Enum):
    """工具权限等级"""
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    ADMIN = "admin"


class ToolStatus(str, Enum):
    """工具状态"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"


# 工具定义
class FunctionDefinition(BaseModel):
    """函数定义（用于Function Calling）"""
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema格式


class ToolCall(BaseModel):
    """工具调用请求"""
    id: str
    name: str
    arguments: Dict[str, Any]


class ToolResult(BaseModel):
    """工具调用结果"""
    tool_call_id: str
    name: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    execution_time_ms: Optional[int] = None


# 工具注册
class ToolRegistration(BaseModel):
    """工具注册信息"""
    id: str
    name: str
    description: str
    function_definition: FunctionDefinition
    required_role: ToolPermission = ToolPermission.PUBLIC
    status: ToolStatus = ToolStatus.ACTIVE
    timeout_seconds: int = 30
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# 工具权限
class ToolPermissionConfig(BaseModel):
    """工具权限配置"""
    tool_id: str
    required_role: ToolPermission
    allowed_roles: List[str] = Field(default_factory=list)
    allowed_users: List[str] = Field(default_factory=list)


class UserRole(BaseModel):
    """用户角色"""
    user_id: str
    role: str
    permissions: List[str] = Field(default_factory=list)


# 工具Hooks
class HookContext(BaseModel):
    """Hook上下文"""
    tool_call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class HookResult(BaseModel):
    """Hook执行结果"""
    success: bool
    modified_arguments: Optional[Dict[str, Any]] = None
    should_continue: bool = True
    error: Optional[str] = None


# 工具请求/响应
class CreateToolRequest(BaseModel):
    """创建工具请求"""
    name: str
    description: str
    function_definition: FunctionDefinition
    required_role: ToolPermission = ToolPermission.PUBLIC
    timeout_seconds: int = 30


class UpdateToolRequest(BaseModel):
    """更新工具请求"""
    description: Optional[str] = None
    required_role: Optional[ToolPermission] = None
    status: Optional[ToolStatus] = None
    timeout_seconds: Optional[int] = None


class ToolResponse(BaseModel):
    """工具响应"""
    id: str
    name: str
    description: str
    function_definition: FunctionDefinition
    required_role: ToolPermission
    status: ToolStatus
    timeout_seconds: int
    created_at: datetime
    
    class Config:
        from_attributes = True


class ToolListResponse(BaseModel):
    """工具列表响应"""
    tools: List[ToolResponse]
    total: int
