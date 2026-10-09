"""Pydantic模型 - Agent相关"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    """Agent状态"""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"
    STOPPED = "stopped"


class AgentType(str, Enum):
    """Agent类型"""
    SINGLE = "single"
    MULTI = "multi"
    SUB = "sub"


class LLMProvider(str, Enum):
    """LLM提供商"""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    ALIYUN = "aliyun"
    DEEPSEEK = "deepseek"
    ZHIPU = "智谱"
    LOCAL = "local"


# Agent配置模型
class AgentConfig(BaseModel):
    """Agent配置"""
    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    system_prompt: str = Field(default="You are a helpful AI assistant.")
    agent_type: AgentType = AgentType.SINGLE
    llm_provider: LLMProvider = LLMProvider.OPENAI
    llm_model: str = "deepseek-v4-flash"
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=384000, ge=1, le=1000000)
    context_window: int = Field(default=1000000, ge=1, le=1000000)
    tool_ids: List[str] = Field(default_factory=list)
    skill_ids: List[str] = Field(default_factory=list)
    memory_config: Optional["MemoryConfig"] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # 模型配置（持久化在 settings.json 的 agent.entries.{name}.models/model，不入库）
    models: Optional[List[str]] = None  # 可用模型列表，元素格式 "provider/model_id"
    primary_model: Optional[str] = None  # 默认主模型，格式 "provider/model_id"
    
    # Subagent 相关字段
    is_subagent: bool = False  # 是否是子agent
    level: Optional[int] = -1  # 层级（-1=主agent, 0, 1, 2）
    parent_agent_id: Optional[str] = None  # 父agent ID
    parent_agent_name: Optional[str] = None  # 父agent名称（用于日志上下文）
    parent_conversation_id: Optional[str] = None  # 父agent的会话ID
    spawn_uuid: Optional[str] = None  # 生成UUID


class MemoryConfig(BaseModel):
    """记忆配置"""
    short_term_size: int = Field(default=50, ge=1, le=1000)
    long_term_enabled: bool = True
    working_memory_enabled: bool = True


# Agent实例模型
class AgentInstance(BaseModel):
    """Agent实例"""
    id: str
    config: AgentConfig
    status: AgentStatus = AgentStatus.IDLE
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    current_task: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# 子Agent配置
class SubAgentConfig(BaseModel):
    """子Agent配置"""
    name: str
    role: str
    capabilities: List[str] = Field(default_factory=list)
    parent_agent_id: Optional[str] = None
    config: AgentConfig


# Agent创建/更新请求
class CreateAgentRequest(BaseModel):
    """创建Agent请求"""
    config: AgentConfig


class UpdateAgentRequest(BaseModel):
    """更新Agent请求"""
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    llm_model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tool_ids: Optional[List[str]] = None
    skill_ids: Optional[List[str]] = None
    memory_config: Optional[MemoryConfig] = None
    models: Optional[List[str]] = None  # 可用模型列表，元素格式 "provider/model_id"
    primary_model: Optional[str] = None  # 默认主模型，格式 "provider/model_id"


# Agent响应
class AgentResponse(BaseModel):
    """Agent响应"""
    id: str
    name: Optional[str] = None  # Agent名称
    description: Optional[str] = None  # Agent描述
    config: Optional[AgentConfig] = None  # ← 改为可选，列表接口可不返回完整配置
    status: AgentStatus
    created_at: datetime
    updated_at: datetime
    current_task: Optional[str] = None
    is_enabled: bool = True  # 是否启用
    
    class Config:
        from_attributes = True


class AgentListResponse(BaseModel):
    """Agent列表响应"""
    agents: List[AgentResponse]
    total: int
