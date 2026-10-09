"""MiniMax TTS Provider - WebSocket 双向流式 (t2a_v2_bidi) + HTTP 后备"""
import asyncio
import json
import time
from collections import deque
from typing import Any, Dict, List, Optional

import structlog

from .base import TTSProviderBase

logger = structlog.get_logger()


class MinimaxRateLimiter:
    """MiniMax 全局速率限制器（滑动窗口算法）

    MiniMax 以账号为维度限制 RPM（每分钟请求数）：
    - 免费用户: 10 RPM
    - 充值用户: 20 RPM

    所有 MinimaxTTSProvider 实例共享同一限制器，
    确保不超出账号级别的速率限制。达到上限时自动等待至下一可用窗口。
    """

    def __init__(self, rpm: int = 10):
        self._timestamps: deque = deque()
        self._rpm: int = rpm
        self._lock = asyncio.Lock()

    def set_rpm(self, rpm: int) -> None:
        """更新 RPM 上限（从配置读取）"""
        if rpm > 0:
            self._rpm = rpm

    async def acquire(self) -> None:
        """等待直到获得发送许可（滑动窗口内请求数 < RPM）"""
        while True:
            async with self._lock:
                now = time.monotonic()
                # 清理 60 秒窗口外的旧记录
                while self._timestamps and self._timestamps[0] <= now - 60.0:
                    self._timestamps.popleft()

                if len(self._timestamps) < self._rpm:
                    self._timestamps.append(now)
                    return  # 获得许可

                # 计算需要等待的时间（最早请求退出窗口的时刻），增加延迟考虑边界情况
                wait_time = self._timestamps[0] + 60.0 - now + 3

            # 释放锁后等待（避免阻塞其他协程）
            logger.info(
                "Minimax RPM limit reached, throttling",
                wait_seconds=round(wait_time, 1),
                rpm=self._rpm,
                queued=len(self._timestamps),
            )
            await asyncio.sleep(max(wait_time, 0.05))


# 模块级全局速率限制器（所有 MinimaxTTSProvider 实例共享）
_rate_limiter = MinimaxRateLimiter(rpm=10)


class MinimaxTTSProvider(TTSProviderBase):
    """MiniMax TTS - WebSocket 双向流式 (t2a_v2_bidi) + HTTP 后备

    WebSocket bidi 协议:
    1. 连接 -> 收到 connected_success
    2. 发送 task_start -> 收到 task_started
    3. 发送 task_continue (文本) -> 收到 task_continued (hex audio)
    4. 发送 task_flush -> 收到 task_flushed
    5. 发送 task_finish -> 收到 task_finished
    """

    name = "minimax"
    supports_two_way = True
    supports_one_way = True

    def __init__(self):
        super().__init__()
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        self._started_event = asyncio.Event()
        self._finished_event = asyncio.Event()
        self._flushed_event = asyncio.Event()
        self._error: Optional[str] = None
        self._config: Dict[str, Any] = {}
        self._text_buffer: List[str] = []  # 累积文本，flush 时一次性发送（节省 RPM）

    async def connect(self, config: Dict[str, Any]) -> None:
        """建立 WebSocket 连接 (bidi 模式)"""
        import websockets

        self._config = config

        # 从配置读取 RPM 并更新全局限流器（免费:10, 充值:20）
        rpm = config.get("rpm", 10)
        _rate_limiter.set_rpm(rpm)

        # 使用 bidi 端点
        ws_url = config.get("ws_url", "wss://api.minimax.cn/ws/v1/t2a_v2")
        # 如果配置的是非 bidi URL，自动替换
        if "t2a_v2_bidi" not in ws_url:
            ws_url = ws_url.replace("/t2a_v2", "/t2a_v2_bidi")

        api_key = config.get("apiKey", "") or config.get("api_key", "")
        if not api_key:
            raise ValueError("Minimax TTS: apiKey is required")

        self._started_event.clear()
        self._finished_event.clear()
        self._flushed_event.clear()
        self._error = None
        self._text_buffer.clear()  # 新连接重置缓冲区

        # 连接 URL：v2 接口不需要 GroupId，仅在显式配置时才拼接
        group_id = config.get("group_id", "") or config.get("GroupId", "")
        url = f"{ws_url}?GroupId={group_id}" if group_id else ws_url
        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        await _rate_limiter.acquire()
        self._ws = await websockets.connect(
            url,
            extra_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        )
        self._connected = True

        # 启动接收循环
        self._recv_task = asyncio.create_task(self._receive_loop())

        # 等待 connected_success (通过 _started_event 复用)
        # 实际上 connected_success 在接收循环中处理
        await asyncio.sleep(0.3)  # 短暂等待连接确认

        # 发送 task_start
        model = config.get("model", "speech-2.8-turbo")
        voice_setting = config.get("voice_setting", {}) or {}
        audio_setting = config.get("audio_setting", {}) or {}

        task_start_msg: Dict[str, Any] = {
            "event": "task_start",
            "model": model,
            "voice_setting": {
                "voice_id": voice_setting.get("voice_id", "Chinese (Mandarin)_Lyrical_Voice"),
                "speed": voice_setting.get("speed", 1.0),
                "vol": voice_setting.get("vol", 1),
                "pitch": voice_setting.get("pitch", 0),
            },
            "audio_setting": {
                "sample_rate": audio_setting.get("sample_rate", 32000),
                "bitrate": audio_setting.get("bitrate", 128000),
                "format": audio_setting.get("format", "mp3"),
                "channel": audio_setting.get("channel", 1),
            },
        }

        # 可选参数
        if voice_setting.get("emotion"):
            task_start_msg["voice_setting"]["emotion"] = voice_setting["emotion"]
        if voice_setting.get("english_normalization"):
            task_start_msg["voice_setting"]["english_normalization"] = True
        if config.get("language_boost"):
            task_start_msg["language_boost"] = config["language_boost"]
        if config.get("pronunciation_dict"):
            task_start_msg["pronunciation_dict"] = config["pronunciation_dict"]
        if config.get("timbre_weights"):
            task_start_msg["timbre_weights"] = config["timbre_weights"]
        if config.get("voice_modify"):
            vm = config["voice_modify"]
            if any(vm.get(k) for k in ("pitch", "intensity", "timbre", "sound_effects")):
                task_start_msg["voice_modify"] = {k: v for k, v in vm.items() if v}
        if config.get("subtitle_enable"):
            task_start_msg["subtitle_enable"] = True
            task_start_msg["subtitle_type"] = config.get("subtitle_type", "sentence")
        if config.get("aigc_watermark"):
            task_start_msg["aigc_watermark"] = True
        if config.get("continuous_sound"):
            task_start_msg["continuous_sound"] = True

        await self._send(task_start_msg)

        # 等待 task_started
        try:
            await asyncio.wait_for(self._started_event.wait(), timeout=90)
        except asyncio.TimeoutError:
            raise TimeoutError("Minimax TTS: timeout waiting for task_started")

        if self._error:
            raise RuntimeError(f"Minimax TTS error: {self._error}")

        logger.debug("Minimax TTS connected", model=model)

    async def _send(self, msg: dict) -> None:
        """发送 WebSocket 消息（受全局 RPM 限制）"""
        await _rate_limiter.acquire()
        await self._ws.send(json.dumps(msg))

    async def send_text(self, text: str) -> None:
        """缓冲文本片段（不立即发送，节省 MiniMax RPM）

        MiniMax 速率限制以账号维度计算（免费 10 RPM，充值 20 RPM），
        每次 task_continue 均计为一个请求。因此采用累积策略：
        send_text 仅写入本地缓冲，flush 时合并为一次 task_continue 发送。
        """
        if not self._ws or not self._connected:
            raise RuntimeError("Minimax TTS: not connected")
        if not text:
            return
        self._text_buffer.append(text)

    async def flush(self) -> None:
        """一次性发送所有缓冲文本，然后发送 flush 信号并等待完成"""
        if not self._ws or not self._connected:
            return

        # 合并缓冲文本，作为单个 task_continue 发送（每条消息仅消耗 1 RPM）
        if self._text_buffer:
            full_text = "".join(self._text_buffer)
            self._text_buffer.clear()
            msg = {"event": "task_continue", "text": full_text}
            await self._send(msg)

        self._flushed_event.clear()
        msg = {"event": "task_flush"}
        await self._send(msg)

        try:
            await asyncio.wait_for(self._flushed_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Minimax TTS: timeout waiting for task_flushed")

    async def finish(self) -> None:
        """结束合成"""
        if not self._ws or not self._connected:
            return

        msg = {"event": "task_finish"}
        try:
            await self._send(msg)
        except Exception:
            pass

        try:
            await asyncio.wait_for(self._finished_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Minimax TTS: timeout waiting for task_finished")

    async def cancel(self) -> None:
        """打断合成"""
        if not self._ws or not self._connected:
            return
        msg = {"event": "task_cancel"}
        try:
            await self._send(msg)
        except Exception:
            pass

    async def synthesize(self, text: str, config: Dict[str, Any]) -> bytes:
        """非流式一次性合成 (使用 HTTP 接口)"""
        import httpx

        http_url = config.get("http_url", "https://api.minimax.cn/v1/t2a_v2")
        api_key = config.get("apiKey", "") or config.get("api_key", "")
        if not api_key:
            raise ValueError("Minimax TTS: apiKey is required")

        model = config.get("model", "speech-2.8-turbo")
        voice_setting = config.get("voice_setting", {}) or {}
        audio_setting = config.get("audio_setting", {}) or {}

        body: Dict[str, Any] = {
            "model": model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice_setting.get("voice_id", "Chinese (Mandarin)_Lyrical_Voice"),
                "speed": voice_setting.get("speed", 1.0),
                "vol": voice_setting.get("vol", 1),
                "pitch": voice_setting.get("pitch", 0),
            },
            "audio_setting": {
                "sample_rate": audio_setting.get("sample_rate", 32000),
                "bitrate": audio_setting.get("bitrate", 128000),
                "format": audio_setting.get("format", "mp3"),
                "channel": audio_setting.get("channel", 1),
            },
        }

        if voice_setting.get("emotion"):
            body["voice_setting"]["emotion"] = voice_setting["emotion"]
        if config.get("language_boost"):
            body["language_boost"] = config["language_boost"]
        if config.get("pronunciation_dict"):
            body["pronunciation_dict"] = config["pronunciation_dict"]
        if config.get("timbre_weights"):
            body["timbre_weights"] = config["timbre_weights"]
        if config.get("voice_modify"):
            vm = config["voice_modify"]
            if any(vm.get(k) for k in ("pitch", "intensity", "timbre", "sound_effects")):
                body["voice_modify"] = {k: v for k, v in vm.items() if v}
        if config.get("subtitle_enable"):
            body["subtitle_enable"] = True
            body["subtitle_type"] = config.get("subtitle_type", "sentence")
        if config.get("aigc_watermark"):
            body["aigc_watermark"] = True
        if config.get("continuous_sound"):
            body["continuous_sound"] = True

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        timeout = config.get("timeout_ms", 30000) / 1000.0
        await _rate_limiter.acquire()
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(http_url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # 解析响应
        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"Minimax TTS HTTP error: {base_resp.get('status_msg', 'unknown')}")

        audio_hex = data.get("data", {}).get("audio", "")
        if not audio_hex:
            raise RuntimeError("Minimax TTS HTTP: empty audio response")

        return bytes.fromhex(audio_hex)

    async def synthesize_stream(self, text: str, config: Dict[str, Any]) -> None:
        """流式合成 (HTTP stream=true)"""
        import httpx

        http_url = config.get("http_url", "https://api.minimax.cn/v1/t2a_v2")
        api_key = config.get("apiKey", "") or config.get("api_key", "")

        model = config.get("model", "speech-2.8-turbo")
        voice_setting = config.get("voice_setting", {}) or {}
        audio_setting = config.get("audio_setting", {}) or {}

        body: Dict[str, Any] = {
            "model": model,
            "text": text,
            "stream": True,
            "voice_setting": {
                "voice_id": voice_setting.get("voice_id", "Chinese (Mandarin)_Lyrical_Voice"),
                "speed": voice_setting.get("speed", 1.0),
                "vol": voice_setting.get("vol", 1),
                "pitch": voice_setting.get("pitch", 0),
            },
            "audio_setting": {
                "sample_rate": audio_setting.get("sample_rate", 32000),
                "bitrate": audio_setting.get("bitrate", 128000),
                "format": audio_setting.get("format", "mp3"),
                "channel": audio_setting.get("channel", 1),
            },
        }

        if voice_setting.get("emotion"):
            body["voice_setting"]["emotion"] = voice_setting["emotion"]
        if config.get("language_boost"):
            body["language_boost"] = config["language_boost"]
        if config.get("pronunciation_dict"):
            body["pronunciation_dict"] = config["pronunciation_dict"]
        if config.get("timbre_weights"):
            body["timbre_weights"] = config["timbre_weights"]
        if config.get("voice_modify"):
            vm = config["voice_modify"]
            if any(vm.get(k) for k in ("pitch", "intensity", "timbre", "sound_effects")):
                body["voice_modify"] = {k: v for k, v in vm.items() if v}
        if config.get("subtitle_enable"):
            body["subtitle_enable"] = True
            body["subtitle_type"] = config.get("subtitle_type", "sentence")
        if config.get("aigc_watermark"):
            body["aigc_watermark"] = True
        if config.get("continuous_sound"):
            body["continuous_sound"] = True

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        timeout = config.get("timeout_ms", 30000) / 1000.0
        await _rate_limiter.acquire()
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", http_url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        try:
                            chunk_data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # 先检查是否为结束事件（status=2），避免重复处理音频
                        status = chunk_data.get("data", {}).get("status")
                        if status == 2:
                            break

                        audio_hex = chunk_data.get("data", {}).get("audio", "")
                        if audio_hex and self._audio_callback:
                            audio_bytes = bytes.fromhex(audio_hex)
                            await self._audio_callback(audio_bytes)

    async def close(self) -> None:
        """关闭连接"""
        self._connected = False
        self._text_buffer.clear()  # 清理未发送的缓冲文本
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _receive_loop(self) -> None:
        """WebSocket 接收循环"""
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    event = data.get("event", "")
                    base_resp = data.get("base_resp", {})

                    if event == "connected_success":
                        pass  # 连接成功，继续等待 task_started
                    elif event == "task_started":
                        self._started_event.set()
                    elif event == "task_continued":
                        # 音频数据 (hex 编码)
                        audio_hex = (data.get("data") or {}).get("audio", "")
                        if audio_hex and self._audio_callback:
                            try:
                                audio_bytes = bytes.fromhex(audio_hex)
                                await self._audio_callback(audio_bytes)
                            except Exception as e:
                                logger.debug("Minimax audio decode error", error=str(e))
                    elif event == "sentence_start":
                        pass  # 可用于跟踪进度
                    elif event == "sentence_end":
                        pass
                    elif event == "task_flushed":
                        self._flushed_event.set()
                    elif event == "task_finished":
                        self._finished_event.set()
                    elif event == "task_canceled":
                        self._finished_event.set()
                    elif event == "task_failed":
                        self._error = base_resp.get("status_msg", "task failed")
                        logger.error("Minimax TTS task failed",
                                     status_code=base_resp.get("status_code"),
                                     error=self._error)
                        self._started_event.set()
                        self._finished_event.set()
                    else:
                        # 检查错误状态码
                        status_code = base_resp.get("status_code", 0)
                        if status_code != 0 and event not in ("task_continued",):
                            self._error = base_resp.get("status_msg", f"error {status_code}")
                            self._started_event.set()
                            self._finished_event.set()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self._connected:
                logger.error("Minimax TTS receive loop error", error=str(e))
                self._error = str(e)
                self._started_event.set()
                self._finished_event.set()
