"""yinchao 音乐生成 Provider - 异步轮询模式，支持流式播放与参考音频上传"""
import asyncio
import base64
import os
import time
from typing import Any, Dict, List, Optional

import httpx
import structlog

from .base import MusicGenerateRequest, MusicProviderBase
from .params import get_api_mode_config, resolve_api_url

logger = structlog.get_logger()

# yinchao 仅支持音频参考
_AUDIO_EXTS = ('.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.opus')

# 文件扩展名 -> MIME 类型
_MIME_MAP = {
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.flac': 'audio/flac',
    '.ogg': 'audio/ogg',
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac',
    '.wma': 'audio/x-ms-wma',
    '.opus': 'audio/opus',
}


def _get_task_query_url(endpoint: Dict[str, Any]) -> Optional[str]:
    """获取 task_query URL（轮询用）"""
    model_params = endpoint.get("params", {}) or {}
    other = model_params.get("other", {}) or {}
    api_cfg = other.get("api", {}) or {}
    tq = api_cfg.get("task_query")
    if tq:
        return resolve_api_url(endpoint, tq)
    return None


def _get_stream_url(endpoint: Dict[str, Any], song_id: str) -> Optional[str]:
    """获取流式播放 URL（stream 端点）"""
    model_params = endpoint.get("params", {}) or {}
    other = model_params.get("other", {}) or {}
    api_cfg = other.get("api", {}) or {}
    stream_cfg = api_cfg.get("stream")
    if stream_cfg:
        url_template = stream_cfg.get("url", "")
        # 替换 {song_id} 占位符
        resolved = url_template.replace("{song_id}", song_id)
        if resolved.startswith("http://") or resolved.startswith("https://"):
            return resolved
        base_url = (endpoint.get("base_url", "") or "").rstrip("/")
        if "/compatible-mode" in base_url:
            from urllib.parse import urlparse
            parsed = urlparse(base_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        return f"{base_url}{resolved}"
    # 默认 fallback
    base_url = (endpoint.get("base_url", "") or "").rstrip("/")
    return f"{base_url}/api/v1/song/stream/{song_id}"


async def _upload_audio_file(
    base_url: str,
    api_key: str,
    audio_ref: str,
    upload_type: str = "reference",
) -> Optional[str]:
    """上传音频文件到 yinchao，返回 upload_id"""
    # 解析音频来源
    original_filename = "audio.mp3"  # 默认名称
    if audio_ref.startswith(("http://", "https://")):
        # 远程 URL: 先下载到临时文件再上传
        try:
            # 从 URL 提取文件名
            from urllib.parse import urlparse, unquote
            url_path = urlparse(audio_ref).path
            if url_path:
                original_filename = unquote(url_path.rsplit('/', 1)[-1]) or original_filename
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(audio_ref)
                resp.raise_for_status()
                audio_bytes = resp.content
        except Exception as e:
            logger.error("Failed to download reference audio", url=audio_ref, error=str(e))
            return None
    elif audio_ref.startswith("/api/v1/chat/files/"):
        # 本地文件 URL
        rel = audio_ref[len("/api/v1/chat/files/"):]
        # 提取文件名（去除查询参数）
        original_filename = rel.split('?')[0].rsplit('/', 1)[-1] or original_filename
        try:
            from app.api.v1.chat import WORKSPACE_ROOT_PATH
            resolved = (WORKSPACE_ROOT_PATH / rel).resolve()
            if not str(resolved).startswith(str(WORKSPACE_ROOT_PATH.resolve())):
                return None
            with open(resolved, "rb") as f:
                audio_bytes = f.read()
        except Exception as e:
            logger.error("Failed to read local audio file", path=audio_ref, error=str(e))
            return None
    else:
        # 本地路径
        from app.core.workspace_isolation import resolve_ws_path
        abs_path = resolve_ws_path(audio_ref)
        if not os.path.isfile(abs_path):
            logger.error("Audio file not found", path=audio_ref)
            return None
        original_filename = os.path.basename(abs_path)
        with open(abs_path, "rb") as f:
            audio_bytes = f.read()

    # 根据文件扩展名推断 MIME 类型
    ext = os.path.splitext(original_filename)[1].lower()
    mime_type = _MIME_MAP.get(ext, 'audio/mpeg')

    # 上传到 yinchao
    upload_url = f"{base_url.rstrip('/')}/api/v1/file/upload"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                upload_url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (original_filename, audio_bytes, mime_type)},
                data={"upload_type": upload_type},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("id")
    except httpx.HTTPStatusError as e:
        # 记录详细错误信息以便调试
        error_body = ""
        try:
            error_body = e.response.text[:500]
        except Exception:
            pass
        logger.error("Failed to upload audio to yinchao",
                     status=e.response.status_code,
                     filename=original_filename,
                     mime=mime_type,
                     size=len(audio_bytes),
                     error=error_body)
        return None
    except Exception as e:
        logger.error("Failed to upload audio to yinchao", error=str(e))
        return None


async def _stream_song(
    endpoint: Dict[str, Any],
    song_id: str,
    conversation_id: str,
    job_id: str,
    timeout_ms: int = 180000,
) -> Dict[str, Any]:
    """从 yinchao stream 端点读取 MP3 二进制流，逐块推送 base64 到前端"""
    from app.core.event_bus import event_bus

    stream_url = _get_stream_url(endpoint, song_id)
    if not stream_url:
        return {"success": False, "error": f"Cannot resolve stream URL for song {song_id}"}

    api_key = endpoint.get("api_key", "")
    headers = {"Authorization": f"Bearer {api_key}"}

    all_chunks: List[str] = []
    chunk_size = 8192  # 8KB per chunk

    try:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
            async with client.stream("GET", stream_url, headers=headers) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(chunk_size):
                    if chunk:
                        b64_data = base64.b64encode(chunk).decode("ascii")
                        all_chunks.append(b64_data)
                        # 推送 chunk 到前端（带 song_id 区分多首歌）
                        try:
                            await event_bus.emit_event(
                                type="music_stream_chunk",
                                source=conversation_id,
                                data={
                                    "job_id": job_id,
                                    "conversation_id": conversation_id,
                                    "song_id": song_id,
                                    "chunk_index": len(all_chunks) - 1,
                                    "data": b64_data,
                                },
                                priority="normal",
                                dimensions=[f"conversation:{conversation_id}"],
                            )
                        except Exception as e:
                            logger.debug("Failed to emit yinchao stream chunk", error=str(e))
    except httpx.HTTPStatusError as e:
        error_body = ""
        try:
            error_body = (await e.response.aread()).decode()[:500]
        except Exception:
            pass
        return {"success": False, "error": f"yinchao stream HTTP {e.response.status_code}: {error_body}"}
    except Exception as e:
        return {"success": False, "error": f"yinchao stream read failed: {str(e)}"}

    if not all_chunks:
        return {"success": False, "error": f"yinchao stream returned no data for song {song_id}"}

    return {
        "success": True,
        "chunks": all_chunks,
        "song_id": song_id,
    }


class YinchaoMusicProvider(MusicProviderBase):
    """yinchao 音乐生成（异步提交任务 + 轮询状态，支持并发流式读取）"""

    provider_type = "yinchao"
    unsupported_params = ("durationSeconds", "gender", "format")
    stream_modes = ("normal", "instrumental", "reference", "extend")

    async def generate(self, request: MusicGenerateRequest) -> Dict[str, Any]:
        endpoint = request.endpoint
        mode = request.mode
        prompt = request.prompt
        params = request.params
        timeout_ms = request.timeout_ms

        # yinchao 仅支持音频参考，过滤非音频文件
        references = request.references
        reference = request.reference
        if references:
            references = [r for r in references if any(r.lower().split('?')[0].endswith(ext) for ext in _AUDIO_EXTS)]
        if reference and not any(reference.lower().split('?')[0].endswith(ext) for ext in _AUDIO_EXTS):
            reference = None

        base_url = endpoint.get("base_url", "")
        api_key = endpoint.get("api_key", "")
        model_id = endpoint.get("model_id", "v4.0")

        mode_config = get_api_mode_config(endpoint, mode)
        if not mode_config:
            return {"success": False, "error": f"yinchao model does not support mode '{mode}'"}

        url = resolve_api_url(endpoint, mode_config)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # gen_lyric 为同步接口
        if mode == "gen_lyric":
            body = {"prompt": prompt}
            try:
                async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
                    resp = await client.post(url, json=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                return {
                    "success": True,
                    "lyrics": data.get("lyric", ""),
                    "title": data.get("title", ""),
                    "response_meta": data,
                }
            except Exception as e:
                return {"success": False, "error": f"gen_lyric failed: {str(e)}"}

        # 构建请求体
        body: Dict[str, Any] = {"model": model_id}

        if mode == "normal":
            body["task_type"] = "normal"
            body["prompt"] = prompt
            if params.get("lyrics"):
                body["lyric"] = params["lyrics"]
            if params.get("n"):
                body["n"] = params["n"]
        elif mode == "instrumental":
            body["prompt"] = prompt
            if params.get("n"):
                body["n"] = params["n"]
        elif mode == "reference":
            body["task_type"] = "reference"
            if prompt:
                body["prompt"] = prompt
            if params.get("lyrics"):
                body["lyric"] = params["lyrics"]
            if params.get("n"):
                body["n"] = params["n"]
            # 上传参考音频
            audio_ref = reference or (references[0] if references else None)
            if audio_ref:
                upload_id = await _upload_audio_file(base_url, api_key, audio_ref, "reference")
                if upload_id:
                    body["reference_audio"] = {"audio_type": "upload_id", "audio_content": upload_id}
                else:
                    return {"success": False, "error": "Failed to upload reference audio"}
            # similarity
            default_params = mode_config.get("default_param", {}) or {}
            if "similarity" in default_params:
                body["similarity"] = default_params["similarity"]
        elif mode == "extend":
            if prompt:
                body["prompt"] = prompt
            if params.get("lyrics"):
                body["lyric"] = params["lyrics"]
            if params.get("n"):
                body["n"] = params["n"]
            # 上传原始音频
            audio_ref = reference or (references[0] if references else None)
            if audio_ref:
                upload_id = await _upload_audio_file(base_url, api_key, audio_ref, "extend")
                if upload_id:
                    body["origin_audio"] = {"audio_type": "upload_id", "audio_content": upload_id}
                else:
                    return {"success": False, "error": "Failed to upload origin audio for extend"}
            else:
                return {"success": False, "error": "extend mode requires a reference audio"}
            # extend_at
            default_params = mode_config.get("default_param", {}) or {}
            extend_at = default_params.get("extend_at", -1)
            if extend_at and extend_at > 0:
                body["extend_at"] = extend_at

        # 合并 default_param（不覆盖已有字段）
        default_params = mode_config.get("default_param", {}) or {}
        for k, v in default_params.items():
            if k not in body:
                body[k] = v

        # 提交任务
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                task_data = resp.json()
        except httpx.HTTPStatusError as e:
            error_body = ""
            try:
                error_body = e.response.text[:500]
            except Exception:
                pass
            return {"success": False, "error": f"yinchao submit failed HTTP {e.response.status_code}: {error_body}"}
        except Exception as e:
            return {"success": False, "error": f"yinchao submit failed: {str(e)}"}

        task_id = task_data.get("id")
        if not task_id:
            return {"success": False, "error": f"yinchao returned no task_id: {task_data}"}

        return await self._poll_task(
            endpoint=endpoint, task_id=task_id, api_key=api_key, base_url=base_url,
            timeout_ms=timeout_ms, stream=request.stream,
            conversation_id=request.conversation_id, job_id=request.job_id,
        )

    async def _poll_task(
        self,
        endpoint: Dict[str, Any],
        task_id: str,
        api_key: str,
        base_url: str,
        timeout_ms: int,
        stream: bool,
        conversation_id: str,
        job_id: str,
    ) -> Dict[str, Any]:
        """轮询任务状态，流式模式下并发读取 status=stream 的歌曲"""
        task_query_url = _get_task_query_url(endpoint)
        if not task_query_url:
            task_query_url = f"{base_url.rstrip('/')}/api/v1/task/query"

        deadline = time.time() + timeout_ms / 1000.0
        poll_interval = 3.0
        streamed_songs: Dict[str, List[str]] = {}  # song_id -> base64 chunks
        stream_tasks: Dict[str, asyncio.Task] = {}  # song_id -> concurrent stream task

        async def _stream_and_collect(sid: str):
            """后台并发流式读取单首歌"""
            result = await _stream_song(
                endpoint=endpoint,
                song_id=sid,
                conversation_id=conversation_id,
                job_id=job_id,
                timeout_ms=timeout_ms,
            )
            if result.get("success"):
                streamed_songs[sid] = result.get("chunks", [])
            else:
                logger.warning("yinchao stream failed for song", song_id=sid, error=result.get("error"))
                streamed_songs[sid] = []

        while time.time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        task_query_url,
                        params={"task_id": task_id},
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    resp.raise_for_status()
                    poll_data = resp.json()
            except Exception as e:
                logger.warning("yinchao poll failed, retrying", task_id=task_id, error=str(e))
                continue

            choices = poll_data.get("choices", []) or []
            if not choices:
                continue

            # 流式模式：检测 status="stream" 的歌曲，并发启动流式读取
            if stream and conversation_id and job_id:
                for song in choices:
                    song_id = song.get("id", "")
                    song_status = song.get("status", "")
                    if song_status == "stream" and song_id and song_id not in streamed_songs and song_id not in stream_tasks:
                        logger.info("yinchao song streaming started", song_id=song_id, job_id=job_id)
                        stream_tasks[song_id] = asyncio.create_task(_stream_and_collect(song_id))

            # 检查是否所有 choice 都完成
            all_done = all(c.get("status") in ("done", "fail") for c in choices)
            if all_done:
                # 等待所有流式任务完成
                if stream_tasks:
                    pending = [t for t in stream_tasks.values() if not t.done()]
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)

                # 提取成功的结果
                audios = []
                for song in choices:
                    if song.get("status") == "done":
                        audios.append({
                            "url": song.get("audio_url", ""),
                            "title": song.get("title", ""),
                            "id": song.get("id", ""),
                            "lyrics": song.get("lyric", ""),
                        })
                    elif song.get("status") == "fail":
                        logger.warning("yinchao song failed", error=song.get("error", ""))

                if audios:
                    # 合并所有歌曲歌词（兼容旧逻辑）
                    all_lyrics = [a.get("lyrics", "") for a in audios if a.get("lyrics")]
                    combined_lyrics = "\n\n---\n\n".join(all_lyrics) if len(all_lyrics) > 1 else (all_lyrics[0] if all_lyrics else "")
                    result = {
                        "success": True,
                        "audios": audios,
                        "lyrics": combined_lyrics,
                        "response_meta": {"task_id": task_id, "choices_count": len(choices)},
                    }
                    # 返回所有歌曲的流式 chunks（按 song_id 索引）
                    if streamed_songs and any(v for v in streamed_songs.values()):
                        result["streamed"] = True
                        result["streamed_songs"] = streamed_songs
                    return result
                else:
                    errors = [c.get("error", "unknown") for c in choices if c.get("status") == "fail"]
                    return {"success": False, "error": f"All songs failed: {'; '.join(errors)}"}

        # 超时前取消未完成的流式任务
        for t in stream_tasks.values():
            if not t.done():
                t.cancel()

        return {"success": False, "error": f"yinchao task {task_id} timed out after {timeout_ms}ms"}
