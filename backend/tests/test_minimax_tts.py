"""MinimaxTTSProvider 双向流式语音合成集成测试

运行条件: 需要设置环境变量 MINIMAX_API_KEY
运行方式: pytest tests/test_minimax_tts.py -v -s
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.tools.tts.minimax import MinimaxTTSProvider

# 跳过条件：无 API Key 时跳过全部测试
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
pytestmark = pytest.mark.skipif(
    not MINIMAX_API_KEY,
    reason="MINIMAX_API_KEY environment variable not set"
)

# ===== 测试配置 =====
BASE_CONFIG = {
    "ws_url": "wss://api.minimax.cn/ws/v1/t2a_v2",
    "http_url": "https://api.minimax.cn/v1/t2a_v2",
    "apiKey": MINIMAX_API_KEY,
    "model": "speech-2.8-turbo",
    "voice_setting": {
        "voice_id": "Chinese (Mandarin)_Lyrical_Voice",
        "speed": 1.0,
        "vol": 1,
        "pitch": 0,
    },
    "audio_setting": {
        "sample_rate": 32000,
        "bitrate": 128000,
        "format": "mp3",
        "channel": 1,
    },
    "language_boost": "auto",
    "timeout_ms": 30000,
    "rpm": 100,  # 测试时使用高 RPM，避免限流等待
}


class TestMinimaxTwoWayStreaming:
    """双向流式 (WebSocket bidi) 测试"""

    @pytest.mark.asyncio
    async def test_connect_and_task_start(self):
        """测试 WebSocket 连接和 task_start 握手"""
        provider = MinimaxTTSProvider()
        try:
            await provider.connect(BASE_CONFIG)
            assert provider._connected is True
            assert provider._ws is not None
            assert provider._error is None
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_send_text_receives_audio(self):
        """测试发送文本后能接收到音频数据"""
        provider = MinimaxTTSProvider()
        audio_chunks: list[bytes] = []

        async def on_audio(chunk: bytes):
            audio_chunks.append(chunk)

        provider.on_audio_chunk(on_audio)

        try:
            await provider.connect(BASE_CONFIG)
            await provider.send_text("你好，这是一段测试文本。")
            # flush 催出缓冲区
            await provider.flush()
            # 等待一些时间让音频数据到达
            await asyncio.sleep(2)
            await provider.finish()

            assert len(audio_chunks) > 0, "应该接收到至少一个音频 chunk"
            total_bytes = sum(len(c) for c in audio_chunks)
            assert total_bytes > 100, f"音频数据太小 ({total_bytes} bytes)，可能有问题"
            print(f"\n[OK] 收到 {len(audio_chunks)} 个音频 chunk, 总计 {total_bytes} bytes")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_incremental_text_feed(self):
        """测试逐步喂入文本（模拟 LLM 流式输出）"""
        provider = MinimaxTTSProvider()
        audio_chunks: list[bytes] = []

        async def on_audio(chunk: bytes):
            audio_chunks.append(chunk)

        provider.on_audio_chunk(on_audio)

        text_parts = [
            "今天天气",
            "真不错，",
            "我们一起",
            "出去散步吧。",
        ]

        try:
            await provider.connect(BASE_CONFIG)

            for part in text_parts:
                await provider.send_text(part)
                await asyncio.sleep(0.1)  # 模拟 LLM token 间隔

            await provider.flush()
            await asyncio.sleep(2)
            await provider.finish()

            assert len(audio_chunks) > 0, "逐步喂入文本后应接收到音频"
            total_bytes = sum(len(c) for c in audio_chunks)
            assert total_bytes > 200, f"音频数据太小 ({total_bytes} bytes)"
            print(f"\n[OK] 逐步喂入 {len(text_parts)} 段文本, 收到 {len(audio_chunks)} chunks, {total_bytes} bytes")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_flush_produces_audio(self):
        """测试 flush 能催出缓冲区中的音频"""
        provider = MinimaxTTSProvider()
        audio_chunks: list[bytes] = []

        async def on_audio(chunk: bytes):
            audio_chunks.append(chunk)

        provider.on_audio_chunk(on_audio)

        try:
            await provider.connect(BASE_CONFIG)
            # 发送短文本（可能不足以触发自动输出）
            await provider.send_text("测试")
            await asyncio.sleep(0.5)

            chunks_before_flush = len(audio_chunks)
            await provider.flush()
            await asyncio.sleep(1.5)

            chunks_after_flush = len(audio_chunks)
            await provider.finish()

            # flush 后应该有新音频（或之前已经输出了）
            assert chunks_after_flush > 0, "flush 后应该有音频数据"
            print(f"\n[OK] flush 前: {chunks_before_flush} chunks, flush 后: {chunks_after_flush} chunks")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_finish_completes_gracefully(self):
        """测试 finish 正常关闭"""
        provider = MinimaxTTSProvider()
        audio_chunks: list[bytes] = []

        async def on_audio(chunk: bytes):
            audio_chunks.append(chunk)

        provider.on_audio_chunk(on_audio)

        try:
            await provider.connect(BASE_CONFIG)
            await provider.send_text("再见。")
            await provider.flush()
            await asyncio.sleep(1)
            await provider.finish()

            # finish 后事件应该被设置
            assert provider._finished_event.is_set(), "finish 后 _finished_event 应被设置"
            assert provider._error is None, f"不应有错误: {provider._error}"
            print(f"\n[OK] finish 正常完成, 收到 {len(audio_chunks)} chunks")
        finally:
            await provider.close()

    @pytest.mark.asyncio
    async def test_close_releases_resources(self):
        """测试 close 正确释放资源"""
        provider = MinimaxTTSProvider()

        async def on_audio(chunk: bytes):
            pass

        provider.on_audio_chunk(on_audio)
        await provider.connect(BASE_CONFIG)
        await provider.send_text("资源释放测试")
        await asyncio.sleep(0.5)

        await provider.close()

        assert provider._connected is False
        assert provider._ws is None
        assert provider._recv_task is None or provider._recv_task.done()
        print("\n[OK] close 正确释放了所有资源")

    @pytest.mark.asyncio
    async def test_long_text_streaming(self):
        """测试较长文本的流式生成"""
        provider = MinimaxTTSProvider()
        audio_chunks: list[bytes] = []

        async def on_audio(chunk: bytes):
            audio_chunks.append(chunk)

        provider.on_audio_chunk(on_audio)

        long_text = (
            "人工智能技术正在快速发展，"
            "语音合成作为其中的重要分支，"
            "已经广泛应用于智能客服、有声阅读、虚拟主播等领域。"
            "MiniMax提供的双向流式语音合成接口，"
            "支持在文本生成的同时进行语音合成，"
            "大大降低了用户感知延迟。"
        )

        try:
            await provider.connect(BASE_CONFIG)

            # 模拟逐句喂入
            sentences = [long_text[i:i+20] for i in range(0, len(long_text), 20)]
            for sentence in sentences:
                await provider.send_text(sentence)
                await asyncio.sleep(0.05)

            await provider.flush()
            # 等待足够时间让所有音频生成
            await asyncio.sleep(5)
            await provider.finish()

            assert len(audio_chunks) > 0, "长文本应产生音频"
            total_bytes = sum(len(c) for c in audio_chunks)
            assert total_bytes > 1000, f"长文本音频数据应较大, 实际: {total_bytes} bytes"
            print(f"\n[OK] 长文本 ({len(long_text)} 字) 生成完毕: {len(audio_chunks)} chunks, {total_bytes} bytes")
        finally:
            await provider.close()


class TestMinimaxErrorHandling:
    """错误处理测试"""

    @pytest.mark.asyncio
    async def test_connect_without_api_key(self):
        """无 API Key 时应抛出 ValueError"""
        provider = MinimaxTTSProvider()
        config = {**BASE_CONFIG, "apiKey": ""}

        with pytest.raises(ValueError, match="apiKey is required"):
            await provider.connect(config)

    @pytest.mark.asyncio
    async def test_send_text_without_connect(self):
        """未连接时发送文本应抛出 RuntimeError"""
        provider = MinimaxTTSProvider()

        with pytest.raises(RuntimeError, match="not connected"):
            await provider.send_text("hello")

    @pytest.mark.asyncio
    async def test_connect_with_invalid_api_key(self):
        """无效 API Key 应触发错误"""
        provider = MinimaxTTSProvider()
        config = {**BASE_CONFIG, "apiKey": "invalid_key_12345"}

        with pytest.raises((RuntimeError, TimeoutError, Exception)):
            await provider.connect(config)

        await provider.close()


class TestMinimaxOneWay:
    """HTTP 流式合成 (one-way) 测试"""

    @pytest.mark.asyncio
    async def test_synthesize_stream_http(self):
        """测试 HTTP 流式合成"""
        provider = MinimaxTTSProvider()
        audio_chunks: list[bytes] = []

        async def on_audio(chunk: bytes):
            audio_chunks.append(chunk)

        provider.on_audio_chunk(on_audio)

        await provider.synthesize_stream("这是一段HTTP流式合成测试。", BASE_CONFIG)

        assert len(audio_chunks) > 0, "HTTP 流式合成应产生音频"
        total_bytes = sum(len(c) for c in audio_chunks)
        assert total_bytes > 100, f"音频数据太小: {total_bytes} bytes"
        print(f"\n[OK] HTTP 流式合成: {len(audio_chunks)} chunks, {total_bytes} bytes")

    @pytest.mark.asyncio
    async def test_synthesize_http(self):
        """测试 HTTP 一次性合成"""
        provider = MinimaxTTSProvider()

        audio_bytes = await provider.synthesize("你好世界", BASE_CONFIG)

        assert audio_bytes is not None
        assert len(audio_bytes) > 100, f"合成音频太小: {len(audio_bytes)} bytes"
        print(f"\n[OK] HTTP 一次性合成: {len(audio_bytes)} bytes")
