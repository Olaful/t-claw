"""图片生成工具 - 多供应商后台异步生图

Provider 实现已拆分至 app/tools/image_providers/ 包（每个 provider 一个文件，
通过 factory 统一注册分发），本模块保留工具编排逻辑：
- 上下文解析 / 模型链构建
- 后台任务调度与状态跟踪
- 图像保存 / 事件推送 / 消息持久化
- 工具 handler 与注册
"""
import asyncio
import base64
import contextvars
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
from app.tools.image_providers import get_provider, normalize_params
from app.tools.registry import tool_registry, ToolPermissionEnum, current_tool_call_id

logger = structlog.get_logger()

# ===== 活动任务跟踪（用于 action=status） =====
_active_jobs: Dict[str, Dict[str, Any]] = {}  # job_id -> job_info

# ===== 外部上下文注入（斜杠命令等非 agent 调用场景） =====
_external_agent_context: contextvars.ContextVar[Optional[Tuple[str, str, Any]]] = contextvars.ContextVar(
    '_external_agent_context', default=None
)


# ===== 辅助：获取当前 agent 上下文 =====

def _get_agent_context() -> Tuple[str, str, Any]:
    """获取当前 agent_name, conversation_id, agent_self。
    优先读取 ContextVar（斜杠命令等外部调用场景），否则从调用栈推断。
    """
    # 1. 优先使用外部注入的上下文（斜杠命令场景）
    ctx = _external_agent_context.get()
    if ctx is not None:
        return ctx
    # 2. 回退：从调用栈推断
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
        logger.warning("Failed to resolve media model endpoint", model_ref=model_ref, error=str(e))
        return None


def _build_model_chain(
    tool_model_param: Optional[str],
    media_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    构建模型尝试链：
    1. tool_model_param (强制，失败不 fallback)
    2. primary
    3. fallbacks

    Returns: [{"endpoint": ..., "force": bool}, ...]
    """
    chain = []
    image_cfg = media_config.get("image", {}) or {}

    if tool_model_param:
        ep = _resolve_model_endpoint(tool_model_param)
        if ep:
            chain.append({"endpoint": ep, "force": True})
        return chain  # 强制指定时不添加 fallback

    primary = image_cfg.get("primary")
    if primary:
        ep = _resolve_model_endpoint(primary)
        if ep:
            chain.append({"endpoint": ep, "force": False})

    fallbacks = image_cfg.get("fallbacks", []) or []
    for fb in fallbacks:
        ep = _resolve_model_endpoint(fb)
        if ep:
            chain.append({"endpoint": ep, "force": False})

    return chain


# ===== 图像数据保存 =====

async def _save_image_data(
    image_data: Dict[str, Any],
    workspace_path: str,
    filename_hint: Optional[str] = None,
    index: int = 0,
) -> Dict[str, Any]:
    """
    保存图像数据到会话工作区。

    Returns: {"path": 相对路径, "url": API访问URL, "filename": 文件名}
    """
    images_dir = os.path.join(workspace_path, "generated_images")
    os.makedirs(images_dir, exist_ok=True)

    # 确定文件名
    if filename_hint:
        base_name = Path(filename_hint).stem
        ext = Path(filename_hint).suffix or ".png"
    else:
        base_name = f"img_{int(time.time())}_{index}"
        ext = ".png"

    filename = f"{base_name}_{uuid.uuid4().hex[:6]}{ext}"
    filepath = os.path.join(images_dir, filename)

    url = image_data.get("url")
    b64 = image_data.get("b64_json")

    if url:
        # 下载 URL 图片到本地
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content = resp.content

            # 根据 content-type 推断扩展名
            ct = resp.headers.get("content-type", "")
            if "jpeg" in ct or "jpg" in ct:
                ext = ".jpg"
            elif "webp" in ct:
                ext = ".webp"
            elif "png" in ct:
                ext = ".png"

            if not filename_hint:
                filename = f"{base_name}_{uuid.uuid4().hex[:6]}{ext}"
                filepath = os.path.join(images_dir, filename)

            with open(filepath, "wb") as f:
                f.write(content)

            logger.info("Image downloaded and saved", path=filepath, size=len(content))
        except Exception as e:
            logger.warning("Failed to download image, keeping URL reference", url=url, error=str(e))
            # 下载失败，保留 URL 引用
            return {
                "url": url,
                "source": "remote",
                "filename": filename,
            }
    elif b64:
        # base64 解码保存
        try:
            content = base64.b64decode(b64)
            with open(filepath, "wb") as f:
                f.write(content)
            logger.info("Image saved from base64", path=filepath, size=len(content))
        except Exception as e:
            logger.error("Failed to decode base64 image", error=str(e))
            return {"error": f"Failed to decode base64: {str(e)}"}
    else:
        return {"error": "No image data (url or b64_json) found"}

    # 构建相对路径（相对于 WORKSPACE_ROOT）
    workspace_root = str(Path(settings.get("WORKSPACE_ROOT", str(Path(".happy-claw") / "workspace"))).resolve())
    try:
        rel_path = os.path.relpath(filepath, workspace_root)
    except ValueError:
        rel_path = filepath

    # 使用正斜杠（URL 兼容）
    rel_path_url = rel_path.replace("\\", "/")
    file_url = f"/api/v1/chat/files/{rel_path_url}"

    return {
        "path": filepath,
        "url": file_url,
        "filename": filename,
        "source": "local",
    }


# ===== 后台生成任务 =====

async def _run_image_generation(
    job_id: str,
    conversation_id: str,
    agent_name: str,
    prompt: str,
    model_chain: List[Dict[str, Any]],
    params: Dict[str, Any],
    images: Optional[List[str]],
    count: int,
    timeout_ms: Optional[int],
    filename_hint: Optional[str],
    openai_opts: Optional[Dict[str, Any]],
    tool_call_id: str,
    workspace_path: str,
    media_config: Dict[str, Any],
    agent_self: Any = None,
):
    """后台执行图片生成任务"""
    global _active_jobs

    job_info = _active_jobs.get(job_id)
    if job_info:
        job_info["status"] = "running"
        job_info["started_at"] = datetime.now(timezone.utc).isoformat()

    # 默认超时
    image_cfg = media_config.get("image", {}) or {}
    effective_timeout = timeout_ms or image_cfg.get("timeoutMs") or 180000

    result = None
    used_model = None
    last_error = None

    for entry in model_chain:
        ep = entry["endpoint"]
        force = entry["force"]
        model_ref = ep.get("model_ref", "unknown")
        api_type = ep.get("api", "")
        model_params = ep.get("params", {}) or {}
        other_cfg = model_params.get("other", {}) or {}
        compat = other_cfg.get("compat", {}) or {}

        # 参数标准化
        mapped_params, normalization, ignored = normalize_params(params, compat, other_cfg.get("params", {}))

        logger.info(
            "Attempting image generation",
            job_id=job_id,
            model=model_ref,
            api=api_type,
            mapped_params=mapped_params,
            ignored=ignored,
        )

        # 根据 api 类型从工厂获取对应 Provider
        provider = get_provider(api_type)
        if provider is None:
            gen_result = {"success": False, "error": f"Unsupported api type: {api_type}"}
        else:
            gen_result = await provider.generate(
                endpoint=ep,
                prompt=prompt,
                params=mapped_params,
                images=images,
                count=count,
                timeout_ms=effective_timeout,
                openai_opts=openai_opts,
            )

        if gen_result.get("success"):
            result = gen_result
            used_model = model_ref
            result["normalization"] = normalization
            result["ignored_overrides"] = ignored
            break
        else:
            last_error = gen_result.get("error", "Unknown error")
            logger.warning(
                "Image generation failed, trying next model",
                job_id=job_id,
                model=model_ref,
                error=last_error,
                force=force,
            )
            if force:
                # 强制指定的模型失败，不再尝试 fallback
                break

    # 处理结果
    saved_images = []
    if result and result.get("success"):
        raw_images = result.get("images", [])
        for idx, img_data in enumerate(raw_images[:count]):
            saved = await _save_image_data(
                image_data=img_data,
                workspace_path=workspace_path,
                filename_hint=filename_hint if count == 1 else None,
                index=idx,
            )
            saved_images.append(saved)

        status = "completed"
    else:
        status = "failed"

    # 更新任务状态
    if job_info:
        job_info["status"] = status
        job_info["completed_at"] = datetime.now(timezone.utc).isoformat()
        job_info["result"] = {
            "images": saved_images,
            "model": used_model,
            "error": last_error if status == "failed" else None,
        }

    # 构建 response_meta（排除 b64_json 数据）
    response_meta = result.get("response_meta", {}) if result else {}

    # 推送事件到前端 + 持久化消息到数据库
    message_id = f"img_{uuid.uuid4().hex[:12]}"
    try:
        from app.core.event_bus import event_bus
        event_data = {
            "message_id": message_id,
            "job_id": job_id,
            "conversation_id": conversation_id,
            "tool_call_id": tool_call_id,
            "status": status,
            "images": saved_images,
            "model": used_model,
            "prompt": prompt[:200],
            "details": {
                "normalization": result.get("normalization", {}) if result else {},
                "ignored_overrides": result.get("ignored_overrides", []) if result else [],
                "response_meta": response_meta,
            },
        }
        if status == "failed":
            event_data["error"] = last_error

        await event_bus.emit_event(
            type="image_generated",
            source=conversation_id,
            data=event_data,
            priority="normal",
            dimensions=[f"conversation:{conversation_id}"],
        )
        logger.info("Image generation event emitted", job_id=job_id, status=status, conversation_id=conversation_id)
    except Exception as e:
        logger.error("Failed to emit image_generated event", job_id=job_id, error=str(e), exc_info=True)

    # ✅ 持久化消息到数据库（防止应用重启后丢失图片生成数据）
    try:
        from app.services.persistence import persistence
        from app.models.schemas.chat import Message, MessageRole, MessageType
        import json as _json

        # 构建消息 metadata（包含完整响应信息，不含 b64_json）
        msg_metadata = {
            "job_id": job_id,
            "tool_call_id": tool_call_id,
            "status": status,
            "model": used_model,
            "images": saved_images,
            "response_meta": response_meta,
            "normalization": result.get("normalization", {}) if result else {},
            "ignored_overrides": result.get("ignored_overrides", []) if result else [],
        }
        if status == "failed":
            msg_metadata["error"] = last_error

        # 消息内容：精简的文本摘要
        if status == "completed":
            content = _json.dumps({
                "status": "completed",
                "images": [{"path": img.get("path"), "url": img.get("file_url")} for img in saved_images],
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
            message_type=MessageType.IMAGE_GENERATED,
            tool_call_id=tool_call_id,
            name="image_generate",
            metadata=msg_metadata,
        )

        # 保存到数据库
        await persistence.save_message(conversation_id, msg)

        # 同步到 agent 内存中的会话（以便刷新页面时从内存读取）
        if agent_self and hasattr(agent_self, 'get_conversation'):
            conv = agent_self.get_conversation(conversation_id)
            if conv:
                conv.messages.append(msg)

        logger.info("Image generation message persisted", job_id=job_id, message_id=message_id, conversation_id=conversation_id)
    except Exception as e:
        logger.error("Failed to persist image_generated message", job_id=job_id, error=str(e), exc_info=True)

    # 清理已完成超过 5 分钟的任务记录
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
                if now - ct > 300:  # 5 分钟
                    to_remove.append(jid)
            except Exception:
                to_remove.append(jid)
    for jid in to_remove:
        del _active_jobs[jid]


# ===== 图片数据加载（URL/路径 → 可传递给模型的数据） =====

def _file_to_data_uri(abs_path: str) -> str:
    """读取本地图片文件，输出标准 data URI 格式：data:image/png;base64,xxxx"""
    ext = os.path.splitext(abs_path)[1].lower().lstrip(".")
    mime_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp", "bmp": "bmp"}
    subtype = mime_map.get(ext, "png")
    with open(abs_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/{subtype};base64,{b64}"


async def _load_image_reference(image_ref: str) -> str:
    """加载图片引用（远程 URL 直接返回，本地文件URL/本地路径读取为 base64）"""
    if image_ref.startswith(("http://", "https://")):
        return image_ref

    # ✅ 本地文件访问 URL：/api/v1/chat/files/{subdir}/{filename} → 解析到 WORKSPACE_ROOT 下读取
    # 避免直接引用服务器本地绝对路径（安全），同时支持会话工作区内的附件图片
    if image_ref.startswith("/api/v1/chat/files/"):
        rel = image_ref[len("/api/v1/chat/files/"):]
        try:
            from app.api.v1.chat import WORKSPACE_ROOT_PATH
            resolved = (WORKSPACE_ROOT_PATH / rel).resolve()
            # 安全校验：必须在 WORKSPACE_ROOT 内，防止路径遍历
            if str(resolved).startswith(str(WORKSPACE_ROOT_PATH.resolve())) and os.path.isfile(str(resolved)):
                return _file_to_data_uri(str(resolved))
        except Exception:
            pass
        return image_ref

    # 本地路径（手动指定的绝对/相对路径）
    from app.core.workspace_isolation import resolve_ws_path
    abs_path = resolve_ws_path(image_ref)
    if os.path.isfile(abs_path):
        return _file_to_data_uri(abs_path)
    return image_ref


# ===== 工具 Handler =====

async def image_generate(
    prompt: str = "",
    action: str = "generate",
    model: str = "",
    image: str = "",
    images: list = None,
    size: str = "",
    aspectRatio: str = "",
    resolution: str = "",
    quality: str = "",
    outputFormat: str = "",
    background: str = "",
    count: int = 1,
    timeoutMs: int = 0,
    filename: str = "",
    openai: dict = None,
    **kwargs,
) -> Dict[str, Any]:
    """图片生成工具 handler"""
    # ✅ 类型容错：无论调用方（斜杠命令 / LLM 工具调用）传入字符串还是数字，
    # 都强制将数值型参数转为 int，避免 min/max 比较时报错或类型不符
    try:
        count = int(count) if count is not None and count != "" else 1
    except (ValueError, TypeError):
        count = 1
    try:
        timeoutMs = int(timeoutMs) if timeoutMs not in (None, "") else 0
    except (ValueError, TypeError):
        timeoutMs = 0
    # images 容错：若误传为字符串（如 JSON 文本或逗号分隔），转为列表
    if isinstance(images, str):
        s = images.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                import json as _json
                images = _json.loads(s)
            except Exception:
                images = [x.strip().strip("'\"") for x in s[1:-1].split(",") if x.strip()]
        elif s:
            images = [x.strip() for x in s.split(",") if x.strip()]
        else:
            images = None

    # 获取 agent 上下文
    agent_name, conversation_id, agent_self = _get_agent_context()

    # 获取 mediaModels 配置
    media_config = _get_media_models_config(agent_name)
    if not media_config:
        return {
            "success": False,
            "error": f"Agent '{agent_name}' has no mediaModels configured. Cannot generate images.",
        }

    # ===== action: list =====
    if action == "list":
        return _handle_list(media_config)

    # ===== action: status =====
    if action == "status":
        return _handle_status(conversation_id)

    # ===== action: generate =====
    if not prompt:
        return {"success": False, "error": "Parameter 'prompt' is required for action='generate'"}

    # 构建模型链
    model_chain = _build_model_chain(model or None, media_config)
    if not model_chain:
        return {
            "success": False,
            "error": "No image generation model available. Check mediaModels configuration.",
        }

    # 加载参考图片
    image_refs = []
    if image:
        image_refs.append(await _load_image_reference(image))
    if images:
        for img in images:
            image_refs.append(await _load_image_reference(img))

    # 构建参数
    params: Dict[str, Any] = {}
    if size:
        params["size"] = size
    if aspectRatio:
        params["aspectRatio"] = aspectRatio
    if resolution:
        params["resolution"] = resolution
    if quality:
        params["quality"] = quality
    if outputFormat:
        params["outputFormat"] = outputFormat
    if background:
        params["background"] = background

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
    job_id = f"img_{uuid.uuid4().hex[:8]}"
    tool_call_id = current_tool_call_id.get()

    _active_jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "conversation_id": conversation_id,
        "agent_name": agent_name,
        "prompt": prompt[:200],
        "model": model_chain[0]["endpoint"].get("model_ref", "unknown"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "completed_at": None,
        "result": None,
    }

    # 启动后台任务
    asyncio.create_task(
        _run_image_generation(
            job_id=job_id,
            conversation_id=conversation_id,
            agent_name=agent_name,
            prompt=prompt,
            model_chain=model_chain,
            params=params,
            images=image_refs if image_refs else None,
            count=min(max(count, 1), 4),
            timeout_ms=timeoutMs or None,
            filename_hint=filename or None,
            openai_opts=openai,
            tool_call_id=tool_call_id,
            workspace_path=workspace_path,
            media_config=media_config,
            agent_self=agent_self,
        )
    )

    return {
        "success": True,
        "status": "started",
        "job_id": job_id,
        "message": f"图片生成任务已提交后台执行（模型: {model_chain[0]['endpoint'].get('model_ref', 'unknown')}）。生成完成后将自动推送到会话界面。",
        "model": model_chain[0]["endpoint"].get("model_ref"),
        "available_models": [e["endpoint"].get("model_ref") for e in model_chain],
    }


def _handle_list(media_config: Dict[str, Any]) -> Dict[str, Any]:
    """列出可用的生图供应商和模型"""
    image_cfg = media_config.get("image", {}) or {}
    primary = image_cfg.get("primary", "")
    fallbacks = image_cfg.get("fallbacks", []) or []
    timeout_ms = image_cfg.get("timeoutMs", 180000)

    models = []
    all_refs = ([primary] if primary else []) + list(fallbacks)

    for ref in all_refs:
        ep = _resolve_model_endpoint(ref)
        if ep:
            model_params = ep.get("params", {}) or {}
            compat = model_params.get("compat", {}) or {}
            models.append({
                "model_ref": ref,
                "provider": ep.get("provider"),
                "model_id": ep.get("model_id"),
                "api": ep.get("api"),
                "role": "primary" if ref == primary else "fallback",
                "compat": {
                    "supportsSize": compat.get("supportsSize", []),
                    "supportsQuality": compat.get("supportsQuality", []),
                    "supportsAspectRatio": compat.get("supportsAspectRatio", []),
                    "supportsResolution": compat.get("supportsResolution", []),
                    "supportsOutputFormat": compat.get("supportsOutputFormat", []),
                    "supportsBackground": compat.get("supportsBackground", []),
                },
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
    """查询当前会话的活动生图任务"""
    jobs = []
    for jid, info in _active_jobs.items():
        if info.get("conversation_id") == conversation_id:
            job_data = {
                "job_id": jid,
                "status": info["status"],
                "model": info.get("model"),
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

_IMAGE_GENERATE_PARAMS = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "Image generation prompt. Required for action='generate'.",
        },
        "action": {
            "type": "string",
            "enum": ["generate", "status", "list"],
            "description": "Use 'status' to check active session tasks, or 'list' to inspect available providers and models at runtime. Defaults to 'generate'.",
        },
        "model": {
            "type": "string",
            "description": "Provider/model override (e.g. 'aliyun/qwen-image-3.0'). When specified, this model is used exclusively without fallback.",
        },
        "image": {
            "type": "string",
            "description": "Single reference image path or URL for edit mode.",
        },
        "images": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Multiple reference images for edit mode or style reference.",
        },
        "size": {
            "type": "string",
            "description": "Size hint: 1024x1024, 1536x1024, 1024x1536, 2048x2048, 3840x2160.",
        },
        "aspectRatio": {
            "type": "string",
            "description": "Aspect ratio: 1:1, 2:1, 20:9, 19.5:9, 2:3, 3:2, 2.35:1, 3:4, 4:3, 4:5, 5:4, 9:16, 9:19.5, 9:20, 16:9, 21:9, 1:2, 4:1, 1:4, 8:1, 1:8.",
        },
        "resolution": {
            "type": "string",
            "enum": ["1K", "2K", "4K"],
            "description": "Resolution hint.",
        },
        "quality": {
            "type": "string",
            "enum": ["low", "medium", "high", "auto"],
            "description": "Quality hint when supported by provider.",
        },
        "outputFormat": {
            "type": "string",
            "enum": ["png", "jpeg", "webp"],
            "description": "Output format hint when supported by provider.",
        },
        "background": {
            "type": "string",
            "enum": ["transparent", "opaque", "auto"],
            "description": "Background hint. Use 'transparent' with outputFormat 'png' or 'webp' for providers supporting transparency.",
        },
        "count": {
            "type": "number",
            "description": "Number of images to generate (1-4).",
        },
        "timeoutMs": {
            "type": "number",
            "description": "Optional provider request timeout in milliseconds. Defaults to model configuration.",
        },
        "filename": {
            "type": "string",
            "description": "Output filename hint.",
        },
        "openai": {
            "type": "object",
            "description": "OpenAI-specific hints: background, moderation, outputCompression, user.",
            "properties": {
                "background": {"type": "string"},
                "moderation": {"type": "string"},
                "outputCompression": {"type": "number"},
                "user": {"type": "string"},
            },
        },
    },
    "required": [],
}


def register_image_generate_tool():
    """注册 image_generate 工具到全局注册表"""
    tool_registry.register(
        name="image_generate",
        description=(
            "Generate images using configured media models. Supports text-to-image and image-to-image. "
            "Runs in background by default to avoid blocking conversation. "
            "Use action='list' to see available models, action='status' to check running tasks."
        ),
        handler=image_generate,
        parameters=_IMAGE_GENERATE_PARAMS,
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=30,  # 工具本身快速返回（后台执行），超时只针对 handler 返回
        metadata={"system_tool": False},
    )
    logger.info("image_generate tool registered")
