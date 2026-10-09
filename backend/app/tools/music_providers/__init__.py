"""音乐生成 Provider 包

架构（参照 tts/ 与 image_providers/ 包的模块化拆分模式）:
- base: MusicProviderBase 抽象基类 + MusicGenerateRequest 请求载体（统一 generate 接口与返回结构）
- params: 参数标准化引擎（normalize_music_params）与 endpoint API 配置解析（provider 无关共享逻辑）
- yinchao: yinchao Provider（异步轮询 + 流式播放 + 参考音频上传）
- aliyun: aliyun Fun-Music Provider（HTTP 同步 / SSE 流式）
- factory: Provider 工厂（新增 provider 只需在 factory.py 注册）
"""

from .base import MusicProviderBase, MusicGenerateRequest
from .params import (
    normalize_music_params,
    get_api_mode_config,
    resolve_api_url,
    identify_provider,
)
from .factory import get_provider, supported_provider_types
from .yinchao import YinchaoMusicProvider
from .aliyun import AliyunMusicProvider

__all__ = [
    "MusicProviderBase",
    "MusicGenerateRequest",
    "normalize_music_params",
    "get_api_mode_config",
    "resolve_api_url",
    "identify_provider",
    "get_provider",
    "supported_provider_types",
    "YinchaoMusicProvider",
    "AliyunMusicProvider",
]
