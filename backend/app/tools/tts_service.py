"""TTS 语音合成服务 - 向后兼容入口

实际实现已拆分到 app.tools.tts 包中，按 provider 独立文件组织。
本文件保留所有公共 API 的 re-export，确保已有 import 路径不受影响:
    from app.tools.tts_service import resolve_tts_config, TTSSession, ...

新增 provider 时:
    1. 在 app/tools/tts/ 下创建新的 .py 文件
    2. 在 app/tools/tts/factory.py 的 _PROVIDER_MAP 中注册
    3. 在 app/tools/tts/__init__.py 中添加导出（可选）
"""
from app.tools.tts import (  # noqa: F401
    # 配置解析
    resolve_tts_config,
    resolve_persona,
    get_provider_config,
    get_effective_stream_mode,
    parse_tts_tags,
    # Provider
    TTSProviderBase,
    create_provider,
    AliyunTTSProvider,
    MinimaxTTSProvider,
    EdgeTTSProvider,
    # Session + Manager
    TTSSession,
    TTSSessionManager,
    get_tts_manager,
    remove_tts_manager,
    # 便捷函数
    tts_synthesize_text,
    record_tts_attempt,
    get_last_tts_attempt,
)
