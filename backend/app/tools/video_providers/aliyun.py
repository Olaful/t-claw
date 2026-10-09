"""aliyun/wan3.0-video Provider - 异步任务提交 + 轮询 + OSS 文件上传"""
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


class AliyunVideoProvider(VideoProviderBase):
    """阿里云百炼 wan3.0-video（DashScope 异步任务）"""

    provider_type = "aliyun"

    async def generate(self, request: VideoGenerateRequest) -> Dict[str, Any]:
        """构建 media 数组 → 提交任务 → 轮询结果"""
        endpoint = request.endpoint
        mode_config = get_api_mode_config(endpoint, "generate-all-in-one")
        if not mode_config:
            return {"success": False, "error": "aliyun video model missing 'generate-all-in-one' config"}

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
        media_items: List[Dict[str, str]],
        params: Dict[str, Any],
        mode_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """通过 aliyun wan3.0 API 创建视频生成任务"""
        api_key = endpoint.get("api_key", "")
        model_id = endpoint.get("model_id", "wan3.0-video")

        url = resolve_api_url(endpoint, mode_config)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        # 如果有 oss:// URL，需要加 OssResourceResolve header
        has_oss = any(item.get("url", "").startswith("oss://") for item in media_items)
        if has_oss:
            headers["X-DashScope-OssResourceResolve"] = "enable"

        # 构建 input
        input_obj: Dict[str, Any] = {}
        if prompt:
            input_obj["prompt"] = prompt
        if media_items:
            input_obj["media"] = media_items

        # 构建 parameters
        parameters: Dict[str, Any] = {}
        default_params = mode_config.get("default_param", {}) or {}
        for k, v in default_params.items():
            parameters[k] = v

        if params.get("resolution"):
            parameters["resolution"] = params["resolution"]
        if params.get("ratio"):
            parameters["ratio"] = params["ratio"]
        if params.get("duration"):
            parameters["duration"] = params["duration"]
        if "audio" in params:
            parameters["audio"] = params["audio"]
        if "watermark" in params:
            parameters["watermark"] = params["watermark"]
        if "seed" in params:
            parameters["seed"] = params["seed"]

        body = {"model": model_id, "input": input_obj}
        if parameters:
            body["parameters"] = parameters

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
            return {"success": False, "error": f"aliyun video submit failed HTTP {e.response.status_code}: {error_body}"}
        except Exception as e:
            return {"success": False, "error": f"aliyun video submit failed: {str(e)}"}

        output = data.get("output", {}) or {}
        task_id = output.get("task_id")
        if not task_id:
            return {"success": False, "error": f"aliyun video returned no task_id: {data}"}

        return {"success": True, "task_id": task_id, "request_id": data.get("request_id", "")}

    # ===== 任务轮询 =====

    async def _poll_task(self, endpoint: Dict[str, Any], task_id: str, timeout_ms: int = 300000) -> Dict[str, Any]:
        """轮询阿里云异步任务状态"""
        api_key = endpoint.get("api_key", "")
        query_url_template = get_query_task_url(endpoint)
        if not query_url_template:
            return {"success": False, "error": "No query_task URL configured for aliyun"}

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
                logger.warning("aliyun video poll failed, retrying", task_id=task_id, error=str(e))
                continue

            output = data.get("output", {}) or {}
            task_status = output.get("task_status", "")

            if task_status == "SUCCEEDED":
                video_url = output.get("video_url", "")
                usage = data.get("usage", {}) or {}
                return {
                    "success": True,
                    "video_url": video_url,
                    "usage": usage,
                    "task_status": task_status,
                }
            elif task_status == "FAILED":
                error_code = output.get("code", "")
                error_msg = output.get("message", "")
                return {"success": False, "error": f"aliyun task failed: {error_code} - {error_msg}"}
            elif task_status in ("PENDING", "RUNNING"):
                continue
            elif task_status == "CANCELED":
                return {"success": False, "error": "aliyun task was canceled"}
            elif task_status == "UNKNOWN":
                return {"success": False, "error": "aliyun task unknown or expired"}

        return {"success": False, "error": f"aliyun task {task_id} timed out after {timeout_ms}ms"}

    # ===== 文件上传 =====

    async def upload_file(self, endpoint: Dict[str, Any], file_path: str) -> Optional[str]:
        """上传文件到阿里云百炼临时存储，返回 oss:// URL"""
        api_key = endpoint.get("api_key", "")
        upload_url = get_file_upload_url(endpoint)
        if not upload_url:
            upload_url = "https://dashscope.aliyuncs.com/api/v1/uploads"
        model_name = endpoint.get("model_id", "")

        try:
            # Step 1: 获取上传凭证
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    upload_url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    params={"action": "getPolicy", "model": model_name},
                )
                resp.raise_for_status()
                policy_data = resp.json().get("data", {})

            # Step 2: 上传文件到 OSS
            file_name = Path(file_path).name
            key = f"{policy_data['upload_dir']}/{file_name}"

            with open(file_path, "rb") as f:
                files = {
                    "OSSAccessKeyId": (None, policy_data["oss_access_key_id"]),
                    "Signature": (None, policy_data["signature"]),
                    "policy": (None, policy_data["policy"]),
                    "x-oss-object-acl": (None, policy_data["x_oss_object_acl"]),
                    "x-oss-forbid-overwrite": (None, policy_data["x_oss_forbid_overwrite"]),
                    "key": (None, key),
                    "success_action_status": (None, "200"),
                    "file": (file_name, f),
                }
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(policy_data["upload_host"], files=files)
                    resp.raise_for_status()

            return f"oss://{key}"
        except Exception as e:
            logger.error("Failed to upload file to aliyun", file_path=file_path, error=str(e))
            return None

    # ===== 媒体项构建 =====

    async def _build_media_items(self, request: VideoGenerateRequest) -> List[Dict[str, str]]:
        """为 aliyun 构建 media 数组"""
        media_items: List[Dict[str, str]] = []

        # 图片处理
        for i, img_ref in enumerate(request.images):
            url = await self.resolve_media_url(request.endpoint, img_ref)
            if not url:
                continue
            if request.mode == "image_to_video":
                # 默认第一张为 first_frame
                role = request.image_roles[i] if i < len(request.image_roles) else ("first_frame" if i == 0 else "reference_image")
            else:
                role = request.image_roles[i] if i < len(request.image_roles) else "reference_image"
            media_items.append({"type": role, "url": url})

        # 视频处理
        for i, vid_ref in enumerate(request.videos):
            url = await self.resolve_media_url(request.endpoint, vid_ref)
            if not url:
                continue
            role = request.video_roles[i] if i < len(request.video_roles) else "reference_video"
            media_items.append({"type": role, "url": url})

        # 音频处理
        for i, aud_ref in enumerate(request.audios):
            url = await self.resolve_media_url(request.endpoint, aud_ref)
            if not url:
                continue
            role = request.audio_roles[i] if i < len(request.audio_roles) else "reference_audio"
            media_items.append({"type": role, "url": url})

        # 文件处理
        for i, file_ref in enumerate(request.files):
            url = await self.resolve_media_url(request.endpoint, file_ref)
            if not url:
                continue
            role = request.file_roles[i] if i < len(request.file_roles) else "file"
            media_items.append({"type": role, "url": url})

        # 链接处理（链接不需要上传）
        for i, link_ref in enumerate(request.links):
            role = request.link_roles[i] if i < len(request.link_roles) else "link"
            media_items.append({"type": role, "url": link_ref})

        return media_items
