"""音乐生成工具 - 编排层

支持多供应商（yinchao 异步轮询 / aliyun HTTP+SSE）的后台异步生乐。
各 provider 的具体实现位于 `music_providers/` 包，本文件只负责：
agent 上下文解析、模型链构建、后台任务编排、结果保存与事件推送、工具注册。
新增 provider 只需在 `music_providers/` 下新建文件并在 factory.py 注册。
"""
import asyncio
import base64
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
from app.tools.music_providers import (
    MusicGenerateRequest,
    get_provider,
    identify_provider,
    get_api_mode_config,
    normalize_music_params,
)

logger = structlog.get_logger()

# ===== 活动任务跟踪（用于 action=status） =====
_active_jobs: Dict[str, Dict[str, Any]] = {}

# ===== 外部上下文注入（斜杠命令等非 agent 调用场景） =====
_external_agent_context: contextvars.ContextVar[Optional[Tuple[str, str, Any]]] = contextvars.ContextVar(
    '_external_music_agent_context', default=None
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
        logger.warning("Failed to resolve music model endpoint", model_ref=model_ref, error=str(e))
        return None


def _build_music_model_chain(
    tool_model_param: Optional[str],
    media_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    构建音乐模型尝试链：
    1. tool_model_param (强制，失败不 fallback)
    2. primary
    3. fallbacks
    """
    chain = []
    music_cfg = media_config.get("music", {}) or {}

    if tool_model_param:
        ep = _resolve_model_endpoint(tool_model_param)
        if ep:
            chain.append({"endpoint": ep, "force": True})
        return chain

    primary = music_cfg.get("primary")
    if primary:
        ep = _resolve_model_endpoint(primary)
        if ep:
            chain.append({"endpoint": ep, "force": False})

    fallbacks = music_cfg.get("fallbacks", []) or []
    for fb in fallbacks:
        ep = _resolve_model_endpoint(fb)
        if ep:
            chain.append({"endpoint": ep, "force": False})

    return chain


# ===== 音频数据保存 =====

async def _save_audio_data(
    audio_data: Dict[str, Any],
    workspace_path: str,
    filename_hint: Optional[str] = None,
    index: int = 0,
    stream_chunks: Optional[List[str]] = None,
    audio_format: str = "mp3",
) -> Dict[str, Any]:
    """
    保存音频数据到会话工作区。

    Returns: {"path": 绝对路径, "url": API访问URL, "filename": 文件名}
    """
    music_dir = os.path.join(workspace_path, "generated_music")
    os.makedirs(music_dir, exist_ok=True)

    # 确定文件名
    if filename_hint:
        base_name = Path(filename_hint).stem
        ext = Path(filename_hint).suffix or f".{audio_format}"
    else:
        base_name = f"music_{int(time.time())}_{index}"
        ext = f".{audio_format}"

    filename = f"{base_name}_{uuid.uuid4().hex[:6]}{ext}"
    filepath = os.path.join(music_dir, filename)

    audio_url = audio_data.get("url", "")

    if stream_chunks:
        # 流式模式: 合并 base64 chunks 保存
        try:
            with open(filepath, "wb") as f:
                for chunk_b64 in stream_chunks:
                    f.write(base64.b64decode(chunk_b64))
            logger.info("Audio saved from stream chunks", path=filepath, chunks=len(stream_chunks))
        except Exception as e:
            logger.error("Failed to save stream chunks", error=str(e))
            return {"error": f"Failed to save audio: {str(e)}"}
    elif audio_url:
        # 下载 URL 音频到本地
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                resp = await client.get(audio_url)
                resp.raise_for_status()
                content = resp.content

            with open(filepath, "wb") as f:
                f.write(content)
            logger.info("Audio downloaded and saved", path=filepath, size=len(content))
        except Exception as e:
            logger.warning("Failed to download audio, keeping URL reference", url=audio_url, error=str(e))
            return {
                "url": audio_url,
                "source": "remote",
                "filename": filename,
            }
    else:
        return {"error": "No audio data (url or chunks) found"}

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

async def _run_music_generation(
    job_id: str,
    conversation_id: str,
    agent_name: str,
    mode: str,
    prompt: str,
    model_chain: List[Dict[str, Any]],
    params: Dict[str, Any],
    timeout_ms: Optional[int],
    filename_hint: Optional[str],
    stream: bool,
    tool_call_id: str,
    workspace_path: str,
    media_config: Dict[str, Any],
    reference: Optional[str] = None,
    references: Optional[List[str]] = None,
    agent_self: Any = None,
):
    """后台执行音乐生成任务"""
    global _active_jobs

    job_info = _active_jobs.get(job_id)
    if job_info:
        job_info["status"] = "running"
        job_info["started_at"] = datetime.now(timezone.utc).isoformat()

    music_cfg = media_config.get("music", {}) or {}
    effective_timeout = timeout_ms or music_cfg.get("timeoutMs") or 180000

    result = None
    used_model = None
    last_error = None
    normalization_details = {}
    ignored_overrides = []

    # 如果是流式模式，先推送 stream_start 事件
    if stream:
        try:
            from app.core.event_bus import event_bus
            await event_bus.emit_event(
                type="music_stream_start",
                source=conversation_id,
                data={
                    "job_id": job_id,
                    "conversation_id": conversation_id,
                    "message_id": f"music_{job_id}",
                    "model": model_chain[0]["endpoint"].get("model_ref", "unknown") if model_chain else "unknown",
                    "prompt": prompt[:200] if prompt else "",
                    "mode": mode,
                },
                priority="normal",
                dimensions=[f"conversation:{conversation_id}"],
            )
        except Exception as e:
            logger.debug("Failed to emit music_stream_start", error=str(e))

    for entry in model_chain:
        ep = entry["endpoint"]
        force = entry["force"]
        model_ref = ep.get("model_ref", "unknown")
        provider_type = identify_provider(ep)
        provider = get_provider(provider_type)

        # 未注册的提供商：记录错误并按 force 决定是否 fallback
        if not provider:
            last_error = f"Unsupported music provider: {provider_type}"
            logger.warning(
                "Music generation failed, trying next model",
                job_id=job_id, model=model_ref, error=last_error, force=force,
            )
            if force:
                break
            continue

        # 获取 mode 对应的 API 配置
        mode_config = get_api_mode_config(ep, mode)
        if not mode_config and mode != "gen_lyric":
            # 如果指定 mode 无配置，尝试 normal
            mode_config = get_api_mode_config(ep, "normal")

        # 参数标准化
        if mode_config:
            norm_params, norm_details, norm_ignored = normalize_music_params(
                params, mode, mode_config, stream=stream
            )
        else:
            norm_params, norm_details, norm_ignored = params, {}, []

        # 构建统一请求载体，交由 provider 做能力协商（修正 mode/stream、登记忽略参数）
        request = MusicGenerateRequest(
            endpoint=ep, mode=mode, prompt=prompt, params=norm_params,
            timeout_ms=effective_timeout, stream=stream,
            conversation_id=conversation_id, job_id=job_id,
            reference=reference, references=references,
        )
        provider.prepare(request)
        # prepare 可能修正 mode/stream 并追加 provider 不支持的参数标记
        norm_ignored = norm_ignored + request.ignored_params

        logger.info(
            "Attempting music generation",
            job_id=job_id, model=model_ref, provider=provider_type,
            mode=request.mode, stream=request.stream,
        )

        gen_result = await provider.generate(request)

        if gen_result.get("success"):
            result = gen_result
            used_model = model_ref
            normalization_details = norm_details
            ignored_overrides = norm_ignored
            break
        else:
            last_error = gen_result.get("error", "Unknown error")
            logger.warning(
                "Music generation failed, trying next model",
                job_id=job_id, model=model_ref, error=last_error, force=force,
            )
            if force:
                break

    # 处理结果
    saved_audios = []
    generated_lyrics = ""
    generated_title = ""

    if result and result.get("success"):
        # gen_lyric 模式只返回歌词
        if mode == "gen_lyric":
            generated_lyrics = result.get("lyrics", "")
            generated_title = result.get("title", "")
            status = "completed"
        else:
            # 保存音频文件
            raw_audios = result.get("audios", [])
            streamed_songs = result.get("streamed_songs", {}) if result.get("streamed") else {}
            audio_format = params.get("format", "mp3")

            for idx, audio_item in enumerate(raw_audios):
                # 按 song_id 匹配对应的 stream chunks
                song_id = audio_item.get("id", "")
                song_chunks = streamed_songs.get(song_id)
                # fallback: 如果只有一首歌且 id 不匹配，用第一个有数据的 entry
                if song_chunks is None and len(streamed_songs) == 1 and len(raw_audios) == 1:
                    song_chunks = next(iter(streamed_songs.values()), None)

                saved = await _save_audio_data(
                    audio_data=audio_item,
                    workspace_path=workspace_path,
                    filename_hint=filename_hint if len(raw_audios) == 1 else None,
                    index=idx,
                    stream_chunks=song_chunks if song_chunks else None,
                    audio_format=audio_format,
                )
                # 携带每首歌的歌词和标题
                if audio_item.get("lyrics"):
                    saved["lyrics"] = audio_item["lyrics"]
                if audio_item.get("title"):
                    saved["title"] = audio_item["title"]
                saved_audios.append(saved)

            # 如果没有 URL 但有 stream chunks（流式可能不返回最终 URL）
            if not saved_audios and streamed_songs:
                for s_idx, (s_id, s_chunks) in enumerate(streamed_songs.items()):
                    if not s_chunks:
                        continue
                    saved = await _save_audio_data(
                        audio_data={},
                        workspace_path=workspace_path,
                        filename_hint=filename_hint if len(streamed_songs) == 1 else None,
                        index=s_idx,
                        stream_chunks=s_chunks,
                        audio_format=audio_format,
                    )
                    saved_audios.append(saved)

            generated_lyrics = result.get("lyrics", "")
            status = "completed"
    else:
        status = "failed"

    # 更新任务状态
    if job_info:
        job_info["status"] = status
        job_info["completed_at"] = datetime.now(timezone.utc).isoformat()
        job_info["result"] = {
            "audios": saved_audios,
            "lyrics": generated_lyrics,
            "title": generated_title,
            "model": used_model,
            "error": last_error if status == "failed" else None,
        }

    # 推送最终事件到前端
    message_id = f"music_{job_id}"
    response_meta = result.get("response_meta", {}) if result else {}

    try:
        from app.core.event_bus import event_bus
        event_data = {
            "message_id": message_id,
            "job_id": job_id,
            "conversation_id": conversation_id,
            "tool_call_id": tool_call_id,
            "status": status,
            "audios": saved_audios,
            "lyrics": generated_lyrics,
            "title": generated_title,
            "model": used_model,
            "mode": mode,
            "prompt": prompt[:200] if prompt else "",
            "details": {
                "normalization": normalization_details,
                "ignored_overrides": ignored_overrides,
                "response_meta": response_meta,
            },
        }
        if status == "failed":
            event_data["error"] = last_error

        await event_bus.emit_event(
            type="music_generated",
            source=conversation_id,
            data=event_data,
            priority="normal",
            dimensions=[f"conversation:{conversation_id}"],
        )
        logger.info("Music generation event emitted", job_id=job_id, status=status, conversation_id=conversation_id)
    except Exception as e:
        logger.error("Failed to emit music_generated event", job_id=job_id, error=str(e), exc_info=True)

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
            "audios": saved_audios,
            "lyrics": generated_lyrics,
            "title": generated_title,
            "response_meta": response_meta,
            "normalization": normalization_details,
            "ignored_overrides": ignored_overrides,
        }
        if status == "failed":
            msg_metadata["error"] = last_error

        if status == "completed":
            content = _json.dumps({
                "status": "completed",
                "audios": [{"url": a.get("url"), "filename": a.get("filename")} for a in saved_audios],
                "lyrics": generated_lyrics[:200] if generated_lyrics else "",
                "title": generated_title,
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
            message_type=MessageType.MUSIC_GENERATED,
            tool_call_id=tool_call_id,
            name="music_generate",
            metadata=msg_metadata,
        )

        await persistence.save_message(conversation_id, msg)

        if agent_self and hasattr(agent_self, 'get_conversation'):
            conv = agent_self.get_conversation(conversation_id)
            if conv:
                conv.messages.append(msg)

        logger.info("Music generation message persisted", job_id=job_id, message_id=message_id)
    except Exception as e:
        logger.error("Failed to persist music_generated message", job_id=job_id, error=str(e), exc_info=True)

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
                if now - ct > 300:
                    to_remove.append(jid)
            except Exception:
                to_remove.append(jid)
    for jid in to_remove:
        del _active_jobs[jid]


# ===== 工具 Handler =====

async def music_generate(
    prompt: str = "",
    action: str = "generate",
    mode: str = "normal",
    model: str = "",
    lyrics: str = "",
    n: int = 2,
    reference: str = "",
    references: list = None,
    durationSeconds: int = 0,
    format: str = "",
    gender: str = "",
    stream: bool = False,
    filename: str = "",
    **kwargs,
) -> Dict[str, Any]:
    """音乐生成工具 handler"""
    # 类型容错
    try:
        n = int(n) if n is not None and n != "" else 2
    except (ValueError, TypeError):
        n = 2
    try:
        durationSeconds = int(durationSeconds) if durationSeconds not in (None, "") else 0
    except (ValueError, TypeError):
        durationSeconds = 0

    # references 容错
    if isinstance(references, str):
        s = references.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                references = _json.loads(s)
            except Exception:
                references = [x.strip().strip("'\"") for x in s[1:-1].split(",") if x.strip()]
        elif s:
            references = [x.strip() for x in s.split(",") if x.strip()]
        else:
            references = None

    # 获取 agent 上下文
    agent_name, conversation_id, agent_self = _get_agent_context()

    # 获取 mediaModels 配置
    media_config = _get_media_models_config(agent_name)
    if not media_config:
        return {
            "success": False,
            "error": f"Agent '{agent_name}' has no mediaModels configured. Cannot generate music.",
        }

    # ===== action: list =====
    if action == "list":
        return _handle_list(media_config)

    # ===== action: status =====
    if action == "status":
        return _handle_status(conversation_id)

    # ===== action: generate =====
    if not prompt and mode != "extend":
        return {"success": False, "error": "Parameter 'prompt' is required for action='generate'"}

    # 验证 mode
    valid_modes = ("normal", "instrumental", "reference", "extend", "gen_lyric")
    if mode not in valid_modes:
        return {"success": False, "error": f"Invalid mode '{mode}'. Must be one of: {', '.join(valid_modes)}"}

    # 构建模型链
    model_chain = _build_music_model_chain(model or None, media_config)
    if not model_chain:
        return {
            "success": False,
            "error": "No music generation model available. Check mediaModels.music configuration.",
        }

    # 构建参数
    params: Dict[str, Any] = {}
    if lyrics:
        params["lyrics"] = lyrics
    if n and n != 2:
        params["n"] = n
    elif n:
        params["n"] = n
    if durationSeconds:
        params["durationSeconds"] = durationSeconds
    if format:
        params["format"] = format
    if gender:
        params["gender"] = gender

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
    job_id = f"music_{uuid.uuid4().hex[:8]}"
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
        _run_music_generation(
            job_id=job_id,
            conversation_id=conversation_id,
            agent_name=agent_name,
            mode=mode,
            prompt=prompt,
            model_chain=model_chain,
            params=params,
            timeout_ms=None,
            filename_hint=filename or None,
            stream=stream,
            tool_call_id=tool_call_id,
            workspace_path=workspace_path,
            media_config=media_config,
            reference=reference or None,
            references=references,
            agent_self=agent_self,
        )
    )

    return {
        "success": True,
        "status": "started",
        "job_id": job_id,
        "message": f"音乐生成任务已提交后台执行（模型: {model_chain[0]['endpoint'].get('model_ref', 'unknown')}，模式: {mode}）。生成完成后将自动推送到会话界面。",
        "model": model_chain[0]["endpoint"].get("model_ref"),
        "mode": mode,
        "stream": stream,
        "available_models": [e["endpoint"].get("model_ref") for e in model_chain],
    }


def _handle_list(media_config: Dict[str, Any]) -> Dict[str, Any]:
    """列出可用的音乐生成供应商和模型"""
    music_cfg = media_config.get("music", {}) or {}
    primary = music_cfg.get("primary", "")
    fallbacks = music_cfg.get("fallbacks", []) or []
    timeout_ms = music_cfg.get("timeoutMs", 180000)

    models = []
    all_refs = ([primary] if primary else []) + list(fallbacks)

    for ref in all_refs:
        ep = _resolve_model_endpoint(ref)
        if ep:
            model_params = ep.get("params", {}) or {}
            other = model_params.get("other", {}) or {}
            api_cfg = other.get("api", {}) or {}
            supported_modes = [k for k in api_cfg.keys() if k != "task_query"]
            models.append({
                "model_ref": ref,
                "provider": ep.get("provider"),
                "model_id": ep.get("model_id"),
                "role": "primary" if ref == primary else "fallback",
                "supported_modes": supported_modes,
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
    """查询当前会话的活动音乐生成任务"""
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

_MUSIC_GENERATE_PARAMS = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "Music generation prompt describing style, mood, theme. Required for action='generate'.",
        },
        "action": {
            "type": "string",
            "enum": ["generate", "status", "list"],
            "description": "'status' returns current session tasks; 'list' checks available providers. Defaults to 'generate'.",
        },
        "mode": {
            "type": "string",
            "enum": ["normal", "instrumental", "reference", "extend", "gen_lyric"],
            "description": "Generation mode. normal: song with vocals, instrumental: pure music, reference: generate from reference audio, extend: extend existing song, gen_lyric: generate lyrics only.",
        },
        "model": {
            "type": "string",
            "description": "Provider/model override (e.g. 'yinchao/v4.0', 'aliyun/fun-music-preview'). When specified, this model is used exclusively without fallback.",
        },
        "lyrics": {
            "type": "string",
            "description": "Optional lyrics when provider supports explicit lyrics input.",
        },
        "n": {
            "type": "number",
            "description": "Number of songs to generate (default 2, max varies by provider).",
        },
        "reference": {
            "type": "string",
            "description": "Single reference audio path or URL (for reference/extend modes).",
        },
        "references": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Multiple reference audio sources.",
        },
        "durationSeconds": {
            "type": "number",
            "description": "Target duration in seconds when provider supports duration hints.",
        },
        "format": {
            "type": "string",
            "enum": ["mp3", "wav"],
            "description": "Output audio format hint when provider supports it.",
        },
        "gender": {
            "type": "string",
            "enum": ["female", "male"],
            "description": "Vocal gender hint when provider supports it.",
        },
        "stream": {
            "type": "boolean",
            "description": "Enable streaming audio output when provider supports it. Streamed audio plays in real-time.",
        },
        "filename": {
            "type": "string",
            "description": "Output filename hint.",
        },
    },
    "required": [],
}


def register_music_generate_tool():
    """注册 music_generate 工具到全局注册表"""
    tool_registry.register(
        name="music_generate",
        description=(
            "Generate music using configured media models. Supports multiple modes: "
            "normal (song with vocals), instrumental (pure music), reference (style from audio or image), "
            "extend (continue a song), gen_lyric (lyrics only). Reference mode can use images as well as audio. "
            "Runs in background by default to avoid blocking conversation. "
            "Use action='list' to see available models, action='status' to check running tasks."
        ),
        handler=music_generate,
        parameters=_MUSIC_GENERATE_PARAMS,
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=30,
        metadata={"system_tool": False},
    )
    logger.info("music_generate tool registered")
