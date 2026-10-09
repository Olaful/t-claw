"""OpenAI Images SDK 生图 Provider（api: openai-images）"""
from typing import Any, Dict, List, Optional

import httpx
import structlog

from .base import ImageProviderBase

logger = structlog.get_logger()


class OpenAIImagesProvider(ImageProviderBase):
    """通过 OpenAI Images SDK 生成图片"""

    api_type = "openai-images"

    async def generate(
        self,
        endpoint: Dict[str, Any],
        prompt: str,
        params: Dict[str, Any],
        images: Optional[List[str]] = None,
        count: int = 1,
        timeout_ms: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        from openai import AsyncOpenAI

        openai_opts: Optional[Dict[str, Any]] = kwargs.get("openai_opts")

        base_url = endpoint.get("base_url")
        api_key = endpoint.get("api_key")
        model_id = endpoint.get("model_id")
        model_params = endpoint.get("params", {}) or {}
        other_cfg = model_params.get("other", {}) or {}
        compat = other_cfg.get("compat", {}) or {}

        client = AsyncOpenAI(
            api_key=api_key or "",
            base_url=base_url or None,
            timeout=httpx.Timeout((timeout_ms or 180000) / 1000.0),
        )

        # 构建 extra_body（从 compat.supportsExtraBody）
        extra_body = {}
        supports_extra = compat.get("supportsExtraBody", {}) or {}
        if supports_extra:
            # image 字段：传入参考图
            if images and "image" in supports_extra:
                extra_body["image"] = images
            # prompt_extend
            if "prompt_extend" in supports_extra:
                extra_body["prompt_extend"] = supports_extra["prompt_extend"]
            if "prompt_extend_mode" in supports_extra:
                extra_body["prompt_extend_mode"] = supports_extra["prompt_extend_mode"]
            # enable_thinking
            if "enable_thinking" in supports_extra:
                extra_body["enable_thinking"] = supports_extra["enable_thinking"]
            # watermark
            if "watermark" in supports_extra:
                extra_body["watermark"] = supports_extra["watermark"]

        # 构建调用参数
        gen_kwargs: Dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "n": count,
        }

        # size
        if params.get("size"):
            gen_kwargs["size"] = params["size"]
        # quality
        if params.get("quality"):
            gen_kwargs["quality"] = params["quality"]
        # outputFormat -> output_format
        if params.get("outputFormat"):
            gen_kwargs["output_format"] = params["outputFormat"]
        # response_format 默认 url
        gen_kwargs["response_format"] = params.get("response_format") or "url"
        # background
        if params.get("background"):
            gen_kwargs["background"] = params["background"]

        # openai 特有参数
        if openai_opts:
            if "background" in openai_opts:
                gen_kwargs["background"] = openai_opts["background"]
            if "moderation" in openai_opts:
                gen_kwargs["moderation"] = openai_opts["moderation"]
            if "outputCompression" in openai_opts:
                extra_body["output_compression"] = openai_opts["outputCompression"]
            if "user" in openai_opts:
                gen_kwargs["user"] = openai_opts["user"]

        if extra_body:
            gen_kwargs["extra_body"] = extra_body

        try:
            response = await client.images.generate(**gen_kwargs)
            # 解析响应
            result_images = []
            for item in (response.data or []):
                img_data = {}
                if hasattr(item, 'url') and item.url:
                    img_data["url"] = item.url
                if hasattr(item, 'b64_json') and item.b64_json:
                    img_data["b64_json"] = item.b64_json
                if hasattr(item, 'revised_prompt') and item.revised_prompt:
                    img_data["revised_prompt"] = item.revised_prompt
                result_images.append(img_data)

            # 保留额外响应字段（不包含 b64_json 数据）
            response_meta = self._extract_response_meta(response)

            return {"success": True, "images": result_images, "response_meta": response_meta}
        except Exception as e:
            logger.error("OpenAI Images generate failed", model=model_id, error=str(e), exc_info=True)
            return {"success": False, "error": str(e)}

    @staticmethod
    def _extract_response_meta(response: Any) -> Dict[str, Any]:
        """从 SDK 响应对象中提取 created/usage/图像属性等元数据"""
        response_meta: Dict[str, Any] = {}
        if hasattr(response, 'created') and response.created:
            response_meta["created"] = response.created
        if hasattr(response, 'usage') and response.usage:
            usage = response.usage
            usage_dict = {}
            if hasattr(usage, 'total_tokens'):
                usage_dict["total_tokens"] = usage.total_tokens
            if hasattr(usage, 'input_tokens'):
                usage_dict["input_tokens"] = usage.input_tokens
            if hasattr(usage, 'output_tokens'):
                usage_dict["output_tokens"] = usage.output_tokens
            if hasattr(usage, 'input_tokens_details') and usage.input_tokens_details:
                details = usage.input_tokens_details
                details_dict = {}
                if hasattr(details, 'text_tokens'):
                    details_dict["text_tokens"] = details.text_tokens
                if hasattr(details, 'image_tokens'):
                    details_dict["image_tokens"] = details.image_tokens
                if details_dict:
                    usage_dict["input_tokens_details"] = details_dict
            if hasattr(usage, 'output_width'):
                usage_dict["output_width"] = usage.output_width
            if hasattr(usage, 'output_height'):
                usage_dict["output_height"] = usage.output_height
            if hasattr(usage, 'output_image_type'):
                usage_dict["output_image_type"] = usage.output_image_type
            if hasattr(usage, 'output_image_count'):
                usage_dict["output_image_count"] = usage.output_image_count
            if hasattr(usage, 'input_image_type'):
                usage_dict["input_image_type"] = usage.input_image_type
            if hasattr(usage, 'input_image_count'):
                usage_dict["input_image_count"] = usage.input_image_count
            if usage_dict:
                response_meta["usage"] = usage_dict
        # 图像属性字段
        for attr in ('background', 'output_format', 'quality', 'size'):
            if hasattr(response, attr) and getattr(response, attr):
                response_meta[attr] = getattr(response, attr)
        return response_meta
