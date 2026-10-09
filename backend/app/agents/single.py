"""单Agent实现 - ReAct执行循环"""
import uuid
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, AsyncIterator, List, Tuple

import structlog
from app.agents.base import BaseAgent
from app.models.schemas.agent import AgentConfig, AgentStatus
from app.models.schemas.chat import Message, MessageRole, MessageType
from app.models.schemas.tool import ToolCall, ToolResult
from app.core.memory import MemoryManager
from app.core.prompt_engine import PromptEngine
from app.core.reflector import Reflector
from app.core.compressor import ContextCompressor
from app.config import settings
from app.tools.registry import ToolRegistry
from app.tools.tool_search import ToolSearchEngine, ensure_tool_search_registered, get_tool_search_definition
from app.services.persistence import persistence
from app.services.llm_service import LLMService
from app.utils.logging import set_agent_context, clear_agent_context

logger = structlog.get_logger()


def _split_tool_result_for_llm(result: Any):
    """拆分工具结果为「LLM 视图字符串」与「前端专用字段（放入 metadata，不进 LLM）」。

    剥离的前端专用字段：
    - dag（spawn_agent 返回的依赖图可视化数据）：纯前端渲染用，LLM 从 results 获取任务结果即可，无条件剥离；
    - structuredContent（MCP 工具结构化结果）：与 content 数据重复，仅当 content 非空时剥离，
      否则保留兜底给 LLM，避免结果丢失。
    剥离后由 metadata 承载（base.py 构建 LLM 消息只用 content、忽略 metadata），节省 token。

    Returns:
        (llm_content_str, frontend_fields: dict)；frontend_fields 为空表示无需分离。
    """
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False), {}

    llm_view = dict(result)
    frontend_fields: Dict[str, Any] = {}

    # 1. dag：纯前端可视化，无条件剥离（LLM 从 results 字段获取任务执行结果）
    if llm_view.get("dag") is not None:
        frontend_fields["dag"] = llm_view.pop("dag")

    # 2. structuredContent：与 content 重复；仅当 content 非空时剥离（否则兜底给 LLM，不丢失）
    if llm_view.get("structuredContent") and (llm_view.get("content") or []):
        frontend_fields["structured_content"] = llm_view.pop("structuredContent")

    return json.dumps(llm_view, ensure_ascii=False), frontend_fields


@dataclass
class _TTSContext:
    """TTS 运行时上下文，避免 process_message 中大量局部变量"""
    manager: Any = None
    config: Optional[Dict[str, Any]] = None
    auto_mode: str = "off"
    stream_mode: str = "none"
    provider_name: str = "edge_tts"
    mode: str = "final"


@dataclass
class _StreamResult:
    """LLM 流式调用的累积结果"""
    content: str = ""
    reasoning_content: str = ""
    tool_calls: Optional[List] = None
    usage: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cancelled: bool = False


class SingleAgent(BaseAgent):
    """单Agent - 支持ReAct执行循环"""
    
    def __init__(
        self,
        config: AgentConfig,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        memory_manager: MemoryManager,
        prompt_engine: PromptEngine,
        reflector: Optional[Reflector] = None,
        compressor: Optional[ContextCompressor] = None,
    ):
        super().__init__(
            config=config,
            llm_service=llm_service,
            tool_registry=tool_registry,
            memory_manager=memory_manager,
            prompt_engine=prompt_engine,
        )
        self.reflector = reflector
        self.compressor = compressor
        self._max_iterations = 50  # 最大ReAct循环次数
        
        # ✅ ToolSearch 延迟加载机制
        self._tool_search_engine: Optional[ToolSearchEngine] = None
        if settings.get("TOOL_SEARCH_ENABLED", False):
            ts_config = settings._json_config.get("tool_search", {})
            if hasattr(ts_config, 'to_dict'):
                ts_config = ts_config.to_dict()
            self._tool_search_engine = ToolSearchEngine(
                registry=self.tool_registry,
                config=ts_config if isinstance(ts_config, dict) else {},
            )
            self._register_tool_search_tool()
            logger.info(
                "ToolSearch enabled",
                agent_id=self.id,
                deferred_count=self._tool_search_engine.deferred_count,
            )

    def _register_tool_search_tool(self) -> None:
        """注册 tool_search 工具到全局 registry（幂等，只注册一次）"""
        ensure_tool_search_registered()

    async def _cleanup_on_cancel(self, conversation_id: str) -> None:
        """取消时清理不完整的历史记录"""
        logger.warning("Cleaning up incomplete history on cancel", conversation_id=conversation_id)
        
        try:
            conv = self.get_conversation(conversation_id)
            if conv and conv.messages:
                
                # 从后往前查找：找到最后一个有 tool_calls 但没有对应 tool_results 的 assistant 消息
                last_incomplete_idx = -1
                
                for i in range(len(conv.messages) - 1, -1, -1):
                    msg = conv.messages[i]
                    if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                        # ✅ 处理 tool_calls 可能是字典列表或 ToolCall 对象列表
                        try:
                            if isinstance(msg.tool_calls[0], dict):
                                # 字典列表
                                tool_call_ids = set(tc.get('id') for tc in msg.tool_calls if isinstance(tc, dict))
                            else:
                                # ToolCall 对象列表
                                tool_call_ids = set(tc.id for tc in msg.tool_calls)
                        except (IndexError, AttributeError) as e:
                            logger.warning(f"Failed to extract tool_call_ids at index {i}: {e}")
                            continue
                        
                        # 统计后面有多少个 tool 消息响应了这些 tool_call_id
                        responded_ids = set()
                        for j in range(i + 1, len(conv.messages)):
                            next_msg = conv.messages[j]
                            if next_msg.role == MessageRole.TOOL and next_msg.tool_call_id:
                                responded_ids.add(next_msg.tool_call_id)
                        
                        # 如果有未响应的 tool_call_id，说明这条消息不完整
                        if not tool_call_ids.issubset(responded_ids):
                            last_incomplete_idx = i
                            logger.warning(f"Found incomplete assistant message at index {i}: {len(tool_call_ids)} tool_calls, {len(responded_ids)} responded")
                            break
                
                if last_incomplete_idx >= 0:
                    removed_count = len(conv.messages) - last_incomplete_idx
                    conv.messages = conv.messages[:last_incomplete_idx]
                    # 子agent跳过数据库持久化
                    if not self.is_subagent:
                        await persistence.save_conversation(conv)
                    logger.info(f"Cleaned up {removed_count} incomplete messages from index {last_incomplete_idx}")
        except Exception as e:
            logger.error("Failed to cleanup incomplete messages", error=str(e), traceback=True)
    
    async def _get_session_model_config(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """获取会话级模型配置（✅ 优先内存，确保与数据库同步）"""
        try:
            # ✅ 优先从内存中的conversation获取（已在上层API更新时同步）
            conv = self.get_conversation(conversation_id)
            if not conv:
                # 内存中不存在，尝试从数据库加载
                conv = await persistence.load_conversation(conversation_id)
                if conv:
                    # 恢复到内存中供后续使用
                    self._conversations[conv.id] = conv
            
            if not conv:
                return None
            
            # 获取session_model配置
            session_model = getattr(conv, 'session_model', None) or (conv.metadata or {}).get('session_model')
            
            # ✅ 如果没有会话级配置，回退到 agent 默认主模型
            #    优先从 settings.json 的 agent.entries.{name}.model.primary 解析（"provider/model_id" 格式），
            #    再回退到 self.config.llm_provider + self.config.llm_model（llm_model 仅为模型名，无 provider 前缀）
            if not session_model:
                provider = None
                model_id = None

                # 1) 优先解析 settings.json 中 agent 的 primary 模型
                try:
                    from app.core.settings_store import resolve_agent_primary_model
                    primary_info = resolve_agent_primary_model(self.config.name)
                    if primary_info and primary_info.get('provider') and primary_info.get('model_id'):
                        provider = primary_info['provider']
                        model_id = primary_info['model_id']
                except Exception as e:
                    logger.debug(f"resolve_agent_primary_model failed: {e}")

                # 2) 回退到 agent 实例配置（llm_provider 为枚举，需取 .value）
                if not provider or not model_id:
                    cfg_provider = getattr(self.config, 'llm_provider', None)
                    if cfg_provider is not None:
                        provider = getattr(cfg_provider, 'value', cfg_provider)
                    model_id = model_id or getattr(self.config, 'llm_model', None)

                if not provider or not model_id:
                    logger.debug("No session model config and cannot resolve agent default model")
                    return None

                logger.debug("Using default agent model for cost calculation",
                           provider=provider, model_id=model_id)
                session_model = {
                    'provider': provider,
                    'model_id': model_id,
                }
            else:
                provider = session_model.get('provider')
                model_id = session_model.get('model_id')

            if not provider or not model_id:
                return None
            
            # ✅ 解析供应商下该模型的能力（是否允许推理、是否支持 reasoning_effort）
            try:
                from app.core.settings_store import resolve_llm_endpoint
                endpoint = resolve_llm_endpoint(provider, model_id)
            except Exception:
                endpoint = {'reasoning': False, 'supports_reasoning_effort': False, "context_window": self.config.context_window}
            
            # ✅ reasoning_level → reasoning_effort（仅当模型允许推理且供应商支持时）
            reasoning_effort = None
            reasoning_level = session_model.get('reasoning_level')
            if (
                reasoning_level is not None
                and endpoint.get('reasoning')
                and endpoint.get('supports_reasoning_effort')
            ):
                reasoning_effort = self._map_reasoning_effort(reasoning_level)
            
            # 计算max_tokens（使用 endpoint 配置或 agent 默认值）
            max_tokens = endpoint.get('max_tokens', self.config.max_tokens)  # 默认使用agent配置
            
            # ✅ speed_mode 映射为 OpenAI API 的 service_tier 参数
            # 可选值: "auto" / "default" / "flex" / "scale" / "priority" / "fast"
            speed_mode = session_model.get('speed_mode')
            service_tier = speed_mode if speed_mode else None
            
            logger.debug(
                "Retrieved session model config",
                conversation_id=conversation_id,
                provider=provider,
                model_id=model_id,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                service_tier=service_tier,
            )
            
            return {
                'provider': provider,
                'model_id': model_id,
                'reasoning_effort': reasoning_effort,
                'max_tokens': max_tokens,
                'service_tier': service_tier,
                "context_window": endpoint.get("context_window", self.config.context_window),
                "auto_explicit_cache": (endpoint.get("params") or {}).get("autoCreateExplicitCache", False),
            }
        except Exception as e:
            logger.warning(
                "Failed to get session model config, using agent default",
                conversation_id=conversation_id,
                error=str(e),
            )
            return None
    
    @staticmethod
    def _map_reasoning_effort(reasoning_level: str) -> Optional[str]:
        """前端直接传递 reasoning_level 字符串（none/low/high/max），校验后透传。"""
        valid = {"none", "low", "high", "max"}
        if isinstance(reasoning_level, str) and reasoning_level in valid:
            return reasoning_level
        return None
    
    async def process_message(
        self,
        message: str,
        conversation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        consume_user_message_id: Optional[str] = None,
    ) -> AsyncIterator[Message]:
        """处理消息 - ReAct循环（支持流式输出）"""
        parent_name = self.config.parent_agent_name if self.is_subagent else None
        set_agent_context(self.config.name, self.id, parent_name)
        try:
            self.status = AgentStatus.RUNNING

            # 1. 解析或创建会话
            conversation, conversation_id = await self._resolve_conversation(conversation_id, message)
            self.clear_cancel(conversation_id)

            # 2. 获取会话级模型配置
            session_config = await self._get_session_model_config(conversation_id)

            # 3. 添加用户消息
            await self._handle_user_message(conversation_id, message, metadata, consume_user_message_id)

            # 4. 记忆预取（后台并行，仅主 Agent）
            self._start_memory_prefetch(conversation_id, message)

            # 5. TTS 初始化
            tts_ctx = await self._init_tts_context(conversation_id)

            # 6. ReAct 循环
            iteration = 0
            reactive_retries = 0
            memory_injected = False
            max_tokens_upgraded = False
            current_max_tokens = self.config.max_tokens
            accumulated_usage: Optional[Dict[str, Any]] = None

            try:
                while iteration < self._max_iterations:
                    if self.compressor and reactive_retries >= self.compressor.reactive_compact_max_retries:
                        yield await self._add_message(conversation_id, MessageRole.ASSISTANT,
                            "ReAct loop reactive compact retries reached max, skipping compression", MessageType.TEXT)
                        break
                    iteration += 1
                    logger.debug("ReAct iteration", iteration=iteration, agent_id=self.id)

                    # 检查取消
                    if self.is_cancelled(conversation_id):
                        for msg in await self._handle_cancelled(conversation_id):
                            yield msg
                        break

                    # 准备 LLM 消息（压缩标记 + 构建 + 历史验证）
                    await self._apply_compression_markers(conversation_id, session_config)
                    llm_messages = await self._build_messages_for_llm(conversation_id,
                        auto_explicit_cache=session_config.get('auto_explicit_cache', False) if session_config else False)
                    llm_messages = await self._validate_history_and_rebuild(conversation_id, llm_messages, session_config)

                    # 记忆注入（仅首次）
                    if not memory_injected and not self.is_subagent:
                        memory_injected = True
                        llm_messages = await self._inject_memory_prefetch(conversation_id, conversation, llm_messages)

                    # 后台任务 & 团队消息注入
                    await self._inject_background_notifications(conversation_id, llm_messages)
                    await self._inject_teammate_messages(conversation_id, llm_messages)

                    # 获取可用工具
                    tools = self._get_available_tools(conversation_id)

                    # LLM 流式调用 + 响应处理
                    try:
                        result_holder: List[_StreamResult] = []
                        async for item in self._stream_llm_call(
                            conversation_id, llm_messages, tools, session_config,
                            current_max_tokens, tts_ctx, result_holder,
                        ):
                            yield item
                        stream_result = result_holder[0] if result_holder else None

                        # 累积 usage
                        if stream_result and stream_result.usage:
                            accumulated_usage = self._accumulate_usage(accumulated_usage, stream_result.usage)

                        # 取消处理
                        if stream_result and stream_result.cancelled:
                            for msg in await self._handle_cancelled(
                                conversation_id, stream_result.content, stream_result.message_id
                            ):
                                yield msg
                            break

                        # max_tokens 截断 → 续写
                        if (stream_result and not max_tokens_upgraded
                            and stream_result.finish_reason in ("length", "max_tokens")
                            and not stream_result.tool_calls):
                            trunc_msgs, current_max_tokens = await self._handle_max_tokens_truncation(
                                conversation_id, stream_result, current_max_tokens, tts_ctx)
                            for msg in trunc_msgs:
                                yield msg
                            max_tokens_upgraded = True
                            continue

                        # 工具调用
                        if stream_result and stream_result.tool_calls:
                            if self.is_cancelled(conversation_id):
                                break
                            for msg in await self._handle_tool_calls_iteration(
                                conversation_id, stream_result, tts_ctx
                            ):
                                yield msg
                            continue

                        # 最终响应
                        if stream_result:
                            final_msgs, should_continue = await self._handle_final_response(
                                conversation_id, stream_result, accumulated_usage, session_config, tts_ctx
                            )
                            for msg in final_msgs:
                                yield msg
                            if should_continue:
                                continue
                            break

                    except Exception as e:
                        # 上下文过长错误恢复
                        error_str = str(e).lower()
                        if ("prompt too long" in error_str or "413" in str(e) or
                            "context_length_exceeded" in error_str) and self.compressor:
                            logger.warning("Context length exceeded, attempting reactive compact", error=str(e))
                            reactive_retries += 1
                            conv = self.get_conversation(conversation_id)
                            conv.messages = await self.compressor.reactive_compact(conv.messages)
                            continue
                        else:
                            logger.error("LLM call failed", error=str(e), exc_info=True)
                            yield await self._add_message(conversation_id, MessageRole.ASSISTANT,
                                f"Error: {str(e)}", MessageType.ERROR)
                            self.status = AgentStatus.ERROR
                            return

                if iteration >= self._max_iterations:
                    yield await self._add_message(conversation_id, MessageRole.ASSISTANT,
                        "Reached maximum iterations. Task may be incomplete.", MessageType.TEXT)
            finally:
                self.clear_cancel(conversation_id)
                if tts_ctx.manager:
                    tts_ctx.manager.complete_all_background()

            # Stop Hook：会话轮次结束后触发记忆提取/整理
            if not self.is_subagent and not self.is_cancelled(conversation_id):
                await self._inject_background_notifications_round_end(conversation_id)
                self._handle_stop_hook(conversation_id)

            self.status = AgentStatus.IDLE
        finally:
            clear_agent_context()

    # ------------------------------------------------------------------
    # process_message 辅助方法
    # ------------------------------------------------------------------

    async def _resolve_conversation(
        self, conversation_id: Optional[str], message: str
    ) -> Tuple[Any, str]:
        """解析或创建会话，返回 (conversation, conversation_id)"""
        if not conversation_id or conversation_id.startswith(('local-', 'temp-')):
            if conversation_id and conversation_id.startswith(('local-', 'temp-')):
                logger.info("Ignoring temporary conversation_id from frontend", temp_id=conversation_id)
            conversation = await self.create_conversation_async(title=message[:50])
            return conversation, conversation.id

        conversation = self.get_conversation(conversation_id)
        if conversation:
            self._current_conversation = conversation.id
            return conversation, conversation_id

        # 内存中不存在，尝试从数据库加载
        conversation = await persistence.load_conversation(conversation_id)
        if conversation:
            self._conversations[conversation.id] = conversation
            self._current_conversation = conversation.id
            logger.info("Conversation restored from database in process_message", conversation_id=conversation.id)
            return conversation, conversation_id

        # 数据库中也不存在，创建新会话
        logger.info("Conversation not found, creating new one", conversation_id=conversation_id)
        conversation = await self.create_conversation_async(title=message[:50])
        return conversation, conversation.id

    async def _handle_user_message(
        self,
        conversation_id: str,
        message: str,
        metadata: Optional[Dict[str, Any]],
        consume_user_message_id: Optional[str],
    ) -> None:
        """处理用户消息添加（支持 consume 模式跳过重复）"""
        if consume_user_message_id:
            conv_check = self.get_conversation(conversation_id)
            exists = bool(conv_check) and any(
                m.id == consume_user_message_id for m in conv_check.messages
            )
            if exists:
                logger.info(
                    "Consuming pre-persisted user message, skip adding",
                    message_id=consume_user_message_id,
                    conversation_id=conversation_id,
                )
                return
            # 找不到已持久化消息时回退为正常添加，避免丢失用户输入

        await self._add_message(conversation_id, MessageRole.USER, message, metadata=metadata)

    def _start_memory_prefetch(self, conversation_id: str, message: str) -> None:
        """启动记忆预取（后台并行，仅主 Agent）"""
        if self.is_subagent:
            return
        try:
            from app.core.memory_system.prefetch import memory_prefetch_manager
            memory_prefetch_manager.start_relevant_memory_prefetch(
                conversation_id=conversation_id,
                user_query=message,
                llm_service=self.llm_service,
            )
        except Exception as e:
            logger.warning("Failed to start memory prefetch", error=str(e))

    async def _init_tts_context(self, conversation_id: str) -> _TTSContext:
        """初始化 TTS 上下文配置（全局配置 + 会话级覆盖）"""
        ctx = _TTSContext()
        if self.is_subagent:
            return ctx
        try:
            from app.tools.tts_service import (
                resolve_tts_config, resolve_persona, get_provider_config,
                get_effective_stream_mode, parse_tts_tags, get_tts_manager,
            )
            ctx.config = resolve_tts_config(self.config.name)

            # 会话级 tts_override（通过 /tts on|off|reset 设置）优先于全局配置
            conv = self.get_conversation(conversation_id)
            if conv and hasattr(conv, 'metadata') and conv.metadata:
                tts_override = conv.metadata.get("tts_override")
                if tts_override:
                    ctx.config = {**ctx.config, **tts_override}

            ctx.auto_mode = ctx.config.get("auto", "off")
            ctx.stream_mode = ctx.config.get("stream_mode", "one-way")
            ctx.mode = ctx.config.get("mode", "final")
            persona_id = ctx.config.get("persona")
            if persona_id:
                ctx.config = resolve_persona(ctx.config, persona_id)
            ctx.provider_name = ctx.config.get("provider", "edge_tts")
            ctx.stream_mode = get_effective_stream_mode(ctx.provider_name, ctx.stream_mode)
            if ctx.auto_mode != "off":
                ctx.manager = get_tts_manager(conversation_id)
                ctx.manager.cleanup_completed()
            logger.debug("TTS config resolved", auto=ctx.auto_mode, stream_mode=ctx.stream_mode,
                         provider=ctx.provider_name, mode=ctx.mode)
        except Exception as e:
            logger.warning("Failed to resolve TTS config", error=str(e))
            ctx.auto_mode = "off"
        return ctx

    async def _apply_compression_markers(self, conversation_id: str, session_config: Optional[Dict]) -> None:
        """在构建 LLM 消息前插入压缩边界标记"""
        if not self.compressor:
            return
        conv = self.get_conversation(conversation_id)
        if not conv or not conv.messages:
            return

        max_output_tokens = session_config.get('max_tokens') if session_config else self.config.max_tokens
        context_window = session_config.get('context_window') if session_config else self.config.context_window
        marker_types = {
            MessageType.COMPRESS_BUDGET_BOUNDARY,
            MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY,
            MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY,
            MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY,
            MessageType.ACTIVE_COMPACT_BOUNDARY,
        }
        existing_marker_ids = {
            msg.id for msg in conv.messages if msg.message_type in marker_types
        }
        conv.messages = await self.compressor.mark_compression_boundaries(
            messages=conv.messages,
            conversation_id=conversation_id,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )
        # 增量保存新增的压缩标记消息到数据库（子agent跳过持久化）
        if not self.is_subagent:
            for msg in conv.messages:
                if msg.message_type in marker_types and msg.id not in existing_marker_ids:
                    await persistence.save_message(conversation_id, msg)

    async def _validate_history_and_rebuild(
        self, conversation_id: str, llm_messages: List[Dict], session_config: Optional[Dict]
    ) -> List[Dict]:
        """验证历史完整性，必要时截断并重建 LLM 消息"""
        try:
            conv = self.get_conversation(conversation_id)
            if not conv or not conv.messages:
                return llm_messages

            for i, msg in enumerate(conv.messages):
                if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                    try:
                        if isinstance(msg.tool_calls[0], dict):
                            tool_call_ids = set(tc.get('id') for tc in msg.tool_calls if isinstance(tc, dict))
                        else:
                            tool_call_ids = set(tc.id for tc in msg.tool_calls)
                    except (IndexError, AttributeError):
                        continue
                    responded_ids = set()
                    for j in range(i + 1, len(conv.messages)):
                        next_msg = conv.messages[j]
                        if next_msg.role == MessageRole.TOOL and next_msg.tool_call_id:
                            responded_ids.add(next_msg.tool_call_id)

                    if not tool_call_ids.issubset(responded_ids):
                        logger.warning(f"Auto-fixing incomplete tool calls at index {i}")
                        conv.messages = conv.messages[:i]
                        if not self.is_subagent:
                            await persistence.save_conversation(conv)
                        return await self._build_messages_for_llm(conversation_id,
                            auto_explicit_cache=session_config.get('auto_explicit_cache', False) if session_config else False)
        except Exception as e:
            logger.error("Failed to validate history integrity", error=str(e), exc_info=True)
        return llm_messages

    async def _inject_memory_prefetch(
        self, conversation_id: str, conversation: Any, llm_messages: List[Dict]
    ) -> List[Dict]:
        """注入记忆预取结果到 LLM 消息"""
        try:
            from app.core.memory_system.prefetch import memory_prefetch_manager
            reminder = await memory_prefetch_manager.collect_prefetch(conversation_id)
            if reminder:
                llm_messages.append({"role": "user", "content": reminder})
                memory_msg = Message(
                    id=str(uuid.uuid4()),
                    role=MessageRole.USER,
                    content=reminder,
                    message_type=MessageType.MEMORY,
                )
                conversation.messages.append(memory_msg)
                await persistence.save_message(conversation_id, memory_msg)
                logger.info("Relevant memories injected", conversation_id=conversation_id)
        except Exception as e:
            logger.warning("Failed to collect memory prefetch", error=str(e))
        return llm_messages

    def _get_available_tools(self, conversation_id: str) -> List[Dict]:
        """获取当前可用工具列表（按会话隔离）"""
        if self._tool_search_engine:
            tools = self._tool_search_engine.get_tools_for_llm(conversation_id)
        else:
            tools = self.tool_registry.get_function_definitions()
        logger.info("Available tools for LLM call", tools_count=len(tools),
                    tool_names=[t["function"]["name"] for t in tools])
        return tools

    async def _stream_llm_call(
        self,
        conversation_id: str,
        llm_messages: List[Dict],
        tools: List[Dict],
        session_config: Optional[Dict],
        current_max_tokens: int,
        tts_ctx: _TTSContext,
        result_holder: List[_StreamResult],
    ) -> AsyncIterator[Message]:
        """LLM 流式调用，yield 流式 Message，完成后将 _StreamResult 存入 result_holder"""
        stream_result = _StreamResult()

        # TTS two-way + mode="all": 延迟到首个文本 chunk 到达时再创建 session
        # （避免工具调用轮次无文本输出时浪费连接和 RPM）
        _tws_session_ready = False

        stream_chunks = []
        async for chunk in self.llm_service.chat_stream(
            messages=llm_messages,
            tools=tools if tools else None,
            temperature=self.config.temperature,
            max_tokens=session_config.get('max_tokens') if session_config else current_max_tokens,
            model=session_config.get('model_id') if session_config else (self.config.llm_model or None),
            provider=session_config.get('provider') if session_config else None,
            reasoning_effort=session_config.get('reasoning_effort') if session_config else None,
            service_tier=session_config.get('service_tier') if session_config else None,
            is_cancelled=lambda: self.is_cancelled(conversation_id),
            prompt_cache_key=conversation_id,
            conversation_id=conversation_id,
        ):
            if self.is_cancelled(conversation_id):
                logger.info("Request cancelled during LLM stream", conversation_id=conversation_id)
                stream_result.cancelled = True
                break

            if chunk.content:
                stream_result.content += chunk.content
                # TTS two-way: 首个文本 chunk 到达时懒创建 session
                if (not _tws_session_ready and tts_ctx.manager
                        and tts_ctx.auto_mode in ("always", "inbound")
                        and tts_ctx.stream_mode == "two-way" and tts_ctx.mode == "all"):
                    ws_path = self.get_workspace_path(conversation_id)
                    await tts_ctx.manager.create_session(
                        message_id=stream_result.message_id,
                        tts_config=tts_ctx.config,
                        provider_name=tts_ctx.provider_name,
                        stream_mode="two-way",
                        workspace_path=ws_path,
                    )
                    _tws_session_ready = True
                # TTS two-way: 逐 chunk 送入
                if tts_ctx.manager and tts_ctx.stream_mode == "two-way":
                    active_sess = tts_ctx.manager.get_session(stream_result.message_id)
                    if active_sess:
                        try:
                            await active_sess.feed_text(chunk.content)
                        except Exception as e:
                            logger.debug("TTS feed_text error", error=str(e))

            if chunk.reasoning_content:
                stream_result.reasoning_content += chunk.reasoning_content

            if chunk.tool_calls_delta:
                stream_result.tool_calls = chunk.tool_calls_delta

            if chunk.usage:
                stream_result.usage = chunk.usage

            stream_chunks.append(chunk)

            # Yield 流式消息（有内容且无工具调用时）
            if stream_result.content and not stream_result.tool_calls:
                yield Message(
                    id=stream_result.message_id,
                    role=MessageRole.ASSISTANT,
                    content=stream_result.content,
                    message_type=MessageType.TEXT,
                    created_at=datetime.utcnow(),
                )

        # 提取 finish_reason
        for c in reversed(stream_chunks):
            if c.finish_reason:
                stream_result.finish_reason = c.finish_reason
                break

        result_holder.append(stream_result)

    @staticmethod
    def _accumulate_usage(
        accumulated: Optional[Dict[str, Any]], new_usage: Dict[str, Any]
    ) -> Dict[str, Any]:
        """累积跨迭代的 token 用量"""
        if accumulated is None:
            accumulated = dict(new_usage)
        else:
            for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                        "cached_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens"):
                if key in new_usage:
                    accumulated[key] = accumulated.get(key, 0) + new_usage[key]
            if "completion_tokens_details" in new_usage:
                details = accumulated.setdefault("completion_tokens_details", {})
                for dk, dv in new_usage["completion_tokens_details"].items():
                    details[dk] = details.get(dk, 0) + dv
        accumulated["llm_calls"] = accumulated.get("llm_calls", 0) + 1
        accumulated["last_prompt_tokens"] = new_usage.get("prompt_tokens", 0)
        return accumulated

    async def _handle_cancelled(
        self, conversation_id: str, content: Optional[str] = None, message_id: Optional[str] = None
    ) -> List[Message]:
        """处理请求取消：清理历史 + 生成取消消息"""
        logger.info("Request cancelled, cleaning up history", conversation_id=conversation_id)
        await self._cleanup_on_cancel(conversation_id)
        cancelled_msg = await self._add_message(
            conversation_id,
            MessageRole.ASSISTANT,
            content or "[已停止生成]",
            MessageType.TEXT,
            message_id_override=message_id,
        )
        return [cancelled_msg]

    async def _handle_max_tokens_truncation(
        self,
        conversation_id: str,
        stream_result: _StreamResult,
        current_max_tokens: int,
        tts_ctx: _TTSContext,
    ) -> Tuple[List[Message], int]:
        """处理 max_tokens 截断：保存截断消息 + 添加续写提示 + 升级 max_tokens"""
        logger.warning(
            "max_tokens hit, upgrading to 1.5x and adding continuation prompt",
            finish_reason=stream_result.finish_reason,
            old_max_tokens=current_max_tokens,
            content_length=len(stream_result.content),
        )
        # 1) 保留截断的 assistant 消息
        truncated_msg = await self._add_message(
            conversation_id, MessageRole.ASSISTANT,
            stream_result.content or "", MessageType.TEXT,
            reasoning_content=stream_result.reasoning_content or None,
            message_id_override=stream_result.message_id,
        )
        # 2) 添加续写提示
        continuation_text = (
            "Output token limit hit. Resume directly — no apology, "
            "no recap of what you were doing. Pick up mid-thought if "
            "that is where the cut happened. Break remaining work into "
            "smaller pieces."
        )
        await self._add_message(conversation_id, MessageRole.USER, continuation_text, MessageType.CONTINUATION)

        # 3) 升级 max_tokens（只升级一次）
        new_max_tokens = int((current_max_tokens or settings.DEFAULT_MAX_TOKENS) * 1.5)
        logger.info("max_tokens upgraded", new_max_tokens=new_max_tokens)

        # TTS: 注册回调
        if tts_ctx.manager and tts_ctx.manager.get_session(stream_result.message_id) and stream_result.content:
            async def _on_trunc_tts_done(msg_id, audio_url, _m=truncated_msg, _c=conversation_id):
                _m.metadata = _m.metadata or {}
                _m.metadata["tts"] = {"status": "complete", "audio_url": audio_url, "provider": tts_ctx.provider_name, "stream_mode": "two-way"}
                await persistence.update_message_metadata(_m.id, _m.metadata)
            tts_ctx.manager.register_on_complete(stream_result.message_id, _on_trunc_tts_done)

        return [truncated_msg], new_max_tokens

    async def _handle_tool_calls_iteration(
        self, conversation_id: str, stream_result: _StreamResult, tts_ctx: _TTSContext
    ) -> List[Message]:
        """处理工具调用迭代：执行工具 + TTS 处理，返回需 yield 的消息列表"""
        messages: List[Message] = []
        tool_calls = stream_result.tool_calls

        logger.info("LLM response received", has_tool_calls=True,
                    tool_calls_count=len(tool_calls), content_length=len(stream_result.content))

        # 保存 assistant 消息（含 tool_calls）
        assistant_msg = await self._add_message(
            conversation_id, MessageRole.ASSISTANT,
            stream_result.content, MessageType.TOOL_CALL,
            metadata={"tool_calls": tool_calls},
            tool_calls=tool_calls,
            reasoning_content=stream_result.reasoning_content or None,
            message_id_override=stream_result.message_id,
        )
        messages.append(assistant_msg)

        # 执行工具调用
        tool_results = await self._execute_tool_calls(tool_calls, conversation_id)

        # 添加工具结果
        for result in tool_results:
            if result.success:
                llm_content, frontend_fields = _split_tool_result_for_llm(result.result)
            else:
                llm_content, frontend_fields = f"Error: {result.error}", {}
            tool_metadata = {
                "tool_call_id": result.tool_call_id,
                "tool_name": result.name,
                "success": result.success,
            }
            tool_metadata.update(frontend_fields)
            tool_msg = await self._add_message(
                conversation_id, MessageRole.TOOL, llm_content, MessageType.TOOL_RESULT,
                metadata=tool_metadata, tool_call_id=result.tool_call_id, name=result.name,
            )
            # 系统工具不发送给前端
            if not self.tool_registry.is_system_tool(result.name):
                messages.append(tool_msg)

        # TTS: tool_calls 迭代的语音处理
        await self._process_tts_for_iteration(conversation_id, stream_result, assistant_msg, tts_ctx)
        return messages

    async def _process_tts_for_iteration(
        self, conversation_id: str, stream_result: _StreamResult,
        assistant_msg: Message, tts_ctx: _TTSContext,
    ) -> None:
        """TTS 处理：工具调用迭代（mode=all 时生成语音）"""
        if not (tts_ctx.manager and tts_ctx.mode == "all"
                and stream_result.content and tts_ctx.auto_mode != "off"):
            return

        async def _on_iter_tts_done(msg_id, audio_url, _m=assistant_msg, _c=conversation_id,
                                     _p=tts_ctx.provider_name, _sm=tts_ctx.stream_mode):
            _m.metadata = _m.metadata or {}
            _m.metadata["tts"] = {"status": "complete", "audio_url": audio_url, "provider": _p, "stream_mode": _sm}
            await persistence.update_message_metadata(_m.id, _m.metadata)

        if tts_ctx.stream_mode == "two-way" and tts_ctx.manager.get_session(stream_result.message_id):
            # 立即后台完成该 session（不等待 finally 统一触发），使 tts_stream_end 尽早发出
            tts_ctx.manager.complete_background(stream_result.message_id, _on_iter_tts_done)
        elif tts_ctx.stream_mode in ("one-way", "none"):
            from app.tools.tts_service import parse_tts_tags as _parse_tags
            ws_path = self.get_workspace_path(conversation_id)
            allow_prov = (tts_ctx.config or {}).get("model_overrides", {}).get("allow_provider", False)
            mo_enabled = (tts_ctx.config or {}).get("model_overrides", {}).get("enabled", False)
            tts_text = stream_result.content
            eff_provider = tts_ctx.provider_name
            if mo_enabled and '[[tts:' in stream_result.content:
                cleaned, segs = _parse_tags(stream_result.content, allow_prov)
                if segs:
                    tts_text = "".join(seg.get("text", "") + seg.get("tts_only_text", "") for seg in segs).strip()
                    if allow_prov:
                        for seg in segs:
                            if seg.get("config_overrides", {}).get("provider"):
                                eff_provider = seg["config_overrides"]["provider"]
                                break
                else:
                    tts_text = cleaned.strip()
            # tagged 模式无标记不触发
            should_synth = True
            if tts_ctx.auto_mode == "tagged" and '[[tts:' not in stream_result.content:
                should_synth = False
            if should_synth and tts_text:
                tts_ctx.manager.synthesize_background(
                    message_id=stream_result.message_id,
                    text=tts_text,
                    tts_config=tts_ctx.config,
                    provider_name=eff_provider,
                    stream_mode=tts_ctx.stream_mode,
                    workspace_path=ws_path,
                    on_complete=_on_iter_tts_done,
                )

    async def _handle_final_response(
        self,
        conversation_id: str,
        stream_result: _StreamResult,
        accumulated_usage: Optional[Dict[str, Any]],
        session_config: Optional[Dict],
        tts_ctx: _TTSContext,
    ) -> Tuple[List[Message], bool]:
        """处理最终响应：计算费用 + 保存消息 + TTS + 团队空闲检查
        Returns: (messages_to_yield, should_continue_react_loop)
        """
        messages: List[Message] = []

        logger.info("LLM response received", has_tool_calls=False,
                    content_length=len(stream_result.content))

        # 计算 token 用量与费用
        token_metadata = self._calculate_token_metadata(accumulated_usage, session_config)

        # 保存最终响应
        final_response = await self._add_message(
            conversation_id, MessageRole.ASSISTANT,
            stream_result.content or "", MessageType.TEXT,
            reasoning_content=stream_result.reasoning_content or None,
            metadata=token_metadata or None,
            message_id_override=stream_result.message_id,
        )
        messages.append(final_response)

        # TTS 最终响应处理
        tts_extra_msgs = await self._process_tts_for_final(
            conversation_id, stream_result, final_response, tts_ctx)
        messages.extend(tts_extra_msgs)

        # Lead 空闲等待循环（团队模式）
        team_injection = await self._team_lead_idle_check(conversation_id)
        if team_injection:
            await self._add_message(conversation_id, MessageRole.USER, team_injection, MessageType.TEAMMATE)
            return messages, True  # should_continue
        return messages, False

    def _calculate_token_metadata(
        self, accumulated_usage: Optional[Dict], session_config: Optional[Dict]
    ) -> Dict[str, Any]:
        """计算 token 用量与费用元数据"""
        if not accumulated_usage or not session_config:
            logger.debug("Skip cost calculation: missing accumulated_usage or session_config",
                         has_accumulated_usage=bool(accumulated_usage),
                         has_session_config=bool(session_config))
            return {}
        provider = session_config.get('provider')
        model_id = session_config.get('model_id')
        if not provider or not model_id:
            logger.debug("Missing provider or model_id for cost calculation")
            return {}
        try:
            cost_info = self.llm_service._calculate_cost(accumulated_usage, provider, model_id)
            return {"usage": accumulated_usage, "cost": cost_info}
        except Exception as e:
            logger.error("Failed to calculate cost", error=str(e), exc_info=True)
            return {}

    async def _process_tts_for_final(
        self,
        conversation_id: str,
        stream_result: _StreamResult,
        final_response: Message,
        tts_ctx: _TTSContext,
    ) -> List[Message]:
        """TTS 处理：最终响应（后台合成/注册回调）"""
        extra_msgs: List[Message] = []
        if not (tts_ctx.manager and tts_ctx.auto_mode != "off" and stream_result.content):
            return extra_msgs

        try:
            from app.tools.tts_service import parse_tts_tags

            allow_provider = (tts_ctx.config or {}).get("model_overrides", {}).get("allow_provider", False)
            model_overrides_enabled = (tts_ctx.config or {}).get("model_overrides", {}).get("enabled", False)
            cleaned_text = stream_result.content
            tts_segments: list = []
            if model_overrides_enabled and '[[tts:' in stream_result.content:
                cleaned_text, tts_segments = parse_tts_tags(stream_result.content, allow_provider)

            # auto_mode="tagged" 时必须有 tts 标记才触发；always/inbound 时与标记无关
            if tts_ctx.auto_mode == "tagged" and not tts_segments:
                return extra_msgs

            tts_text = cleaned_text.strip()
            if tts_segments:
                tts_text = "".join(
                    seg.get("text", "") + seg.get("tts_only_text", "") for seg in tts_segments
                ).strip()

            if tts_text:
                ws_path = self.get_workspace_path(conversation_id)
                effective_provider = tts_ctx.provider_name
                if tts_segments and allow_provider:
                    for seg in tts_segments:
                        if seg.get("config_overrides", {}).get("provider"):
                            effective_provider = seg["config_overrides"]["provider"]
                            break

                async def _on_final_tts_done(msg_id, audio_url, _m=final_response,
                                              _c=conversation_id, _p=effective_provider,
                                              _sm=tts_ctx.stream_mode):
                    _m.metadata = _m.metadata or {}
                    _m.metadata["tts"] = {"status": "complete", "audio_url": audio_url, "provider": _p, "stream_mode": _sm}
                    await persistence.update_message_metadata(_m.id, _m.metadata)

                if tts_ctx.stream_mode == "two-way" and tts_ctx.manager.get_session(stream_result.message_id):
                    # 立即后台完成该 session，使 tts_stream_end 尽早发出
                    tts_ctx.manager.complete_background(stream_result.message_id, _on_final_tts_done)
                else:
                    # mode="final": 使用 one-way 方式生成语音（已有完整文本，无需 two-way 流式）
                    tts_ctx.manager.synthesize_background(
                        message_id=stream_result.message_id,
                        text=tts_text,
                        tts_config=tts_ctx.config,
                        provider_name=effective_provider,
                        stream_mode="one-way",
                        workspace_path=ws_path,
                        on_complete=_on_final_tts_done,
                    )

            # 清理 [[tts:...]] 标记后的文本存入消息
            if tts_segments and final_response:
                final_response.content = cleaned_text
                await persistence.save_message(conversation_id, final_response)
                extra_msgs.append(final_response)

        except Exception as e:
            logger.warning("TTS post-processing failed", error=str(e), exc_info=True)
        return extra_msgs
    
    async def _inject_background_notifications(
        self, conversation_id: str, llm_messages: list
    ) -> None:
        """LLM 调用前注入已完成的后台任务通知到 llm_messages 和会话历史"""
        try:
            from app.core.background_jobs import background_job_manager
            notifications = background_job_manager.consume_notifications(conversation_id)
            if not notifications:
                return
            
            notification_text = background_job_manager.build_notification_message(notifications)
            
            # 追加到 LLM 消息
            llm_messages.append({"role": "user", "content": notification_text})
            
            # 保存到会话历史
            bg_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.USER,
                content=notification_text,
                message_type=MessageType.BACKGROUND_TASK,
            )
            conv = self.get_conversation(conversation_id)
            if conv:
                conv.messages.append(bg_msg)
                if not self.is_subagent:
                    await persistence.save_message(conversation_id, bg_msg)
            
            logger.info(
                "Background task notifications injected",
                conversation_id=conversation_id,
                count=len(notifications),
            )
        except Exception as e:
            logger.warning("Failed to inject background notifications", error=str(e))
    
    async def _inject_background_notifications_round_end(
        self, conversation_id: str
    ) -> None:
        """轮次结束后注入后台任务通知（用于下一轮用户输入前）"""
        try:
            from app.core.background_jobs import background_job_manager
            notifications = background_job_manager.consume_notifications(conversation_id)
            if not notifications:
                return
            
            notification_text = background_job_manager.build_notification_message(notifications)
            
            # 保存到会话历史
            bg_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.USER,
                content=notification_text,
                message_type=MessageType.BACKGROUND_TASK,
            )
            conv = self.get_conversation(conversation_id)
            if conv:
                conv.messages.append(bg_msg)
                if not self.is_subagent:
                    await persistence.save_message(conversation_id, bg_msg)
            
            logger.info(
                "Background task notifications injected at round end",
                conversation_id=conversation_id,
                count=len(notifications),
            )
        except Exception as e:
            logger.warning("Failed to inject background notifications at round end", error=str(e))

    async def _inject_teammate_messages(
        self, conversation_id: str, llm_messages: list
    ) -> None:
        """团队模式：每次 LLM 调用前，将 Lead 收件箱未读消息以 <teammate-message> 包裹注入"""
        if self.is_subagent:
            return
        try:
            from app.core.team_system.manager import team_manager
            team = team_manager.get_team_for_session(conversation_id)
            if team is None:
                return

            from app.core.team_system.models import (
                TeamMessageTypes,
                build_teammate_injection,
                parse_inbox_message_protocol,
            )
            from app.core.team_system.store import team_store

            entries = team_store.read_unread(team.session_id, team.team_name, team.lead_name)
            if not entries:
                return

            # 心跳/终止通知不注入对话，其余都需 Lead 关注（权限/计划审批/文本/任务分配）
            _skip = {TeamMessageTypes.IDLE_NOTIFICATION, TeamMessageTypes.TEAMMATE_TERMINATED}
            injectable = []
            for e in entries:
                proto = parse_inbox_message_protocol(e)
                if proto and proto.get("type") in _skip:
                    continue
                injectable.append(e)
            if not injectable:
                return

            text = build_teammate_injection(injectable)
            llm_messages.append({"role": "user", "content": text})

            tm_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.USER,
                content=text,
                message_type=MessageType.TEAMMATE,
            )
            conv = self.get_conversation(conversation_id)
            if conv:
                conv.messages.append(tm_msg)
                await persistence.save_message(conversation_id, tm_msg)

            logger.info(
                "Teammate messages injected",
                conversation_id=conversation_id,
                count=len(injectable),
            )
        except Exception as e:
            logger.warning("Failed to inject teammate messages", error=str(e))

    async def _team_lead_idle_check(self, conversation_id: str) -> Optional[str]:
        """Lead 轮次结束后的空闲等待循环（仅团队模式）

        最多等待 TEAMS_LEAD_IDLE_TIMEOUT_SECONDS 秒，每 TEAMS_LEAD_IDLE_POLL_SECONDS 秒检查：
            1. 收件箱有新消息 -> 返回注入文本（恢复工作）
            2. 任务板有未认领任务 -> 返回协调提示（恢复工作）
            3. 全部任务完成 -> 触发关机，返回 None
            4. 超时 -> 触发关机，返回 None
        非团队模式返回 None（行为不变）。
        """
        if self.is_subagent:
            return None
        try:
            from app.core.team_system.manager import team_manager
            team = team_manager.get_team_for_session(conversation_id)
        except Exception:
            return None
        if team is None:
            return None
        # 没有任何队友时不进入空闲循环（团队尚未真正组建）
        if not team.members:
            return None

        import asyncio as _asyncio
        from app.core.team_system.models import (
            TeamMessageTypes,
            build_teammate_injection,
            parse_inbox_message_protocol,
        )
        from app.core.team_system.store import team_store

        timeout = float(settings.get("TEAMS_LEAD_IDLE_TIMEOUT_SECONDS", 60))
        poll = float(settings.get("TEAMS_LEAD_IDLE_POLL_SECONDS", 5))
        _skip = {TeamMessageTypes.IDLE_NOTIFICATION, TeamMessageTypes.TEAMMATE_TERMINATED}

        loop = _asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await _asyncio.sleep(poll)
            if self.is_cancelled(conversation_id):
                return None

            # 1. 收件箱新消息 -> 恢复工作（排除心跳/终止通知）
            entries = team_store.read_unread(team.session_id, team.team_name, team.lead_name)
            injectable = []
            for e in entries:
                proto = parse_inbox_message_protocol(e)
                if proto and proto.get("type") in _skip:
                    continue
                injectable.append(e)
            if injectable:
                return build_teammate_injection(injectable)

            # 2. 任务板检查：未认领任务 -> 恢复协调；全部完成且无队友在干活 -> 关机
            try:
                from app.core.task_system.store import task_store
                tasks = await task_store.list_tasks(task_list_id=team.team_id)
                open_tasks = [t for t in tasks if t.get("status") in ("pending", "in_progress")]
                unclaimed = [t for t in open_tasks if not t.get("owner") and t.get("status") == "pending"]
                if unclaimed:
                    subjects = "; ".join(t.get("subject", t.get("id", "")) for t in unclaimed[:5])
                    return (
                        f"There are {len(unclaimed)} unclaimed task(s) on the team task board: "
                        f"{subjects}. Assign them to teammates via task_update(owner=...) or "
                        f"send_team_message, or claim/complete them yourself."
                    )
                if tasks and not open_tasks:
                    # 3. 任务板全部完成——但通过 send_team_message 指派的任务可能不在任务板上，
                    # 若还有队友处于 working 状态，继续等待，不能直接关机（由超时兜底）
                    busy = [m.name for m in team.members.values() if m.state == "working"]
                    if busy:
                        logger.debug(
                            "Tasks done but teammates still working, keep waiting",
                            team_id=team.team_id, busy=busy,
                        )
                    else:
                        logger.info("All team tasks done, shutting down team", team_id=team.team_id)
                        await team_manager.shutdown_team(team)
                        return None
            except Exception as e:
                logger.debug("Lead idle task check failed", error=str(e))

        # 4. 超时 -> 关机团队，不再执行
        logger.info("Lead idle timeout, shutting down team", team_id=team.team_id)
        try:
            await team_manager.shutdown_team(team)
        except Exception as e:
            logger.warning("Team shutdown on timeout failed", error=str(e))
        return None

    def _handle_stop_hook(self, conversation_id: str) -> None:
        """会话轮次结束钩子：即发即弃触发记忆后台任务，不影响主流程"""
        try:
            from app.core.memory_system.extractor import extract_memories_fire_and_forget
            from app.core.memory_system.consolidator import consolidate_fire_and_forget
            from app.core.memory_system.session_memory import session_memory_manager

            # 会话记忆：自然停顿点检查并增量更新笔记
            # todo
            # session_memory_manager.on_turn_end(self, conversation_id)
            # 记忆提取：forked agent 异步提取新记忆
            extract_memories_fire_and_forget(self, conversation_id)
            # 记忆整理：四层门控通过后 forked agent 合并/剪枝
            consolidate_fire_and_forget(self, conversation_id)
        except Exception as e:
            logger.warning("Stop hook memory tasks failed to start", error=str(e), exc_info=True)

    async def execute_task(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """执行任务"""
        # 设置 agent 上下文（用于日志追踪）
        parent_name = self.config.parent_agent_name if self.is_subagent else None
        set_agent_context(self.config.name, self.id, parent_name)
        try:
            self.current_task = task
            self.status = AgentStatus.RUNNING
            
            # 创建任务会话
            conversation = self.create_conversation(title=f"Task: {task[:50]}")
            
            # 存储任务上下文到工作记忆
            if context:
                await self.memory_manager.set_working_memory(
                    self.id,
                    "task_context",
                    context,
                )
                
                # ✅ 将依赖结果注入到LLM上下文中（作为用户消息）
                dep_results = context.get("dependency_results")
                if dep_results:
                    dep_info_parts = []
                    for dep in dep_results:
                        status = "成功" if dep.get("success") else "失败"
                        dep_info_parts.append(f"- {dep.get('subagent_name', 'Unknown')}: {status}")
                        if dep.get("result"):
                            dep_info_parts.append(f"  结果: {dep['result'][:10000]}...")
                    
                    dep_context_msg = "以下是你依赖的其他子agent的执行结果，请参考这些信息完成你的任务：\n\n" + "\n".join(dep_info_parts)
                    
                    # 添加为消息到会话中
                    dep_msg = Message(
                        id=str(uuid.uuid4()),
                        role=MessageRole.USER,
                        content=dep_context_msg,
                        message_type=MessageType.TEXT,
                    )
                    conversation.messages.append(dep_msg)
                    
                    # 保存到数据库（子agent跳过持久化）
                    if not self.is_subagent:
                        await persistence.save_message(conversation.id, dep_msg)
                    
                    logger.info("Injected dependency results into LLM context", 
                               dep_count=len(dep_results),
                               conversation_id=conversation.id)
            
            # 执行任务 - 只保留最后一条assistant消息作为汇总
            final_result = None
            async for msg in self.process_message(task, conversation.id):
                if msg.role == MessageRole.ASSISTANT and msg.message_type == MessageType.TEXT:
                    final_result = msg.content
            
            self.current_task = None
            self.status = AgentStatus.IDLE
            
            return final_result if final_result else "Task completed."
        finally:
            # 清除 agent 上下文
            clear_agent_context()
    
    async def think(
        self,
        question: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """思考模式 - 不使用工具"""
        prompt = f"""请思考以下问题并给出详细回答：

问题: {question}
{f'上下文: {context}' if context else ''}
"""
        
        response = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=self.config.temperature,
            model=self.config.llm_model or None,
        )
        
        return response
    
    async def plan_and_execute(
        self,
        goal: str,
    ) -> str:
        """规划并执行复杂任务"""
        from app.core.planner import Planner, PlanningStrategy
        
        planner = Planner(self.llm_service)
        
        # 创建计划
        plan = await planner.create_plan(
            goal=goal,
            strategy=PlanningStrategy.PLAN_AND_SOLVE,
        )
        
        # 存储计划到工作记忆
        await self.memory_manager.set_working_memory(
            self.id,
            "current_plan",
            {"goal": goal, "subtasks": [st.dict() for st in plan.subtasks]},
        )
        
        # 逐步执行子任务
        results = []
        for subtask in plan.subtasks:
            logger.info("Executing subtask", subtask_id=subtask.id, title=subtask.title)
            
            result = await self.execute_task(
                f"子任务: {subtask.title}\n描述: {subtask.description}",
                context={"plan_goal": goal},
            )
            results.append(f"[{subtask.title}]: {result}")
        
        return "\n\n".join(results)


def create_single_agent(
    config: AgentConfig,
    llm_service: LLMService,
    tool_registry: ToolRegistry,
    memory_manager: MemoryManager,
    prompt_engine: PromptEngine,
    reflector: Optional[Reflector] = None,
    compressor: Optional[ContextCompressor] = None,
) -> SingleAgent:
    """创建单Agent实例"""
    return SingleAgent(
        config=config,
        llm_service=llm_service,
        tool_registry=tool_registry,
        memory_manager=memory_manager,
        prompt_engine=prompt_engine,
        reflector=reflector,
        compressor=compressor,
    )
