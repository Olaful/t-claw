"""对话API - 支持WebSocket实时通信"""
import json
import os
import uuid
import shutil
import mimetypes
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse, FileResponse, Response

import structlog

from app.config import settings
from app.models.schemas.chat import (
    SendMessageRequest, ChatResponse, ConversationResponse, ConversationListResponse,
    MessageType, SessionModelConfig, ConversationStatus,
)
from app.services.task_service import task_service
from app.services.persistence import persistence
from app.tools.registry import tool_registry

logger = structlog.get_logger()

router = APIRouter()

# 文件存储根目录（包含 uploads/staging 和 agents/ 两个子目录）
WORKSPACE_ROOT_PATH = Path(settings.get("WORKSPACE_ROOT", str(Path(".happy-claw") / "workspace")))
# 上传暂存目录（发送消息前文件临时存储位置）
STAGING_DIR = WORKSPACE_ROOT_PATH / "uploads" / "staging"
# Agent 工作区根目录（附件最终存储位置：agents/{agent_name}/{conversation_id}/）
AGENTS_ROOT = Path(settings.get("WORKSPACE_BASE_PATH", str(WORKSPACE_ROOT_PATH / "agents")))


@router.post("/send")
async def send_message(
    agent_id: str,
    request: SendMessageRequest,
):
    """发送消息并获取响应"""
    # 如果没有提供agent_id，使用默认值或创建新agent
    if not agent_id or agent_id == '':
        agent_id = 'default-agent'
    
    agent = task_service.get_agent(agent_id)
    if not agent:
        # 创建默认agent（使用.env中配置的模型）
        from app.models.schemas.agent import AgentConfig, LLMProvider
        from app.config import settings
        config = AgentConfig(
            id=agent_id,
            name="Default Agent",
            description="Default conversational agent",
            llm_provider=LLMProvider.OPENAI,
            llm_model=settings.DEFAULT_LLM_MODEL,  # 使用环境变量中的模型
            temperature=settings.DEFAULT_TEMPERATURE,
            max_tokens=settings.DEFAULT_MAX_TOKENS,
        )
        agent = task_service.create_agent(config)
    
    response = await agent.chat(
        message=request.message,
        conversation_id=request.conversation_id,
    )
    
    return {
        "message": {
            "id": response.id,
            "role": response.role,
            "content": response.content,
            "message_type": response.message_type,
            "created_at": response.created_at.isoformat(),
        },
        "conversation_id": request.conversation_id or agent.get_current_conversation().id if agent.get_current_conversation() else None,
    }


@router.post("/stream")
async def stream_message(
    agent_id: str,
    request: SendMessageRequest,
):
    """流式发送消息"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    async def event_generator():
        current_conversation_id = None
        try:
            # ✅ 在处理消息前，先获取或创建会话，确保有 conversation_id
            conv = None
            
            # ✅ 优先使用前端传入的有效 conversation_id
            if request.conversation_id and not request.conversation_id.startswith(('local-', 'temp-')):
                # 先查内存
                conv = agent.get_conversation(request.conversation_id)
                if conv:
                    # ✅ 内存中存在，也要设置 _current_conversation
                    agent._current_conversation = conv.id
                else:
                    # ✅ 内存中没有，从数据库加载
                    conv = await persistence.load_conversation(request.conversation_id)
                    if conv:
                        # 恢复到 agent 内存中
                        agent._conversations[conv.id] = conv
                        agent._current_conversation = conv.id
                        logger.info("Conversation restored from database", conversation_id=conv.id)
                if not conv:
                    # 数据库中也不存在，创建新会话
                    logger.info("Conversation not found, creating new one", conversation_id=request.conversation_id)
                    title = request.message[:50] if len(request.message) > 50 else request.message
                    conv = await agent.create_conversation_async(title=title)
            
            # ✅ 前端没传 conversation_id 或是临时 ID，直接创建新会话
            if not conv:
                if request.conversation_id:
                    logger.info("Ignoring temporary conversation_id from frontend", temp_id=request.conversation_id)
                title = request.message[:50] if len(request.message) > 50 else request.message
                conv = await agent.create_conversation_async(title=title)
            
            if conv:
                current_conversation_id = conv.id
                # ✅ 新会话：应用前端预选的会话级模型配置（发送首条消息前切换的模型）
                # 必须在 process_message 之前写入，确保首条消息即使用所选模型
                if request.session_model:
                    sm = request.session_model
                    session_model_data = {
                        "provider": sm.get("provider"),
                        "model_id": sm.get("model_id"),
                        "reasoning_level": sm.get("reasoning_level"),
                        "speed_mode": sm.get("speed_mode"),
                    }
                    if session_model_data.get("provider") and session_model_data.get("model_id"):
                        conv.session_model = session_model_data
                        agent._conversations[conv.id] = conv
                        try:
                            await persistence.update_conversation_session_model(conv.id, session_model_data)
                            logger.debug("Applied pre-selected session model to new conversation",
                                       conversation_id=conv.id,
                                       provider=session_model_data["provider"],
                                       model_id=session_model_data["model_id"])
                        except Exception as e:
                            logger.warning("Failed to persist pre-selected session model", error=str(e))
            
            # ✅ 若会话为 NEW（点新建时预创建），在发送 init 事件之前激活为 ACTIVE 并设置标题
            # 必须早于 init：前端收到 init 会立即刷新会话列表，避免 NEW 会话被清理逻辑误删
            if conv and conv.status == ConversationStatus.NEW:
                new_title = request.message[:50] if request.message else conv.title
                try:
                    await persistence.activate_conversation(conv.id, new_title)
                    conv.status = ConversationStatus.ACTIVE
                    if new_title:
                        conv.title = new_title
                    agent._conversations[conv.id] = conv
                    logger.info("NEW conversation activated on message", conversation_id=conv.id)
                except Exception as e:
                    logger.warning("Failed to activate NEW conversation", conversation_id=conv.id, error=str(e))

            # ✅ 构建附件 metadata（文件上传时已直接落到会话工作区，url/subdir 已正确，无需移动）
            attachments_metadata = []
            if request.attachments:
                for att in request.attachments:
                    subdir = att.get("subdir", "uploads/staging")
                    stored_filename = att.get("stored_filename", "")
                    attachments_metadata.append({
                        "file_id": att.get("file_id", ""),
                        "name": att.get("name", ""),
                        "url": att.get("url") or f"/api/v1/chat/files/{subdir}/{stored_filename}",
                        "size": att.get("size", 0),
                        "mime_type": att.get("mime_type", ""),
                        "is_image": att.get("is_image", False),
                        "stored_filename": stored_filename,
                        "subdir": subdir,
                    })
            
            # ✅ 发送 init 事件（附件 url 已在上传时确定，前端可直接使用）
            if conv:
                init_payload = {"type": "init", "conversation_id": current_conversation_id}
                if attachments_metadata:
                    init_payload["attachments"] = attachments_metadata
                yield f"data: {json.dumps(init_payload, ensure_ascii=False)}\n\n"
            
            # ✅ 构建用户消息的 metadata（含附件信息）
            msg_metadata = dict(request.metadata) if request.metadata else {}
            if attachments_metadata:
                msg_metadata["attachments"] = attachments_metadata
            
            async for msg in agent.process_message(
                message=request.message,
                conversation_id=current_conversation_id,  # ✅ 使用刚创建的真实会话ID
                metadata=msg_metadata or None,  # ✅ 传递附件 metadata
                consume_user_message_id=request.consume_user_message_id,  # ✅ 定时任务注入消息去重
            ):
                # tool 消息特殊处理：使用 tool_call_id 作为 id，以便前端匹配
                msg_id = msg.tool_call_id if msg.role.value == "tool" and msg.tool_call_id else msg.id
                
                # ✅ 记录当前会话ID（第一条消息时获取）
                if current_conversation_id is None:
                    conv = agent.get_current_conversation()
                    if conv:
                        current_conversation_id = conv.id
                
                # ✅ 过滤系统工具调用（如 todo_write），不发送给前端
                filtered_tool_calls = None
                if msg.tool_calls:
                    filtered_tool_calls = [
                        tc for tc in msg.tool_calls
                        if not tool_registry.is_system_tool(
                            tc.get("function", {}).get("name", "") if isinstance(tc, dict) else tc.function.name
                        )
                    ]
                
                data = {
                    "id": msg_id,
                    "role": msg.role,
                    "content": msg.content,
                    "message_type": msg.message_type,
                    "name": msg.name,  # 工具名称
                    "tool_calls": filtered_tool_calls,  # ✅ 过滤后的工具调用列表
                    "conversation_id": current_conversation_id,  # ✅ 前端可用于取消请求
                }
                # ✅ 仅在有实质内容时发送 metadata，避免空对象覆盖前端已有的 tts/music 流式数据
                if msg.metadata:
                    data["metadata"] = msg.metadata
                yield f"data: {json.dumps(data, default=str)}\n\n"
            
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error("SSE stream error", error=str(e), exc_info=True)
        finally:
            # ✅ 客户端断开连接时，通知 Agent 停止生成
            if current_conversation_id:
                agent.cancel_request(current_conversation_id)
                logger.info("SSE client disconnected, request cancelled", conversation_id=current_conversation_id)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )


@router.post("/cancel")
async def cancel_generation(
    agent_id: str,
    request: dict,
):
    """取消Agent生成"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    conversation_id = request.get("conversation_id")
    if not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id is required")
    
    agent.cancel_request(conversation_id)
    return {"status": "cancelled", "conversation_id": conversation_id}


@router.websocket("/ws/{agent_id}")
async def websocket_chat(websocket: WebSocket, agent_id: str):
    """WebSocket实时对话"""
    await websocket.accept()
    
    agent = task_service.get_agent(agent_id)
    if not agent:
        await websocket.close(code=4004, reason="Agent not found")
        return
    
    try:
        while True:
            # 接收消息
            data = await websocket.receive_json()
            message_type = data.get("type", "message")
            
            if message_type == "message":
                # 处理聊天消息
                message = data.get("message", "")
                conversation_id = data.get("conversation_id")
                
                async for msg in agent.process_message(
                    message=message,
                    conversation_id=conversation_id,
                ):
                    await websocket.send_json({
                        "type": "message",
                        "data": {
                            "id": msg.id,
                            "role": msg.role,
                            "content": msg.content,
                            "message_type": msg.message_type,
                            "created_at": msg.created_at.isoformat(),
                        },
                    })
                
                # 发送完成标记
                await websocket.send_json({"type": "done"})
            
            elif message_type == "ping":
                await websocket.send_json({"type": "pong"})
            
            elif message_type == "get_conversations":
                conversations = agent.list_conversations()
                await websocket.send_json({
                    "type": "conversations",
                    "data": [
                        {
                            "id": c.id,
                            "title": c.title,
                            "message_count": len(c.messages),
                        }
                        for c in conversations
                    ],
                })
    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.close(code=1011, reason=str(e))


@router.get("/conversations/{conversation_id}")
async def get_conversation(agent_id: str, conversation_id: str):
    """获取会话详情"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    conversation = agent.get_conversation(conversation_id)
    
    # ✅ 内存中不存在时，从数据库加载（服务器重启后内存清空的场景）
    if not conversation:
        conversation = await persistence.load_conversation(conversation_id)
        if conversation:
            agent._conversations[conversation.id] = conversation
            logger.info("Conversation restored from database", conversation_id=conversation_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    return {
        "id": conversation.id,
        "agent_id": conversation.agent_id,
        "title": conversation.title,
        "status": conversation.status,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "message_type": m.message_type,
                "created_at": m.created_at.isoformat(),
                # ✅ 过滤系统工具调用（如 todo_write），不显示在前端
                "tool_calls": [
                    tc for tc in (m.tool_calls or [])
                    if not tool_registry.is_system_tool(
                        tc.get("function", {}).get("name", "") if isinstance(tc, dict) else tc.function.name
                    )
                ] if m.tool_calls else [],
                "tool_call_id": m.tool_call_id,  # ✅ 添加工具调用ID（tool消息）
                "name": m.name,  # ✅ 添加名称（tool消息的工具名）
                "metadata": m.metadata,  # ✅ 添加元数据（含 dag、structured_content 等前端专用数据）
            }
            for m in conversation.messages
            # ✅ 过滤系统工具的 tool 消息（不显示在前端）
            if not (m.role.value == "tool" and m.name and tool_registry.is_system_tool(m.name))
            # ✅ 过滤压缩标记消息
            and m.message_type not in (
                MessageType.COMPRESS_BUDGET_BOUNDARY,
                MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY,
                MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY,
                MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY,
                MessageType.ACTIVE_COMPACT_BOUNDARY,
                MessageType.MEMORY,
                MessageType.CONTINUATION,
                MessageType.BACKGROUND_TASK,
                MessageType.TEAMMATE
            )
        ],
        "context_tokens": conversation.context_tokens,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


@router.post("/conversations/{conversation_id}/compress")
async def compress_conversation(agent_id: str, conversation_id: str):
    """压缩会话上下文"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    conversation = agent.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # 如果有压缩器，执行压缩
    if hasattr(agent, 'compressor') and agent.compressor:
        compressed = await agent.compressor.compress(conversation.messages)
        return {
            "original_tokens": compressed.original_tokens,
            "compressed_tokens": compressed.compressed_tokens,
            "compression_ratio": compressed.compression_ratio,
            "summary": compressed.summary,
        }
    
    return {"message": "Compression not available"}


@router.put("/conversations/{conversation_id}")
async def rename_conversation(agent_id: str, conversation_id: str, request: dict):
    """重命名会话"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    title = request.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    
    # 更新内存中的会话
    conversation = agent.get_conversation(conversation_id)
    if conversation:
        conversation.title = title
    
    # 更新数据库
    await persistence.rename_conversation(conversation_id, title)
    
    return {"message": "Conversation renamed", "title": title}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(agent_id: str, conversation_id: str):
    """删除会话"""
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    if conversation_id in agent._conversations:
        del agent._conversations[conversation_id]
    
    # 从数据库删除
    await persistence.delete_conversation(conversation_id)
    
    # ✅ 清理记忆系统关联资源（会话记忆目录 + 预取状态）
    try:
        from app.core.memory_system.session_memory import session_memory_manager
        from app.core.memory_system.prefetch import memory_prefetch_manager
        session_memory_manager.clear_conversation(conversation_id)
        memory_prefetch_manager.clear_conversation(conversation_id)
    except Exception as e:
        logger.warning("Failed to cleanup memory resources for conversation", conversation_id=conversation_id, error=str(e))
    
    # ✅ 清理工作区隔离关联资源（会话级 worktree 工作区与分支）
    try:
        from app.core.workspace_isolation import workspace_isolation
        if workspace_isolation.get_scope() == "session":
            await workspace_isolation.remove_session_workspace(
                agent.config.name, conversation_id
            )
    except Exception as e:
        logger.warning("Failed to cleanup session workspace", conversation_id=conversation_id, error=str(e))
    
    # ✅ 清理文件型任务系统关联资源（会话归属的任务目录 + 团队任务目录）
    # 仅在未设置 HAPPY_CLAW_TASK_LIST_ID 时任务列表才归属会话，此时整体删除目录
    try:
        import os
        if not os.environ.get("HAPPY_CLAW_TASK_LIST_ID", "").strip():
            from app.core.task_system.store import task_store
            task_store.remove_task_list(conversation_id)
            # 清理该会话下所有团队任务目录（team-{session_id}-{team_name}）
            task_store.remove_task_lists_by_prefix(f"team-{conversation_id}-")
    except Exception as e:
        logger.warning("Failed to cleanup task list for conversation", conversation_id=conversation_id, error=str(e))
    
    # ✅ 清理后台任务关联资源
    try:
        from app.core.background_jobs import background_job_manager
        await background_job_manager.cleanup_session(conversation_id)
    except Exception as e:
        logger.warning("Failed to cleanup background jobs for conversation", conversation_id=conversation_id, error=str(e))
    
    # ✅ 清理定时任务关联资源（会话删除后其定时任务全部移除）
    try:
        from app.core.scheduler.store import schedule_store
        schedule_store.cleanup_session(conversation_id)
    except Exception as e:
        logger.warning("Failed to cleanup scheduled tasks for conversation", conversation_id=conversation_id, error=str(e))
    
    # ✅ 清理团队协作资源（停止队友任务、删除团队目录与团队任务目录）
    try:
        from app.core.team_system.manager import team_manager
        await team_manager.cleanup_session(conversation_id)
    except Exception as e:
        logger.warning("Failed to cleanup team resources for conversation", conversation_id=conversation_id, error=str(e))

    # ✅ 关闭活跃TTS会话连接
    try:
        from app.tools.tts import remove_tts_manager
        remove_tts_manager(conversation_id)
    except Exception as e:
        logger.warning("Failed to cleanup TTS resources for conversation", conversation_id=conversation_id, error=str(e))
    
    return {"message": "Conversation deleted"}


@router.get("/conversations")
async def list_all_conversations(agent_id: str, exclude_id: Optional[str] = None):
    """获取所有会话列表（从数据库）

    会清理无用的 NEW 会话（点了新建但从未发送消息）：删除 DB 记录 + 移除其工作区目录。
    exclude_id 用于保护前端当前正在使用的 NEW 会话不被误删。
    NEW 会话始终不出现在返回列表中。
    """
    # ✅ 清理无用 NEW 会话（保护当前正在使用的 exclude_id）
    try:
        new_convs = await persistence.find_new_conversations(agent_id)
        if new_convs:
            agent = task_service.get_agent(agent_id)
            agent_name = getattr(agent.config, "name", None) if agent else None
            from app.core.workspace_isolation import workspace_isolation
            for c in new_convs:
                if exclude_id and c.id == exclude_id:
                    continue  # 保护当前会话
                await persistence.delete_conversation(c.id)
                if agent_name:
                    try:
                        await workspace_isolation.remove_session_workspace(agent_name, c.id)
                    except Exception as e:
                        logger.warning("Failed to remove workspace for NEW conversation", conversation_id=c.id, error=str(e))
                logger.info("Cleaned up unused NEW conversation", conversation_id=c.id)
    except Exception as e:
        logger.warning("Failed to cleanup NEW conversations", agent_id=agent_id, error=str(e))

    conversations = await persistence.load_conversations_for_agent(agent_id)
    # ✅ NEW 会话不展示在列表中
    conversations = [c for c in conversations if c.status != ConversationStatus.NEW]
    return {
        "conversations": [
            {
                "id": c.id,
                "title": c.title,
                "status": c.status.value,
                "message_count": len(c.messages),
                "context_tokens": c.context_tokens,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }
            for c in conversations
        ],
        "total": len(conversations),
    }


@router.post("/conversations/new")
async def create_new_conversation(agent_id: str):
    """创建（或复用）一条 status=NEW 的新会话

    - 若该 agent 已存在 NEW 会话，直接复用返回，避免创建冗余会话。
    - 否则创建新会话（同时创建会话工作区），返回 conversation_id。
    前端点击“新建会话”或进入空聊天页时调用。
    """
    agent = task_service.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # ✅ 复用已存在的 NEW 会话
    existing = await persistence.find_new_conversation(agent_id)
    if existing:
        if not agent.get_conversation(existing.id):
            # 加载完整会话（含系统提示消息），避免重启后内存会话丢失 system prompt
            full = await persistence.load_conversation(existing.id)
            agent._conversations[existing.id] = full or existing
        return {"conversation_id": existing.id, "status": "new", "title": existing.title, "reused": True}

    # ✅ 创建 NEW 会话（内部会创建会话工作区）
    conv = await agent.create_conversation_async(title=None, status=ConversationStatus.NEW)
    return {"conversation_id": conv.id, "status": "new", "title": conv.title, "reused": False}


@router.put("/conversations/{conversation_id}/model")
async def set_session_model(
    agent_id: str,
    conversation_id: str,
    config: SessionModelConfig,
):
    """设置会话级模型配置（✅ 同时更新数据库与内存）"""
    # 验证会话存在
    conversation = await persistence.load_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # 更新session_model字段
    session_model_data = {
        "provider": config.provider,
        "model_id": config.model_id,
        "reasoning_level": config.reasoning_level,
        "speed_mode": config.speed_mode,
    }
    
    # ✅ 先更新数据库
    await persistence.update_conversation_session_model(conversation_id, session_model_data)
    
    # ✅ 再同步更新内存中的会话对象（避免每次读取都查DB）
    agent = task_service.get_agent(agent_id)
    if agent:
        conv = agent.get_conversation(conversation_id)
        if conv:
            # 更新内存中的会话对象
            conv.session_model = session_model_data
            logger.debug(
                "Memory cache updated for session model",
                conversation_id=conversation_id,
                provider=config.provider,
                model_id=config.model_id,
            )
    
    logger.info(
        "Session model config updated",
        conversation_id=conversation_id,
        provider=config.provider,
        model_id=config.model_id,
    )
    
    return {"message": "Session model config updated", "config": session_model_data}


@router.get("/conversations/{conversation_id}/model")
async def get_session_model(
    agent_id: str,
    conversation_id: str,
):
    """获取会话级模型配置"""
    # 验证会话存在
    conversation = await persistence.load_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # 返回session_model配置（如果不存在则返回None）
    session_model = getattr(conversation, 'session_model', None) or (conversation.metadata or {}).get('session_model')
    
    return {
        "conversation_id": conversation_id,
        "session_model": session_model,
    }


# ===== 文件上传与访问 =====

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico"}


def _is_image_file(filename: str, mime_type: str = "") -> bool:
    """判断文件是否为图片"""
    if mime_type and mime_type.startswith("image/"):
        return True
    ext = Path(filename).suffix.lower()
    return ext in _IMAGE_EXTENSIONS


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    conversation_id: Optional[str] = Form(None),
    agent_id: Optional[str] = Form(None),
):
    """上传文件（图片或普通文件）——直接存入会话工作区目录 agents/{agent_name}/{conversation_id}/"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    file_id = str(uuid.uuid4())
    # 清理文件名，保留扩展名
    safe_name = Path(file.filename).name.replace(" ", "_")
    stored_filename = f"{file_id}_{safe_name}"

    # ✅ 解析目标目录：优先会话工作区（前端已先创建 NEW 会话）
    agent_name = None
    if agent_id:
        agent = task_service.get_agent(agent_id)
        agent_name = getattr(agent.config, "name", None) if agent else None

    if conversation_id and agent_name:
        subdir = f"agents/{agent_name}/{conversation_id}"
        target_dir = AGENTS_ROOT / agent_name / conversation_id
    else:
        # 兜底：无会话上下文时仍用 staging（正常流程不会走到）
        subdir = "uploads/staging"
        target_dir = STAGING_DIR

    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / stored_filename

    # 写入文件
    content = await file.read()
    file_path.write_bytes(content)

    # 确定 mime_type
    mime_type = file.content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    is_image = _is_image_file(safe_name, mime_type)

    # 构建访问 URL（相对于 WORKSPACE_ROOT 的路径）
    url = f"/api/v1/chat/files/{subdir}/{stored_filename}"

    logger.info(
        "File uploaded",
        file_id=file_id,
        filename=safe_name,
        size=len(content),
        is_image=is_image,
        subdir=subdir,
    )

    return {
        "file_id": file_id,
        "url": url,
        "name": safe_name,
        "size": len(content),
        "mime_type": mime_type,
        "is_image": is_image,
        "stored_filename": stored_filename,
        "subdir": subdir,
    }


@router.delete("/upload/{stored_filename}")
async def delete_uploaded_file(stored_filename: str, subdir: Optional[str] = None):
    """删除已上传的待发送文件（用户取消附件时调用）——支持 staging 与会话工作区"""
    safe_name = Path(stored_filename).name
    if subdir:
        base = (WORKSPACE_ROOT_PATH / subdir).resolve()
        # 安全校验：必须在 WORKSPACE_ROOT 内，防止路径遍历
        if not str(base).startswith(str(WORKSPACE_ROOT_PATH.resolve())):
            raise HTTPException(status_code=403, detail="禁止访问")
        file_path = base / safe_name
    else:
        file_path = STAGING_DIR / safe_name

    if not file_path.exists():
        return {"status": "not_found"}

    try:
        file_path.unlink()
        logger.info("Uploaded file deleted", filename=safe_name, subdir=subdir)
        return {"status": "deleted"}
    except Exception as e:
        logger.warning("Failed to delete uploaded file", filename=safe_name, error=str(e))
        raise HTTPException(status_code=500, detail="删除失败")


@router.get("/files/{file_path:path}")
async def serve_file(file_path: str, request: Request):
    """提供上传文件的访问（图片/音频内联显示，其他文件触发下载）"""
    # 安全检查：防止路径遍历（解析相对于 WORKSPACE_ROOT）
    resolved = (WORKSPACE_ROOT_PATH / file_path).resolve()
    if not str(resolved).startswith(str(WORKSPACE_ROOT_PATH.resolve())):
        raise HTTPException(status_code=403, detail="禁止访问")

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    # 确定 mime_type
    mime_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    is_image = mime_type.startswith("image/")
    is_audio = mime_type.startswith("audio/")

    # 音频文件支持 Range 请求（用于播放器拖动进度条）
    if is_audio:
        file_size = resolved.stat().st_size
        range_header = request.headers.get("range")

        if range_header:
            # 解析 Range: bytes=start-end
            try:
                range_spec = range_header.replace("bytes=", "")
                parts = range_spec.split("-")
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1
                end = min(end, file_size - 1)
                content_length = end - start + 1

                def iter_range():
                    with open(resolved, "rb") as f:
                        f.seek(start)
                        remaining = content_length
                        while remaining > 0:
                            chunk_size = min(65536, remaining)
                            data = f.read(chunk_size)
                            if not data:
                                break
                            remaining -= len(data)
                            yield data

                return StreamingResponse(
                    iter_range(),
                    status_code=206,
                    media_type=mime_type,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(content_length),
                        "Content-Disposition": "inline",
                    },
                )
            except (ValueError, IndexError):
                pass  # Range 解析失败，回退到完整响应

        # 无 Range 头，返回完整文件（支持 Range）
        return FileResponse(
            path=str(resolved),
            media_type=mime_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Disposition": "inline",
            },
        )

    # 图片内联显示，其他文件触发下载
    headers = {}
    if not is_image:
        headers["Content-Disposition"] = f'attachment; filename="{resolved.name}"'

    return FileResponse(
        path=str(resolved),
        media_type=mime_type,
        headers=headers,
    )
