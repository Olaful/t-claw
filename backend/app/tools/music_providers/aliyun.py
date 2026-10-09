"""aliyun Fun-Music 音乐生成 Provider - HTTP 同步 / SSE 流式"""
import json as _json
from typing import Any, Dict, List

import httpx
import structlog

from .base import MusicGenerateRequest, MusicProviderBase
from .params import get_api_mode_config, resolve_api_url

logger = structlog.get_logger()


class AliyunMusicProvider(MusicProviderBase):
    """aliyun Fun-Music 音乐生成（仅支持 normal/instrumental 模式）"""

    provider_type = "aliyun"
    unsupported_params = ("durationSeconds", "references")
    # aliyun SSE 流式不受 mode 限制（仅 normal/instrumental 可达 generate）
    stream_modes = ()

    def prepare(self, request: MusicGenerateRequest) -> None:
        # aliyun 只支持 normal/instrumental 模式，其余降级为 normal
        if request.mode not in ("normal", "instrumental"):
            request.add_ignored(f"mode:{request.mode}(aliyun only supports normal/instrumental)")
            request.mode = "normal"
        super().prepare(request)

    async def generate(self, request: MusicGenerateRequest) -> Dict[str, Any]:
        endpoint = request.endpoint
        api_key = endpoint.get("api_key", "")
        model_id = endpoint.get("model_id", "fun-music-preview")

        mode_config = get_api_mode_config(endpoint, request.mode)
        if not mode_config:
            # aliyun 只支持 normal 模式（含 instrumental）
            mode_config = get_api_mode_config(endpoint, "normal")
            if not mode_config:
                return {"success": False, "error": f"aliyun model does not support mode '{request.mode}'"}

        url = resolve_api_url(endpoint, mode_config)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 构建 input 对象
        input_obj: Dict[str, Any] = {}
        if request.prompt:
            input_obj["prompt"] = request.prompt
        params = request.params
        if params.get("lyrics"):
            input_obj["lyrics"] = params["lyrics"]
        if params.get("gender"):
            input_obj["gender"] = params["gender"]
        if params.get("format"):
            input_obj["format"] = params["format"]
        # instrumental 模式
        if request.mode == "instrumental":
            input_obj["is_instrumental"] = True

        # 合并 default_param
        default_params = mode_config.get("default_param", {}) or {}
        for k, v in default_params.items():
            if k not in input_obj:
                input_obj[k] = v

        body = {
            "model": model_id,
            "input": input_obj,
        }

        if request.stream:
            headers["X-DashScope-SSE"] = "enable"
            return await self._stream_request(
                url=url, headers=headers, body=body,
                timeout_ms=request.timeout_ms,
                conversation_id=request.conversation_id, job_id=request.job_id,
            )
        else:
            return await self._sync_request(url=url, headers=headers, body=body, timeout_ms=request.timeout_ms)

    async def _sync_request(
        self, url: str, headers: Dict[str, str], body: Dict[str, Any], timeout_ms: int,
    ) -> Dict[str, Any]:
        """aliyun 非流式请求"""
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            error_body = ""
            try:
                error_body = e.response.text[:500]
            except Exception:
                pass
            return {"success": False, "error": f"aliyun HTTP {e.response.status_code}: {error_body}"}
        except Exception as e:
            return {"success": False, "error": f"aliyun request failed: {str(e)}"}

        output = data.get("output", {}) or {}
        audio = output.get("audio", {}) or {}
        extra_info = output.get("extra_info", {}) or {}
        usage = data.get("usage", {}) or {}

        audio_url = audio.get("url", "")
        if not audio_url:
            return {"success": False, "error": f"aliyun returned no audio URL: {data}"}

        return {
            "success": True,
            "audios": [{"url": audio_url, "id": audio.get("id", ""), "title": ""}],
            "lyrics": extra_info.get("lyrics", ""),
            "response_meta": {
                "request_id": data.get("request_id", ""),
                "channels": extra_info.get("channels"),
                "sample_rate": extra_info.get("sample_rate"),
                "duration": usage.get("duration"),
                "expires_at": audio.get("expires_at"),
            },
        }

    async def _stream_request(
        self,
        url: str,
        headers: Dict[str, str],
        body: Dict[str, Any],
        timeout_ms: int,
        conversation_id: str,
        job_id: str,
    ) -> Dict[str, Any]:
        """aliyun SSE 流式请求 - 逐块推送 base64 音频数据"""
        from app.core.event_bus import event_bus

        all_chunks: List[str] = []  # base64 chunks
        audio_id = ""
        final_url = ""
        extra_info = {}
        usage = {}
        request_id = ""

        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if not data_str:
                                continue
                            try:
                                chunk_data = _json.loads(data_str)
                            except _json.JSONDecodeError:
                                continue

                            request_id = chunk_data.get("request_id", request_id)
                            output = chunk_data.get("output", {}) or {}
                            audio = output.get("audio", {}) or {}
                            finish_reason = output.get("finish_reason")

                            # 累积 base64 数据
                            b64_data = audio.get("data", "")
                            if b64_data:
                                all_chunks.append(b64_data)
                                # 推送 chunk 到前端（带 song_id 统一接口）
                                try:
                                    await event_bus.emit_event(
                                        type="music_stream_chunk",
                                        source=conversation_id,
                                        data={
                                            "job_id": job_id,
                                            "conversation_id": conversation_id,
                                            "song_id": "0",
                                            "chunk_index": len(all_chunks) - 1,
                                            "data": b64_data,
                                        },
                                        priority="normal",
                                        dimensions=[f"conversation:{conversation_id}"],
                                    )
                                except Exception as e:
                                    logger.debug("Failed to emit stream chunk", error=str(e))

                            # 音频 ID
                            if audio.get("id"):
                                audio_id = audio["id"]

                            # 最终消息包含 url
                            if audio.get("url"):
                                final_url = audio["url"]

                            # extra_info
                            if output.get("extra_info"):
                                extra_info = output["extra_info"]

                            # usage
                            if chunk_data.get("usage"):
                                usage = chunk_data["usage"]

                            # 完成
                            if finish_reason == "stop":
                                break

        except httpx.HTTPStatusError as e:
            error_body = ""
            try:
                error_body = (await e.response.aread()).decode()[:500]
            except Exception:
                pass
            return {"success": False, "error": f"aliyun stream HTTP {e.response.status_code}: {error_body}"}
        except Exception as e:
            return {"success": False, "error": f"aliyun stream failed: {str(e)}"}

        if not all_chunks and not final_url:
            return {"success": False, "error": "aliyun stream returned no audio data"}

        return {
            "success": True,
            "streamed": True,
            "streamed_songs": {"0": all_chunks},
            "audios": [{"url": final_url, "id": audio_id, "title": ""}] if final_url else [],
            "lyrics": extra_info.get("lyrics", ""),
            "response_meta": {
                "request_id": request_id,
                "channels": extra_info.get("channels"),
                "sample_rate": extra_info.get("sample_rate"),
                "duration": usage.get("duration"),
                "stream_chunks_count": len(all_chunks),
            },
        }
