"""斜杠命令 API - 获取配置与执行命令"""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import structlog

from app.config import settings
from app.models.schemas.chat import Message, MessageRole, MessageType, ConversationStatus
from app.services.persistence import persistence

logger = structlog.get_logger()

router = APIRouter()


# ===== 请求/响应模型 =====

class SlashCommandExecuteRequest(BaseModel):
    """执行斜杠命令请求"""
    title: str  # 命令分类，如 "tool", "session"
    command: str  # 命令名，如 "tools", "test"
    args: Dict[str, Any] = Field(default_factory=dict)  # 关键字参数
    conversation_id: Optional[str] = None
    agent_id: Optional[str] = None
    raw_input: Optional[str] = None  # 用户原始输入文本，如 "/tools verbose"
    # ✅ 输入框已加载的附件（图片）元数据，image_generate 命令会将其填充到 images 参数
    attachments: Optional[List[Dict[str, Any]]] = None


# ===== API 端点 =====

@router.get("/")
async def get_slash_commands(agent_id: Optional[str] = None):
    """获取斜杠命令配置（从内存读取，无需磁盘IO）
    
    可选参数 agent_id：传入时会额外返回该 agent 的 mediaModels 配置，
    前端可据此决定是否显示 image_generate 等依赖媒体模型的命令。
    """
    data = settings.to_dict()
    commands = data.get("slash_commands", {})
    
    # 获取 agent 的 mediaModels 配置（用于前端条件性显示命令）
    media_models = None
    has_pdf = False
    if agent_id:
        from app.services.task_service import task_service
        agent = task_service.get_agent(agent_id)
        if agent:
            agent_name = getattr(agent.config, 'name', None)
            if agent_name:
                entries = (data.get("agent", {}) or {}).get("entries", {}) or {}
                entry = entries.get(agent_name, {}) or {}
                media_models = entry.get("mediaModels")
                if media_models and hasattr(media_models, 'to_dict'):
                    media_models = media_models.to_dict()
                # ✅ agent 是否配置了 pdf（决定是否显示 /pdf 命令）
                has_pdf = bool(entry.get("pdf"))
    
    return {"commands": commands, "media_models": media_models, "has_pdf": has_pdf}


@router.post("/execute")
async def execute_slash_command(request: SlashCommandExecuteRequest):
    """执行斜杠命令"""
    title = request.title
    command = request.command
    args = request.args

    logger.info(
        "Executing slash command",
        title=title,
        command=command,
        args=args,
        conversation_id=request.conversation_id,
    )

    # 路由到对应处理器
    handler_key = f"{title}.{command}"
    handler = _COMMAND_HANDLERS.get(handler_key)

    if handler:
        try:
            result = await handler(args, request)
        except Exception as e:
            logger.error("Slash command execution failed", command=handler_key, error=str(e), exc_info=True)
            result = f"命令执行失败: {str(e)}"
    else:
        result = "该命令暂未实现"

    # 持久化消息（如果有 conversation_id）
    if request.conversation_id:
        raw_input = request.raw_input or f"/{command} {' '.join(str(v) for v in args.values())}".strip()

        # ✅ 若会话为 NEW（点新建时预创建），激活为 ACTIVE 并设置标题
        activated = False
        try:
            from app.services.task_service import task_service
            _agent = task_service.get_agent(request.agent_id) if request.agent_id else None
            _conv = _agent.get_conversation(request.conversation_id) if _agent else None
            if _conv is None:
                _conv = await persistence.load_conversation(request.conversation_id)
            if _conv is not None and _conv.status == ConversationStatus.NEW:
                _title = raw_input[:50]
                await persistence.activate_conversation(request.conversation_id, _title)
                _conv.status = ConversationStatus.ACTIVE
                _conv.title = _title
                if _agent:
                    _agent._conversations[_conv.id] = _conv
                activated = True
        except Exception as e:
            logger.warning("Failed to activate NEW conversation for slash command", conversation_id=request.conversation_id, error=str(e))

        # ✅ 将附件图片存入消息 metadata，便于刷新后回显
        req_metadata: Dict[str, Any] = {}
        if request.attachments:
            req_metadata["attachments"] = [
                {
                    "file_id": att.get("file_id", ""),
                    "name": att.get("name", ""),
                    "url": att.get("url") or f"/api/v1/chat/files/{att.get('subdir', 'uploads/staging')}/{att.get('stored_filename', '')}",
                    "size": att.get("size", 0),
                    "mime_type": att.get("mime_type", ""),
                    "is_image": att.get("is_image", False),
                    "stored_filename": att.get("stored_filename", ""),
                    "subdir": att.get("subdir", ""),
                }
                for att in request.attachments
            ]

        # 用户命令消息
        request_msg = Message(
            id=str(uuid.uuid4()),
            role=MessageRole.USER,
            content=raw_input,
            message_type=MessageType.SLASH_COMMAND_REQUEST,
            metadata=req_metadata,
            created_at=datetime.utcnow(),
        )
        await persistence.save_message(request.conversation_id, request_msg)

        # 命令结果消息
        # ✅ 如果 TTS 合成成功，保存为 audio_generated 类型消息（带 metadata）
        if isinstance(result, dict) and result.get("type") == "audio_generated":
            response_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.ASSISTANT,
                content=result.get("text", ""),
                message_type=MessageType.AUDIO_GENERATED,
                metadata={
                    "audio_url": result.get("audio_url", ""),
                },
                created_at=datetime.utcnow(),
            )
        elif isinstance(result, dict) and result.get("type") == "files_generated":
            # ✅ 文件生成类命令（如 /pdf 解析）：以附件文件图标呈现，可点击下载
            response_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.ASSISTANT,
                content=result.get("text", ""),
                message_type=MessageType.SLASH_COMMAND_RESPONSE,
                metadata={"attachments": result.get("files", [])},
                created_at=datetime.utcnow(),
            )
        else:
            response_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.ASSISTANT,
                content=result if isinstance(result, str) else str(result),
                message_type=MessageType.SLASH_COMMAND_RESPONSE,
                created_at=datetime.utcnow(),
            )
        await persistence.save_message(request.conversation_id, response_msg)

        # ✅ 同步更新 agent 内存中的会话（刷新页面时 chat.py 优先从内存读取）
        if request.agent_id:
            from app.services.task_service import task_service
            agent = task_service.get_agent(request.agent_id)
            if agent:
                conv = agent.get_conversation(request.conversation_id)
                if conv:
                    conv.messages.append(request_msg)
                    conv.messages.append(response_msg)

        return {
            "success": True,
            "result": result,
            "request_message": {
                "id": request_msg.id,
                "role": request_msg.role.value,
                "content": request_msg.content,
                "message_type": request_msg.message_type.value,
                "metadata": request_msg.metadata,
                "created_at": request_msg.created_at.isoformat(),
            },
            "response_message": {
                "id": response_msg.id,
                "role": response_msg.role.value,
                "content": response_msg.content,
                "message_type": response_msg.message_type.value,
                "metadata": response_msg.metadata,
                "created_at": response_msg.created_at.isoformat(),
            },
        }

    return {
        "success": True,
        "result": result,
    }


# ===== 命令处理器 =====

async def _handle_session_name(args: Dict[str, Any], request: SlashCommandExecuteRequest) -> str:
    """session.name - 重命名当前会话"""
    title = args.get("title", "").strip()
    if not title:
        return "错误: 请提供新的会话名称"
    if not request.conversation_id:
        return "错误: 没有活跃的会话"

    await persistence.rename_conversation(request.conversation_id, title)

    # 同步更新 agent 内存中的会话标题
    if request.agent_id:
        from app.services.task_service import task_service
        agent = task_service.get_agent(request.agent_id)
        if agent:
            conv = agent.get_conversation(request.conversation_id)
            if conv:
                conv.title = title

    return f"会话已重命名为: {title}"


async def _handle_tool_tools(args: Dict[str, Any], request: SlashCommandExecuteRequest) -> str:
    """tool.tools - 列出当前 agent 运行时可见的工具"""
    from app.services.task_service import task_service

    mode = args.get("mode", "compact")

    # 获取当前 agent
    agent = task_service.get_agent(request.agent_id) if request.agent_id else None

    # 优先从 ToolSearchEngine 获取运行时可见工具（always_load + 当前会话已加载）
    tool_search_engine = getattr(agent, '_tool_search_engine', None) if agent else None
    if tool_search_engine:
        # get_tools_for_llm 返回 function definition dicts
        tool_defs = tool_search_engine.get_tools_for_llm(request.conversation_id)
        tool_names = [d["function"]["name"] for d in tool_defs]

        if not tool_names:
            return "当前没有可用的工具"

        lines = []
        if mode == "verbose":
            lines.append(f"# 运行时可见工具 (共 {len(tool_names)} 个)\n")
            registry = agent.tool_registry
            for name in tool_names:
                reg = registry.get(name)
                if reg:
                    params = ""
                    if reg.function_definition and reg.function_definition.parameters:
                        props = reg.function_definition.parameters.get("properties", {})
                        if props:
                            params = f" ({', '.join(props.keys())})"
                    lines.append(f"## {reg.name}")
                    lines.append(f"- 描述: {reg.description}")
                    lines.append(f"- 超时: {reg.timeout_seconds}s")
                    if params:
                        lines.append(f"- 参数: {params}")
                else:
                    lines.append(f"## {name}")
                lines.append("")
        else:
            lines.append(f"运行时可见工具 ({len(tool_names)} 个):")
            for name in tool_names:
                lines.append(f"  - {name}")

        # 附加 deferred 工具统计
        deferred_count = tool_search_engine.deferred_count
        loaded_count = len(tool_search_engine.loaded_tools_for(request.conversation_id))
        if deferred_count > 0:
            lines.append(f"\n(另有 {deferred_count} 个延迟工具，当前会话已加载 {loaded_count} 个)")

        return "\n".join(lines)

    # 回退：无 ToolSearchEngine 时使用 agent 的 tool_registry
    if agent and hasattr(agent, 'tool_registry'):
        registry = agent.tool_registry
    else:
        from app.tools.registry import tool_registry as registry

    tools = registry.list_tools()

    if not tools:
        return "当前没有可用的工具"

    lines = []
    if mode == "verbose":
        lines.append(f"# 可用工具列表 (共 {len(tools)} 个)\n")
        for t in tools:
            params = ""
            if t.function_definition and t.function_definition.parameters:
                props = t.function_definition.parameters.get("properties", {})
                if props:
                    params = f" ({', '.join(props.keys())})"
            lines.append(f"## {t.name}")
            lines.append(f"- 描述: {t.description}")
            lines.append(f"- 超时: {t.timeout_seconds}s")
            if params:
                lines.append(f"- 参数: {params}")
            lines.append("")
    else:
        lines.append(f"可用工具 ({len(tools)} 个):")
        for t in tools:
            lines.append(f"  - {t.name}")

    return "\n".join(lines)


async def _handle_tool_test(args: Dict[str, Any], request: SlashCommandExecuteRequest) -> str:
    """tool.test - 测试命令效果"""
    title = args.get("title", "无标题")
    desc = args.get("desc", "无描述")
    other = args.get("other", "other")

    return (
        f"**测试命令执行成功**\n\n"
        f"- 标题: {title}\n"
        f"- 描述: {desc}\n"
        f"- 其他参数: {other}\n"
        f"- 会话ID: {request.conversation_id or '无'}\n"
        f"- Agent ID: {request.agent_id or '无'}"
    )


async def _handle_tool_example(args: Dict[str, Any], request: SlashCommandExecuteRequest) -> str:
    """tool.example - 示例命令"""
    example_arg = args.get("example_arg", "")
    example_arg2 = args.get("example_arg2", "value")

    return (
        f"**Example command executed**\n\n"
        f"- example_arg: {example_arg}\n"
        f"- example_arg2: {example_arg2}"
    )


async def _handle_image_generate(args: Dict[str, Any], request: SlashCommandExecuteRequest) -> str:
    """图片生成命令 - 调用 image_generate 工具"""
    from app.services.task_service import task_service
    from app.tools.image_generate import image_generate, _external_agent_context

    # 获取 agent 上下文
    agent = task_service.get_agent(request.agent_id) if request.agent_id else None
    if not agent:
        return "错误: 无法获取当前 Agent"

    agent_name = getattr(agent.config, 'name', 'unknown')
    conversation_id = request.conversation_id or agent._current_conversation or "default"

    # ✅ 构建 images：以命令参数中的 images 为基础，追加输入框附件图片的 /api/v1/chat/files URL
    # 使用文件访问 URL 而非服务器本地绝对路径，避免暴露本地路径（手动指定的本地路径仍支持）
    images_arg = args.get("images") or []
    if isinstance(images_arg, str):
        images_arg = [images_arg] if images_arg else []
    images_arg = list(images_arg)
    if request.attachments:
        for att in request.attachments:
            if not att.get("is_image", False):
                continue
            url = att.get("url") or (
                f"/api/v1/chat/files/{att.get('subdir', 'uploads/staging')}/{att.get('stored_filename', '')}"
                if att.get("stored_filename") else ""
            )
            if url and url not in images_arg:
                images_arg.append(url)

    # 设置外部上下文（让 image_generate 内部能获取 agent 信息）
    token = _external_agent_context.set((agent_name, conversation_id, agent))
    try:
        result = await image_generate(
            prompt=args.get("prompt", ""),
            action=args.get("action", "generate"),
            model=args.get("model", ""),
            image=args.get("image", ""),
            images=images_arg or None,
            size=args.get("size", ""),
            aspectRatio=args.get("aspectRatio", ""),
            quality=args.get("quality", ""),
            outputFormat=args.get("outputFormat", ""),
            background=args.get("background", ""),
            count=int(args.get("count", 1)),
            timeoutMs=int(args.get("timeoutMs", 0)),
            filename=args.get("filename", ""),
        )
    finally:
        _external_agent_context.reset(token)

    # 格式化返回结果
    if not result.get("success"):
        return f"图片生成失败: {result.get('error', '未知错误')}"

    action = args.get("action", "generate")
    if action == "list":
        models = result.get("models", [])
        if not models:
            return "当前没有配置可用的图片生成模型"
        lines = ["**可用图片生成模型:**\n"]
        for m in models:
            lines.append(f"- {m.get('model_ref', '?')} ({m.get('provider', '?')})")
            if m.get('capabilities'):
                lines.append(f"  能力: {', '.join(m['capabilities'])}")
        return "\n".join(lines)
    elif action == "status":
        jobs = result.get("jobs", [])
        if not jobs:
            return "当前没有活动的图片生成任务"
        lines = ["**图片生成任务状态:**\n"]
        for j in jobs:
            lines.append(f"- [{j.get('status')}] {j.get('job_id')}: {j.get('prompt', '')[:50]}")
        return "\n".join(lines)
    else:
        # generate
        return (
            f"✅ {result.get('message', '图片生成任务已提交')}\n\n"
            f"- 任务ID: {result.get('job_id', '?')}\n"
            f"- 模型: {result.get('model', '?')}"
        )


async def _handle_music_generate(args: Dict[str, Any], request: SlashCommandExecuteRequest) -> str:
    """音乐生成命令 - 调用 music_generate 工具"""
    from app.services.task_service import task_service
    from app.tools.music_generate import music_generate, _external_agent_context

    # 获取 agent 上下文
    agent = task_service.get_agent(request.agent_id) if request.agent_id else None
    if not agent:
        return "错误: 无法获取当前 Agent"

    agent_name = getattr(agent.config, 'name', 'unknown')
    conversation_id = request.conversation_id or agent._current_conversation or "default"

    # ✅ 构建 references：以命令参数中的 references 为基础，追加输入框附件的文件 URL
    # 注意：不限定媒体类型，由各供应商适配器层面做校验（如 yinchao 仅支持音频）
    references_arg = args.get("references") or []
    if isinstance(references_arg, str):
        references_arg = [references_arg] if references_arg else []
    references_arg = list(references_arg)
    if request.attachments:
        for att in request.attachments:
            url = att.get("url") or (
                f"/api/v1/chat/files/{att.get('subdir', 'uploads/staging')}/{att.get('stored_filename', '')}"
                if att.get("stored_filename") else ""
            )
            if url and url not in references_arg:
                references_arg.append(url)

    # 设置外部上下文（让 music_generate 内部能获取 agent 信息）
    token = _external_agent_context.set((agent_name, conversation_id, agent))
    try:
        result = await music_generate(
            prompt=args.get("prompt", "") or "",
            action=args.get("action", "generate"),
            mode=args.get("mode", "normal") or "normal",
            model=args.get("model") or "",
            lyrics=args.get("lyrics", "") or "",
            n=int(args.get("n", 1) or 1),
            references=references_arg or None,
            durationSeconds=int(args.get("durationSeconds", 0) or 0),
            format=args.get("format", "") or "",
            gender=args.get("gender", "") or "",
            stream=bool(args.get("stream", False)),
            filename=args.get("filename", "") or "",
        )
    finally:
        _external_agent_context.reset(token)

    # 格式化返回结果
    if not result.get("success"):
        return f"音乐生成失败: {result.get('error', '未知错误')}"

    action = args.get("action", "generate")
    if action == "list":
        models = result.get("models", [])
        if not models:
            return "当前没有配置可用的音乐生成模型"
        lines = ["**可用音乐生成模型:**\n"]
        for m in models:
            if m.get("error"):
                lines.append(f"- {m.get('model_ref', '?')} ({m.get('error')})")
                continue
            lines.append(f"- {m.get('model_ref', '?')} ({m.get('provider', '?')}) [{m.get('role', '')}]")
            modes = m.get("supported_modes") or []
            if modes:
                lines.append(f"  支持模式: {', '.join(modes)}")
        return "\n".join(lines)
    elif action == "status":
        jobs = result.get("jobs", [])
        if not jobs:
            return "当前没有活动的音乐生成任务"
        lines = ["**音乐生成任务状态:**\n"]
        for j in jobs:
            lines.append(f"- [{j.get('status')}] {j.get('job_id')}: {j.get('prompt', '')[:50]}")
        return "\n".join(lines)
    else:
        # generate
        return (
            f"✅ {result.get('message', '音乐生成任务已提交')}\n\n"
            f"- 任务ID: {result.get('job_id', '?')}\n"
            f"- 模型: {result.get('model', '?')}\n"
            f"- 模式: {result.get('mode', '?')}"
        )


async def _handle_video_generate(args: Dict[str, Any], request: SlashCommandExecuteRequest) -> str:
    """视频生成命令 - 调用 video_generate 工具"""
    from app.services.task_service import task_service
    from app.tools.video_generate import video_generate, _external_agent_context

    # 获取 agent 上下文
    agent = task_service.get_agent(request.agent_id) if request.agent_id else None
    if not agent:
        return "错误: 无法获取当前 Agent"

    agent_name = getattr(agent.config, 'name', 'unknown')
    conversation_id = request.conversation_id or agent._current_conversation or "default"

    # ✅ 规范化列表参数
    def _as_list(val):
        if not val:
            return []
        if isinstance(val, str):
            return [val]
        return list(val)

    images_arg = _as_list(args.get("images"))
    videos_arg = _as_list(args.get("videos"))
    audio_refs_arg = _as_list(args.get("audioRefs"))
    file_refs_arg = _as_list(args.get("fileRefs"))

    # ✅ 追加输入框附件：按类型分流到 images/videos/audioRefs/fileRefs（用文件 URL，避免暴露本地路径）
    if request.attachments:
        for att in request.attachments:
            url = att.get("url") or (
                f"/api/v1/chat/files/{att.get('subdir', 'uploads/staging')}/{att.get('stored_filename', '')}"
                if att.get("stored_filename") else ""
            )
            if not url:
                continue
            mime = (att.get("mime_type") or "").lower()
            if att.get("is_image") or mime.startswith("image/"):
                target = images_arg
            elif mime.startswith("video/"):
                target = videos_arg
            elif mime.startswith("audio/"):
                target = audio_refs_arg
            else:
                target = file_refs_arg
            if url not in target:
                target.append(url)

    # 设置外部上下文（让 video_generate 内部能获取 agent 信息）
    token = _external_agent_context.set((agent_name, conversation_id, agent))
    try:
        result = await video_generate(
            prompt=args.get("prompt", "") or "",
            action=args.get("action", "generate"),
            mode=args.get("mode", "normal") or "normal",
            model=args.get("model") or "",
            images=images_arg or None,
            imageRoles=_as_list(args.get("imageRoles")) or None,
            videos=videos_arg or None,
            videoRoles=_as_list(args.get("videoRoles")) or None,
            audioRefs=audio_refs_arg or None,
            audioRoles=_as_list(args.get("audioRoles")) or None,
            fileRefs=file_refs_arg or None,
            fileRoles=_as_list(args.get("fileRoles")) or None,
            linkRefs=_as_list(args.get("linkRefs")) or None,
            linkRoles=_as_list(args.get("linkRoles")) or None,
            aspectRatio=args.get("aspectRatio", "") or "",
            resolution=args.get("resolution", "") or "",
            durationSeconds=int(args.get("durationSeconds", 0) or 0),
            size=args.get("size", "") or "",
            audio=args.get("audio"),
            watermark=args.get("watermark"),
            filename=args.get("filename", "") or "",
            timeoutMs=int(args.get("timeoutMs", 0) or 0),
        )
    finally:
        _external_agent_context.reset(token)

    # 格式化返回结果
    if not result.get("success"):
        return f"视频生成失败: {result.get('error', '未知错误')}"

    action = args.get("action", "generate")
    if action == "list":
        models = result.get("models", [])
        if not models:
            return "当前没有配置可用的视频生成模型"
        lines = ["**可用视频生成模型:**\n"]
        for m in models:
            if m.get("error"):
                lines.append(f"- {m.get('model_ref', '?')} ({m.get('error')})")
                continue
            lines.append(f"- {m.get('model_ref', '?')} ({m.get('provider', '?')}) [{m.get('role', '')}]")
        return "\n".join(lines)
    elif action == "status":
        jobs = result.get("jobs", [])
        if not jobs:
            return "当前没有活动的视频生成任务"
        lines = ["**视频生成任务状态:**\n"]
        for j in jobs:
            lines.append(f"- [{j.get('status')}] {j.get('job_id')}: {j.get('prompt', '')[:50]}")
        return "\n".join(lines)
    else:
        # generate
        return (
            f"✅ {result.get('message', '视频生成任务已提交')}\n\n"
            f"- 任务ID: {result.get('job_id', '?')}\n"
            f"- 模型: {result.get('model', '?')}\n"
            f"- 模式: {result.get('mode', '?')}"
        )


async def _handle_tool_pdf(args: Dict[str, Any], request: SlashCommandExecuteRequest):
    """PDF 解析命令 - 调用 pdf_parse 工具（强制同步以取回文件结果）"""
    from app.services.task_service import task_service
    from app.tools.pdf_parse import pdf_parse, _external_agent_context

    # 获取 agent 上下文
    agent = task_service.get_agent(request.agent_id) if request.agent_id else None
    if not agent:
        return "错误: 无法获取当前 Agent"

    agent_name = getattr(agent.config, 'name', 'unknown')
    conversation_id = request.conversation_id or agent._current_conversation or "default"

    def _as_list(val):
        if not val:
            return []
        if isinstance(val, str):
            return [val]
        return list(val)

    # ✅ 构建 pdfs：args.pdfs + 输入框中 PDF 附件的 url（去重）
    pdfs_arg = _as_list(args.get("pdfs"))
    if request.attachments:
        for att in request.attachments:
            url = att.get("url") or (
                f"/api/v1/chat/files/{att.get('subdir', 'uploads/staging')}/{att.get('stored_filename', '')}"
                if att.get("stored_filename") else ""
            )
            if not url:
                continue
            mime = (att.get("mime_type") or "").lower()
            name = (att.get("name") or "").lower()
            if mime == "application/pdf" or mime.endswith("pdf") or name.endswith(".pdf"):
                if url not in pdfs_arg:
                    pdfs_arg.append(url)

    action = (args.get("action") or "parse").strip().lower()
    if action == "parse" and not pdfs_arg:
        return "错误: 请提供待解析的 PDF 文件（参数 pdfs 或输入框附件）"

    # 设置外部上下文（让 pdf_parse 内部能获取 agent 信息）
    token = _external_agent_context.set((agent_name, conversation_id, agent))
    try:
        result = await pdf_parse(
            pdfs=pdfs_arg or None,
            provider=args.get("provider") or "",
            is_ocr=bool(args.get("is_ocr", False)),
            enable_formula=bool(args.get("enable_formula", True)),
            enable_table=bool(args.get("enable_table", True)),
            language=args.get("language") or "ch",
            extra_formats=_as_list(args.get("extra_formats")) or None,
            page_ranges=args.get("page_ranges") or "",
            passwords=_as_list(args.get("passwords")) or None,
            background=bool(args.get("background", False)),
            timeoutMs=int(args.get("timeoutMs", 0) or 0),
            action=action,
        )
    finally:
        _external_agent_context.reset(token)

    if not isinstance(result, dict):
        return f"PDF 解析失败: {result}"

    if not result.get("success"):
        return f"PDF 解析失败: {result.get('error', '未知错误')}"

    # ===== action: list =====
    if action == "list":
        providers = result.get("providers", [])
        if not providers:
            return "当前没有配置可用的 PDF 解析提供商"
        lines = ["**可用 PDF 解析提供商:**\n"]
        for p in providers:
            caps = []
            if p.get("supports_ocr"):
                caps.append("OCR")
            if p.get("supports_password"):
                caps.append("密码")
            fmts = p.get("supported_extra_formats") or []
            if fmts:
                caps.append("格式:" + "/".join(fmts))
            limit = []
            if p.get("pdf_max_mb"):
                limit.append(f"{p.get('pdf_max_mb')}MB")
            if p.get("pdf_max_pages"):
                limit.append(f"{p.get('pdf_max_pages')}页")
            suffix = ""
            if caps:
                suffix += " | " + ", ".join(caps)
            if limit:
                suffix += " | 限额:" + "/".join(limit)
            lines.append(f"- {p.get('name', '?')} [{p.get('role', '')}]{suffix}")
        return "\n".join(lines)

    # ===== action: status =====
    if action == "status":
        jobs = result.get("jobs", [])
        if not jobs:
            return "当前没有活动的 PDF 解析任务"
        lines = ["**PDF 解析任务状态:**\n"]
        for j in jobs:
            lines.append(f"- [{j.get('status')}] {j.get('job_id')}: {j.get('provider', '?')} ({j.get('files', 0)} 文件)")
        return "\n".join(lines)

    # ===== action: parse → 以文件图标呈现 =====
    files = result.get("files", []) or []
    details = result.get("details", {}) or {}
    ignored = details.get("ignored_overrides") or []
    text_lines = [f"✅ PDF 解析完成（提供商: {result.get('provider', '?')}，产出 {len(files)} 个文件）"]
    if ignored:
        text_lines.append(f"\n> 已忽略不支持的参数: {', '.join(ignored)}")
    partial_errors = result.get("partial_errors") or []
    if partial_errors:
        text_lines.append("\n> 部分文件解析失败: " + "; ".join(str(e) for e in partial_errors))

    out_files = [
        {
            "file_id": f.get("filename") or f.get("url", ""),
            "name": f.get("filename") or "",
            "url": f.get("url", ""),
            "size": f.get("size", 0),
            "is_image": False,
            "mime_type": "text/markdown" if f.get("format") == "markdown" else "",
            "format": f.get("format", ""),
        }
        for f in files
    ]
    return {"type": "files_generated", "text": "\n".join(text_lines), "files": out_files}


# ===== TTS 斜杠命令处理器（整合为 tool.tts）=====

async def _handle_tool_tts(args: Dict[str, Any], request: SlashCommandExecuteRequest) -> str:
    """TTS语音合成管理统一处理器"""
    action = args.get("action", "").strip().lower()
    value = args.get("value", "").strip()

    if action == "off":
        from app.core.settings_store import update_settings
        def _mutator(data: Dict[str, Any]) -> None:
            tts = data.setdefault("tts", {})
            tts["auto"] = "off"
        update_settings(_mutator)
        return "✅ TTS 已关闭 (auto=off)"

    elif action == "on":
        from app.core.settings_store import update_settings
        def _mutator2(data: Dict[str, Any]) -> None:
            tts = data.setdefault("tts", {})
            tts["auto"] = "always"
        update_settings(_mutator2)
        return "✅ TTS 已开启 (auto=always)"

    elif action == "status":
        from app.tools.tts_service import resolve_tts_config, get_last_tts_attempt
        agent_name = None
        if request.agent_id:
            from app.services.task_service import task_service
            agent = task_service.get_agent(request.agent_id)
            if agent:
                agent_name = getattr(agent.config, 'name', None)
        config = resolve_tts_config(agent_name)
        last_attempt = get_last_tts_attempt()
        lines = [
            "** TTS 状态**\n",
            f"- 自动模式: `{config.get('auto', 'off')}`",
            f"- 提供商: `{config.get('provider', 'edge_tts')}`",
            f"- 流式模式: `{config.get('stream_mode', 'one-way')}`",
            f"- Persona: `{config.get('persona', '无')}`",
            f"- 字符限制: `{config.get('limit_chars', '无限制')}`",
        ]
        if last_attempt:
            import time
            elapsed = int(time.time() - last_attempt.get("timestamp", 0))
            status = "✅ 成功" if last_attempt.get("success") else "❌ 失败"
            lines.append(f"\n**最近一次合成:**")
            lines.append(f"- 结果: {status}")
            lines.append(f"- 提供商: `{last_attempt.get('provider', '?')}`")
            lines.append(f"- 延迟: {last_attempt.get('latency_ms', 0)}ms")
            lines.append(f"- 时间: {elapsed}秒前")
            if last_attempt.get("reason_code"):
                lines.append(f"- 原因: {last_attempt['reason_code']}")
            if last_attempt.get("fallback_from"):
                lines.append(f"- 回退自: {last_attempt['fallback_from']}")
        else:
            lines.append("\n暂无合成记录")
        return "\n".join(lines)

    elif action == "chat":
        if not request.conversation_id:
            return "错误: 没有活跃的会话"
        conv_action = value.lower() if value else "default"
        if conv_action == "on":
            await _set_conversation_tts_override(request.conversation_id, {"auto": "always"})
            return "✅ 当前会话 TTS 已开启"
        elif conv_action == "off":
            await _set_conversation_tts_override(request.conversation_id, {"auto": "off"})
            return "✅ 当前会话 TTS 已关闭"
        else:
            await _set_conversation_tts_override(request.conversation_id, None)
            return "✅ 当前会话 TTS 已恢复默认（跟随全局配置）"

    elif action == "latest":
        if not request.conversation_id:
            return "错误: 没有活跃的会话"
        from app.services.task_service import task_service
        agent = task_service.get_agent(request.agent_id) if request.agent_id else None
        latest_text = ""
        if agent:
            conv = agent.get_conversation(request.conversation_id)
            if conv and conv.messages:
                for msg in reversed(conv.messages):
                    if msg.role == MessageRole.ASSISTANT and msg.content and msg.message_type == MessageType.TEXT:
                        latest_text = msg.content
                        break
        if not latest_text:
            try:
                conv = await persistence.load_conversation(request.conversation_id)
                if conv and conv.messages:
                    for msg in reversed(conv.messages):
                        if msg.role == MessageRole.ASSISTANT and msg.content and msg.message_type == MessageType.TEXT:
                            latest_text = msg.content
                            break
            except Exception:
                pass
        if not latest_text:
            return "错误: 找不到最新的助手回复"
        limit = 2000
        if len(latest_text) > limit:
            latest_text = latest_text[:limit]
        from app.tools.tts_service import tts_synthesize_text
        import uuid as _uuid
        agent_name = getattr(agent.config, 'name', None) if agent else None
        workspace_path = agent.get_workspace_path(request.conversation_id) if agent else None
        audio_url = await tts_synthesize_text(
            text=latest_text,
            conversation_id=request.conversation_id,
            message_id=str(_uuid.uuid4()),
            agent_name=agent_name,
            workspace_path=workspace_path,
        )
        if audio_url:
            return {
                "type": "audio_generated",
                "audio_url": audio_url,
                "text": latest_text[:100] + ("..." if len(latest_text) > 100 else ""),
            }
        else:
            return "❌ 语音合成失败，请检查 TTS 配置"

    elif action == "provider":
        provider_id = value.strip() if value else args.get("id", "").strip()
        if not provider_id:
            return "错误: 请提供提供商ID (aliyun/minimax/edge_tts)"
        valid_providers = ["aliyun", "minimax", "edge_tts"]
        if provider_id not in valid_providers:
            return f"错误: 无效的提供商 '{provider_id}'，可选: {', '.join(valid_providers)}"
        from app.core.settings_store import update_settings
        def _mutator3(data: Dict[str, Any]) -> None:
            tts = data.setdefault("tts", {})
            tts["provider"] = provider_id
        update_settings(_mutator3)
        return f"✅ TTS 提供商已切换为: `{provider_id}`"

    elif action == "persona":
        persona_id = value.strip() if value else args.get("id", "").strip()
        from app.core.settings_store import update_settings, read_settings
        if persona_id.lower() in ("none", "off", "default", "清除"):
            def _mutator4(data: Dict[str, Any]) -> None:
                tts = data.setdefault("tts", {})
                tts["persona"] = None
            update_settings(_mutator4)
            return "✅ Persona 已清除，使用提供商默认配置"
        data = read_settings()
        personas = (data.get("tts", {}) or {}).get("personas", {}) or {}
        if persona_id not in personas:
            available = list(personas.keys()) if personas else ["无"]
            return f"错误: Persona '{persona_id}' 不存在\n可用: {', '.join(available)}"
        def _mutator5(d: Dict[str, Any]) -> None:
            tts = d.setdefault("tts", {})
            tts["persona"] = persona_id
        update_settings(_mutator5)
        label = personas[persona_id].get("label", persona_id)
        return f"✅ 语音风格已切换为: `{label}` ({persona_id})"

    elif action == "limit":
        chars_str = value.strip() if value else str(args.get("chars", ""))
        if not chars_str:
            return "错误: 请提供字符数"
        try:
            chars = int(chars_str)
        except (ValueError, TypeError):
            return "错误: 字符数必须为整数"
        from app.core.settings_store import update_settings
        if chars <= 0:
            def _mutator6(data: Dict[str, Any]) -> None:
                tts = data.setdefault("tts", {})
                tts["limit_chars"] = None
            update_settings(_mutator6)
            return "✅ TTS 字符限制已取消"
        def _mutator7(data: Dict[str, Any]) -> None:
            tts = data.setdefault("tts", {})
            tts["limit_chars"] = chars
        update_settings(_mutator7)
        return f"✅ TTS 字符限制已设置为: {chars}"

    elif action == "audio":
        # 生成一次性语音
        text = value.strip() if value else args.get("text", "").strip()
        if not text:
            return "错误: 请提供要合成的文本"
        from app.tools.tts_service import tts_synthesize_text
        import uuid as _uuid
        agent = None
        agent_name = None
        workspace_path = None
        if request.agent_id:
            from app.services.task_service import task_service
            agent = task_service.get_agent(request.agent_id)
            if agent:
                agent_name = getattr(agent.config, 'name', None)
                if request.conversation_id:
                    workspace_path = agent.get_workspace_path(request.conversation_id)
        conversation_id = request.conversation_id or "slash_command"
        audio_url = await tts_synthesize_text(
            text=text,
            conversation_id=conversation_id,
            message_id=str(_uuid.uuid4()),
            agent_name=agent_name,
            workspace_path=workspace_path,
        )
        if audio_url:
            return {
                "type": "audio_generated",
                "audio_url": audio_url,
                "text": text[:100] + ("..." if len(text) > 100 else ""),
            }
        else:
            return "❌ 语音合成失败，请检查 TTS 配置"

    else:
        return f"错误: 未知的 TTS 操作 '{action}'，可用: off, on, status, chat, latest, provider, persona, limit, audio"


async def _set_conversation_tts_override(conversation_id: str, override: Optional[Dict[str, Any]]) -> None:
    """设置会话级 TTS 覆盖配置"""
    from app.services.task_service import task_service
    # 尝试在内存中找到会话并设置 metadata
    for agent in task_service._agents.values():
        conv = agent.get_conversation(conversation_id)
        if conv:
            if not hasattr(conv, 'metadata') or conv.metadata is None:
                conv.metadata = {}
            if override is None:
                conv.metadata.pop("tts_override", None)
            else:
                conv.metadata["tts_override"] = override
            await persistence.save_conversation(conv)
            return
    # 如果内存中找不到，尝试通过 persistence
    try:
        conv = await persistence.load_conversation(conversation_id)
        if conv:
            if not hasattr(conv, 'metadata') or conv.metadata is None:
                conv.metadata = {}
            if override is None:
                conv.metadata.pop("tts_override", None)
            else:
                conv.metadata["tts_override"] = override
            await persistence.save_conversation(conv)
    except Exception:
        pass


# 命令处理器注册表
_COMMAND_HANDLERS: Dict[str, Any] = {
    "session.name": _handle_session_name,
    "tool.tools": _handle_tool_tools,
    "tool.test": _handle_tool_test,
    "tool.example": _handle_tool_example,
    "tool.image_generate": _handle_image_generate,
    "tool.music_generate": _handle_music_generate,
    "tool.video_generate": _handle_video_generate,
    "tool.pdf": _handle_tool_pdf,
    "tool.tts": _handle_tool_tts,
}
