"""Pydantic模型 - 任务与记忆相关"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field


# ===== 任务规划相关 =====

class PlanStatus(str, Enum):
    """计划状态"""
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubTaskStatus(str, Enum):
    """子任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Plan(BaseModel):
    """任务计划"""
    id: str
    goal: str
    strategy: str  # react, plan_and_solve, tree_of_thoughts
    subtasks: List["SubTask"] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SubTask(BaseModel):
    """子任务"""
    id: str
    plan_id: str
    title: str
    description: str
    dependencies: List[str] = Field(default_factory=list)  # 依赖的子任务ID
    status: SubTaskStatus = SubTaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TaskDependency(BaseModel):
    """任务依赖"""
    task_id: str
    depends_on: str
    dependency_type: str = "finish_to_start"  # finish_to_start, start_to_start


# ===== 反思与错误恢复 =====

class ErrorReport(BaseModel):
    """错误报告"""
    id: str
    agent_id: str
    task_id: Optional[str] = None
    error_type: str
    error_message: str
    stack_trace: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class RecoveryPlan(BaseModel):
    """恢复计划"""
    id: str
    error_report_id: str
    strategy: str  # retry, fallback, escalate, abort
    actions: List[Dict[str, Any]] = Field(default_factory=list)
    max_retries: int = 3
    current_retry: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ===== 记忆管理 =====

class MemoryType(str, Enum):
    """记忆类型"""
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class MemoryEntry(BaseModel):
    """记忆条目"""
    id: str
    agent_id: str
    memory_type: MemoryType
    content: str
    embedding: Optional[List[float]] = None  # 向量嵌入
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    accessed_at: Optional[datetime] = None
    access_count: int = 0
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class MemoryQuery(BaseModel):
    """记忆查询"""
    agent_id: str
    query: Optional[str] = None
    memory_type: Optional[MemoryType] = None
    top_k: int = Field(default=10, ge=1, le=100)
    min_similarity: float = Field(default=0.7, ge=0.0, le=1.0)
    filters: Dict[str, Any] = Field(default_factory=dict)


class MemoryStore(BaseModel):
    """记忆存储"""
    agent_id: str
    entries: List[MemoryEntry] = Field(default_factory=list)
    total_count: int = 0


# ===== Skills相关 =====

class SkillManifest(BaseModel):
    """技能清单"""
    id: str
    name: str
    description: str
    version: str
    entry_point: str
    dependencies: List[str] = Field(default_factory=list)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SkillContext(BaseModel):
    """技能执行上下文"""
    skill_id: str
    agent_id: str
    input_data: Dict[str, Any]
    environment: Dict[str, Any] = Field(default_factory=dict)


# ===== MCP插件相关 =====

class MCPTool(BaseModel):
    """MCP工具"""
    id: str
    name: str
    description: str
    input_schema: Dict[str, Any]
    endpoint_id: str
    server_name: Optional[str] = None
    annotations: Optional[Dict[str, Any]] = None


class MCPEndpoint(BaseModel):
    """MCP端点"""
    id: str
    name: str
    transport: str  # stdio, sse, http, ws, sdk
    url: Optional[str] = None
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    environment: Dict[str, str] = Field(default_factory=dict)
    tools: List[MCPTool] = Field(default_factory=list)
    status: str = "disconnected"


# ===== 后台任务 =====

class AsyncTask(BaseModel):
    """异步任务"""
    id: str
    task_type: str
    status: str  # pending, running, completed, failed
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)


class TaskStatus(BaseModel):
    """任务状态"""
    task_id: str
    status: str
    progress: float
    result: Optional[Any] = None
    error: Optional[str] = None


class ScheduledTask(BaseModel):
    """定时任务"""
    id: str
    name: str
    task_type: str
    cron_expression: str
    params: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None


class CronTrigger(BaseModel):
    """Cron触发器"""
    minute: str = "*"
    hour: str = "*"
    day: str = "*"
    month: str = "*"
    day_of_week: str = "*"


# ===== 提示词引擎 =====

class PromptTemplate(BaseModel):
    """提示词模板"""
    id: str
    name: str
    template: str  # Jinja2模板
    variables: List[str] = Field(default_factory=list)
    version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PromptVersion(BaseModel):
    """提示词版本"""
    template_id: str
    version: int
    template: str
    created_at: datetime
    created_by: Optional[str] = None
