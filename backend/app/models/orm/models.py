"""SQLAlchemy ORM模型 - 数据库表定义"""
from datetime import datetime
from sqlalchemy import (
    Column, String, Text, Integer, Float, Boolean, DateTime,
    ForeignKey, JSON, Enum as SAEnum, Index
)
from sqlalchemy.orm import relationship
import enum

from app.utils.database import Base


# ===== 枚举类型 =====

class AgentStatusEnum(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"
    STOPPED = "stopped"


class ConversationStatusEnum(str, enum.Enum):
    NEW = "new"
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ToolStatusEnum(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"


class TaskStatusEnum(str, enum.Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatusEnum(str, enum.Enum):
    """工具调用状态"""
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"


# ===== Agent模型 =====

class AgentORM(Base):
    """Agent数据库模型"""
    __tablename__ = "agents"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, index=True)
    description = Column(Text)
    system_prompt = Column(Text, nullable=False, default="You are a helpful AI assistant.")
    agent_type = Column(String(20), nullable=False, default="single")
    llm_provider = Column(String(20), nullable=False, default="openai")
    llm_model = Column(String(50), nullable=False, default="gpt-4-turbo-preview")
    temperature = Column(Float, nullable=False, default=0.7)
    context_window = Column(Integer, nullable=False, default=4096)
    max_tokens = Column(Integer, nullable=False, default=4096)
    status = Column(SAEnum(AgentStatusEnum), nullable=False, default=AgentStatusEnum.IDLE)
    tool_ids = Column(JSON, default=list)
    skill_ids = Column(JSON, default=list)
    memory_config = Column(JSON)
    metadata_ = Column("metadata", JSON, default=dict)
    current_task = Column(String(36))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Subagent 相关字段
    is_subagent = Column(Boolean, nullable=False, default=False)
    level = Column(Integer)  # 层级（0, 1, 2）
    parent_agent_id = Column(String(36), ForeignKey("agents.id"), index=True)  # 父agent ID
    parent_conversation_id = Column(String(36), index=True)  # 父agent的会话ID
    spawn_uuid = Column(String(100), index=True)  # 生成UUID
    
    # 启用/禁用状态
    is_enabled = Column(Boolean, nullable=False, default=True, index=True)
    
    # 关系
    conversations = relationship("ConversationORM", back_populates="agent", cascade="all, delete-orphan")
    parent = relationship("AgentORM", remote_side=[id], foreign_keys=[parent_agent_id], back_populates="children")
    children = relationship("AgentORM", foreign_keys=[parent_agent_id], back_populates="parent")
    
    __table_args__ = (
        Index("idx_agent_status", "status"),
        Index("idx_agent_parent", "parent_agent_id"),
        Index("idx_agent_spawn_uuid", "spawn_uuid"),
    )


# ===== 对话模型 =====

class ConversationORM(Base):
    """对话数据库模型"""
    __tablename__ = "conversations"
    
    id = Column(String(36), primary_key=True)
    agent_id = Column(String(36), ForeignKey("agents.id"), nullable=False, index=True)
    user_id = Column(String(36), index=True)
    title = Column(String(200))
    status = Column(SAEnum(ConversationStatusEnum), nullable=False, default=ConversationStatusEnum.ACTIVE)
    context_tokens = Column(Integer, nullable=False, default=0)
    metadata_ = Column("metadata", JSON, default=dict)
    # 会话级模型配置（覆盖agent默认配置）
    session_model = Column(JSON)  # {provider: str, model_id: str, reasoning_level: str, speed_mode: str}
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    agent = relationship("AgentORM", back_populates="conversations")
    messages = relationship("MessageORM", back_populates="conversation", cascade="all, delete-orphan")


class MessageORM(Base):
    """消息数据库模型"""
    __tablename__ = "messages"
    
    id = Column(String(36), primary_key=True)
    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    message_type = Column(String(50), nullable=False, default="text")
    reasoning_content = Column(Text)  # DeepSeek思考模式的推理内容
    tool_calls = Column(JSON)
    tool_call_id = Column(String(36))
    name = Column(String(100))
    metadata_ = Column("metadata", JSON, default=dict)
    token_count = Column(Integer)
    tool_call_status = Column(SAEnum(ToolCallStatusEnum), nullable=True, default=None)  # 工具调用状态
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    # 关系
    conversation = relationship("ConversationORM", back_populates="messages")
    
    __table_args__ = (
        Index("idx_message_conversation", "conversation_id", "created_at"),
    )


# ===== 工具模型 =====

class ToolORM(Base):
    """工具数据库模型"""
    __tablename__ = "tools"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=False)
    function_definition = Column(JSON, nullable=False)
    required_role = Column(String(20), nullable=False, default="public")
    status = Column(SAEnum(ToolStatusEnum), nullable=False, default=ToolStatusEnum.ACTIVE)
    timeout_seconds = Column(Integer, nullable=False, default=30)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# ===== 团队模型 =====

class TeamORM(Base):
    """团队数据库模型"""
    __tablename__ = "teams"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, index=True)
    description = Column(Text)
    max_members = Column(Integer, nullable=False, default=10)
    status = Column(String(20), nullable=False, default="active")
    communication_protocol = Column(String(50), default="redis_pubsub")
    workspace_isolation = Column(Boolean, default=True)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    members = relationship("TeamMemberORM", back_populates="team", cascade="all, delete-orphan")
    tasks = relationship("TeamTaskORM", back_populates="team", cascade="all, delete-orphan")


class TeamMemberORM(Base):
    """团队成员数据库模型"""
    __tablename__ = "team_members"
    
    id = Column(String(36), primary_key=True)
    team_id = Column(String(36), ForeignKey("teams.id"), nullable=False, index=True)
    agent_id = Column(String(36), nullable=False, index=True)
    role = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="idle")
    capabilities = Column(JSON, default=list)
    current_task = Column(String(36))
    joined_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    # 关系
    team = relationship("TeamORM", back_populates="members")


class TeamTaskORM(Base):
    """团队任务数据库模型（白板任务）"""
    __tablename__ = "team_tasks"
    
    id = Column(String(36), primary_key=True)
    team_id = Column(String(36), ForeignKey("teams.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    priority = Column(String(20), nullable=False, default="medium")
    status = Column(SAEnum(TaskStatusEnum), nullable=False, default=TaskStatusEnum.PENDING)
    assigned_to = Column(String(36), index=True)
    created_by = Column(String(36))
    due_date = Column(DateTime)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # 关系
    team = relationship("TeamORM", back_populates="tasks")


# ===== 记忆模型 =====

class MemoryORM(Base):
    """记忆数据库模型"""
    __tablename__ = "memories"
    
    id = Column(String(36), primary_key=True)
    agent_id = Column(String(36), nullable=False, index=True)
    memory_type = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(JSON)  # 向量嵌入存储
    metadata_ = Column("metadata", JSON, default=dict)
    importance = Column(Float, nullable=False, default=0.5)
    access_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    accessed_at = Column(DateTime)
    
    __table_args__ = (
        Index("idx_memory_agent_type", "agent_id", "memory_type"),
    )


# ===== 用户模型 =====

class UserORM(Base):
    """用户数据库模型"""
    __tablename__ = "users"
    
    id = Column(String(36), primary_key=True)
    username = Column(String(50), nullable=False, unique=True, index=True)
    email = Column(String(100), nullable=False, unique=True, index=True)
    hashed_password = Column(String(200), nullable=False)
    role = Column(String(20), nullable=False, default="user")
    is_active = Column(Boolean, nullable=False, default=True)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


# ===== 提示词模板模型 =====

class PromptTemplateORM(Base):
    """提示词模板数据库模型"""
    __tablename__ = "prompt_templates"
    
    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    template = Column(Text, nullable=False)
    variables = Column(JSON, default=list)
    version = Column(Integer, nullable=False, default=1)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
