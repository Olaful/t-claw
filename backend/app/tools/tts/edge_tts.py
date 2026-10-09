"""Edge TTS Provider - 使用 edge-tts 库，模拟单向流式"""
from typing import Any, Dict, List

import structlog

from .base import TTSProviderBase

logger = structlog.get_logger()


class EdgeTTSProvider(TTSProviderBase):
    """Edge TTS - 使用 edge-tts 库，模拟单向流式

    不支持双向流式（无 send_text 语义），降级为 one-way。
    使用 Communicate.stream() 逐 chunk 推送音频。
    """

    name = "edge_tts"
    supports_two_way = False
    supports_one_way = True

    def __init__(self):
        super().__init__()
        self._buffer: List[str] = []

    async def connect(self, config: Dict[str, Any]) -> None:
        """Edge TTS 无需持久连接"""
        self._config = config
        self._connected = True
        self._buffer = []

    async def send_text(self, text: str) -> None:
        """缓冲文本（two-way 降级时不使用）"""
        if text:
            self._buffer.append(text)

    async def flush(self) -> None:
        """Edge TTS 无缓冲区概念"""
        pass

    async def finish(self) -> None:
        """合成缓冲的文本"""
        full_text = "".join(self._buffer)
        self._buffer.clear()
        if full_text:
            await self.synthesize_stream(full_text, self._config)

    async def synthesize(self, text: str, config: Dict[str, Any]) -> bytes:
        """一次性合成"""
        import edge_tts

        voice = config.get("voice", "zh-CN-XiaoxiaoNeural")
        rate = config.get("rate", "+0%")
        volume = config.get("volume", "+0%")
        pitch = config.get("pitch", "+0Hz")

        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
        )

        audio_chunks: List[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])

        return b"".join(audio_chunks)

    async def synthesize_stream(self, text: str, config: Dict[str, Any]) -> None:
        """流式合成: 通过 callback 逐块推送"""
        import edge_tts

        voice = config.get("voice", "zh-CN-XiaoxiaoNeural")
        rate = config.get("rate", "+0%")
        volume = config.get("volume", "+0%")
        pitch = config.get("pitch", "+0Hz")

        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            volume=volume,
            pitch=pitch,
        )

        async for chunk in communicate.stream():
            if chunk["type"] == "audio" and self._audio_callback:
                await self._audio_callback(chunk["data"])

    async def close(self) -> None:
        """关闭"""
        self._connected = False
        self._buffer.clear()
