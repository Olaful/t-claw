"""阿里云百炼 TTS Provider - WebSocket 双向流式"""
import asyncio
import base64
import json
import random
import time
import uuid
from typing import Any, Dict, List, Optional

import structlog

from .base import TTSProviderBase

logger = structlog.get_logger()


class AliyunTTSProvider(TTSProviderBase):
    """阿里云百炼 TTS - WebSocket 双向流式

    协议 (参考 dashscope SDK):
    1. 连接 ws_url
    2. 发送 run-task JSON (含 model/voice/format 等)
    3. 接收 task-started 事件
    4. 发送 continue-task (文本 chunk)
    5. 接收 binary frames (音频数据)
    6. 发送 finish-task
    7. 接收 task-finished
    """

    name = "aliyun"
    supports_two_way = True
    supports_one_way = True

    def __init__(self):
        super().__init__()
        self._ws = None
        self._task_id: str = ""
        self._recv_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._started_event = asyncio.Event()
        self._finished_event = asyncio.Event()
        self._error: Optional[str] = None
        self._last_send_time: float = 0

    async def connect(self, config: Dict[str, Any]) -> None:
        """建立 WebSocket 连接并发送 run-task"""
        import websockets

        ws_url = config.get("ws_url", "")
        api_key = config.get("apiKey", "") or config.get("api_key", "")
        if not ws_url:
            raise ValueError("Aliyun TTS: ws_url is required")
        if not api_key:
            raise ValueError("Aliyun TTS: apiKey is required")

        self._task_id = str(uuid.uuid4())
        self._started_event.clear()
        self._finished_event.clear()
        self._error = None

        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-DashScope-DataInspection": "enable",
        }

        self._ws = await websockets.connect(
            ws_url,
            extra_headers=headers,
            ping_interval=20,   # 发送 ws 层 ping 保持连接活跃
            ping_timeout=None,  # 不强制关闭（阿里云可能不回 pong）
        )
        self._connected = True
        self._last_send_time = time.monotonic()

        # 启动接收循环
        self._recv_task = asyncio.create_task(self._receive_loop())

        # 构建 run-task 消息
        model = config.get("model", "qwen-audio-3.1-tts-flash")
        voice = config.get("voice", "longanhuan_v3.6")
        audio_format = config.get("responseFormat", "mp3")
        volume = config.get("volume", 50)
        speech_rate = config.get("speech_rate", 1.0)
        pitch_rate = config.get("pitch_rate", 1.0)

        # 构建 parameters
        parameters: Dict[str, Any] = {
            "text_type": "PlainText",
            "voice": voice,
            "format": audio_format,
            "volume": volume,
            "rate": speech_rate,
            "pitch": pitch_rate,
        }
        if config.get("sample_rate"):
            parameters["sample_rate"] = config["sample_rate"]
        if config.get("instruction"):
            parameters["instruction"] = config["instruction"]
        if config.get("seed"):
            parameters["seed"] = config["seed"]
        if config.get("language_hints"):
            parameters["language_hints"] = config["language_hints"]
        if config.get("word_timestamp_enabled"):
            parameters["word_timestamp_enabled"] = True
        if config.get("enable_aigc_tag"):
            parameters["enable_aigc_tag"] = True
            if config.get("aigc_propagator"):
                parameters["aigc_propagator"] = config["aigc_propagator"]

        # hot_fix
        if config.get("hot_fix"):
            parameters["hot_fix"] = config["hot_fix"]

        run_task_msg = {
            "header": {
                "action": "run-task",
                "task_id": self._task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": model,
                "parameters": parameters,
                "input": {},
            },
        }

        await self._ws.send(json.dumps(run_task_msg))
        self._last_send_time = time.monotonic()

        # 等待 task-started（支持速率限制重试）
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                await asyncio.wait_for(self._started_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                raise TimeoutError("Aliyun TTS: timeout waiting for task-started")

            if not self._error:
                break  # 成功启动

            # 检查是否为速率限制错误，可重试
            if "rate limit" in self._error.lower() or "throttling" in self._error.lower():
                if attempt < max_retries:
                    wait_time = (attempt + 1) * 2 + random.uniform(0, 1)
                    logger.info("Aliyun TTS rate limited, retrying",
                                attempt=attempt + 1, wait=f"{wait_time:.1f}s")
                    # 关闭当前连接并重建
                    if self._recv_task and not self._recv_task.done():
                        self._recv_task.cancel()
                    await self._ws.close()
                    self._error = None
                    self._started_event.clear()
                    self._finished_event.clear()
                    self._task_id = str(uuid.uuid4())
                    await asyncio.sleep(wait_time)
                    # 重新连接
                    self._ws = await websockets.connect(
                        ws_url, extra_headers=headers,
                        ping_interval=20, ping_timeout=None,
                    )
                    self._last_send_time = time.monotonic()
                    self._recv_task = asyncio.create_task(self._receive_loop())
                    # 重新发送 run-task
                    run_task_msg["header"]["task_id"] = self._task_id
                    await self._ws.send(json.dumps(run_task_msg))
                    self._last_send_time = time.monotonic()
                    continue
            # 非速率限制错误或重试耗尽
            raise RuntimeError(f"Aliyun TTS error: {self._error}")

        # 启动 keep-alive 防止服务端空闲超时（~23s）
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        logger.debug("Aliyun TTS connected", task_id=self._task_id, model=model, voice=voice)

    async def send_text(self, text: str) -> None:
        """发送文本片段"""
        if not self._ws or not self._connected:
            raise RuntimeError("Aliyun TTS: not connected")
        if not text:
            return

        msg = {
            "header": {
                "action": "continue-task",
                "task_id": self._task_id,
                "streaming": "duplex",
            },
            "payload": {
                "input": {
                    "text": text,
                },
            },
        }
        await self._ws.send(json.dumps(msg))
        self._last_send_time = time.monotonic()

    async def flush(self) -> None:
        """阿里云 TTS 服务端自动攒句，flush 即发送空文本触发"""
        # 阿里云的协议不需要显式 flush，服务端会自动处理
        pass

    async def finish(self) -> None:
        """发送 finish-task 并等待完成"""
        # 停止 keep-alive
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None

        if not self._ws or not self._connected:
            return

        msg = {
            "header": {
                "action": "finish-task",
                "task_id": self._task_id,
                "streaming": "duplex",
            },
            "payload": {
                "input": {},
            },
        }
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            pass

        # 等待 task-finished
        try:
            await asyncio.wait_for(self._finished_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Aliyun TTS: timeout waiting for task-finished")

    async def cancel(self) -> None:
        """取消合成"""
        if not self._ws or not self._connected:
            return
        msg = {
            "header": {
                "action": "cancel-task",
                "task_id": self._task_id,
            },
        }
        try:
            await self._ws.send(json.dumps(msg))
            await asyncio.wait_for(self._finished_event.wait(), timeout=5.0)
        except Exception:
            pass

    async def synthesize(self, text: str, config: Dict[str, Any]) -> bytes:
        """非流式一次性合成"""
        audio_chunks: List[bytes] = []

        async def collect_chunk(chunk: bytes):
            audio_chunks.append(chunk)

        old_callback = self._audio_callback
        self._audio_callback = collect_chunk

        try:
            await self.connect(config)
            await self.send_text(text)
            await self.finish()
        finally:
            self._audio_callback = old_callback
            await self.close()

        return b"".join(audio_chunks)

    async def synthesize_stream(self, text: str, config: Dict[str, Any]) -> None:
        """流式合成: 通过 callback 逐块推送"""
        await self.connect(config)
        await self.send_text(text)
        await self.finish()
        await self.close()

    async def close(self) -> None:
        """关闭连接"""
        self._connected = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
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

    async def _keepalive_loop(self) -> None:
        """定期发送 continue-task 防止服务端空闲超时（阿里云约23s无消息则断开）"""
        try:
            while self._connected and not self._finished_event.is_set():
                # 15秒基础间隔 + 随机抖动，避免多 session 同时发送触发速率限制
                await asyncio.sleep(15 + random.uniform(0, 3))
                if not self._connected or self._finished_event.is_set():
                    break
                elapsed = time.monotonic() - self._last_send_time
                if elapsed >= 14:  # 距上次发送超过14秒才发 keep-alive
                    try:
                        msg = {
                            "header": {
                                "action": "continue-task",
                                "task_id": self._task_id,
                                "streaming": "duplex",
                            },
                            "payload": {
                                "input": {
                                    "text": " ",  # 空格而非空串，避免服务端状态机异常
                                },
                            },
                        }
                        await self._ws.send(json.dumps(msg))
                        self._last_send_time = time.monotonic()
                    except Exception:
                        break  # 连接已断开，退出
        except asyncio.CancelledError:
            pass

    async def _receive_loop(self) -> None:
        """WebSocket 接收循环"""
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    # 二进制帧 = 音频数据
                    if self._audio_callback:
                        await self._audio_callback(message)
                elif isinstance(message, str):
                    # JSON 文本帧 = 事件
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    header = data.get("header", {})
                    event = header.get("event", "")

                    if event == "task-started":
                        self._started_event.set()
                    elif event == "task-finished":
                        self._finished_event.set()
                    elif event == "task-failed":
                        header_data = data.get("header", {})
                        payload = data.get("payload", {})
                        error_code = header_data.get("error_code", "")
                        task_id = header_data.get("task_id", "")
                        # 优先从 header.error_message 取，其次 payload.message
                        self._error = (
                            header_data.get("error_message")
                            or payload.get("message")
                            or f"task-failed ({error_code})"
                        )
                        logger.warning("Aliyun TTS task failed", error_code=error_code, task_id=task_id, error=self._error)
                        self._started_event.set()
                        self._finished_event.set()
                    elif event == "result-generated":
                        # 可能包含音频数据（base64）
                        payload = data.get("payload", {})
                        output = payload.get("output", {})
                        audio_data = output.get("audio", "")
                        if audio_data and self._audio_callback:
                            try:
                                chunk = base64.b64decode(audio_data)
                                await self._audio_callback(chunk)
                            except Exception:
                                pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self._connected:
                logger.error("Aliyun TTS receive loop error", error=str(e))
                self._error = str(e)
                self._started_event.set()
                self._finished_event.set()
