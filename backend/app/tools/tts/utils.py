"""TTS 便捷函数 + 状态跟踪"""
import time
from typing import Any, Dict, Optional

import structlog

from .config import resolve_tts_config, resolve_persona
from .session import TTSSession

logger = structlog.get_logger()


# ===== 便捷函数 =====

async def tts_synthesize_text(
    text: str,
    conversation_id: str,
    message_id: str,
    agent_name: Optional[str] = None,
    workspace_path: Optional[str] = None,
    provider_override: Optional[str] = None,
    stream_mode_override: Optional[str] = None,
) -> Optional[str]:
    """一次性 TTS 合成的便捷函数 (用于斜杠命令等同步场景)

    Returns:
        audio_url 或 None
    """
    config = resolve_tts_config(agent_name)
    persona_id = config.get("persona")
    if persona_id:
        config = resolve_persona(config, persona_id)

    provider_name = provider_override or config.get("provider", "edge_tts")
    stream_mode = stream_mode_override or "none"

    session = TTSSession(
        conversation_id=conversation_id,
        message_id=message_id,
        tts_config=config,
        provider_name=provider_name,
        stream_mode=stream_mode,
        workspace_path=workspace_path,
    )

    return await session.synthesize_once(text, emit_events=False)


# ===== TTS 状态跟踪 (用于 /tts status 诊断) =====

_last_tts_attempt: Dict[str, Any] = {}


def record_tts_attempt(
    provider: str,
    success: bool,
    reason_code: str = "",
    latency_ms: int = 0,
    fallback_from: str = "",
) -> None:
    """记录最近一次 TTS 尝试结果（用于 /tts status 诊断）"""
    global _last_tts_attempt
    _last_tts_attempt = {
        "provider": provider,
        "success": success,
        "reason_code": reason_code,
        "latency_ms": latency_ms,
        "fallback_from": fallback_from,
        "timestamp": time.time(),
    }


def get_last_tts_attempt() -> Dict[str, Any]:
    """获取最近一次 TTS 尝试结果"""
    return _last_tts_attempt
