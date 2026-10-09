"""TTS Provider 抽象基类"""
from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine, Dict, Optional


class TTSProviderBase(ABC):
    """TTS 提供商抽象基类"""

    name: str = "base"
    supports_two_way: bool = False
    supports_one_way: bool = True

    def __init__(self):
        self._audio_callback: Optional[Callable[[bytes], Coroutine]] = None
        self._connected = False

    def on_audio_chunk(self, callback: Callable[[bytes], Coroutine]) -> None:
        """注册音频数据回调 (async def callback(chunk: bytes))"""
        self._audio_callback = callback

    @abstractmethod
    async def connect(self, config: Dict[str, Any]) -> None:
        """建立连接 (two-way 模式)"""
        ...

    @abstractmethod
    async def send_text(self, text: str) -> None:
        """发送文本片段 (two-way 模式)"""
        ...

    @abstractmethod
    async def flush(self) -> None:
        """催出缓冲区残留文本"""
        ...

    @abstractmethod
    async def finish(self) -> None:
        """结束合成会话"""
        ...

    async def cancel(self) -> None:
        """打断合成"""
        await self.finish()

    @abstractmethod
    async def synthesize(self, text: str, config: Dict[str, Any]) -> bytes:
        """一次性合成完整文本，返回音频 bytes (one-way / none 模式)"""
        ...

    async def synthesize_stream(self, text: str, config: Dict[str, Any]) -> None:
        """流式合成: 发送完整文本，通过 callback 逐块推送音频 (one-way 模式)"""
        # 默认实现: 一次性合成后整体回调
        audio = await self.synthesize(text, config)
        if self._audio_callback and audio:
            await self._audio_callback(audio)

    async def close(self) -> None:
        """关闭连接，释放资源"""
        self._connected = False
