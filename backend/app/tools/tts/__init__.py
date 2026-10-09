"""TTS 语音合成服务包

架构:
- config: 配置解析 + [[tts:...]] 标记解析
- base: TTSProviderBase 抽象基类
- aliyun: 阿里云百炼 WebSocket 双向流式 Provider
- minimax: MiniMax WebSocket bidi + HTTP 后备 Provider
- edge_tts: edge-tts 库单向流式 Provider
- factory: Provider 工厂 (新增 provider 只需在 factory.py 注册)
- session: TTSSession 合成会话生命周期管理
- manager: TTSSessionManager 多消息并行管理
- utils: 便捷函数 + 状态跟踪
"""

# ===== 配置解析 =====
from .config import (
    resolve_tts_config,
    resolve_persona,
    get_provider_config,
    get_effective_stream_mode,
    parse_tts_tags,
)

# ===== Provider 基类 + 工厂 =====
from .base import TTSProviderBase
from .factory import create_provider

# ===== Provider 实现 =====
from .aliyun import AliyunTTSProvider
from .minimax import MinimaxTTSProvider
from .edge_tts import EdgeTTSProvider

# ===== Session + Manager =====
from .session import TTSSession
from .manager import TTSSessionManager, get_tts_manager, remove_tts_manager

# ===== 便捷函数 + 状态跟踪 =====
from .utils import tts_synthesize_text, record_tts_attempt, get_last_tts_attempt

__all__ = [
    # 配置
    "resolve_tts_config",
    "resolve_persona",
    "get_provider_config",
    "get_effective_stream_mode",
    "parse_tts_tags",
    # Provider
    "TTSProviderBase",
    "create_provider",
    "AliyunTTSProvider",
    "MinimaxTTSProvider",
    "EdgeTTSProvider",
    # Session + Manager
    "TTSSession",
    "TTSSessionManager",
    "get_tts_manager",
    "remove_tts_manager",
    # 便捷函数
    "tts_synthesize_text",
    "record_tts_attempt",
    "get_last_tts_attempt",
]
