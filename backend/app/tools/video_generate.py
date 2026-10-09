"""视频生成工具 - 支持多供应商（aliyun/wan3.0-video、minimax/MiniMax-H3）的后台异步生视频

本文件为编排/兼容层，负责：上下文解析、模型链构建、后台任务编排、结果保存、
事件推送与消息持久化、工具注册。各 provider 的具体实现（媒体上传、请求构建、
任务提交与轮询）已拆分至 app.tools.video_providers 包，新增 provider 只需在该包
新建文件并在 factory.py 注册。对外 API 保持不变。
"""
import asyncio
import contextvars
import json as _json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog

from app.config import settings
from app.core.workspace_isolation import get_current_workspace
from app.tools.registry import tool_registry, ToolPermissionEnum, current_tool_call_id
from app.tools.video_providers import (
    VideoGenerateRequest,
    get_provider,
    identify_provider,
    get_api_mode_config,
    normalize_video_params,
)

logger = structlog.get_logger()

# ===== 活动任务跟踪（用于 action=status） =====
_active_jobs: Dict[str, Dict[str, Any]] = {}

# ===== 外部上下文注入（斜杠命令等非 agent 调用场景） =====
_external_agent_context: contextvars.ContextVar[Optional[Tuple[str, str, Any]]] = contextvars.ContextVar(
    '_external_video_agent_context', default=None
)


# ===== 辅助：获取当前 agent 上下文 =====

def _get_agent_context() -> Tuple[str, str, Any]:
    """获取当前 agent_name, conversation_id, agent_self。"""
    ctx = _external_agent_context.get()
    if ctx is not None:
        return ctx
    import inspect
    frame = inspect.currentframe()
    for _ in range(15):
        frame = frame.f_back
        if frame is None:
            break
        if 'self' in frame.f_locals:
            caller_self = frame.f_locals['self']
            if hasattr(caller_self, 'id') and hasattr(caller_self, '_current_conversation'):
                agent_name = getattr(caller_self.config, 'name', 'unknown') if hasattr(caller_self, 'config') else 'unknown'
                conversation_id = caller_self._current_conversation or "default"
                return agent_name, conversation_id, caller_self
    return "unknown", "default", None


def _get_media_models_config(agent_name: str) -> Optional[Dict[str, Any]]:
    """读取 agent.entries.{agent_name}.mediaModels 配置"""
    data = settings.to_dict()
    entries = (data.get("agent", {}) or {}).get("entries", {}) or {}
    entry = entries.get(agent_name, {}) or {}
    media_models = entry.get("mediaModels")
    if media_models and hasattr(media_models, 'to_dict'):
        media_models = media_models.to_dict()
    return media_models


# ===== 供应商解析 =====

def _resolve_model_endpoint(model_ref: str) -> Optional[Dict[str, Any]]:
    """解析 provider/model_id 引用，返回 endpoint 信息"""
    from app.core.settings_store import resolve_llm_endpoint
    provider, _, model_id = model_ref.partition("/")
    if not model_id:
        model_id = provider
        provider = ""
    try:
        ep = resolve_llm_endpoint(provider, model_id)
        ep["model_ref"] = model_ref
        return ep
    except Exception as e:
        logger.warning("Failed to resolve video model endpoint", model_ref=model_ref, error=str(e))
        return None


def _build_video_model_chain(
    tool_model_param: Optional[str],
    media_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    构建视频模型尝试链：
    1. tool_model_param (强制，失败不 fallback)
    2. primary
    3. fallbacks
    """
    chain = []
    video_cfg = media_config.get("video", {}) or {}

    if tool_model_param:
        ep = _resolve_model_endpoint(tool_model_param)
        if ep:
            chain.append({"endpoint": ep, "force": True})
        return chain

    primary = video_cfg.get("primary")
    if primary:
        ep = _resolve_model_endpoint(primary)
        if ep:
            chain.append({"endpoint": ep, "force": False})

    fallbacks = video_cfg.get("fallbacks", []) or []
    for fb in fallbacks:
        ep = _resolve_model_endpoint(fb)
        if ep:
            chain.append({"endpoint": ep, "force": False})

    return chain


# ===== 视频数据保存 =====

async def _save_video_data(
    video_url: str,
    workspace_path: str,
    filename_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """下载视频并保存到会话工作区"""
    videos_dir = os.path.join(workspace_path, "generated_videos")
    os.makedirs(videos_dir, exist_ok=True)

    # 确定文件名
    if filename_hint:
        base_name = Path(filename_hint).stem
        ext = Path(filename_hint).suffix or ".mp4"
    else:
        base_name = f"video_{int(time.time())}"
        ext = ".mp4"

    filename = f"{base_name}_{uuid.uuid4().hex[:6]}{ext}"
    filepath = os.path.join(videos_dir, filename)

    try:
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            resp = await client.get(video_url)
            resp.raise_for_status()
            content = resp.content

        with open(filepath, "wb") as f:
            f.write(content)
        logger.info("Video downloaded and saved", path=filepath, size=len(content))
    except Exception as e:
        logger.warning("Failed to download video, keeping URL reference", url=video_url, error=str(e))
        return {"url": video_url, "source": "remote", "filename": filename}

    # 构建相对路径
    workspace_root = str(Path(settings.get("WORKSPACE_ROOT", str(Path(".happy-claw") / "workspace"))).resolve())
    try:
        rel_path = os.path.relpath(filepath, workspace_root)
    except ValueError:
        rel_path = filepath

    rel_path_url = rel_path.replace("\\", "/")
    file_url = f"/api/v1/chat/files/{rel_path_url}"

    return {
        "path": filepath,
        "url": file_url,
        "filename": filename,
        "source": "local",
    }


# ===== 后台生成任务 =====

async def _run_video_generation(
    job_id: str,
    conversation_id: str,
    agent_name: str,
    mode: str,
    prompt: str,
    model_chain: List[Dict[str, Any]],
    params: Dict[str, Any],
    timeout_ms: Optional[int],
    filename_hint: Optional[str],
    tool_call_id: str,
    workspace_path: str,
    media_config: Dict[str, Any],
    all_images: List[str],
    image_roles: List[str],
    all_videos: List[str],
    video_roles: List[str],
    all_audios: List[str],
    audio_roles: List[str],
    all_files: List[str],
    file_roles: List[str],
    all_links: List[str],
    link_roles: List[str],
    agent_self: Any = None,
):
    """后台执行视频生成任务"""
    global _active_jobs

    job_info = _active_jobs.get(job_id)
    if job_info:
        job_info["status"] = "running"
        job_info["started_at"] = datetime.now(timezone.utc).isoformat()

    video_cfg = media_config.get("video", {}) or {}
    effective_timeout = timeout_ms or video_cfg.get("timeoutMs") or 300000

    result = None
    used_model = None
    last_error = None
    normalization_details = {}
    ignored_overrides = []
    usage_info = {}

    for entry in model_chain:
        ep = entry["endpoint"]
        force = entry["force"]
        model_ref = ep.get("model_ref", "unknown")
        provider_type = identify_provider(ep)
        provider = get_provider(provider_type)

        # 未注册的提供商：记录错误并按 force 决定是否 fallback
        if not provider:
            last_error = f"Unsupported video provider: {provider_type}"
            logger.warning(
                "Video generation failed, trying next model",
                job_id=job_id, model=model_ref, error=last_error, force=force,
            )
            if force:
                break
            continue

        # 获取 API 配置
        mode_config = get_api_mode_config(ep, "generate-all-in-one")
        if not mode_config:
            last_error = f"{model_ref}: no generate-all-in-one config"
            if force:
                break
            continue

        # 参数标准化
        norm_params, norm_details, norm_ignored = normalize_video_params(params, mode_config)

        # 构建统一请求载体，交由 provider 做能力协商（登记不支持的媒体类型）
        request = VideoGenerateRequest(
            endpoint=ep, mode=mode, prompt=prompt, params=norm_params,
            timeout_ms=effective_timeout, job_id=job_id,
            images=all_images, image_roles=image_roles,
            videos=all_videos, video_roles=video_roles,
            audios=all_audios, audio_roles=audio_roles,
            files=all_files, file_roles=file_roles,
            links=all_links, link_roles=link_roles,
        )
        provider.prepare(request)
        # prepare 可能登记 provider 不支持的媒体类型标记
        norm_ignored = norm_ignored + request.ignored_params
        normalization_details = norm_details
        ignored_overrides = norm_ignored

        logger.info(
            "Attempting video generation",
            job_id=job_id, model=model_ref, provider=provider_type, mode=mode,
        )

        # 由 provider 完成：媒体构建 → 任务提交 → 轮询
        gen_result = await provider.generate(request)

        if gen_result.get("success"):
            result = gen_result
            used_model = model_ref
            usage_info = gen_result.get("usage", {})
            break
        else:
            last_error = gen_result.get("error", "Unknown error")
            logger.warning(
                "Video generation failed, trying next model",
                job_id=job_id, model=model_ref, error=last_error, force=force,
            )
            if force:
                break

    # 处理结果
    saved_video = None
    if result and result.get("success"):
        video_url = result.get("video_url", "")
        if video_url:
            saved_video = await _save_video_data(video_url, workspace_path, filename_hint)
        status = "completed"
    else:
        status = "failed"

    # 更新任务状态
    if job_info:
        job_info["status"] = status
        job_info["completed_at"] = datetime.now(timezone.utc).isoformat()
        job_info["result"] = {
            "videos": [saved_video] if saved_video else [],
            "model": used_model,
            "error": last_error if status == "failed" else None,
        }

    # 推送最终事件到前端
    message_id = f"video_{job_id}"
    try:
        from app.core.event_bus import event_bus
        event_data = {
            "message_id": message_id,
            "job_id": job_id,
            "conversation_id": conversation_id,
            "tool_call_id": tool_call_id,
            "status": status,
            "videos": [saved_video] if saved_video else [],
            "model": used_model,
            "mode": mode,
            "prompt": prompt[:200] if prompt else "",
            "usage": usage_info,
            "details": {
                "normalization": normalization_details,
                "ignored_overrides": ignored_overrides,
            },
        }
        if status == "failed":
            event_data["error"] = last_error

        await event_bus.emit_event(
            type="video_generated",
            source=conversation_id,
            data=event_data,
            priority="normal",
            dimensions=[f"conversation:{conversation_id}"],
        )
        logger.info("Video generation event emitted", job_id=job_id, status=status, conversation_id=conversation_id)
    except Exception as e:
        logger.error("Failed to emit video_generated event", job_id=job_id, error=str(e), exc_info=True)

    # 持久化消息到数据库
    try:
        from app.services.persistence import persistence
        from app.models.schemas.chat import Message, MessageRole, MessageType

        msg_metadata = {
            "job_id": job_id,
            "tool_call_id": tool_call_id,
            "status": status,
            "model": used_model,
            "mode": mode,
            "videos": [saved_video] if saved_video else [],
            "usage": usage_info,
            "normalization": normalization_details,
            "ignored_overrides": ignored_overrides,
        }
        if status == "failed":
            msg_metadata["error"] = last_error

        if status == "completed":
            content = _json.dumps({
                "status": "completed",
                "videos": [{"url": saved_video.get("url"), "filename": saved_video.get("filename")}] if saved_video else [],
                "model": used_model,
            }, ensure_ascii=False)
        else:
            content = _json.dumps({
                "status": "failed",
                "error": last_error,
                "model": used_model,
            }, ensure_ascii=False)

        msg = Message(
            id=message_id,
            role=MessageRole.TOOL,
            content=content,
            message_type=MessageType.VIDEO_GENERATED,
            tool_call_id=tool_call_id,
            name="video_generate",
            metadata=msg_metadata,
        )

        await persistence.save_message(conversation_id, msg)

        if agent_self and hasattr(agent_self, 'get_conversation'):
            conv = agent_self.get_conversation(conversation_id)
            if conv:
                conv.messages.append(msg)

        logger.info("Video generation message persisted", job_id=job_id, message_id=message_id)
    except Exception as e:
        logger.error("Failed to persist video_generated message", job_id=job_id, error=str(e), exc_info=True)

    _cleanup_old_jobs()


def _cleanup_old_jobs():
    """清理过期的任务记录"""
    global _active_jobs
    now = time.time()
    to_remove = []
    for jid, info in _active_jobs.items():
        completed = info.get("completed_at")
        if completed:
            try:
                ct = datetime.fromisoformat(completed.replace("Z", "+00:00")).timestamp()
                if now - ct > 600:  # 10 分钟（视频任务较长）
                    to_remove.append(jid)
            except Exception:
                to_remove.append(jid)
    for jid in to_remove:
        del _active_jobs[jid]


# ===== 工具 Handler =====

async def video_generate(
    prompt: str = "",
    action: str = "generate",
    mode: str = "normal",
    model: str = "",
    image: str = "",
    images: list = None,
    imageRoles: list = None,
    video: str = "",
    videos: list = None,
    videoRoles: list = None,
    audioRef: str = "",
    audioRefs: list = None,
    audioRoles: list = None,
    fileRef: str = "",
    fileRefs: list = None,
    fileRoles: list = None,
    linkRef: str = "",
    linkRefs: list = None,
    linkRoles: list = None,
    aspectRatio: str = "",
    resolution: str = "",
    durationSeconds: int = 0,
    size: str = "",
    audio: bool = None,
    watermark: bool = None,
    filename: str = "",
    timeoutMs: int = 0,
    **kwargs,
) -> Dict[str, Any]:
    """视频生成工具 handler"""
    # 类型容错
    try:
        durationSeconds = int(durationSeconds) if durationSeconds not in (None, "") else 0
    except (ValueError, TypeError):
        durationSeconds = 0
    try:
        timeoutMs = int(timeoutMs) if timeoutMs not in (None, "") else 0
    except (ValueError, TypeError):
        timeoutMs = 0

    # 列表参数容错
    def _ensure_list(val):
        if val is None:
            return []
        if isinstance(val, str):
            s = val.strip()
            if s.startswith("[") and s.endswith("]"):
                try:
                    return _json.loads(s)
                except Exception:
                    return [x.strip().strip("'\"") for x in s[1:-1].split(",") if x.strip()]
            elif s:
                return [x.strip() for x in s.split(",") if x.strip()]
            return []
        if isinstance(val, list):
            return val
        return []

    all_images = ([image] if image else []) + _ensure_list(images)
    all_videos = ([video] if video else []) + _ensure_list(videos)
    all_audios = ([audioRef] if audioRef else []) + _ensure_list(audioRefs)
    all_files = ([fileRef] if fileRef else []) + _ensure_list(fileRefs)
    all_links = ([linkRef] if linkRef else []) + _ensure_list(linkRefs)
    image_roles = _ensure_list(imageRoles)
    video_roles = _ensure_list(videoRoles)
    audio_roles = _ensure_list(audioRoles)
    file_roles = _ensure_list(fileRoles)
    link_roles = _ensure_list(linkRoles)

    # 获取 agent 上下文
    agent_name, conversation_id, agent_self = _get_agent_context()

    # 获取 mediaModels 配置
    media_config = _get_media_models_config(agent_name)
    if not media_config:
        return {
            "success": False,
            "error": f"Agent '{agent_name}' has no mediaModels configured. Cannot generate video.",
        }

    # ===== action: list =====
    if action == "list":
        return _handle_list(media_config)

    # ===== action: status =====
    if action == "status":
        return _handle_status(conversation_id)

    # ===== action: generate =====
    if not prompt and not all_images and not all_videos and not all_files and not all_links:
        return {"success": False, "error": "Parameter 'prompt' is required for action='generate'"}

    # 验证 mode
    valid_modes = ("normal", "image_to_video", "video_to_video", "media_to_video")
    if mode not in valid_modes:
        return {"success": False, "error": f"Invalid mode '{mode}'. Must be one of: {', '.join(valid_modes)}"}

    # 构建模型链
    model_chain = _build_video_model_chain(model or None, media_config)
    if not model_chain:
        return {
            "success": False,
            "error": "No video generation model available. Check mediaModels.video configuration.",
        }

    # 构建参数
    params: Dict[str, Any] = {}
    if aspectRatio:
        params["ratio"] = aspectRatio
    if resolution:
        params["resolution"] = resolution
    if durationSeconds:
        params["duration"] = durationSeconds
    if audio is not None:
        params["audio"] = audio
    if watermark is not None:
        params["watermark"] = watermark
    if size:
        params["size"] = size

    # 获取工作区路径
    workspace_path = get_current_workspace()
    if not workspace_path and agent_self:
        try:
            workspace_path = await agent_self.ensure_workspace(conversation_id)
        except Exception:
            pass
    if not workspace_path:
        workspace_path = os.getcwd()

    # 创建后台任务
    job_id = f"video_{uuid.uuid4().hex[:8]}"
    tool_call_id = current_tool_call_id.get()

    _active_jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "conversation_id": conversation_id,
        "agent_name": agent_name,
        "prompt": prompt[:200] if prompt else "",
        "mode": mode,
        "model": model_chain[0]["endpoint"].get("model_ref", "unknown"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "completed_at": None,
        "result": None,
    }

    # 启动后台任务
    asyncio.create_task(
        _run_video_generation(
            job_id=job_id,
            conversation_id=conversation_id,
            agent_name=agent_name,
            mode=mode,
            prompt=prompt,
            model_chain=model_chain,
            params=params,
            timeout_ms=timeoutMs or None,
            filename_hint=filename or None,
            tool_call_id=tool_call_id,
            workspace_path=workspace_path,
            media_config=media_config,
            all_images=all_images,
            image_roles=image_roles,
            all_videos=all_videos,
            video_roles=video_roles,
            all_audios=all_audios,
            audio_roles=audio_roles,
            all_files=all_files,
            file_roles=file_roles,
            all_links=all_links,
            link_roles=link_roles,
            agent_self=agent_self,
        )
    )

    return {
        "success": True,
        "status": "started",
        "job_id": job_id,
        "message": f"视频生成任务已提交后台执行（模型: {model_chain[0]['endpoint'].get('model_ref', 'unknown')}，模式: {mode}）。生成完成后将自动推送到会话界面。",
        "model": model_chain[0]["endpoint"].get("model_ref"),
        "mode": mode,
        "available_models": [e["endpoint"].get("model_ref") for e in model_chain],
    }


def _handle_list(media_config: Dict[str, Any]) -> Dict[str, Any]:
    """列出可用的视频生成供应商和模型"""
    video_cfg = media_config.get("video", {}) or {}
    primary = video_cfg.get("primary", "")
    fallbacks = video_cfg.get("fallbacks", []) or []
    timeout_ms = video_cfg.get("timeoutMs", 300000)

    models = []
    all_refs = ([primary] if primary else []) + list(fallbacks)

    for ref in all_refs:
        ep = _resolve_model_endpoint(ref)
        if ep:
            provider_type = identify_provider(ep)
            mode_config = get_api_mode_config(ep, "generate-all-in-one")
            param_limit = mode_config.get("param_limit", {}) if mode_config else {}
            models.append({
                "model_ref": ref,
                "provider": ep.get("provider"),
                "model_id": ep.get("model_id"),
                "role": "primary" if ref == primary else "fallback",
                "provider_type": provider_type,
                "param_limit": param_limit,
            })
        else:
            models.append({"model_ref": ref, "error": "Failed to resolve endpoint"})

    return {
        "success": True,
        "primary": primary,
        "fallbacks": fallbacks,
        "timeoutMs": timeout_ms,
        "models": models,
    }


def _handle_status(conversation_id: str) -> Dict[str, Any]:
    """查询当前会话的活动视频生成任务"""
    jobs = []
    for jid, info in _active_jobs.items():
        if info.get("conversation_id") == conversation_id:
            job_data = {
                "job_id": jid,
                "status": info["status"],
                "model": info.get("model"),
                "mode": info.get("mode"),
                "prompt": info.get("prompt"),
                "created_at": info.get("created_at"),
            }
            if info.get("completed_at"):
                job_data["completed_at"] = info["completed_at"]
            if info.get("result"):
                job_data["result"] = info["result"]
            jobs.append(job_data)

    return {
        "success": True,
        "conversation_id": conversation_id,
        "active_jobs": len([j for j in jobs if j["status"] in ("pending", "running")]),
        "jobs": jobs,
    }


# ===== 工具注册 =====

_VIDEO_GENERATE_PARAMS = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "Video generation prompt describing the desired video content. Required for action='generate'.",
        },
        "action": {
            "type": "string",
            "enum": ["generate", "status", "list"],
            "description": "'status' returns current session tasks; 'list' checks available providers. Defaults to 'generate'.",
        },
        "mode": {
            "type": "string",
            "enum": ["normal", "image_to_video", "video_to_video", "media_to_video"],
            "description": "Generation mode. normal: text-to-video, image_to_video: reference image to video, video_to_video: reference video to video, media_to_video: mixed media (images, videos, audio, files) to video.",
        },
        "model": {
            "type": "string",
            "description": "Provider/model override (e.g. 'aliyun/wan3.0-video', 'minimax/MiniMax-H3'). When specified, this model is used exclusively without fallback.",
        },
        "image": {
            "type": "string",
            "description": "Single reference image (path or URL).",
        },
        "images": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Multiple reference images.",
        },
        "imageRoles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Per-position role hints for merged image list. Values: first_frame, last_frame, reference_image.",
        },
        "video": {
            "type": "string",
            "description": "Single reference video (path or URL).",
        },
        "videos": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Multiple reference videos.",
        },
        "videoRoles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Per-position role hints for merged video list. Values: reference_video.",
        },
        "audioRef": {
            "type": "string",
            "description": "Single reference audio (path or URL) for background music or voice reference.",
        },
        "audioRefs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Multiple reference audios.",
        },
        "audioRoles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Per-position role hints for merged audio list. Values: reference_audio.",
        },
        "fileRef": {
            "type": "string",
            "description": "Single reference file (path or URL) such as doc, xls, pdf.",
        },
        "fileRefs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Multiple reference files.",
        },
        "fileRoles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Per-position role hints for merged file list. Values: file.",
        },
        "linkRef": {
            "type": "string",
            "description": "Single reference web link URL.",
        },
        "linkRefs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Multiple reference web links.",
        },
        "linkRoles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Per-position role hints for merged link list. Values: link.",
        },
        "aspectRatio": {
            "type": "string",
            "description": "Aspect ratio hint: 1:1, 16:9, 9:16, 4:3, 3:4, 21:9, adaptive, or provider-specific values.",
        },
        "resolution": {
            "type": "string",
            "description": "Resolution hint: 480P, 720P, 768P, 1080P, 2K, or provider-specific values.",
        },
        "durationSeconds": {
            "type": "number",
            "description": "Target duration in seconds (rounded to nearest provider-supported value).",
        },
        "size": {
            "type": "string",
            "description": "Size hint when supported by provider.",
        },
        "audio": {
            "type": "boolean",
            "description": "Enable generated audio in output video when supported.",
        },
        "watermark": {
            "type": "boolean",
            "description": "Toggle provider watermark when supported.",
        },
        "filename": {
            "type": "string",
            "description": "Output filename hint.",
        },
        "timeoutMs": {
            "type": "number",
            "description": "Optional provider operation timeout in milliseconds.",
        },
    },
    "required": [],
}


def register_video_generate_tool():
    """注册 video_generate 工具到全局注册表"""
    tool_registry.register(
        name="video_generate",
        description=(
            "Generate video using configured media models. Supports multiple modes: "
            "normal (text-to-video), image_to_video (reference image to video), "
            "video_to_video (reference video to video), media_to_video (mixed media to video). "
            "Runs in background to avoid blocking conversation. "
            "Use action='list' to see available models, action='status' to check running tasks."
        ),
        handler=video_generate,
        parameters=_VIDEO_GENERATE_PARAMS,
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=30,
        metadata={"system_tool": False},
    )
    logger.info("video_generate tool registered")
