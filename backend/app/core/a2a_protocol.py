"""A2A (Agent to Agent) 通信协议定义"""
from enum import Enum
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from datetime import datetime


class A2AMessageType(str, Enum):
    """A2A 消息类型"""
    SPAWN_REQUEST = "spawn_request"      # 父agent请求生成子agent
    SPAWN_RESPONSE = "spawn_response"    # 子agent生成结果
    TASK_COMPLETE = "task_complete"      # 子agent任务完成通知
    TASK_FAILED = "task_failed"          # 子agent任务失败通知
    STOP_REQUEST = "stop_request"        # 停止请求（级联）
    STATUS_UPDATE = "status_update"      # 状态更新


class SpawnRequest(BaseModel):
    """生成子agent的请求"""
    parent_agent_id: str                 # 父agent ID
    parent_agent_name: Optional[str] = None  # 父agent名称
    parent_conversation_id: str          # 父agent的会话ID
    spawn_uuid: str                      # 本次生成的唯一标识
    level: int                           # 层级（0, 1, 2）
    subagent_name: str                   # 子agent名称
    subagent_role: str                   # 子agent角色描述
    task_description: str                # 任务描述
    allowed_tools: List[str]             # 允许使用的工具列表
    config_overrides: Optional[Dict[str, Any]] = None  # 配置覆盖
    depends_on: List[str] = []           # 依赖的其他子agent spawn_uuid 列表
    agent_type: str = "common"            # agent类型: "common"(普通指派) 或 "skill"(skill指派)


class SpawnResponse(BaseModel):
    """生成子agent的响应"""
    success: bool                        # 是否成功
    subagent_id: Optional[str] = None    # 子agent ID（成功后）
    error_message: Optional[str] = None  # 错误信息（失败后）
    spawn_uuid: str                      # 对应的生成UUID


class TaskResult(BaseModel):
    """子agent任务执行结果"""
    subagent_id: str                     # 子agent ID
    spawn_uuid: str                      # 生成UUID
    success: bool                        # 是否成功
    result: Optional[str] = None         # 执行结果
    error_message: Optional[str] = None  # 错误信息
    execution_time_seconds: float        # 执行耗时（秒）
    tool_calls_count: int                # 工具调用次数
    token_usage: Optional[Dict[str, int]] = None  # Token使用情况


class DependencyResult(BaseModel):
    """依赖项的执行结果 - 用于子agent间传递依赖结果"""
    spawn_uuid: str                      # 被依赖的子agent的 spawn_uuid
    subagent_id: str                     # 被依赖的子agent ID
    subagent_name: str                   # 被依赖的子agent名称
    success: bool                        # 是否成功
    result: Optional[str] = None         # 执行结果
    error_message: Optional[str] = None  # 错误信息


class A2AMessage(BaseModel):
    """A2A 通信消息"""
    message_type: A2AMessageType         # 消息类型
    sender_id: str                       # 发送者ID（agent或subagent）
    receiver_id: str                     # 接收者ID
    spawn_uuid: Optional[str] = None     # 关联的生成UUID
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    # 消息内容（根据类型不同而不同）
    payload: Optional[Dict[str, Any]] = None
    
    class Config:
        arbitrary_types_allowed = True


def generate_subagent_id(spawn_uuid: str) -> str:
    """
    生成子agent的唯一ID
    格式: subagent:<spawn_uuid>
    """
    return f"subagent:{spawn_uuid}"


def parse_subagent_id(subagent_id: str) -> Dict[str, str]:
    """
    解析子agent ID，提取 spawn_uuid
    格式: subagent:<spawn_uuid>
    """
    prefix = "subagent:"
    if not subagent_id.startswith(prefix):
        raise ValueError(f"Invalid subagent ID format (missing subagent: prefix): {subagent_id}")
    
    spawn_uuid = subagent_id[len(prefix):]
    if not spawn_uuid:
        raise ValueError(f"Invalid subagent ID format (empty spawn_uuid): {subagent_id}")
    
    return {
        "spawn_uuid": spawn_uuid,
    }


def extract_level_from_spawn_uuid(spawn_uuid: str) -> int:
    """
    从 spawn_uuid 中提取层级信息
    约定：spawn_uuid 格式为 <level>-<random-uuid>
    """
    try:
        level_str = spawn_uuid.split("-")[0]
        return int(level_str)
    except (ValueError, IndexError):
        return 0
