"""Pydantic模型 - 对话与消息相关"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageType(str, Enum):
    """消息类型"""
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    ERROR = "error"
    MULTI_MODAL = "multi_modal"
    MEMORY = "memory"  # 记忆注入消息
    # 压缩标记消息类型
    COMPRESS_BUDGET_BOUNDARY = "compress_budget_boundary"
    COMPRESS_SNIP_COMPACT_BOUNDARY = "compress_snip_compact_boundary"
    COMPRESS_MICRO_COMPACT_BOUNDARY = "compress_micro_compact_boundary"
    COMPRESS_AUTO_COMPACT_BOUNDARY = "compress_auto_compact_boundary"
    ACTIVE_COMPACT_BOUNDARY = "active_compact_boundary"
    CONTINUATION = "continuation"  # 续写提示消息（max_tokens截断后触发）
    BACKGROUND_TASK = "background_task"  # 后台任务完成通知消息
    SCHEDULED = "scheduled"  # 定时任务结果注入消息
    TEAMMATE = "teammate"  # 团队队友消息注入
    # 斜杠命令消息类型
    SLASH_COMMAND_REQUEST = "slash_command_request"  # 用户发送的斜杠命令
    SLASH_COMMAND_RESPONSE = "slash_command_response"  # 斜杠命令执行结果
    # 图片生成消息类型
    IMAGE_GENERATED = "image_generated"  # 图片生成结果消息
    # 音乐生成消息类型
    MUSIC_GENERATED = "music_generated"  # 音乐生成结果消息
    # 视频生成消息类型
    VIDEO_GENERATED = "video_generated"  # 视频生成结果消息
    # PDF 解析消息类型
    PDF_PARSED = "pdf_parsed"  # PDF 解析结果消息
    # 语音合成消息类型
    AUDIO_GENERATED = "audio_generated"  # TTS语音合成结果消息


class ToolCallStatus(str, Enum):
    """工具调用状态"""
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"


class ConversationStatus(str, Enum):
    """会话状态"""
    NEW = "new"  # 已创建但尚未发送任何消息（不展示在会话列表，可被清理）
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


# 消息模型
class Message(BaseModel):
    """单条消息"""
    id: str
    role: MessageRole
    content: str
    message_type: MessageType = MessageType.TEXT
    reasoning_content: Optional[str] = None  # DeepSeek思考模式的推理内容
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # 工具名称
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    token_count: Optional[int] = None
    tool_call_status: Optional[ToolCallStatus] = None  # 工具调用状态


class Conversation(BaseModel):
    """对话会话"""
    id: str
    agent_id: str
    user_id: Optional[str] = None
    title: Optional[str] = None
    status: ConversationStatus = ConversationStatus.ACTIVE
    messages: List[Message] = Field(default_factory=list)
    context_tokens: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    session_model: Optional[Dict[str, Any]] = None  # 会话级模型配置 {provider, model_id, reasoning_level, speed_mode}


# 对话请求/响应
class SendMessageRequest(BaseModel):
    """发送消息请求"""
    message: str
    conversation_id: Optional[str] = None
    attachments: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    consume_user_message_id: Optional[str] = None  # 已持久化的用户消息ID（定时任务注入），非空时后端不重复追加用户消息
    session_model: Optional[Dict[str, Any]] = None  # ✅ 新会话预选的会话级模型配置 {provider, model_id, reasoning_level, speed_mode}，创建会话后立即应用


class ChatResponse(BaseModel):
    """聊天响应（流式）"""
    id: str
    conversation_id: str
    message: Message
    tool_calls: Optional[List[Dict[str, Any]]] = None
    thinking: Optional[str] = None
    is_complete: bool = False


# 上下文管理
class CompressedContext(BaseModel):
    """压缩后的上下文"""
    original_tokens: int
    compressed_tokens: int
    summary: str
    key_messages: List[Message] = Field(default_factory=list)
    compression_ratio: float


class SummaryResult(BaseModel):
    """摘要结果"""
    summary: str
    key_points: List[str] = Field(default_factory=list)
    token_count: int


# 对话列表响应
class ConversationResponse(BaseModel):
    """对话响应"""
    id: str
    agent_id: str
    title: Optional[str] = None
    status: ConversationStatus
    message_count: int
    context_tokens: int
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


class ConversationListResponse(BaseModel):
    """对话列表响应"""
    conversations: List[ConversationResponse]
    total: int


# 会话级模型配置

class SessionModelConfig(BaseModel):
    """会话级模型配置"""
    provider: str  # LLM提供商，如 deepseek
    model_id: str  # 模型ID，如 deepseek-v4-flash
    reasoning_level: Optional[str] = None  # 推理级别：none/low/high/max
    speed_mode: Optional[str] = None  # 速度模式：default/fast/standard/auto
