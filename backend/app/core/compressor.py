"""上下文压缩器 - 智能摘要与裁剪（五级压缩策略）"""
import uuid
import json
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple, Set
from pathlib import Path

import structlog
from app.models.schemas.chat import Message, CompressedContext, SummaryResult, MessageRole, MessageType
from app.config import settings
from app.services.llm_service import LLMService

logger = structlog.get_logger()

# ===== 摘要 Prompt 模板 =====
SUMMARIZATION_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

You are a context compression assistant. Analyze the conversation history and produce a focused summary.

<analysis>
[Analyze the conversation: what tasks were completed, key decisions, current state, important file paths, user preferences, test results/errors]
</analysis>

<summary>
[Produce a concise summary focusing on:
- Completed tasks and their results
- Important decisions and reasoning
- Current in-progress task status
- Key code changes and file paths
- User preferences and constraints
- Test results and error information
- OMIT: detailed tool call outputs, intermediate steps, minor details]
</summary>

REMINDER: Respond with TEXT ONLY. Do NOT call any tools."""


class ContextCompressor:
    """上下文压缩器 - 实现五级压缩策略"""

    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service
        # 从配置加载参数
        self.budget_max_chars = getattr(settings, 'COMPRESSION_BUDGET_MAX_CHARS', 200000)
        self.budget_preview_chars = getattr(settings, 'COMPRESSION_BUDGET_PREVIEW_CHARS', 2000)
        self.micro_compact_ttl_seconds = getattr(settings, 'COMPRESSION_MICRO_COMPACT_TTL_SECONDS', 3600)
        self.micro_compact_keep_recent = getattr(settings, 'COMPRESSION_MICRO_COMPACT_KEEP_RECENT', 5)
        self.micro_compact_multimodal_token_threshold = getattr(settings, 'COMPRESSION_MICRO_COMPACT_MULTIMODAL_TOKEN_THRESHOLD', 2000)
        self.micro_compact_whitelist = getattr(settings, 'COMPRESSION_MICRO_COMPACT_WHITELIST', ["web_search", "read_file", "bash_command"])
        self.auto_compact_warning_threshold = getattr(settings, 'COMPRESSION_AUTO_COMPACT_WARNING_THRESHOLD', 0.85)
        self.auto_compact_force_threshold = getattr(settings, 'COMPRESSION_AUTO_COMPACT_FORCE_THRESHOLD', 0.95)
        self.auto_compact_keep_recent = getattr(settings, 'COMPRESSION_AUTO_COMPACT_KEEP_RECENT', 5)
        self.reactive_compact_keep_recent = getattr(settings, 'COMPRESSION_REACTIVE_COMPACT_KEEP_RECENT', 5)
        self.reactive_compact_max_retries = getattr(settings, 'COMPRESSION_REACTIVE_COMPACT_MAX_RETRIES', 3)
        self.post_restore_max_files = getattr(settings, 'COMPRESSION_POST_RESTORE_MAX_FILES', 5)
        self.post_restore_max_file_tokens = getattr(settings, 'COMPRESSION_POST_RESTORE_MAX_FILE_TOKENS', 5000)
        self.post_restore_total_budget_tokens = getattr(settings, 'COMPRESSION_POST_RESTORE_TOTAL_BUDGET_TOKENS', 50000)

    # ========== 辅助方法 ==========

    def _estimate_tokens(self, text: str) -> int:
        """估算 token 数"""
        try:
            import tiktoken
            encoding = tiktoken.encoding_for_model("gpt-4")
            return len(encoding.encode(text))
        except Exception:
            # 粗略估算：1 token ≈ 3-4 字符
            return len(text) // 3

    def _find_tool_call_pairs(self, messages: List[Message]) -> Set[int]:
        """识别 assistant(tool_calls) + tool(tool_call_id) 的配对关系，返回所有配对消息的索引集合"""
        paired_indices: Set[int] = set()
        for i, msg in enumerate(messages):
            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                # 提取该 assistant 消息的所有 tool_call_id
                try:
                    if isinstance(msg.tool_calls[0], dict):
                        call_ids = {tc.get('id') for tc in msg.tool_calls if isinstance(tc, dict)}
                    else:
                        call_ids = {tc.id for tc in msg.tool_calls}
                except (IndexError, AttributeError):
                    continue

                # 在后续消息中查找对应的 tool_result
                matched_ids = set()
                for j in range(i + 1, len(messages)):
                    next_msg = messages[j]
                    if next_msg.role == MessageRole.TOOL and next_msg.tool_call_id:
                        if next_msg.tool_call_id in call_ids:
                            matched_ids.add(next_msg.tool_call_id)
                            paired_indices.add(j)

                # 如果所有 call_id 都找到了响应，标记为配对
                if call_ids and call_ids.issubset(matched_ids):
                    paired_indices.add(i)

        return paired_indices

    async def _persist_output(self, content: str, conversation_id: str) -> str:
        """将超长工具输出写入磁盘，返回占位符"""
        workspace_dir = Path(getattr(settings, 'WORKSPACE_BASE_PATH', './workspaces')).resolve()
        persist_dir = workspace_dir / conversation_id / "temp" / "compressed_outputs"
        persist_dir.mkdir(parents=True, exist_ok=True)

        filename = f"output_{uuid.uuid4().hex[:12]}.txt"
        filepath = persist_dir / filename

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            preview = content[:self.budget_preview_chars]
            filepath_str = str(filepath.resolve())
            placeholder = f"<persisted-output>\nFull output: {filepath_str}\nPreview: {preview}"
            logger.info("Persisted long output to disk", path=filepath_str, original_length=len(content))
            return placeholder, filepath_str
        except Exception as e:
            logger.error("Failed to persist output", error=str(e), exc_info=True)
            # 降级：截断内容
            return content[:self.budget_preview_chars] + "\n... [truncated due to persistence failure]", ""

    def _strip_analysis(self, summary_text: str) -> str:
        """剥离 <analysis>...</analysis> 标签内容，只保留 <summary> 部分"""
        import re
        # 提取 <summary> 标签内容
        match = re.search(r'<summary>(.*?)</summary>', summary_text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # 如果没有找到标签，返回原文
        return summary_text.strip()

    # ========== 压缩标记方法 ==========

    @staticmethod
    def _is_compression_marker(msg: Message) -> bool:
        """判断消息是否为压缩标记消息"""
        return msg.message_type in (
            MessageType.COMPRESS_BUDGET_BOUNDARY,
            MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY,
            MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY,
            MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY,
            MessageType.ACTIVE_COMPACT_BOUNDARY,
        )

    @staticmethod
    def _is_not_llm_msg(msg: Message) -> bool:
        """判断消息是否为非 LLM 消息"""
        return msg.message_type in (
            MessageType.COMPRESS_BUDGET_BOUNDARY,
            MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY,
            MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY,
            MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY,
            MessageType.ACTIVE_COMPACT_BOUNDARY,
            MessageType.SLASH_COMMAND_REQUEST,
            MessageType.SLASH_COMMAND_RESPONSE,
            MessageType.IMAGE_GENERATED,
            MessageType.MUSIC_GENERATED,
            MessageType.VIDEO_GENERATED,
            MessageType.AUDIO_GENERATED,
        )

    def _insert_marker(
        self,
        messages: List[Message],
        marker_type: MessageType,
        insert_after_idx: int,
        metadata: Dict[str, Any],
        content: str = "Context compacted",
    ) -> List[Message]:
        """在指定位置后插入压缩标记消息。
        标记消息的 created_at 设为前一条消息的 created_at + 1秒，
        确保从数据库按 created_at 排序时位置正确。
        实际创建时间保存在 metadata 的 original_created_at 字段以便回溯。"""
        metadata.setdefault("trigger", "auto")
        
        # 计算标记消息的 created_at：前一条消息的 created_at + 1秒
        actual_now = datetime.utcnow()
        if 0 <= insert_after_idx < len(messages):
            prev_created_at = messages[insert_after_idx].created_at
            marker_created_at = prev_created_at + timedelta(seconds=1)
        else:
            marker_created_at = actual_now
        
        # 保存实际创建时间以便回溯
        metadata["original_created_at"] = actual_now.isoformat()
        
        marker = Message(
            id=str(uuid.uuid4()),
            role=MessageRole.SYSTEM,
            content=content,
            message_type=marker_type,
            metadata=metadata,
            created_at=marker_created_at,
        )
        new_messages = messages[:insert_after_idx + 1] + [marker] + messages[insert_after_idx + 1:]
        return new_messages

    async def _mark_budget_boundary(
        self, messages: List[Message], conversation_id: str
    ) -> List[Message]:
        """Budget 压缩标记：超长工具输出落磁盘，插入标记。
        倒序遍历，遇到 BUDGET 标记则停止（更早的已被标记过）。
        仅在新轮次（最后一条是 user 消息）时才执行压缩，避免当前轮次压缩导致 LLM 重新读取文件。"""
        # 检查最后一条消息是否为 user，只有新轮次才执行压缩
        if not messages or messages[-1].role != MessageRole.USER:
            return messages
        
        qualifying = []
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            # 遇到同类型标记，停止遍历
            if msg.message_type == MessageType.COMPRESS_BUDGET_BOUNDARY:
                break
            if msg.role == MessageRole.TOOL and len(msg.content) > self.budget_max_chars:
                qualifying.append((i, msg))

        if not qualifying:
            return messages

        # 超长输出落磁盘，收集路径
        tool_result_path = {}
        total_tokens_saved = 0
        for idx, msg in qualifying:
            _, path = await self._persist_output(msg.content, conversation_id)
            tool_id = msg.tool_call_id or msg.id
            tool_result_path[tool_id] = path
            original_tokens = self._estimate_tokens(msg.content)
            preview_tokens = self._estimate_tokens(msg.content[:self.budget_preview_chars])
            total_tokens_saved += original_tokens - preview_tokens

        last_idx = max(idx for idx, _ in qualifying)
        compacted_ids = [msg.tool_call_id or msg.id for _, msg in qualifying]
        pre_tokens = sum(self._estimate_tokens(msg.content) for _, msg in qualifying)

        metadata = {
            "pre_tokens": pre_tokens,
            "tokens_saved": total_tokens_saved,
            "compacted_tool_ids": compacted_ids,
            "tool_result_path": tool_result_path,
            "compacted_last_idx": last_idx,
        }

        logger.info("Budget boundary marked", tools_count=len(qualifying), tokens_saved=total_tokens_saved)
        return self._insert_marker(messages, MessageType.COMPRESS_BUDGET_BOUNDARY, last_idx, metadata, "Context budget compacted")

    def _mark_snip_boundary(self, messages: List[Message]) -> List[Message]:
        """Snip 压缩标记：空结果/被拒绝的工具调用。
        倒序遍历，遇到 SNIP 标记则停止。"""
        if not messages:
            return messages

        keep_recent = self.auto_compact_keep_recent
        total = len(messages)
        qualifying = []

        for i in range(total - 1, -1, -1):
            msg = messages[i]
            # 遇到同类型标记，停止遍历
            if msg.message_type == MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY:
                break
            if i >= total - keep_recent:
                continue
            if msg.role != MessageRole.TOOL:
                continue

            can_snip = False
            # 1. 空结果
            if not msg.content or msg.content.strip() in ("", "null", "None"):
                can_snip = True
            # 2. 被拒绝
            if not can_snip:
                if msg.tool_call_status in ("rejected", "denied"):
                    can_snip = True

            if can_snip:
                qualifying.append((i, msg))

        if not qualifying:
            return messages

        last_idx = max(idx for idx, _ in qualifying)
        compacted_ids = [msg.tool_call_id or msg.id for _, msg in qualifying]
        tokens_saved = sum(self._estimate_tokens(msg.content) for _, msg in qualifying)
        pre_tokens = tokens_saved

        metadata = {
            "pre_tokens": pre_tokens,
            "tokens_saved": tokens_saved,
            "compacted_tool_ids": compacted_ids,
            "compacted_last_idx": last_idx,
        }

        logger.info("Snip compact boundary marked", tools_count=len(qualifying))
        return self._insert_marker(messages, MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY, last_idx, metadata, "Context snip compacted")

    def _mark_micro_boundary(self, messages: List[Message]) -> List[Message]:
        """Micro 压缩标记：基于时间/多模态/白名单。
        倒序遍历，遇到 MICRO 标记则停止。"""
        if not messages:
            return messages

        now = datetime.utcnow()
        ttl_delta = timedelta(seconds=self.micro_compact_ttl_seconds)
        keep_recent = self.micro_compact_keep_recent
        whitelist = set(self.micro_compact_whitelist)

        # 计算最近 N 条 tool 消息的索引
        tool_indices = [i for i, msg in enumerate(messages) if msg.role == MessageRole.TOOL]
        recent_tool_indices = set(tool_indices[-keep_recent:]) if tool_indices else set()

        qualifying = []
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            # 遇到同类型标记，停止遍历
            if msg.message_type == MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY:
                break
            if msg.role != MessageRole.TOOL:
                continue
            if i in recent_tool_indices:
                continue

            should_compress = False

            # 1. 基于时间：超过 TTL
            if msg.created_at and (now - msg.created_at) > ttl_delta:
                should_compress = True

            # 2. 多模态内容：估算 token 超过阈值
            if not should_compress and msg.message_type in (MessageType.MULTI_MODAL,):
                estimated_tokens = self._estimate_tokens(msg.content)
                if estimated_tokens > self.micro_compact_multimodal_token_threshold:
                    should_compress = True

            # 3. 白名单门控：只有白名单中的工具才被压缩
            if should_compress and msg.name and msg.name not in whitelist:
                should_compress = False

            if should_compress:
                qualifying.append((i, msg))

        if not qualifying:
            return messages

        last_idx = max(idx for idx, _ in qualifying)
        compacted_ids = [msg.tool_call_id or msg.id for _, msg in qualifying]
        tokens_saved = sum(
            self._estimate_tokens(msg.content) - self._estimate_tokens("[Tool output compressed]")
            for _, msg in qualifying
        )
        pre_tokens = sum(self._estimate_tokens(msg.content) for _, msg in qualifying)

        metadata = {
            "pre_tokens": pre_tokens,
            "tokens_saved": max(0, tokens_saved),
            "compacted_tool_ids": compacted_ids,
            "compacted_last_idx": last_idx,
        }

        logger.info("Micro compact boundary marked", tools_count=len(qualifying))
        return self._insert_marker(messages, MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY, last_idx, metadata, "Context micro compacted")

    async def _mark_auto_boundary(
        self, messages: List[Message], context_window: int, max_output_tokens: int,
        conversation_id: Optional[str] = None,
    ) -> List[Message]:
        """Auto 压缩标记：LLM 摘要，摘要内容填入 marker content。
        倒序遍历，遇到 AUTO 标记则停止。
        若 Session Memory 摘要已足够，直接复用，跳过 LLM 调用。"""
        if not messages:
            return messages

        # 计算当前 token 使用量
        # 优先从最近一条有 usage 信息的 assistant 消息获取 prompt_tokens
        # 因为 LLM 返回的 prompt_tokens 反映实际 token 情况，比 tiktoken 估算更准确
        recent_assistant_with_usage = None
        recent_assistant_idx = None
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.role == MessageRole.ASSISTANT and msg.metadata:
                usage = msg.metadata.get("usage")
                if usage and usage.get("prompt_tokens"):
                    recent_assistant_with_usage = msg
                    recent_assistant_idx = i
                    break
        
        if recent_assistant_with_usage is not None:
            # 有 usage 信息：优先使用 last_prompt_tokens（多轮工具调用时反映实际上下文大小）
            usage_info = recent_assistant_with_usage.metadata["usage"]
            current_tokens = usage_info.get("last_prompt_tokens") or usage_info["prompt_tokens"]
            messages_after = messages[recent_assistant_idx + 1:]
            current_tokens += sum(
                self._estimate_tokens(msg.content) 
                for msg in messages_after 
                if not self._is_not_llm_msg(msg)
            )
        else:
            # 无 usage 信息：回退到原有计算方式
            # 找到最近的 auto/active 标记，只计算该标记之后的消息 token
            # 因为标记之前的消息已经被压缩到标记的 content 中了
            recent_marker_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].message_type in (MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY, MessageType.ACTIVE_COMPACT_BOUNDARY):
                    recent_marker_idx = i
                    break
            
            if recent_marker_idx is not None:
                # 有压缩标记：只计算标记之后的消息 + 标记本身的 content（已压缩的历史摘要）
                messages_to_count = messages[recent_marker_idx + 1:]
                current_tokens = self._estimate_tokens(messages[recent_marker_idx].content)
                current_tokens += sum(self._estimate_tokens(msg.content) for msg in messages_to_count if not self._is_not_llm_msg(msg))
            else:
                # 无压缩标记：计算所有llm消息
                non_marker = [msg for msg in messages if not self._is_not_llm_msg(msg)]
                current_tokens = sum(self._estimate_tokens(msg.content) for msg in non_marker)
        
        threshold = context_window - max_output_tokens - 13000

        usage_ratio = current_tokens / threshold if threshold > 0 else 0

        if usage_ratio < self.auto_compact_warning_threshold:
            return messages

        if usage_ratio >= self.auto_compact_force_threshold:
            logger.warning("Auto compact force threshold reached", usage_ratio=usage_ratio)

        # 确定可压缩消息范围（倒序遍历）
        keep_recent = self.auto_compact_keep_recent
        total = len(messages)

        # --- 循环外：计算遍历起始位置 ---
        # 从 total - keep_recent 位置开始，检查边界处的 tool 消息的配对 assistant
        start_pos = total - keep_recent - 1  # 初始起始位置（最近 N 条之前）
        if 0 <= start_pos < total and messages[start_pos].role == MessageRole.TOOL and messages[start_pos].tool_call_id:
            # 往前查找配对的 assistant
            tool_call_id = messages[start_pos].tool_call_id
            for j in range(start_pos - 1, -1, -1):
                m = messages[j]
                if m.role == MessageRole.ASSISTANT and m.tool_calls:
                    try:
                        if isinstance(m.tool_calls[0], dict):
                            call_ids = {tc.get('id') for tc in m.tool_calls if isinstance(tc, dict)}
                        else:
                            call_ids = {tc.id for tc in m.tool_calls}
                    except (IndexError, AttributeError):
                        continue
                    if tool_call_id in call_ids:
                        start_pos = j - 1  # 以 assistant 位置后一位作为起始点
                        break
        if 0 <= start_pos < total and messages[start_pos].role == MessageRole.ASSISTANT and messages[start_pos].tool_calls:
            start_pos -= 1

        # --- 循环内：从计算好的起始位置开始遍历 ---
        compressible = []

        for i in range(start_pos, -1, -1):
            msg = messages[i]
            # 遇到同类型标记，停止遍历，并将标记内容作为 user 消息加入压缩列表
            if msg.message_type in (MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY, MessageType.ACTIVE_COMPACT_BOUNDARY):
                # 基于标记消息的 content 创建 user 类型消息
                prev_summary_msg = Message(
                    id=str(uuid.uuid4()),
                    role=MessageRole.USER,
                    content=msg.content,
                    message_type=MessageType.TEXT,
                )
                compressible.append(prev_summary_msg)
                break
            if self._is_not_llm_msg(msg):
                continue
            # 保留：首条 system、含 reasoning_content
            if i == 0 and msg.role == MessageRole.SYSTEM:
                continue
            if msg.reasoning_content:
                continue
            compressible.append(msg)

        if not compressible:
            return messages

        # 构建待压缩文本（保持原始顺序）
        compressible_ordered = compressible[::-1]
        texts = []
        for msg in compressible_ordered:
            role_label = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool"}.get(msg.role.value, msg.role.value)
            texts.append(f"{role_label}: {msg.content}")
        conversation_text = "\n\n".join(texts)

        # ✅ Session Memory 复用：在实际调用 LLM 前先检查会话记忆摘要是否足够
        summary = None
        used_session_memory = False
        if conversation_id:
            try:
                from app.core.memory_system.session_memory import session_memory_manager
                summary = session_memory_manager.get_usable_summary(conversation_id)
                if summary:
                    logger.info("Auto compact reusing session memory summary", conversation_id=conversation_id)
                    used_session_memory = True
            except Exception as e:
                logger.warning("Session memory summary lookup failed", error=str(e))
                summary = None

        # 调用 LLM 生成摘要（仅在无可用会话记忆摘要时）
        try:
            if not summary:
                summary_response = await self.llm_service.chat(
                    messages=[
                        {"role": "system", "content": SUMMARIZATION_PROMPT},
                        {"role": "user", "content": f"Conversation history to compress:\n\n{conversation_text}"}
                    ],
                    temperature=0.3,
                    max_tokens=2000,
                )
                summary = self._strip_analysis(summary_response)

            pre_tokens = sum(self._estimate_tokens(msg.content) for msg in compressible)
            summary_tokens = self._estimate_tokens(summary)
            tokens_saved = pre_tokens - summary_tokens

            # 插入位置：可压缩消息中索引最大的
            last_compressible_idx = max(
                i for i, msg in enumerate(messages)
                if msg in compressible
            )

            metadata = {
                "pre_tokens": pre_tokens,
                "tokens_saved": tokens_saved,
                "compacted_last_idx": last_compressible_idx,
                "used_session_memory": used_session_memory,
            }

            if used_session_memory:
                content = "Session memory summary:\n" + summary
            else:
                content = f"Context auto compacted\n\n[Conversation summary]:\n{summary}"

            logger.info("Auto compact boundary marked", tokens_saved=tokens_saved)
            return self._insert_marker(
                messages, MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY, last_compressible_idx, metadata, content
            )
        except Exception as e:
            logger.error("Auto compact marking failed", error=str(e), exc_info=True)
            return messages

    async def mark_compression_boundaries(
        self,
        messages: List[Message],
        conversation_id: str,
        context_window: int = 128000,
        max_output_tokens: int = 4096,
    ) -> List[Message]:
        """
        在 conversation.messages 上插入压缩标记。
        不修改原始消息内容，仅在符合条件的最后一条消息后插入标记消息。
        每个策略倒序遍历，遇到同类型标记则停止（更早的已被标记过）。
        顺序：budget -> snip_compact -> micro_compact -> auto_compact
        """
        if not messages:
            return messages

        result = messages

        # 1. Budget 标记
        result = await self._mark_budget_boundary(result, conversation_id)

        # 2. Snip compact 标记
        result = self._mark_snip_boundary(result)

        # 3. Micro compact 标记
        result = self._mark_micro_boundary(result)

        # 4. Auto compact 标记（需要 LLM 调用，优先复用 Session Memory 摘要）
        result = await self._mark_auto_boundary(result, context_window, max_output_tokens, conversation_id)

        return result

    # ========== 2.2 Budget 压缩 ==========

    async def budget(self, messages: List[Message], conversation_id: str) -> List[Message]:
        """预算压缩：超长工具输出落磁盘"""
        modified = False
        new_messages = []

        for msg in messages:
            if msg.role == MessageRole.TOOL and len(msg.content) > self.budget_max_chars:
                # 超长输出，落盘
                placeholder = await self._persist_output(msg.content, conversation_id)
                new_msg = Message(
                    id=msg.id,
                    role=msg.role,
                    content=placeholder,
                    message_type=msg.message_type,
                    metadata=msg.metadata.copy(),
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                    created_at=msg.created_at,
                    token_count=self._estimate_tokens(placeholder),
                )
                new_messages.append(new_msg)
                modified = True
            else:
                new_messages.append(msg)

        if modified:
            logger.info("Budget compression applied", messages_count=len(new_messages))
        return new_messages

    # ========== 2.3 Snip Compact 裁剪 ==========

    def snip_compact(self, messages: List[Message]) -> List[Message]:
        """裁剪中间消息：空结果、被拒绝的工具调用"""
        if not messages:
            return messages

        keep_recent = self.auto_compact_keep_recent
        total = len(messages)

        # 找出可安全裁剪的消息索引
        removable_indices: Set[int] = set()
        paired_indices = self._find_tool_call_pairs(messages)

        for i, msg in enumerate(messages):
            # 跳过最近 N 条消息
            if i >= total - keep_recent:
                continue

            if msg.role != MessageRole.TOOL:
                continue

            # 检查是否可裁剪
            can_remove = False

            # 1. 空结果
            if not msg.content or msg.content.strip() in ("", "null", "None"):
                can_remove = True

            # 2. 被拒绝
            if not can_remove:
                content_lower = msg.content.lower()
                if "rejected" in content_lower or "denied" in content_lower:
                    can_remove = True
                if msg.metadata and msg.metadata.get("rejected"):
                    can_remove = True

            # 保持配对完整性：只有当配对的 assistant 也在可移除列表中，或没有配对时才移除
            if can_remove and i not in paired_indices:
                removable_indices.add(i)

        # 构建新消息列表
        new_messages = [msg for i, msg in enumerate(messages) if i not in removable_indices]
        removed_count = len(messages) - len(new_messages)

        if removed_count > 0:
            logger.info("Snip compact applied", removed_count=removed_count)
        return new_messages

    # ========== 2.4 Micro Compact 微压缩 ==========

    def micro_compact(self, messages: List[Message]) -> List[Message]:
        """微压缩：按规则替换工具执行结果为占位符"""
        if not messages:
            return messages

        now = datetime.utcnow()
        ttl_delta = timedelta(seconds=self.micro_compact_ttl_seconds)
        keep_recent = self.micro_compact_keep_recent
        total = len(messages)
        whitelist = set(self.micro_compact_whitelist)

        # 找出最近的 tool 消息索引
        tool_indices = [i for i, msg in enumerate(messages) if msg.role == MessageRole.TOOL]
        recent_tool_indices = set(tool_indices[-keep_recent:]) if tool_indices else set()

        modified = False
        new_messages = []

        for i, msg in enumerate(messages):
            if msg.role != MessageRole.TOOL:
                new_messages.append(msg)
                continue

            # 跳过最近 N 条
            if not recent_tool_indices or i in recent_tool_indices:
                new_messages.append(msg)
                continue

            should_compress = False

            # 1. 基于时间：超过 TTL
            if msg.created_at and (now - msg.created_at) > ttl_delta:
                should_compress = True

            # 2. 多模态内容：估算 token 超过阈值
            if not should_compress and msg.message_type in (MessageType.MULTI_MODAL,):
                # 简单判断：如果 content 看起来像 base64 或很长
                estimated_tokens = self._estimate_tokens(msg.content)
                if estimated_tokens > self.micro_compact_multimodal_token_threshold:
                    should_compress = True

            # 3. 白名单门控：只有白名单中的工具才被压缩
            if should_compress and msg.name and msg.name not in whitelist:
                should_compress = False

            if should_compress:
                compressed_msg = Message(
                    id=msg.id,
                    role=msg.role,
                    content="[Tool output compressed]",
                    message_type=msg.message_type,
                    metadata=msg.metadata.copy(),
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                    created_at=msg.created_at,
                    token_count=self._estimate_tokens("[Tool output compressed]"),
                )
                new_messages.append(compressed_msg)
                modified = True
            else:
                new_messages.append(msg)

        if modified:
            logger.info("Micro compact applied", messages_count=len(new_messages))
        return new_messages

    # ========== 2.5 Auto Compact LLM 摘要压缩 ==========

    async def auto_compact(
        self,
        messages: List[Message],
        context_window: int,
        max_output_tokens: int,
    ) -> Tuple[List[Message], bool]:
        """
        LLM 摘要压缩
        Returns: (compressed_messages, was_compressed)
        """
        if not messages:
            return messages, False

        # 计算当前 token 使用量
        current_tokens = sum(self._estimate_tokens(msg.content) for msg in messages)
        threshold = context_window - max_output_tokens - 13000

        usage_ratio = current_tokens / threshold if threshold > 0 else 0

        # 未达警告阈值，不压缩
        if usage_ratio < self.auto_compact_warning_threshold:
            return messages, False

        if usage_ratio >= self.auto_compact_force_threshold:
            logger.warning("Context usage exceeds force threshold, triggering auto compact",
                          usage_ratio=usage_ratio, current_tokens=current_tokens)

        # 确定哪些消息需要压缩
        keep_recent = self.auto_compact_keep_recent
        total = len(messages)
        paired_indices = self._find_tool_call_pairs(messages)

        # 找出可压缩的消息范围
        compressible_messages = []
        preserved_messages = []

        for i, msg in enumerate(messages):
            # 保留：首条 system、最近 N 条、含 reasoning_content 的消息
            if i == 0 and msg.role == MessageRole.SYSTEM:
                preserved_messages.append((i, msg))
                continue
            if i >= total - keep_recent:
                preserved_messages.append((i, msg))
                continue
            if msg.reasoning_content:
                preserved_messages.append((i, msg))
                continue

            # 保持配对完整性
            if i in paired_indices:
                preserved_messages.append((i, msg))
                continue

            compressible_messages.append(msg)

        if not compressible_messages:
            return messages, False

        # 构建待压缩文本
        texts = []
        for msg in compressible_messages:
            role_label = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool"}.get(msg.role.value, msg.role.value)
            texts.append(f"{role_label}: {msg.content}")

        conversation_text = "\n\n".join(texts)

        # 调用 LLM 生成摘要
        try:
            summary_response = await self.llm_service.chat(
                messages=[
                    {"role": "system", "content": SUMMARIZATION_PROMPT},
                    {"role": "user", "content": f"Conversation history to compress:\n\n{conversation_text}"}
                ],
                temperature=0.3,
                max_tokens=2000,
            )

            summary = self._strip_analysis(summary_response)
            logger.info("Auto compact generated summary", summary_length=len(summary))

            # 创建摘要消息
            summary_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.SYSTEM,
                content=f"[Previous conversation summary]:\n{summary}",
                message_type=MessageType.TEXT,
                token_count=self._estimate_tokens(summary),
            )

            # 重组消息列表：摘要 + 保留的消息
            new_messages = [summary_msg] + [msg for _, msg in preserved_messages]
            return new_messages, True

        except Exception as e:
            logger.error("Auto compact failed, keeping original messages", error=str(e), exc_info=True)
            return messages, False

    # ========== 2.6 Reactive Compact 紧急恢复压缩 ==========

    async def reactive_compact(self, messages: List[Message]) -> List[Message]:
        """
        紧急恢复压缩（用于 413 错误）。
        参考 auto_compact 标记模式：在原始消息列表中插入标记消息，
        由 _build_messages_for_llm 处理标记完成实际压缩。
        """
        if not messages:
            return messages


        # 压缩非最近 N 条消息
        keep_recent = self.reactive_compact_keep_recent
        total = len(messages)

        compressible_messages = []

        # --- 循环外：计算遍历起始位置 ---
        # 从 total - keep_recent 位置开始，检查边界处的 tool 消息的配对 assistant
        start_pos = total - keep_recent - 1  # 初始起始位置（最近 N 条之前）
        if 0 <= start_pos < total and messages[start_pos].role == MessageRole.TOOL and messages[start_pos].tool_call_id:
            # 往前查找配对的 assistant
            tool_call_id = messages[start_pos].tool_call_id
            for j in range(start_pos - 1, -1, -1):
                m = messages[j]
                if m.role == MessageRole.ASSISTANT and m.tool_calls:
                    try:
                        if isinstance(m.tool_calls[0], dict):
                            call_ids = {tc.get('id') for tc in m.tool_calls if isinstance(tc, dict)}
                        else:
                            call_ids = {tc.id for tc in m.tool_calls}
                    except (IndexError, AttributeError):
                        continue
                    if tool_call_id in call_ids:
                        start_pos = j - 1  # 以 assistant 位置后一位作为起始点
                        break
        if 0 <= start_pos < total and messages[start_pos].role == MessageRole.ASSISTANT and messages[start_pos].tool_calls:
            start_pos -= 1


        for i in range(start_pos, -1, -1):
            msg = messages[i]
            # 遇到同类型标记，停止遍历，并将标记内容作为 user 消息加入压缩列表
            if msg.message_type == MessageType.ACTIVE_COMPACT_BOUNDARY:
                # 基于标记消息的 content 创建 user 类型消息
                prev_summary_msg = Message(
                    id=str(uuid.uuid4()),
                    role=MessageRole.USER,
                    content=msg.content,
                    message_type=MessageType.TEXT,
                )
                compressible_messages.append(prev_summary_msg)
                break
            if self._is_not_llm_msg(msg):
                continue
            # 保留：首条 system、含 reasoning_content
            if i == 0 and msg.role == MessageRole.SYSTEM:
                continue
            if msg.reasoning_content:
                continue

            compressible_messages.append(msg)

        if not compressible_messages:
            return messages

        # 构建待压缩文本
        compressible_ordered = compressible_messages[::-1]
        texts = []
        for msg in compressible_ordered:
            role_label = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool"}.get(msg.role.value, msg.role.value)
            texts.append(f"{role_label}: {msg.content}")

        conversation_text = "\n\n".join(texts)

        try:
            summary_response = await self.llm_service.chat(
                messages=[
                    {"role": "system", "content": SUMMARIZATION_PROMPT},
                    {"role": "user", "content": f"Conversation history to compress:\n\n{conversation_text}"}
                ],
                temperature=0.3,
                max_tokens=2000,
            )

            summary = self._strip_analysis(summary_response)
            logger.info("Reactive compact generated summary", summary_length=len(summary))

            # 插入标记消息（参考 auto_compact 标记模式）
            pre_tokens = sum(self._estimate_tokens(msg.content) for msg in compressible_messages)
            summary_tokens = self._estimate_tokens(summary)
            tokens_saved = pre_tokens - summary_tokens

            # 找到最后一个可压缩消息的索引
            last_compressible_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                if messages[i] in compressible_messages:
                    last_compressible_idx = i
                    break

            metadata = {
                "trigger": "reactive",
                "pre_tokens": pre_tokens,
                "tokens_saved": tokens_saved,
                "compacted_last_idx": last_compressible_idx,
            }
            content = f"Context reactive compacted\n\n[Conversation summary]:\n{summary}"

            return self._insert_marker(
                messages, MessageType.ACTIVE_COMPACT_BOUNDARY, last_compressible_idx, metadata, content
            )

        except Exception as e:
            logger.error("Reactive compact failed", error=str(e), exc_info=True)
            return messages

    # ========== 2.7 统一压缩管线 ==========

    async def run_compression_pipeline(
        self,
        messages: List[Message],
        conversation_id: str,
        context_window: int,
        max_output_tokens: int,
    ) -> List[Message]:
        """运行完整压缩管线：budget -> snip_compact -> micro_compact -> auto_compact"""
        if not messages:
            return messages

        current = messages

        # 1. Budget 压缩
        current = await self.budget(current, conversation_id)

        # 2. Snip compact
        current = self.snip_compact(current)

        # 3. Micro compact
        current = self.micro_compact(current)

        # 4. Auto compact
        current, was_compressed = await self.auto_compact(current, context_window, max_output_tokens)

        if was_compressed:
            logger.info("Compression pipeline completed with auto compact")
        else:
            logger.debug("Compression pipeline completed without auto compact")

        return current

    # ========== 2.8 后压缩恢复 ==========

    async def post_restore(self, messages: List[Message], agent=None) -> List[Message]:
        """后压缩恢复：重新读取最近文件、计划、agent/skill/tool"""
        if not messages:
            return messages

        # 从摘要或压缩后的消息中识别最近读过的文件路径
        file_paths = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM and "Previous conversation summary" in msg.content:
                # 尝试从摘要中提取文件路径（简单启发式）
                import re
                paths = re.findall(r'(?:file|path|read):\s*([^\s,]+)', msg.content, re.IGNORECASE)
                file_paths.extend(paths)

        if not file_paths:
            return messages

        # 按预算恢复：最多 N 个文件、每个最大 token、总预算
        max_files = self.post_restore_max_files
        max_file_tokens = self.post_restore_max_file_tokens
        total_budget = self.post_restore_total_budget_tokens

        restored_content_parts = []
        total_restored_tokens = 0

        for path in file_paths[:max_files]:
            try:
                from pathlib import Path as PathLib
                file_path = PathLib(path)
                if file_path.exists() and file_path.is_file():
                    content = file_path.read_text(encoding='utf-8', errors='replace')
                    tokens = self._estimate_tokens(content)

                    if tokens > max_file_tokens:
                        content = content[:int(max_file_tokens * 3)]  # 粗略截断
                        tokens = max_file_tokens

                    if total_restored_tokens + tokens > total_budget:
                        break

                    restored_content_parts.append(f"Restored file: {path}\n{content}")
                    total_restored_tokens += tokens
            except Exception as e:
                logger.warning("Failed to restore file", path=path, error=str(e))

        if restored_content_parts:
            restore_msg = Message(
                id=str(uuid.uuid4()),
                role=MessageRole.SYSTEM,
                content="\n\n---\n\n".join(restored_content_parts),
                message_type=MessageType.TEXT,
                token_count=total_restored_tokens,
            )
            # 插入到第一条消息之后
            messages.insert(1, restore_msg)
            logger.info("Post-restore applied", files_count=len(restored_content_parts), total_tokens=total_restored_tokens)

        return messages

    # ========== 便捷方法：单独暴露 persist_and_replace ==========

    async def persist_and_replace(self, content: str, conversation_id: str) -> str:
        """单独调用：将内容落盘并返回占位符（用于工具结果即时处理）"""
        if len(content) > self.budget_max_chars:
            return await self._persist_output(content, conversation_id)
        return content


# 便捷函数
def create_compressor(llm_service: LLMService) -> ContextCompressor:
    """创建压缩器实例"""
    return ContextCompressor(llm_service)
