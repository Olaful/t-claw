"""图片生成 Provider 包

架构（参照 tts/ 包的模块化拆分模式）:
- base: ImageProviderBase 抽象基类（统一 generate 接口与返回结构）
- params: 参数标准化引擎（compat 校验 / 值映射 / 参数名映射）
- openai_images: OpenAI Images SDK Provider (api: openai-images)
- http: 通用 HTTP Provider (api: http)
- factory: Provider 工厂 (新增 provider 只需在 factory.py 注册)
"""

from .base import ImageProviderBase
from .params import normalize_params
from .factory import get_provider, supported_api_types
from .openai_images import OpenAIImagesProvider
from .http import HttpImageProvider

__all__ = [
    "ImageProviderBase",
    "normalize_params",
    "get_provider",
    "supported_api_types",
    "OpenAIImagesProvider",
    "HttpImageProvider",
]
