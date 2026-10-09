"""System Prompt 分层缓存管理器

将 system prompt 拆分为静态区（基础骨架）与动态区（按需插件包）。
- 静态区：始终缓存，随 Agent 生命周期存在
- 动态区：触发构建后缓存 30 分钟，到期自动清除，下次触发时重新构建
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import structlog

logger = structlog.get_logger()


def _get_ttl() -> int:
    """从配置读取动态区缓存 TTL（秒），默认 1800（30 分钟）"""
    try:
        from app.config import settings
        return int(settings.get("SYSTEM_PROMPT_DYNAMIC_TTL", 1800))
    except Exception:
        return 1800


@dataclass
class _TimedSegment:
    """带 TTL 的动态段缓存"""
    text: str = ""
    built_at: float = 0.0  # time.monotonic() 时间戳

    @property
    def is_valid(self) -> bool:
        return self.built_at > 0 and (time.monotonic() - self.built_at) < _get_ttl()

    def mark_built(self, text: str) -> None:
        self.text = text
        self.built_at = time.monotonic()

    def invalidate(self) -> None:
        self.text = ""
        self.built_at = 0.0


@dataclass
class _AgentCacheEntry:
    """单个 Agent 的 system prompt 缓存条目"""
    # 静态区：config.system_prompt（始终保留）
    base_prompt: str = ""
    # 动态区：skills 段（TTL 控制）
    skills_segment: _TimedSegment = field(default_factory=_TimedSegment)
    # 动态区：memory 段（TTL 控制）
    memory_segment: _TimedSegment = field(default_factory=_TimedSegment)
    # 动态区：工作区段（工作区路径变化时立即重建，不受 TTL 控制）
    workspace_segment: _TimedSegment = field(default_factory=_TimedSegment)
    # 动态区：ToolSearch 延迟工具摘要（每轮重建，因为 loaded 状态会变）
    tool_search_text: str = ""
    # 工作区路径快照（变化时强制重建工作区段）
    workspace_key: str = ""
    # 最终拼接结果
    full_prompt: str = ""
    # deny_skills 快照（变化时强制重建 skills 段）
    deny_skills_key: str = ""
    # 是否为子 agent
    is_subagent: bool = False


class SystemPromptCache:
    """
    System prompt 分层缓存管理器。

    缓存策略：
    - 静态区：config.system_prompt 直接缓存，永不主动过期
    - 动态区（skills / memory）：触发构建后缓存 30 分钟，到期清除
    - deny_skills 变化时强制重建 skills 段
    - 任一段变化时重新拼接 full_prompt
    """

    def __init__(self):
        self._cache: Dict[str, _AgentCacheEntry] = {}

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get_or_build(self, agent) -> str:
        """
        获取缓存的 system prompt；若缓存失效则增量重建。

        Args:
            agent: BaseAgent 实例（需有 config / is_subagent / deny_skills 属性）

        Returns:
            完整的 system prompt 字符串
        """
        agent_id = agent.id
        entry = self._cache.get(agent_id)
        if entry is None:
            entry = _AgentCacheEntry(
                is_subagent=agent.is_subagent,
                base_prompt=agent.config.system_prompt,
            )
            self._cache[agent_id] = entry

        current_deny_key = self._deny_skills_key(agent.deny_skills)
        deny_changed = (current_deny_key != entry.deny_skills_key)

        # 工作区路径（随会话/层级变化，不受 TTL 控制）
        current_ws_key = ""
        try:
            if hasattr(agent, "get_workspace_path"):
                current_ws_key = agent.get_workspace_path() or ""
        except Exception:
            current_ws_key = ""
        ws_changed = (current_ws_key != entry.workspace_key)

        # --- 检查各动态段是否需要重建 ---
        skills_need_rebuild = deny_changed or not entry.skills_segment.is_valid
        memory_need_rebuild = (not entry.is_subagent) and not entry.memory_segment.is_valid

        # ToolSearch 段：每轮重建（因为 loaded 状态会随 tool_search 调用变化）
        tool_search_text = self._build_tool_search_segment(agent)
        tool_search_changed = (tool_search_text != entry.tool_search_text)
        entry.tool_search_text = tool_search_text

        # --- 重建变化的段（重建后比对内容，内容未变则不触发 full_prompt 重拼接）---
        skills_content_changed = False
        if skills_need_rebuild:
            old_text = entry.skills_segment.text
            new_text = self._build_skills_segment(agent)
            entry.skills_segment.mark_built(new_text)
            entry.deny_skills_key = current_deny_key
            skills_content_changed = (new_text != old_text)
            logger.debug("Skills segment rebuilt", agent_id=agent_id, content_changed=skills_content_changed)

        memory_content_changed = False
        if memory_need_rebuild:
            old_text = entry.memory_segment.text
            new_text = self._build_memory_segment()
            entry.memory_segment.mark_built(new_text)
            memory_content_changed = (new_text != old_text)
            logger.debug("Memory segment rebuilt", agent_id=agent_id, content_changed=memory_content_changed)

        if ws_changed:
            entry.workspace_segment.mark_built(self._build_workspace_segment(current_ws_key))
            entry.workspace_key = current_ws_key
            logger.debug("Workspace segment rebuilt", agent_id=agent_id, workspace=current_ws_key)

        # --- 如果没有任何段变化，直接返回缓存 ---
        # 动态区 TTL 过期但重建后内容一致时，不触发 full_prompt 重拼接
        if (
            not skills_content_changed
            and not memory_content_changed
            and not ws_changed
            and not tool_search_changed
            and entry.full_prompt
        ):
            return entry.full_prompt

        # --- 重新拼接 ---
        parts = [entry.base_prompt]
        if entry.skills_segment.text:
            parts.append(entry.skills_segment.text)
        if entry.memory_segment.text:
            parts.append(entry.memory_segment.text)
        if entry.workspace_segment.text:
            parts.append(entry.workspace_segment.text)
        if entry.tool_search_text:
            parts.append(entry.tool_search_text)

        entry.full_prompt = "\n".join(p for p in parts if p)

        logger.info(
            "System prompt assembled",
            agent_id=agent_id,
            skills_rebuilt=skills_need_rebuild,
            memory_rebuilt=memory_need_rebuild,
            tool_search_changed=tool_search_changed,
            total_len=len(entry.full_prompt),
        )

        return entry.full_prompt

    def invalidate(self, agent_id: Optional[str] = None) -> None:
        """
        主动失效缓存。

        Args:
            agent_id: 指定 agent 则只失效该 agent；None 则清空全部
        """
        if agent_id:
            entry = self._cache.pop(agent_id, None)
            if entry:
                logger.debug("Cache invalidated", agent_id=agent_id)
        else:
            self._cache.clear()
            logger.debug("All prompt cache invalidated")

    # ------------------------------------------------------------------
    # 各段构建
    # ------------------------------------------------------------------

    @staticmethod
    def _build_skills_segment(agent) -> str:
        """构建 skills 段落文本"""
        try:
            from app.skills import skill_loader
            return skill_loader.build_skills_prompt_section(
                exclude_names=agent.deny_skills if agent.deny_skills else None
            )
        except Exception as e:
            logger.warning("Failed to build skills segment", error=str(e))
            return ""

    @staticmethod
    def _build_memory_segment() -> str:
        """构建 memory 段落文本"""
        try:
            from app.core.memory_system.prompts import load_memory_prompt
            return load_memory_prompt()
        except Exception as e:
            logger.warning("Failed to build memory segment", error=str(e))
            return ""

    @staticmethod
    def _build_workspace_segment(path: str) -> str:
        """构建工作区段落文本（告知 agent 相对路径的解析基准）"""
        if not path:
            return ""
        return (
            "\n## Workspace\n"
            f"Your current workspace (working directory) is: {path}\n"
            "- Relative paths in file tools (read_file / write_file / edit_file / "
            "glob / grep / bash_command) are resolved against this workspace. "
            "For example, `read_file config.json` reads `config.json` inside your workspace.\n"
            "- Absolute paths are not restricted; you can still access files outside "
            "the workspace via absolute paths.\n"
            "- Stay in your own workspace for file changes unless the task requires otherwise.\n"
        )

    @staticmethod
    def _build_tool_search_segment(agent) -> str:
        """构建 ToolSearch 延迟工具摘要段落（告知 agent 哪些工具可通过 tool_search 加载）"""
        engine = getattr(agent, '_tool_search_engine', None)
        if engine is None:
            return ""
        return engine.get_deferred_summary()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _deny_skills_key(deny_skills: list) -> str:
        """deny_skills 列表的缓存键"""
        if not deny_skills:
            return ""
        return "|".join(sorted(deny_skills))


# 全局单例
system_prompt_cache = SystemPromptCache()
