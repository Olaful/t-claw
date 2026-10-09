"""记忆选择 - LLM side-query 从候选清单中选出相关记忆"""
import json
import re
from typing import List, Optional, Set

import structlog
from app.config import settings
from app.core.memory_system.prompts import SELECTION_PROMPT
from app.core.memory_system.scanner import MemoryEntry

logger = structlog.get_logger()


async def select_relevant_memories(
    llm_service,
    user_query: str,
    entries: List[MemoryEntry],
    already_surfaced: Optional[Set[str]] = None,
    active_tools: Optional[List[str]] = None,
    model: Optional[str] = None,
) -> List[MemoryEntry]:
    """
    用 side-query（LLM）从候选记忆中选出相关记忆（最多 select_max 个）。

    规则（写入 prompt）：
    - 严格标准：不确定就不选
    - 去重保护：already_surfaced 提前过滤
    - 工具降噪：当前工具的用法文档类过滤（Bug/注意事项仍可选）

    Args:
        llm_service: LLM 服务
        user_query: 用户当前问题
        entries: 候选记忆清单
        already_surfaced: 本会话已展示过的文件名集合
        active_tools: 当前正在使用的工具名列表
        model: 可选的模型覆盖（如低成本模型），None 时用服务实例默认模型

    Returns:
        选中的 MemoryEntry 列表
    """
    if not entries or not user_query.strip():
        return []

    already_surfaced = already_surfaced or set()
    select_max = int(settings.get("MEMORY_SELECT_MAX", 5))

    # 去重保护：提前过滤已展示的记忆与工具类参考文档记忆
    candidates = [e for e in entries if e.filename not in already_surfaced]
    # todo
    candidates = [e for e in candidates if not (e.memory_type == "reference" and any(f"[{at}] tool reference" in e.description for at in active_tools))]
    if not candidates:
        logger.debug("All candidate memories already surfaced, skip selection")
        return []

    manifest_text = "\n".join(e.to_manifest_line() for e in candidates)

    prompt = SELECTION_PROMPT.format(
        select_max=select_max,
        user_query=user_query[:2000],
        manifest=manifest_text
    )

    try:
        response = await llm_service.chat(
            messages=[
                {"role": "system", "content": "You are a precise memory relevance selector."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=200,
            model=model,
        )
    except Exception as e:
        logger.warning("Memory selection side-query failed", error=str(e))
        return []

    selected_names = _parse_selection(response)
    if not selected_names:
        return []

    # 按清单顺序映射回 MemoryEntry，校验文件名存在
    name_to_entry = {e.filename: e for e in candidates}
    selected = []
    for name in selected_names[:select_max]:
        entry = name_to_entry.get(name)
        if entry:
            selected.append(entry)

    logger.info(
        "Memory selection completed",
        candidates=len(candidates),
        selected=[e.filename for e in selected],
    )
    return selected


def _parse_selection(response: str) -> List[str]:
    """容错解析 LLM 返回的文件名 JSON 数组"""
    if not response:
        return []

    text = response.strip()

    # 尝试直接解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except (json.JSONDecodeError, TypeError):
        pass

    # 提取第一个 JSON 数组
    match = re.search(r"\[(.*?)\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads("[" + match.group(1) + "]")
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass

    # 兜底：提取所有 .md 文件名
    fallback = re.findall(r"[\w\-.]+\.md", text)
    return list(dict.fromkeys(fallback))
