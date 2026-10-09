"""图片生成 Provider 抽象基类"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ImageProviderBase(ABC):
    """图片生成提供商抽象基类

    子类须声明 `api_type`（对应模型 endpoint 的 `api` 字段），
    并在 factory.py 的 `_PROVIDER_MAP` 中注册。

    generate() 统一返回结构:
    - 成功: {"success": True, "images": [{"url": ...} | {"b64_json": ...}, ...], "response_meta": {...}}
    - 失败: {"success": False, "error": "..."}
    """

    api_type: str = ""

    @abstractmethod
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
        """执行图片生成

        Args:
            endpoint: 模型 endpoint 信息（含 api_key/base_url/model_id/params 等）
            prompt: 生图提示词
            params: 已标准化的参数（normalize_params 输出）
            images: 参考图列表（URL 或 data URI）
            count: 生成数量
            timeout_ms: 请求超时（毫秒）
            **kwargs: Provider 特有选项（如 openai_opts）
        """
        ...
