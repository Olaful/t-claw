"""TTS Provider 工厂 - 新增 provider 只需在此注册"""
from typing import Dict

from .base import TTSProviderBase
from .aliyun import AliyunTTSProvider
from .minimax import MinimaxTTSProvider
from .edge_tts import EdgeTTSProvider

# Provider 注册表: 新增 provider 时在此添加映射
_PROVIDER_MAP: Dict[str, type] = {
    "aliyun": AliyunTTSProvider,
    "minimax": MinimaxTTSProvider,
    "edge_tts": EdgeTTSProvider,
}


def create_provider(provider_name: str) -> TTSProviderBase:
    """创建 TTS provider 实例"""
    cls = _PROVIDER_MAP.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown TTS provider: {provider_name}. Available: {list(_PROVIDER_MAP.keys())}")
    return cls()
