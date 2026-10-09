"""Agent基类 - 所有Agent的基础"""
import uuid
import base64
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, AsyncIterator, Tuple

import structlog
from app.models.schemas.agent import AgentConfig, AgentStatus, AgentInstance
from app.models.schemas.chat import Message, MessageRole, MessageType, Conversation, ConversationStatus
from app.models.schemas.tool import ToolCall, ToolResult
from app.core.memory import MemoryManager
from app.core.prompt_engine import PromptEngine
from app.tools.registry import ToolRegistry
from app.services.llm_service import LLMService
from app.services.persistence import persistence

logger = structlog.get_logger()


class BaseAgent(ABC):
    """Agent基类"""
    
    def __init__(
        self,
        config: AgentConfig,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        memory_manager: MemoryManager,
        prompt_engine: PromptEngine,
    ):
        # 使用config中的id（如果提供），否则生成新的UUID
        self.id = config.id or str(uuid.uuid4())
        self.config = config
        self.llm_service = llm_service
        self.tool_registry = tool_registry
        self.memory_manager = memory_manager
        self.prompt_engine = prompt_engine
        
        self.status = AgentStatus.IDLE
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        self.current_task: Optional[str] = None
        
        # 会话管理
        self._conversations: Dict[str, Conversation] = {}
        self._current_conversation: Optional[str] = None
        
        # ✅ 取消机制：跟踪需要中止的请求
        self._cancelled_requests: set = set()
        
        # Subagent 相关
        self.is_subagent = config.is_subagent if hasattr(config, 'is_subagent') else False
        self.level = config.level if hasattr(config, 'level') and config.level is not None else -1
        self.parent_agent_id = config.parent_agent_id if hasattr(config, 'parent_agent_id') else None
        self.spawn_uuid = config.spawn_uuid if hasattr(config, 'spawn_uuid') else None
        self.deny_skills = []
        self.agent_type = "common"  # 默认为common，可选值有："common", "skill"
        # 待处理的子agent结果
        self._pending_subagent_results: List[Any] = []
    
    @abstractmethod
    async def process_message(
        self,
        message: str,
        conversation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Message]:
        """处理消息（流式响应）"""
        pass
    
    @abstractmethod
    async def execute_task(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """执行任务"""
        pass
    
    async def chat(
        self,
        message: str,
        conversation_id: Optional[str] = None,
    ) -> Message:
        """聊天接口（非流式）"""
        messages = []
        async for msg in self.process_message(message, conversation_id):
            messages.append(msg)
        
        if messages:
            return messages[-1]
        
        return Message(
            id=str(uuid.uuid4()),
            role=MessageRole.ASSISTANT,
            content="No response generated.",
            message_type=MessageType.ERROR,
        )
    
    async def start(self):
        """启动Agent"""
        self.status = AgentStatus.IDLE
        self.updated_at = datetime.utcnow()
        logger.info("Agent started", agent_id=self.id, name=self.config.name)
        
        # 发射 Agent 启动事件
        try:
            from app.core.event_bus import event_bus
            await event_bus.emit_task_status(
                source=self.id,
                status="idle",
                extra={"agent_name": self.config.name},
                dimensions=[f"agent:{self.id}"],
            )
        except Exception as e:
            logger.warning("Failed to emit agent start event", error=str(e))
    
    async def stop(self):
        """停止Agent"""
        self.status = AgentStatus.STOPPED
        self.updated_at = datetime.utcnow()
        logger.info("Agent stopped", agent_id=self.id)
        
        # 发射 Agent 停止事件
        try:
            from app.core.event_bus import event_bus
            await event_bus.emit_task_status(
                source=self.id,
                status="stopped",
                extra={"agent_name": self.config.name},
                dimensions=[f"agent:{self.id}"],
            )
        except Exception as e:
            logger.warning("Failed to emit agent stop event", error=str(e))
        
        # 如果是父agent，触发级联停止
        if not self.is_subagent:
            from app.core.subagent_manager import subagent_manager
            await subagent_manager.cascade_stop(self.id)
    
    async def pause(self):
        """暂停Agent"""
        self.status = AgentStatus.PAUSED
        self.updated_at = datetime.utcnow()
    
    async def resume(self):
        """恢复Agent"""
        self.status = AgentStatus.IDLE
        self.updated_at = datetime.utcnow()
    
    def _build_full_system_prompt(self) -> str:
        """构建完整的系统提示词（带分层缓存：静态区常驻 + 动态区 TTL 30min）"""
        from app.core.prompt_cache import system_prompt_cache
        return system_prompt_cache.get_or_build(self)
    
    def create_conversation(
        self,
        user_id: Optional[str] = None,
        title: Optional[str] = None,
        status: ConversationStatus = ConversationStatus.ACTIVE,
    ) -> Conversation:
        """创建新会话（同步版本）"""
        conversation = Conversation(
            id=str(uuid.uuid4()),
            agent_id=self.id,
            user_id=user_id,
            title=title or f"Conversation {len(self._conversations) + 1}",
            status=status,
        )
        
        # 添加系统消息（注入技能列表）
        system_prompt = self._build_full_system_prompt()
        system_message = Message(
            id=str(uuid.uuid4()),
            role=MessageRole.SYSTEM,
            content=system_prompt,
        )
        conversation.messages.append(system_message)
        
        self._conversations[conversation.id] = conversation
        self._current_conversation = conversation.id
        
        logger.info("Conversation created", conversation_id=conversation.id)
        return conversation
    
    async def create_conversation_async(
        self,
        user_id: Optional[str] = None,
        title: Optional[str] = None,
        status: ConversationStatus = ConversationStatus.ACTIVE,
    ) -> Conversation:
        """创建新会话（异步版本，等待保存到数据库）"""
        conversation = self.create_conversation(user_id, title, status)
        
        # ✅ 同步保存到数据库（子agent跳过持久化）
        if not self.is_subagent:
            await persistence.save_conversation(conversation)
            await persistence.save_message(conversation.id, conversation.messages[0])  # 系统消息
            logger.info("Conversation saved to database", conversation_id=conversation.id)
            # ✅ 会话级工作区：主agent创建会话时同步创建 worktree 工作区
            try:
                from app.core.workspace_isolation import workspace_isolation
                if workspace_isolation.get_scope() == "session":
                    await workspace_isolation.create_session_workspace(
                        self.config.name, conversation.id
                    )
            except Exception as e:
                logger.warning("Failed to create session workspace", 
                             conversation_id=conversation.id, error=str(e))
        else:
            logger.info("Conversation created for subagent (no persistence)", conversation_id=conversation.id)
        
        return conversation
    
    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """获取会话"""
        return self._conversations.get(conversation_id)
    
    def get_current_conversation(self) -> Optional[Conversation]:
        """获取当前会话"""
        if self._current_conversation:
            return self._conversations.get(self._current_conversation)
        return None
    
    def list_conversations(self) -> List[Conversation]:
        """列出所有会话"""
        return list(self._conversations.values())
    
    async def _add_message(
        self,
        conversation_id: str,
        role: MessageRole,
        content: str,
        message_type: MessageType = MessageType.TEXT,
        metadata: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        name: Optional[str] = None,
        reasoning_content: Optional[str] = None,
        message_id_override: Optional[str] = None,  # ✅ 允许覆盖消息ID
    ) -> Message:
        """添加消息到会话"""
        conversation = self._conversations.get(conversation_id)
        if not conversation:
            raise ValueError(f"Conversation not found: {conversation_id}")
        
        message = Message(
            id=message_id_override or str(uuid.uuid4()),  # ✅ 使用override或生成新ID
            role=role,
            content=content,
            message_type=message_type,
            metadata=metadata or {},
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            name=name,
            reasoning_content=reasoning_content,
        )
        
        conversation.messages.append(message)
        conversation.updated_at = datetime.utcnow()
        
        # ✅ Session Memory 监控：统计 token 增长与工具调用（仅主 Agent）
        if not self.is_subagent:
            try:
                from app.core.memory_system.session_memory import session_memory_manager
                session_memory_manager.on_message_added(conversation_id, message)
            except Exception as e:
                logger.debug("Session memory tracking skipped", error=str(e))
        
        # 存储到短期记忆
        await self.memory_manager.add_short_term(
            self.id,
            f"{role.value}: {content}",
            metadata={"conversation_id": conversation_id},
        )
        
        # 保存到数据库（子agent跳过持久化）
        if not self.is_subagent:
            import asyncio
            asyncio.create_task(persistence.save_message(conversation_id, message))
            asyncio.create_task(persistence.save_conversation(conversation))
        
        return message
    
    async def _build_messages_for_llm(
        self,
        conversation_id: str,
        auto_explicit_cache: bool = False,
    ) -> List[Dict[str, Any]]:
        """构建LLM消息列表（含压缩标记处理）"""
        conversation = self._conversations.get(conversation_id)
        if not conversation:
            return []
        
        # 创建副本，不修改原始 conversation.messages
        working = list(conversation.messages)
        
        # ===== 替换 system prompt 为最新缓存版本 =====
        # 数据库中的 system prompt 是创建时的快照，动态区（skills/memory）可能已变化
        # 发给 LLM 的始终使用缓存中最新的 system prompt
        if working and working[0].role == MessageRole.SYSTEM:
            # ✅ 设置会话上下文：ToolSearch 延迟工具摘要需按会话生成，
            # 避免把其它会话已加载的工具从 deferred 列表中错误移除。
            from app.tools.tool_search import current_conversation_id
            conv_token = current_conversation_id.set(conversation_id)
            try:
                latest_system_prompt = self._build_full_system_prompt()
            finally:
                current_conversation_id.reset(conv_token)
            working[0] = Message(
                id=working[0].id,
                role=MessageRole.SYSTEM,
                content=latest_system_prompt,
            )
        
        # ===== 压缩标记处理：active -> auto -> snip -> budget -> micro =====
        
        # --- Active (reactive): 倒序查找最近一条 active 标记，构建摘要消息 ---
        active_idx = -1
        for i in range(len(working) - 1, -1, -1):
            if working[i].message_type == MessageType.ACTIVE_COMPACT_BOUNDARY:
                active_idx = i
                break
        
        if active_idx >= 0:
            active_marker = working[active_idx]
            summary_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.USER,
                content=active_marker.content,
                message_type=MessageType.TEXT,
            )
            new_list = []
            if working and working[0].role == MessageRole.SYSTEM:
                new_list.append(working[0])
            new_list.append(summary_msg)
            new_list.extend(working[active_idx + 1:])
            working = new_list
        
        # --- Auto: 倒序查找最近一条 auto 标记，构建摘要消息 ---
        auto_idx = -1
        for i in range(len(working) - 1, -1, -1):
            if working[i].message_type == MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY:
                auto_idx = i
                break
        
        if auto_idx >= 0:
            auto_marker = working[auto_idx]
            # 创建 user 类型的摘要消息
            summary_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.USER,
                content=auto_marker.content,
                message_type=MessageType.TEXT,
            )
            # 新列表：首条 system + 摘要消息 + auto 标记后的所有消息
            new_list = []
            if working and working[0].role == MessageRole.SYSTEM:
                new_list.append(working[0])
            new_list.append(summary_msg)
            new_list.extend(working[auto_idx + 1:])
            working = new_list
        
        # --- Snip: 移除被裁剪的工具调用及结果 ---
        snip_tool_ids = set()
        for msg in working:
            if msg.message_type == MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY:
                snip_tool_ids.update(msg.metadata.get("compacted_tool_ids", []))
        
        if snip_tool_ids:
            snipped = []
            for msg in working:
                # 跳过标记消息
                if msg.message_type in (
                    MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY,
                    MessageType.COMPRESS_BUDGET_BOUNDARY,
                    MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY,
                    MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY,
                    MessageType.ACTIVE_COMPACT_BOUNDARY,
                ):
                    continue
                
                # 删除匹配的 tool 消息
                if msg.role == MessageRole.TOOL and msg.tool_call_id in snip_tool_ids:
                    continue
                
                # 处理 assistant 消息中的 tool_calls
                if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                    remaining_tcs = [
                        tc for tc in msg.tool_calls
                        if tc.get('id') not in snip_tool_ids
                    ]
                    if not remaining_tcs:
                        # 所有 tool_calls 都被删除，删除整条 assistant 消息
                        continue
                    if len(remaining_tcs) < len(msg.tool_calls):
                        # 部分 tool_calls 被删除，创建新消息
                        msg = Message(
                            id=msg.id, role=msg.role, content=msg.content,
                            message_type=msg.message_type, tool_calls=remaining_tcs,
                            reasoning_content=msg.reasoning_content,
                            tool_call_id=msg.tool_call_id, name=msg.name,
                            metadata=msg.metadata.copy(), created_at=msg.created_at,
                            token_count=msg.token_count,
                        )
                
                snipped.append(msg)
            working = snipped
        
        # --- Budget: 替换超长工具输出为 persisted-output 占位符 ---
        budget_paths = {}
        for msg in working:
            if msg.message_type == MessageType.COMPRESS_BUDGET_BOUNDARY:
                budget_paths.update(msg.metadata.get("tool_result_path", {}))
        
        if budget_paths:
            budgeted = []
            for msg in working:
                if msg.role == MessageRole.TOOL and msg.tool_call_id in budget_paths:
                    result_path = budget_paths[msg.tool_call_id]
                    preview = msg.content[:200]
                    new_content = f"<persisted-output>\nFull output: {result_path}\nPreview: {preview}"
                    msg = Message(
                        id=msg.id, role=msg.role, content=new_content,
                        message_type=msg.message_type,
                        tool_call_id=msg.tool_call_id, name=msg.name,
                        metadata=msg.metadata.copy(), created_at=msg.created_at,
                        token_count=msg.token_count,
                    )
                budgeted.append(msg)
            working = budgeted
        
        # --- Micro: 替换工具输出为压缩占位符 ---
        micro_tool_ids = set()
        for msg in working:
            if msg.message_type == MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY:
                micro_tool_ids.update(msg.metadata.get("compacted_tool_ids", []))
        
        if micro_tool_ids:
            microd = []
            for msg in working:
                if msg.role == MessageRole.TOOL and msg.tool_call_id in micro_tool_ids:
                    msg = Message(
                        id=msg.id, role=msg.role, content="[Tool output compressed]",
                        message_type=msg.message_type,
                        tool_call_id=msg.tool_call_id, name=msg.name,
                        metadata=msg.metadata.copy(), created_at=msg.created_at,
                        token_count=msg.token_count,
                    )
                microd.append(msg)
            working = microd
        
        # ===== 构建 LLM dict 消息 =====
        messages = []
        for msg in working:
            # 跳过压缩标记消息和记忆注入消息和斜杠命令消息
            if msg.message_type in (
                MessageType.COMPRESS_BUDGET_BOUNDARY,
                MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY,
                MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY,
                MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY,
                MessageType.ACTIVE_COMPACT_BOUNDARY,
                MessageType.SLASH_COMMAND_REQUEST,
                MessageType.SLASH_COMMAND_RESPONSE,
                MessageType.IMAGE_GENERATED,
                MessageType.MUSIC_GENERATED,
                MessageType.VIDEO_GENERATED,
                MessageType.AUDIO_GENERATED,
            ):
                continue
            
            message_dict = {
                "role": msg.role.value,
                "content": msg.content,
            }
            
            # ✅ 多模态内容：用户消息带附件时，根据配置构建 multimodal content
            if msg.role == MessageRole.USER and msg.metadata and msg.metadata.get("attachments"):
                attachments = msg.metadata["attachments"]
                image_attachments = [a for a in attachments if a.get("is_image")]
                non_image_attachments = [a for a in attachments if not a.get("is_image")]
                
                # ✅ 读取配置
                from app.config import settings
                settings_dict = settings.to_dict()
                conv_config = settings_dict.get("conversation", {})
                image_config = conv_config.get("image", {})
                file_config = conv_config.get("file", {})
                
                image_infer_mode = image_config.get("infer_mode", "inline")
                file_infer_mode = file_config.get("infer_mode", "inline")
                image_detail = image_config.get("detail", "auto")
                image_expires = image_config.get("expires_after", 86400)
                file_expires = file_config.get("expires_after", 86400)
                
                uploads_root = Path(settings.get("WORKSPACE_ROOT", str(Path(".happy-claw") / "workspace")))
                
                # ✅ 文本类文件扩展名（inline 模式下可读取内容发送给 LLM）
                _TEXT_EXTENSIONS = {
                    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
                    ".csv", ".xml", ".html", ".css", ".sql", ".sh", ".bat",
                    ".toml", ".ini", ".cfg", ".conf", ".log", ".env",
                    ".java", ".go", ".rs", ".c", ".cpp", ".h", ".rb", ".php",
                }
                _MAX_TEXT_FILE_SIZE = 50 * 1024  # 50KB 上限
                
                def _build_file_content_inline(att: dict) -> str:
                    """读取文本类附件内容，二进制文件仅返回文件名提示"""
                    name = att.get("name", "")
                    ext = Path(name).suffix.lower()
                    subdir = att.get("subdir", "")
                    stored_filename = att.get("stored_filename", "")
                    
                    if ext in _TEXT_EXTENSIONS and subdir and stored_filename:
                        file_path = uploads_root / subdir / stored_filename
                        if file_path.exists():
                            try:
                                size = file_path.stat().st_size
                                if size <= _MAX_TEXT_FILE_SIZE:
                                    content = file_path.read_text(encoding="utf-8", errors="replace")
                                    return f"[附件: {name}]\n```\n{content}\n```"
                                else:
                                    content = file_path.read_text(encoding="utf-8", errors="replace")[:_MAX_TEXT_FILE_SIZE]
                                    return f"[附件: {name} (已截断，原文件 {size // 1024}KB)]\n```\n{content}\n...\n```"
                            except Exception as e:
                                logger.warning("Failed to read text attachment", file=name, error=str(e))
                    
                    return f"[附件: {name}]"
                
                # ✅ 构建 multimodal content 数组
                content_parts: List[Dict[str, Any]] = []
                text_part = msg.content or ""
                
                # --- 处理图片附件 ---
                need_persist_metadata = False  # ✅ 跟踪是否需要持久化 metadata
                for img_att in image_attachments:
                    if image_infer_mode == "file_id":
                        # 上传到供应商，通过 file_id 引用
                        remote_id, is_new = await self._upload_attachment_to_provider(
                            img_att, uploads_root, purpose="user_data", expires_seconds=image_expires
                        )
                        if is_new:
                            need_persist_metadata = True
                        if remote_id:
                            content_parts.append({
                                "type": "file",
                                "file_id": remote_id
                            })
                        else:
                            # 上传失败，回退到 base64 inline
                            subdir = img_att.get("subdir", "")
                            stored_filename = img_att.get("stored_filename", "")
                            if subdir and stored_filename:
                                img_path = uploads_root / subdir / stored_filename
                                if img_path.exists():
                                    try:
                                        img_data = base64.b64encode(img_path.read_bytes()).decode("utf-8")
                                        mime = img_att.get("mime_type", "image/png")
                                        content_parts.append({
                                            "type": "image_url",
                                            "image_url": {"url": f"data:{mime};base64,{img_data}", "detail": image_detail},
                                        })
                                    except Exception as e:
                                        logger.warning("Failed to read image for LLM", file=stored_filename, error=str(e))
                    else:
                        # inline 模式：读取图片转 base64
                        subdir = img_att.get("subdir", "")
                        stored_filename = img_att.get("stored_filename", "")
                        if subdir and stored_filename:
                            img_path = uploads_root / subdir / stored_filename
                            if img_path.exists():
                                try:
                                    img_data = base64.b64encode(img_path.read_bytes()).decode("utf-8")
                                    mime = img_att.get("mime_type", "image/png")
                                    content_parts.append({
                                        "type": "image_url",
                                        "image_url": {"url": f"data:{mime};base64,{img_data}", "detail": image_detail},
                                    })
                                except Exception as e:
                                    logger.warning("Failed to read image for LLM", file=stored_filename, error=str(e))
                
                # --- 处理非图片附件 ---
                for file_att in non_image_attachments:
                    if file_infer_mode == "file_id":
                        # 上传到供应商，通过 file_id 引用
                        remote_id, is_new = await self._upload_attachment_to_provider(
                            file_att, uploads_root, purpose="assistants", expires_seconds=file_expires
                        )
                        if is_new:
                            need_persist_metadata = True
                        if remote_id:
                            content_parts.append({
                                "type": "file",
                                "file": {"file_id": remote_id},
                            })
                        else:
                            # 上传失败，回退到 inline
                            text_part = (text_part + "\n" if text_part else "") + _build_file_content_inline(file_att)
                    else:
                        # inline 模式：读取文本内容或追加提示
                        text_part = (text_part + "\n" if text_part else "") + _build_file_content_inline(file_att)
                
                # ✅ 组装最终 content
                if content_parts:
                    # 有 multimodal 内容（图片或 file_id 引用），构建数组格式
                    final_parts = [{"type": "text", "text": text_part}] if text_part else []
                    final_parts.extend(content_parts)
                    message_dict["content"] = final_parts
                elif text_part != (msg.content or ""):
                    # 只有 inline 文本附件追加，content 仍为字符串
                    message_dict["content"] = text_part
                
                # ✅ 首次上传附件后，立即持久化 metadata（仅在新上传时执行一次）
                if need_persist_metadata and not self.is_subagent:
                    from app.services.persistence import persistence
                    await persistence.update_message_metadata(msg.id, msg.metadata)
            
            # DeepSeek思考模式：将reasoning_content传回API
            if msg.reasoning_content:
                message_dict["reasoning_content"] = msg.reasoning_content
            
            if msg.tool_calls:
                message_dict["tool_calls"] = msg.tool_calls
                # OpenAI要求：有tool_calls时content必须为null
                if not msg.content:
                    message_dict["content"] = None
            
            if msg.tool_call_id:
                message_dict["tool_call_id"] = msg.tool_call_id
                message_dict["name"] = msg.name
            
            messages.append(message_dict)
        
        # ===== 显式缓存优化：添加 cache_control 标记 =====
        if auto_explicit_cache:
            messages = self._add_cache_control_markers(messages)
        
        logger.debug("Built messages for LLM", 
            messages_count=len(messages),
            roles=[m["role"] for m in messages],
            has_tool_calls=[bool(m.get("tool_calls")) for m in messages],
            has_tool_call_id=[bool(m.get("tool_call_id")) for m in messages],
            auto_explicit_cache=auto_explicit_cache,
        )
        return messages
    
    # ========== 附件上传到 LLM 供应商 ==========
    
    async def _upload_attachment_to_provider(
        self,
        att: dict,
        uploads_root: Path,
        purpose: str,
        expires_seconds: int,
    ) -> Tuple[Optional[str], bool]:
        """上传附件到 LLM 供应商，返回 (remote_file_id, is_new_upload)。
        
        已上传的附件会在 metadata 中缓存 remote_file_id，避免重复上传。
        is_new_upload=True 表示本次是新上传，需要持久化 metadata。
        """
        # ✅ 已上传过，直接返回缓存的 file_id（不需要再次持久化）
        if att.get("remote_file_id"):
            return att["remote_file_id"], False
        
        subdir = att.get("subdir", "")
        stored_filename = att.get("stored_filename", "")
        if not subdir or not stored_filename:
            return None, False
        
        file_path = uploads_root / subdir / stored_filename
        if not file_path.exists():
            return None, False
        
        try:
            # 获取当前 agent 对应的 LLM 客户端
            ep = self.llm_service._resolve_endpoint(self.llm_service.provider)
            client = self.llm_service._get_client(ep)
            
            # 上传文件
            with open(file_path, "rb") as f:
                remote_file = await client.files.create(
                    file=f,
                    purpose=purpose,
                    expires_after={"anchor": "created_at", "seconds": expires_seconds},
                )
            
            # ✅ 缓存 remote_file_id 到附件 metadata（后续调用不再重复上传）
            att["remote_file_id"] = remote_file.id
            logger.info(
                "Attachment uploaded to provider",
                remote_file_id=remote_file.id,
                filename=stored_filename,
                purpose=purpose,
            )
            return remote_file.id, True  # ✅ is_new_upload=True，需要持久化
        except Exception as e:
            logger.warning("Failed to upload attachment to provider", file=stored_filename, error=str(e))
            return None, False
    
    # ========== 显式缓存优化 ==========
    
    @staticmethod
    def _add_cache_control_markers(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """为显式缓存添加 cache_control 标记。
        
        标记位置：
        - 首条 system 消息
        - 最近一条 user 消息
        """
        if not messages:
            return messages
        
        # 首条 system 消息添加 cache_control
        for msg in messages:
            if msg.get("role") == "system":
                msg["cache_control"] = {"type": "ephemeral"}
                break
        
        # 最近一条 user 消息添加 cache_control（倒序查找）
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                messages[i]["cache_control"] = {"type": "ephemeral"}
                break
        
        return messages
    
    # ========== 工作区隔离 ==========
    
    def _get_injected_workspace(self) -> Optional[str]:
        """获取创建方注入的工作区路径（子agent/队友由管理器创建 worktree 后注入）"""
        metadata = getattr(self.config, "metadata", None) or {}
        if isinstance(metadata, dict):
            return metadata.get("workspace_path")
        return None
    
    def get_workspace_path(self, conversation_id: Optional[str] = None) -> Optional[str]:
        """
        计算当前 agent 应使用的工作区路径（不保证已创建）。
        
        按 agent.defaults.sandbox.scope 配置解析:
        - shared: 全局共享工作区 .happy-claw/workspace
        - agent:  agent级工作区 .happy-claw/workspace/agents/{agent_name}
        - session: 会话级 worktree .happy-claw/workspace/agents/{agent_name}/{session_id}
        子agent/队友优先使用创建方注入的 workspace_path。
        """
        import os
        from app.core.workspace_isolation import workspace_isolation, sanitize_dir_name
        
        injected = self._get_injected_workspace()
        if injected:
            return injected
        
        scope = workspace_isolation.get_scope()
        if scope == "shared":
            return workspace_isolation.root
        
        agent_dir = os.path.join(
            workspace_isolation.agents_dir, sanitize_dir_name(self.config.name)
        )
        if scope == "agent":
            return agent_dir
        
        # session 级
        conv_id = conversation_id or self._current_conversation
        if not conv_id:
            return agent_dir  # 无会话上下文时降级到 agent 工作区
        return os.path.join(agent_dir, sanitize_dir_name(conv_id))
    
    async def ensure_workspace(self, conversation_id: Optional[str] = None) -> str:
        """
        确保工作区存在并返回路径（懒创建：首次工具调用时触发）。
        """
        import os
        from app.core.workspace_isolation import workspace_isolation
        
        injected = self._get_injected_workspace()
        if injected:
            os.makedirs(injected, exist_ok=True)
            return injected
        
        scope = workspace_isolation.get_scope()
        if scope == "shared":
            return await workspace_isolation.ensure_global_repo()
        if scope == "agent":
            return await workspace_isolation.ensure_agent_workspace(self.config.name)
        
        conv_id = conversation_id or self._current_conversation
        if not conv_id:
            return await workspace_isolation.ensure_agent_workspace(self.config.name)
        path, _branch = await workspace_isolation.create_session_workspace(
            self.config.name, conv_id
        )
        return path
    
    async def _execute_tool_calls(
        self,
        tool_calls: List[Dict[str, Any]],
        conversation_id: Optional[str] = None,
    ) -> List[ToolResult]:
        """执行工具调用"""
        from app.core.workspace_isolation import current_workspace
        from app.tools.tool_search import current_search_engine, current_conversation_id
        
        # ✅ 工作区切换：将当前工作区写入上下文，工具内的相对路径以此为基准解析。
        # 不使用 os.chdir（进程级、并发不安全），主agent与子agent并发时各用各的工作区。
        ws_path: Optional[str] = None
        try:
            ws_path = await self.ensure_workspace()
        except Exception as e:
            logger.warning("Workspace prepare failed, tools fall back to process cwd",
                         agent_id=self.id, error=str(e))
        
        ws_token = current_workspace.set(ws_path) if ws_path else None
        # ✅ 设置当前 agent 的 ToolSearchEngine 上下文（多 agent 场景下 handler 分发）
        ts_engine = getattr(self, '_tool_search_engine', None)
        ts_token = current_search_engine.set(ts_engine) if ts_engine else None
        # ✅ 设置当前会话上下文：tool_search 加载的延迟工具仅对该会话生效，
        # 避免同一 agent 的其它会话共享 loaded 状态。
        conv_token = current_conversation_id.set(conversation_id) if conversation_id else None
        try:
            return await self._execute_tool_calls_inner(tool_calls)
        finally:
            if conv_token is not None:
                current_conversation_id.reset(conv_token)
            if ts_token is not None:
                current_search_engine.reset(ts_token)
            if ws_token is not None:
                current_workspace.reset(ws_token)
    
    async def _execute_tool_calls_inner(
        self,
        tool_calls: List[Dict[str, Any]],
    ) -> List[ToolResult]:
        """执行工具调用（内部实现，已在工作区上下文中）"""
        results = []
        
        for tc in tool_calls:
            # 先解析arguments（流式累积的是JSON字符串，需先解析为dict）
            raw_args = tc["function"].get("arguments", {})
            if isinstance(raw_args, str):
                import json
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {}
            
            tool_call = ToolCall(
                id=tc.get("id", str(uuid.uuid4())),
                name=tc["function"]["name"],
                arguments=raw_args,
            )
            
            result = await self.tool_registry.execute(
                tool_call,
                agent_id=self.id,
            )
            results.append(result)
            
            # ✅ 检测主 Agent 主动写入记忆目录（用于提取门控）
            if (
                not self.is_subagent
                and tool_call.name == "write_file"
                and result.success
            ):
                try:
                    written_path = tool_call.arguments.get("file_path", "")
                    from app.core.memory_system.store import memory_store
                    if memory_store.is_within_memory_root(written_path):
                        from app.core.memory_system.prefetch import memory_prefetch_manager
                        if self._current_conversation:
                            memory_prefetch_manager.mark_active_memory_write(self._current_conversation)
                        logger.info("Main agent wrote memory directly", path=written_path)
                except Exception as e:
                    logger.debug("Memory write detection skipped", error=str(e))
        
        return results
    
    def get_status(self) -> AgentInstance:
        """获取Agent状态"""
        return AgentInstance(
            id=self.id,
            config=self.config,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            current_task=self.current_task,
        )
    
    async def cleanup(self):
        """清理资源"""
        # ✅ 释放 ToolSearchEngine（从全局通知列表中移除）
        engine = getattr(self, '_tool_search_engine', None)
        if engine is not None:
            engine.dispose()

        # ✅ 清理该agent的所有子agent
        try:
            from app.core.subagent_manager import subagent_manager
            # 获取该agent的所有子agent
            children = subagent_manager._parent_children_map.get(self.id, [])
            for child_id in children:
                try:
                    await subagent_manager.stop_subagent(child_id)
                except Exception as e:
                    logger.warning("Failed to stop subagent during cleanup", 
                                 subagent_id=child_id, error=str(e))
            # 清理映射关系
            if self.id in subagent_manager._parent_children_map:
                del subagent_manager._parent_children_map[self.id]
        except Exception as e:
            logger.warning("Failed to cleanup subagents", agent_id=self.id, error=str(e), exc_info=True)
        
        self._conversations.clear()
        self._cancelled_requests.clear()
        await self.memory_manager.clear_working_memory(self.id)
        logger.info("Agent cleaned up", agent_id=self.id)
    
    # ========== 取消机制 ==========
    
    def cancel_request(self, conversation_id: str):
        """标记某个会话的当前请求为已取消"""
        self._cancelled_requests.add(conversation_id)
        logger.info("Request cancelled", conversation_id=conversation_id, agent_id=self.id)
    
    def is_cancelled(self, conversation_id: str) -> bool:
        """检查某个会话的请求是否已被取消"""
        return conversation_id in self._cancelled_requests
    
    def clear_cancel(self, conversation_id: str):
        """清除取消标记"""
        self._cancelled_requests.discard(conversation_id)
    
    # ========== 子Agent结果接收 ==========
    
    async def receive_subagent_result(self, result: Any) -> None:
        """
        接收子agent的执行结果（由 A2A 回调触发）
        
        将结果存储到 _pending_subagent_results 供 ReAct 循环使用。
        注意：不重复添加 TOOL 消息，因为 spawn_agent 工具返回时已经添加了。
        
        Args:
            result: DependencyResult 对象
        """
        pass
