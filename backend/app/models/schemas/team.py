"""Pydantic模型 - 团队协作相关"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field


class TeamStatus(str, Enum):
    """团队状态"""
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class MemberStatus(str, Enum):
    """成员状态"""
    IDLE = "idle"
    WORKING = "working"
    ERROR = "error"
    OFFLINE = "offline"


class MessageType(str, Enum):
    """团队消息类型"""
    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"
    HEARTBEAT = "heartbeat"
    BROADCAST = "broadcast"


class TaskPriority(str, Enum):
    """任务优先级"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TaskStatus(str, Enum):
    """任务状态"""
    PENDING = "pending"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 团队配置
class TeamConfig(BaseModel):
    """团队配置"""
    name: str
    description: Optional[str] = None
    max_members: int = Field(default=10, ge=1, le=100)
    communication_protocol: str = "redis_pubsub"
    workspace_isolation: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TeamMember(BaseModel):
    """团队成员"""
    agent_id: str
    role: str
    status: MemberStatus = MemberStatus.IDLE
    joined_at: datetime = Field(default_factory=datetime.utcnow)
    capabilities: List[str] = Field(default_factory=list)
    current_task: Optional[str] = None


class Team(BaseModel):
    """团队"""
    id: str
    config: TeamConfig
    status: TeamStatus = TeamStatus.ACTIVE
    members: List[TeamMember] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# 团队通讯
class TeamMessage(BaseModel):
    """团队消息"""
    id: str
    team_id: str
    from_agent_id: str
    to_agent_id: Optional[str] = None  # None表示广播
    message_type: MessageType
    content: Dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# 白板任务
class TaskItem(BaseModel):
    """白板任务项"""
    id: str
    team_id: str
    title: str
    description: Optional[str] = None
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING
    assigned_to: Optional[str] = None  # Agent ID
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    due_date: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ClaimStatus(BaseModel):
    """任务认领状态"""
    task_id: str
    agent_id: str
    claimed_at: datetime
    expires_at: Optional[datetime] = None


# 工作区配置
class WorkspaceConfig(BaseModel):
    """工作区配置"""
    agent_id: str
    base_path: str
    allowed_paths: List[str] = Field(default_factory=list)
    environment: Dict[str, str] = Field(default_factory=dict)
    resource_limits: Dict[str, Any] = Field(default_factory=dict)


class FileAccessLog(BaseModel):
    """文件访问日志"""
    agent_id: str
    file_path: str
    action: str  # read, write, delete
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    success: bool = True


# 请求/响应
class CreateTeamRequest(BaseModel):
    """创建团队请求"""
    config: TeamConfig
    initial_members: List[str] = Field(default_factory=list)  # Agent IDs


class AddMemberRequest(BaseModel):
    """添加成员请求"""
    agent_id: str
    role: str


class CreateTaskRequest(BaseModel):
    """创建任务请求"""
    title: str
    description: Optional[str] = None
    priority: TaskPriority = TaskPriority.MEDIUM
    assigned_to: Optional[str] = None
    due_date: Optional[datetime] = None


class ClaimTaskRequest(BaseModel):
    """认领任务请求"""
    agent_id: str


# 响应模型
class TeamResponse(BaseModel):
    """团队响应"""
    id: str
    config: TeamConfig
    status: TeamStatus
    member_count: int
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class TeamListResponse(BaseModel):
    """团队列表响应"""
    teams: List[TeamResponse]
    total: int


class TaskItemResponse(BaseModel):
    """任务项响应"""
    id: str
    team_id: str
    title: str
    description: Optional[str]
    priority: TaskPriority
    status: TaskStatus
    assigned_to: Optional[str]
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class TaskListResponse(BaseModel):
    """任务列表响应"""
    tasks: List[TaskItemResponse]
    total: int
