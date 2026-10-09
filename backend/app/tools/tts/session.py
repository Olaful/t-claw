"""TTS Session - 管理一次合成会话的完整生命周期"""
import asyncio
import base64
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from app.config import settings, _HAPPYCLAW_HOME
from .base import TTSProviderBase
from .config import get_effective_stream_mode, get_provider_config
from .factory import create_provider

logger = structlog.get_logger()


class TTSSession:
    """一次 TTS 合成会话，管理 provider 连接和事件推送

    使用方式:
        session = TTSSession(conversation_id, message_id, config, provider_name, stream_mode)
        await session.start()
        await session.feed_text("chunk1")  # two-way only
        await session.feed_text("chunk2")
        await session.complete()  # -> 保存文件, emit tts_stream_end
    """

    def __init__(
        self,
        conversation_id: str,
        message_id: str,
        tts_config: Dict[str, Any],
        provider_name: str,
        stream_mode: str = "one-way",
        workspace_path: Optional[str] = None,
    ):
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.tts_config = tts_config
        self.provider_name = provider_name
        self.stream_mode = get_effective_stream_mode(provider_name, stream_mode)
        self.workspace_path = workspace_path

        self._provider: Optional[TTSProviderBase] = None
        self._audio_chunks: List[bytes] = []
        self._chunk_index: int = 0
        self._started: bool = False
        self._completed: bool = False
        self._audio_url: str = ""
        self._start_time: float = 0
        # 非阻塞 feed_text: 队列 + 后台消费任务
        self._text_queue: asyncio.Queue = asyncio.Queue()
        self._feed_worker: Optional[asyncio.Task] = None
        # 后台连接任务（two-way 模式下 connect 不阻塞主循环）
        self._connect_task: Optional[asyncio.Task] = None
        self._connect_error: Optional[str] = None

    @property
    def provider_config(self) -> Dict[str, Any]:
        return get_provider_config(self.tts_config, self.provider_name)

    async def start(self) -> None:
        """启动 TTS 会话

        two-way 模式下，provider.connect() 在后台执行（不阻塞 Agent 主循环）。
        feed_text 可立即调用，文本入队缓冲，连接完成后 worker 开始消费。
        """
        self._provider = create_provider(self.provider_name)
        self._provider.on_audio_chunk(self._on_audio_chunk)
        self._start_time = time.time()
        self._started = True  # 立即标记，允许 feed_text 入队缓冲
        self._connect_error = None

        prov_config = self.provider_config
        prov_config["timeout_ms"] = self.tts_config.get("timeout_ms", 30000)

        if self.stream_mode == "two-way":
            # 后台建立连接（不阻塞 Agent 主循环）
            # feed_text 调用会入队等待，连接完成后 worker 开始消费
            self._connect_task = asyncio.create_task(self._bg_connect(prov_config))
        else:
            # one-way / none: 无需预连接
            self._connect_task = None

    async def _bg_connect(self, prov_config: Dict[str, Any]) -> None:
        """后台建立 provider 连接，完成后启动 feed worker 并推送 tts_stream_start 事件"""
        from app.core.event_bus import event_bus

        try:
            await self._provider.connect(prov_config)

            # 连接成功后启动 feed worker（开始消费队列中已缓冲的文本）
            self._feed_worker = asyncio.create_task(self._feed_worker_loop())

            # 推送 tts_stream_start 事件
            await event_bus.emit_event(
                type="tts_stream_start",
                source=self.conversation_id,
                data={
                    "message_id": self.message_id,
                    "conversation_id": self.conversation_id,
                    "provider": self.provider_name,
                    "stream_mode": self.stream_mode,
                    "model": prov_config.get("model", ""),
                    "voice": prov_config.get("voice", "") or prov_config.get("voice_setting", {}).get("voice_id", ""),
                },
                priority="normal",
                dimensions=[f"conversation:{self.conversation_id}"],
            )
        except Exception as e:
            logger.error("TTS background connect failed", error=str(e), message_id=self.message_id)
            self._connect_error = str(e)
            self._started = False

    async def feed_text(self, text: str) -> None:
        """发送文本片段 (two-way 模式) — 非阻塞，放入内部队列由后台任务消费"""
        if not self._provider or not self._started:
            return
        if self.stream_mode != "two-way":
            return
        if not text:
            return
        await self._text_queue.put(text)

    async def _feed_worker_loop(self) -> None:
        """后台消费队列，顺序调用 provider.send_text，保证文本顺序且不阻塞 Agent 循环"""
        while True:
            text = await self._text_queue.get()
            if text is None:  # 毒丸信号，退出
                break
            try:
                await self._provider.send_text(text)
            except Exception as e:
                logger.warning("TTS feed_worker send_text error", error=str(e))
            finally:
                self._text_queue.task_done()

    async def _drain_feed_queue(self) -> None:
        """等待队列中所有文本发送完毕，然后停止 worker"""
        if self._feed_worker and not self._feed_worker.done():
            await self._text_queue.join()  # 等待所有已入队的文本处理完
            await self._text_queue.put(None)  # 发送毒丸停止 worker
            try:
                await asyncio.wait_for(self._feed_worker, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._feed_worker.cancel()
            self._feed_worker = None

    async def complete(self) -> Optional[str]:
        """完成合成，保存文件，推送结束事件

        Returns:
            audio_url (成功时) 或 None (失败时)
        """
        if not self._provider or self._completed:
            return self._audio_url or None

        from app.core.event_bus import event_bus

        # 等待后台连接任务完成（可能被速率限制器阻塞）
        if self._connect_task and not self._connect_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._connect_task), timeout=90.0)
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning("TTS connect task wait failed", error=str(e), message_id=self.message_id)

        # 连接失败时无法合成
        if not self._started or self._connect_error:
            self._completed = True
            if self._provider:
                await self._provider.close()
            return None

        try:
            if self.stream_mode == "two-way":
                # 等待队列中的文本全部发送完毕
                await self._drain_feed_queue()
                # 催出残留 + 结束
                await self._provider.flush()
                await self._provider.finish()
            elif self.stream_mode == "one-way":
                # 此时 feed_text 的文本已经在 _buffer 中（对 edge_tts）
                # 或由调用方传入完整文本
                await self._provider.finish()
            # none 模式由 synthesize_once 处理

            # 保存音频文件
            self._audio_url = await self._save_audio()

            # 计算时长
            duration_ms = int((time.time() - self._start_time) * 1000)
            file_size = sum(len(c) for c in self._audio_chunks)

            # 推送 tts_stream_end 事件
            await event_bus.emit_event(
                type="tts_stream_end",
                source=self.conversation_id,
                data={
                    "message_id": self.message_id,
                    "conversation_id": self.conversation_id,
                    "audio_url": self._audio_url,
                    "duration_ms": duration_ms,
                    "file_size": file_size,
                    "provider": self.provider_name,
                    "chunk_count": self._chunk_index,
                },
                priority="normal",
                dimensions=[f"conversation:{self.conversation_id}"],
            )
        except Exception as e:
            logger.error("TTS session complete failed", error=str(e), exc_info=True)
            await event_bus.emit_event(
                type="tts_stream_end",
                source=self.conversation_id,
                data={
                    "message_id": self.message_id,
                    "conversation_id": self.conversation_id,
                    "error": str(e),
                    "provider": self.provider_name,
                },
                priority="normal",
                dimensions=[f"conversation:{self.conversation_id}"],
            )
        finally:
            self._completed = True
            await self._provider.close()

        return self._audio_url or None

    async def cancel(self) -> None:
        """取消合成，通知前端清除播放器"""
        # 停止后台连接任务
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except (asyncio.CancelledError, Exception):
                pass
            self._connect_task = None
        # 停止 feed worker
        if self._feed_worker and not self._feed_worker.done():
            self._feed_worker.cancel()
            self._feed_worker = None
        if self._provider:
            await self._provider.cancel()
            await self._provider.close()
        # 如果已经推送过 tts_stream_start，需要推送 tts_stream_end(cancelled) 让前端清理
        if self._started and not self._completed:
            try:
                from app.core.event_bus import event_bus
                await event_bus.emit_event(
                    type="tts_stream_end",
                    source=self.conversation_id,
                    data={
                        "message_id": self.message_id,
                        "conversation_id": self.conversation_id,
                        "cancelled": True,
                        "provider": self.provider_name,
                    },
                    priority="normal",
                    dimensions=[f"conversation:{self.conversation_id}"],
                )
            except Exception:
                pass
        self._completed = True

    async def synthesize_once(self, text: str, emit_events: bool = True) -> Optional[str]:
        """非流式一次性合成

        Args:
            text: 要合成的文本
            emit_events: 是否推送 tts_generated 事件（同步场景如斜杠命令无需推送）

        Returns:
            audio_url 或 None
        """
        from app.core.event_bus import event_bus

        prov_config = self.provider_config
        prov_config["timeout_ms"] = self.tts_config.get("timeout_ms", 30000)

        provider = create_provider(self.provider_name)
        self._start_time = time.time()

        try:
            if self.stream_mode == "none":
                audio = await provider.synthesize(text, prov_config)
                self._audio_chunks = [audio]
            else:
                # one-way 流式
                provider.on_audio_chunk(self._on_audio_chunk)
                await provider.synthesize_stream(text, prov_config)

            self._audio_url = await self._save_audio()
            duration_ms = int((time.time() - self._start_time) * 1000)
            file_size = sum(len(c) for c in self._audio_chunks)

            if emit_events:
                await event_bus.emit_event(
                    type="tts_generated",
                    source=self.conversation_id,
                    data={
                        "message_id": self.message_id,
                        "conversation_id": self.conversation_id,
                        "audio_url": self._audio_url,
                        "duration_ms": duration_ms,
                        "file_size": file_size,
                        "provider": self.provider_name,
                        "text_preview": text[:50] if text else "",
                    },
                    priority="normal",
                    dimensions=[f"conversation:{self.conversation_id}"],
                )
            return self._audio_url
        except Exception as e:
            logger.error("TTS synthesize_once failed", error=str(e), exc_info=True)
            if emit_events:
                await event_bus.emit_event(
                    type="tts_generated",
                    source=self.conversation_id,
                    data={
                        "message_id": self.message_id,
                        "conversation_id": self.conversation_id,
                        "error": str(e),
                        "provider": self.provider_name,
                    },
                    priority="normal",
                    dimensions=[f"conversation:{self.conversation_id}"],
                )
            return None
        finally:
            await provider.close()

    async def _on_audio_chunk(self, chunk: bytes) -> None:
        """音频 chunk 回调: 累积 + 推送事件"""
        from app.core.event_bus import event_bus

        self._audio_chunks.append(chunk)
        chunk_b64 = base64.b64encode(chunk).decode("ascii")

        try:
            await event_bus.emit_event(
                type="tts_stream_chunk",
                source=self.conversation_id,
                data={
                    "message_id": self.message_id,
                    "conversation_id": self.conversation_id,
                    "chunk_index": self._chunk_index,
                    "audio_base64": chunk_b64,
                },
                priority="normal",
                dimensions=[f"conversation:{self.conversation_id}"],
            )
        except Exception as e:
            logger.debug("Failed to emit tts_stream_chunk", error=str(e))

        self._chunk_index += 1

    async def _save_audio(self) -> str:
        """保存音频到工作区，返回访问 URL"""
        if not self._audio_chunks:
            return ""

        audio_data = b"".join(self._audio_chunks)

        # 确定保存目录
        if self.workspace_path:
            tts_dir = os.path.join(self.workspace_path, "generated_tts")
        else:
            # fallback: 使用全局工作区
            ws_root = str(Path(settings.get("WORKSPACE_ROOT", str(_HAPPYCLAW_HOME / "workspace"))).resolve())
            tts_dir = os.path.join(ws_root, "generated_tts")

        os.makedirs(tts_dir, exist_ok=True)

        # 生成文件名
        prov_config = self.provider_config
        audio_format = (
            prov_config.get("responseFormat")
            or prov_config.get("audio_setting", {}).get("format")
            or "mp3"
        )
        filename = f"tts_{int(time.time())}_{uuid.uuid4().hex[:6]}.{audio_format}"
        filepath = os.path.join(tts_dir, filename)

        with open(filepath, "wb") as f:
            f.write(audio_data)

        logger.info("TTS audio saved", path=filepath, size=len(audio_data))

        # 构建相对 URL
        workspace_root = str(Path(settings.get("WORKSPACE_ROOT", str(_HAPPYCLAW_HOME / "workspace"))).resolve())
        try:
            rel_path = os.path.relpath(filepath, workspace_root)
        except ValueError:
            rel_path = filepath

        rel_path_url = rel_path.replace("\\", "/")
        return f"/api/v1/chat/files/{rel_path_url}"
