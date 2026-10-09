"""minimax/MiniMax-H3 Provider - 异步任务提交 + 轮询 + 文件上传"""
import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import structlog

from .base import VideoGenerateRequest, VideoProviderBase
from .params import (
    get_api_mode_config,
    get_file_upload_url,
    get_query_task_url,
    resolve_api_url,
)

logger = structlog.get_logger()

# 视频生成较慢，轮询间隔（秒）
_POLL_INTERVAL = 15.0


class MinimaxVideoProvider(VideoProviderBase):
    """MiniMax-H3 视频生成（异步任务）"""

    provider_type = "minimax"
    # minimax 不支持 file 和 link 类型媒体
    unsupported_media = ("files", "links")

    async def generate(self, request: VideoGenerateRequest) -> Dict[str, Any]:
        """构建 content 媒体项 → 提交任务 → 轮询结果"""
        endpoint = request.endpoint
        mode_config = get_api_mode_config(endpoint, "generate-all-in-one")
        if not mode_config:
            return {"success": False, "error": "minimax video model missing 'generate-all-in-one' config"}

        media_items = await self._build_media_items(request)
        submit_result = await self._submit(endpoint, request.prompt, media_items, request.params, mode_config)
        if not submit_result.get("success"):
            return submit_result

        return await self._poll_task(endpoint, submit_result["task_id"], request.timeout_ms)

    # ===== 任务提交 =====

    async def _submit(
        self,
        endpoint: Dict[str, Any],
        prompt: str,
        media_items: List[Dict[str, Any]],
        params: Dict[str, Any],
        mode_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """通过 minimax API 创建视频生成任务"""
        api_key = endpoint.get("api_key", "")
        model_id = endpoint.get("model_id", "MiniMax-H3")

        url = resolve_api_url(endpoint, mode_config)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 构建 content 数组
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]

        for item in media_items:
            item_type = item.get("type", "")
            item_url = item.get("url", "")
            item_role = item.get("role", "")

            if item_type == "image":
                entry: Dict[str, Any] = {"type": "image_url", "image_url": {"url": item_url}}
            elif item_type == "video":
                entry = {"type": "video_url", "video_url": {"url": item_url}}
            elif item_type == "audio":
                entry = {"type": "audio_url", "audio_url": {"url": item_url}}
            else:
                continue

            if item_role:
                entry["role"] = item_role
            content.append(entry)

        body: Dict[str, Any] = {
            "model": model_id,
            "content": content,
        }

        # resolution
        if params.get("resolution"):
            body["resolution"] = params["resolution"]
        # ratio
        if params.get("ratio"):
            body["ratio"] = params["ratio"]
        # duration
        if params.get("duration"):
            body["duration"] = params["duration"]
        # watermark
        if "watermark" in params:
            body["aigc_watermark"] = params["watermark"]

        # extra (default_param 中的 prompt_expansion_mode)
        default_params = mode_config.get("default_param", {}) or {}
        extra = {}
        if "prompt_expansion_mode" in default_params:
            extra["prompt_expansion_mode"] = default_params["prompt_expansion_mode"]
        if extra:
            body["extra"] = extra

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            error_body = ""
            try:
                error_body = e.response.text[:500]
            except Exception:
                pass
            return {"success": False, "error": f"minimax video submit failed HTTP {e.response.status_code}: {error_body}"}
        except Exception as e:
            return {"success": False, "error": f"minimax video submit failed: {str(e)}"}

        task_id = data.get("task_id")
        if not task_id:
            return {"success": False, "error": f"minimax video returned no task_id: {data}"}

        return {"success": True, "task_id": task_id}

    # ===== 任务轮询 =====

    async def _poll_task(self, endpoint: Dict[str, Any], task_id: str, timeout_ms: int = 300000) -> Dict[str, Any]:
        """轮询 minimax 异步任务状态"""
        api_key = endpoint.get("api_key", "")
        query_url_template = get_query_task_url(endpoint)
        if not query_url_template:
            return {"success": False, "error": "No query_task URL configured for minimax"}

        query_url = query_url_template.replace("{task_id}", task_id)
        headers = {"Authorization": f"Bearer {api_key}"}

        deadline = time.time() + timeout_ms / 1000.0

        while time.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(query_url, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as e:
                logger.warning("minimax video poll failed, retrying", task_id=task_id, error=str(e))
                continue

            task = data.get("task", {}) or {}
            status = task.get("status", "")

            if status == "succeeded":
                content = task.get("content", {}) or {}
                video_url = content.get("url", "")
                usage = task.get("usage", {}) or {}
                return {
                    "success": True,
                    "video_url": video_url,
                    "usage": usage,
                    "task_status": status,
                    "duration": task.get("duration"),
                    "resolution": task.get("resolution"),
                    "ratio": task.get("ratio"),
                }
            elif status == "failed":
                error = task.get("error", {}) or {}
                return {"success": False, "error": f"minimax task failed: {error.get('code', '')} - {error.get('message', '')}"}
            elif status in ("queued", "running"):
                continue
            elif status == "cancelled":
                return {"success": False, "error": "minimax task was cancelled"}

        return {"success": False, "error": f"minimax task {task_id} timed out after {timeout_ms}ms"}

    # ===== 文件上传 =====

    async def upload_file(self, endpoint: Dict[str, Any], file_path: str) -> Optional[str]:
        """上传文件到 minimax，返回 mm_file://{file_id} URL"""
        api_key = endpoint.get("api_key", "")
        upload_url = get_file_upload_url(endpoint)
        if not upload_url:
            upload_url = "https://api.minimax.cn/v1/files/upload"

        try:
            file_name = Path(file_path).name
            with open(file_path, "rb") as f:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(
                        upload_url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        files={"file": (file_name, f)},
                        data={"purpose": "video_generation_input"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    file_id = data.get("file", {}).get("file_id")
                    if file_id:
                        return f"mm_file://{file_id}"
            return None
        except Exception as e:
            logger.error("Failed to upload file to minimax", file_path=file_path, error=str(e))
            return None

    # ===== 媒体项构建 =====

    async def _build_media_items(self, request: VideoGenerateRequest) -> List[Dict[str, Any]]:
        """为 minimax 构建 content 中的媒体项（不支持 file/link）"""
        media_items: List[Dict[str, Any]] = []

        for i, img_ref in enumerate(request.images):
            url = await self.resolve_media_url(request.endpoint, img_ref)
            if not url:
                continue
            if request.mode == "image_to_video":
                role = request.image_roles[i] if i < len(request.image_roles) else ("first_frame" if i == 0 else "reference_image")
            else:
                role = request.image_roles[i] if i < len(request.image_roles) else "reference_image"
            media_items.append({"type": "image", "url": url, "role": role})

        for i, vid_ref in enumerate(request.videos):
            url = await self.resolve_media_url(request.endpoint, vid_ref)
            if not url:
                continue
            role = request.video_roles[i] if i < len(request.video_roles) else "reference_video"
            media_items.append({"type": "video", "url": url, "role": role})

        for i, aud_ref in enumerate(request.audios):
            url = await self.resolve_media_url(request.endpoint, aud_ref)
            if not url:
                continue
            role = request.audio_roles[i] if i < len(request.audio_roles) else "reference_audio"
            media_items.append({"type": "audio", "url": url, "role": role})

        return media_items
