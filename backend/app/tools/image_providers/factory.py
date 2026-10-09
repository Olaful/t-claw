"""图片生成 Provider 工厂 - 新增 provider 只需在此注册"""
from typing import Dict, Optional

from .base import ImageProviderBase
from .openai_images import OpenAIImagesProvider
from .http import HttpImageProvider

# Provider 注册表: api 类型 -> Provider 类，新增 provider 时在此添加映射
_PROVIDER_MAP: Dict[str, type] = {
    OpenAIImagesProvider.api_type: OpenAIImagesProvider,
    HttpImageProvider.api_type: HttpImageProvider,
}


def get_provider(api_type: str) -> Optional[ImageProviderBase]:
    """按 api 类型创建 Provider 实例，未注册的类型返回 None"""
    cls = _PROVIDER_MAP.get(api_type)
    if not cls:
        return None
    return cls()


def supported_api_types() -> list:
    """返回已注册的 api 类型列表"""
    return list(_PROVIDER_MAP.keys())
