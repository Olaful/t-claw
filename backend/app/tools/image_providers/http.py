"""通用 HTTP 生图 Provider（api: http）

通过模型配置的 other.url / other.method / other.auth 发起 HTTP 请求，
并按 other.response JSON schema（is_image 标记）提取图片数据。
"""
from typing import Any, Dict, List, Optional

import httpx
import structlog

from .base import ImageProviderBase

logger = structlog.get_logger()


class HttpImageProvider(ImageProviderBase):
    """通过通用 HTTP 请求生成图片"""

    api_type = "http"

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
        model_params = endpoint.get("params", {}) or {}
        other_cfg = model_params.get("other", {}) or {}

        url = other_cfg.get("url")
        method = other_cfg.get("method", "POST").upper()
        auth_type = other_cfg.get("auth", "Bearer")
        api_key = endpoint.get("api_key", "")

        if not url:
            return {"success": False, "error": "HTTP model missing 'url' configuration"}

        # 构建请求头
        headers = {"Content-Type": "application/json"}
        if auth_type == "Bearer" and api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 构建请求体
        body: Dict[str, Any] = {"prompt": prompt}
        body["model"] = endpoint.get("model_id", "")

        # 通过 tool_param_map 映射参数名
        params_schema = other_cfg.get("params", {}) or {}

        # params.properties 定义
        properties = params_schema.get("properties", {}) or {}

        for key, value in params.items():
            # 如果已经在 normalize 阶段通过 tool_param_map 重命名了，直接使用
            body[key] = value

        # 如果有 images 且模型支持 image input
        if images:
            model_input = model_params.get("input", [])
            if "image" in model_input:
                body["image"] = images if len(images) > 1 else images[0]

        if count > 1:
            body["n"] = count

        # 添加默认值（从 properties 中读取 default）
        for prop_name, prop_def in properties.items():
            if prop_name not in body and "default" in prop_def:
                body[prop_name] = prop_def["default"]

        # watermark_enabled 特殊处理
        if "watermark_enabled" in properties and "watermark_enabled" not in body:
            body["watermark_enabled"] = properties["watermark_enabled"].get("default", False)

        try:
            timeout_s = (timeout_ms or 180000) / 1000.0
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                if method == "POST":
                    resp = await client.post(url, json=body, headers=headers)
                else:
                    resp = await client.request(method, url, json=body, headers=headers)

                resp.raise_for_status()
                resp_data = resp.json()

            # 按 response schema 提取图片和其他字段
            extracted = self._extract_response(resp_data, other_cfg)
            return {"success": True, "images": extracted["images"], "response_meta": extracted["other"]}
        except httpx.HTTPStatusError as e:
            error_body = ""
            try:
                error_body = e.response.text[:500]
            except Exception:
                pass
            logger.error("HTTP image generate failed", url=url, status=e.response.status_code, error=error_body)
            return {"success": False, "error": f"HTTP {e.response.status_code}: {error_body}"}
        except Exception as e:
            logger.error("HTTP image generate failed", url=url, error=str(e), exc_info=True)
            return {"success": False, "error": str(e)}

    # ===== 响应提取 =====

    @staticmethod
    def _extract_response(resp_data: Dict[str, Any], other_cfg: Dict[str, Any]) -> Dict[str, Any]:
        """根据模型配置的 response JSON schema 从 HTTP 响应中提取图片数据和其他字段。

        通过 schema 中字段的 `is_image: true` 标记判断是否为图片数据（URL 或 base64）。

        Args:
            resp_data: HTTP 响应 JSON
            other_cfg: 模型配置的 other 字段（含 response schema）

        Returns:
            {
                "images": [{"url": ...} | {"b64_json": ...}, ...],  # is_image 标记的字段
                "other": {field: value, ...}                         # 非图片字段
            }
        """
        images: List[Dict[str, Any]] = []
        other: Dict[str, Any] = {}

        # 获取 response schema
        response_schema = other_cfg.get("response", {}) or {}
        schema_properties = response_schema.get("properties", {}) or {}

        if not schema_properties:
            # 无 response schema 配置，回退到默认提取逻辑
            return HttpImageProvider._extract_response_fallback(resp_data)

        for field_name, field_schema in schema_properties.items():
            field_value = resp_data.get(field_name)
            if field_value is None:
                continue

            field_type = field_schema.get("type", "")

            # 情况 1：字段本身标记为 is_image（顶层图片字段）
            if field_schema.get("is_image"):
                images.append({"url": field_value} if isinstance(field_value, str) and field_value.startswith("http") else {"b64_json": field_value})
                continue

            # 情况 2：数组类型，检查 items.properties 中是否有 is_image 字段
            if field_type == "array" and isinstance(field_value, list):
                items_schema = field_schema.get("items", {}) or {}
                items_properties = items_schema.get("properties", {}) or {}

                # 检查 items 中是否有 is_image 标记的字段
                image_fields = [k for k, v in items_properties.items() if v.get("is_image")]

                if image_fields:
                    # 此数组包含图片数据
                    for item in field_value:
                        if isinstance(item, dict):
                            img_entry = {}
                            for img_field in image_fields:
                                val = item.get(img_field)
                                if val:
                                    # 根据字段名推断类型
                                    if "b64" in img_field or "base64" in img_field:
                                        img_entry["b64_json"] = val
                                    else:
                                        img_entry["url"] = val
                            if img_entry:
                                images.append(img_entry)
                        elif isinstance(item, str):
                            # 直接是 URL 字符串
                            images.append({"url": item})
                else:
                    # 数组中无图片字段，归入 other
                    other[field_name] = field_value
                continue

            # 情况 3：普通字段，归入 other
            other[field_name] = field_value

        return {"images": images, "other": other}

    @staticmethod
    def _extract_response_fallback(resp_data: Dict[str, Any]) -> Dict[str, Any]:
        """无 response schema 时的回退提取逻辑（兼容旧配置）"""
        images: List[Dict[str, Any]] = []
        other: Dict[str, Any] = {}

        data_list = resp_data.get("data", [])
        if isinstance(data_list, list):
            for item in data_list:
                if isinstance(item, dict):
                    img = {}
                    if item.get("url"):
                        img["url"] = item["url"]
                    if item.get("b64_json"):
                        img["b64_json"] = item["b64_json"]
                    if img:
                        images.append(img)
                elif isinstance(item, str):
                    images.append({"url": item})

        # 其余字段归入 other
        for key, value in resp_data.items():
            if key != "data":
                other[key] = value

        return {"images": images, "other": other}
