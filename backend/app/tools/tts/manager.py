"""TTS Session Manager - 每条消息维护独立 Session，支持后台完成和跨轮次并行"""
import asyncio
from typing import Any, Callable, Coroutine, Dict, Optional

import structlog

from .session import TTSSession

logger = structlog.get_logger()


class TTSSessionManager:
    """每条消息维护独立的 TTSSession，支持后台完成和跨轮次并行

    设计原则:
    - 每条消息一个 session，各自推各自的语音数据
    - 流式期间只做 create + feed_text，不阻塞 Agent 循环
    - 每次迭代文本 feed 完毕后立即 complete_background，使 tts_stream_end 尽早发出
    - complete_all_background 作为安全网，处理边界情况
    - 新一轮对话开始时清理已完成的 session
    - 多轮次的语音可并行处理（即使上一轮未完成也不影响新一轮）
    - 永远不 cancel，中间消息的语音不会被中断
    """

    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        self._sessions: Dict[str, TTSSession] = {}       # message_id -> session
        self._bg_tasks: Dict[str, asyncio.Task] = {}     # message_id -> background task
        self._callbacks: Dict[str, Callable] = {}        # message_id -> on_complete callback

    def get_session(self, message_id: str) -> Optional[TTSSession]:
        """获取活跃 session（用于 feed_text）"""
        return self._sessions.get(message_id)

    def register_on_complete(self, message_id: str, callback: Callable[[str, str], Coroutine]) -> None:
        """注册 session 完成回调（在 finally 块统一触发时使用）

        当 session 的文本已全部 feed 完毕后调用此方法注册回调，
        实际的 complete 操作延迟到 complete_all_background() 统一执行。

        Args:
            message_id: 消息ID
            callback: async (message_id, audio_url) -> None
        """
        self._callbacks[message_id] = callback

    async def create_session(
        self,
        message_id: str,
        tts_config: Dict[str, Any],
        provider_name: str,
        stream_mode: str = "two-way",
        workspace_path: Optional[str] = None,
    ) -> Optional[TTSSession]:
        """创建并启动一个 session，注册到管理器"""
        try:
            session = TTSSession(
                conversation_id=self.conversation_id,
                message_id=message_id,
                tts_config=tts_config,
                provider_name=provider_name,
                stream_mode=stream_mode,
                workspace_path=workspace_path,
            )
            await session.start()
            self._sessions[message_id] = session
            return session
        except Exception as e:
            logger.warning("TTSSessionManager: create_session failed", message_id=message_id, error=str(e))
            return None

    def complete_background(
        self,
        message_id: str,
        on_complete: Optional[Callable[[str, str], Coroutine]] = None,
    ) -> None:
        """后台完成 session（不阻塞调用方）

        Args:
            message_id: 消息ID
            on_complete: 完成回调 async (message_id, audio_url) -> None
        """
        session = self._sessions.get(message_id)
        if not session:
            return
        task = asyncio.create_task(self._bg_complete(message_id, session, on_complete))
        self._bg_tasks[message_id] = task

    async def _bg_complete(
        self,
        message_id: str,
        session: TTSSession,
        on_complete: Optional[Callable[[str, str], Coroutine]],
    ) -> None:
        """后台完成 task 实体"""
        try:
            audio_url = await session.complete()
            if on_complete and audio_url:
                await on_complete(message_id, audio_url)
        except Exception as e:
            logger.warning("TTSSessionManager: bg complete failed", message_id=message_id, error=str(e))

    def synthesize_background(
        self,
        message_id: str,
        text: str,
        tts_config: Dict[str, Any],
        provider_name: str,
        stream_mode: str = "two-way",
        workspace_path: Optional[str] = None,
        on_complete: Optional[Callable[[str, str], Coroutine]] = None,
    ) -> None:
        """后台创建 + 喂入文本 + 完成（用于 mode=final 确认后，或 one-way/none 模式）

        整个流程在后台执行，不阻塞 Agent 循环。
        """
        task = asyncio.create_task(
            self._bg_synthesize(message_id, text, tts_config, provider_name, stream_mode, workspace_path, on_complete)
        )
        self._bg_tasks[message_id] = task

    async def _bg_synthesize(
        self,
        message_id: str,
        text: str,
        tts_config: Dict[str, Any],
        provider_name: str,
        stream_mode: str,
        workspace_path: Optional[str],
        on_complete: Optional[Callable[[str, str], Coroutine]],
    ) -> None:
        """后台合成 task 实体"""
        session: Optional[TTSSession] = None
        try:
            session = TTSSession(
                conversation_id=self.conversation_id,
                message_id=message_id,
                tts_config=tts_config,
                provider_name=provider_name,
                stream_mode=stream_mode,
                workspace_path=workspace_path,
            )
            self._sessions[message_id] = session
            await session.start()

            if stream_mode == "two-way":
                await session.feed_text(text)
                audio_url = await session.complete()
            elif stream_mode == "one-way":
                prov = session._provider
                prov_config = session.provider_config
                prov_config["timeout_ms"] = tts_config.get("timeout_ms", 30000)
                await prov.synthesize_stream(text, prov_config)
                audio_url = await session.complete()
            else:
                audio_url = await session.synthesize_once(text)

            if on_complete and audio_url:
                await on_complete(message_id, audio_url)
        except Exception as e:
            logger.warning("TTSSessionManager: bg synthesize failed", message_id=message_id, error=str(e))

    def complete_all_background(self) -> int:
        """统一后台完成所有已注册回调的 session（在 finally 块调用）

        遍历所有已注册 on_complete 回调的 session，为每个启动独立的后台 task。
        不阻塞调用方，各 session 并行完成。

        Returns:
            启动的后台任务数量
        """
        count = 0
        for msg_id, callback in list(self._callbacks.items()):
            session = self._sessions.get(msg_id)
            if not session or msg_id in self._bg_tasks:
                continue  # 跳过已完成或已在后台处理中的
            task = asyncio.create_task(self._bg_complete(msg_id, session, callback))
            self._bg_tasks[msg_id] = task
            count += 1
        self._callbacks.clear()
        if count:
            logger.debug("TTSSessionManager: complete_all_background started", count=count)
        return count

    def cleanup_completed(self) -> int:
        """清理已完成的后台任务，释放资源。新一轮对话开始时调用。

        Returns:
            清理数量
        """
        done_ids = [mid for mid, task in self._bg_tasks.items() if task.done()]
        for mid in done_ids:
            self._sessions.pop(mid, None)
            self._bg_tasks.pop(mid, None)
            self._callbacks.pop(mid, None)
        if done_ids:
            logger.debug("TTSSessionManager: cleaned up", count=len(done_ids), remaining=len(self._sessions))
        return len(done_ids)

    @property
    def active_count(self) -> int:
        """当前活跃（未完成）的 session 数"""
        return len([t for t in self._bg_tasks.values() if not t.done()])

    async def shutdown(self) -> None:
        """关闭所有活跃 session（会话删除/Agent 停止时调用）"""
        for mid, task in list(self._bg_tasks.items()):
            if not task.done():
                task.cancel()
        for mid, session in list(self._sessions.items()):
            try:
                await session.cancel()
            except Exception:
                pass
        self._sessions.clear()
        self._bg_tasks.clear()


# ===== 全局管理器注册表 (每个会话一个 Manager) =====

_tts_managers: Dict[str, TTSSessionManager] = {}


def get_tts_manager(conversation_id: str) -> TTSSessionManager:
    """获取或创建会话级 TTS 管理器"""
    if conversation_id not in _tts_managers:
        _tts_managers[conversation_id] = TTSSessionManager(conversation_id)
    return _tts_managers[conversation_id]


def remove_tts_manager(conversation_id: str) -> None:
    """移除会话管理器（会话删除时调用）"""
    mgr = _tts_managers.pop(conversation_id, None)
    if mgr:
        asyncio.create_task(mgr.shutdown())
