"""TTS 配置解析 + [[tts:...]] 标记解析"""
import copy
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import structlog

from app.config import settings

logger = structlog.get_logger()

# ===== [[tts:...]] 标记解析正则 =====
_TTS_TAG_RE = re.compile(r'\[\[tts:([^\]]*)\]\]')
_TTS_TEXT_BLOCK_RE = re.compile(r'\[\[tts:text\]\](.*?)\[\[/tts:text\]\]', re.DOTALL)
_TTS_KV_RE = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))')


# ===== 配置解析 =====

def _resolve_env_vars(value: Any) -> Any:
    """递归解析配置中的 ${ENV_VAR} 占位符"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        return os.environ.get(env_name, "")
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """深合并字典，override 覆盖 base"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_tts_config(agent_name: Optional[str] = None) -> Dict[str, Any]:
    """解析并合并 TTS 配置: 全局 tts + agent 级覆盖

    Returns:
        完整的 TTS 配置字典 (已解析环境变量)
    """
    raw = settings.to_dict() if hasattr(settings, 'to_dict') else {}
    global_tts = raw.get("tts", {}) or {}

    # Agent 级覆盖
    if agent_name:
        entries = raw.get("agent", {}).get("entries", {}) or {}
        agent_entry = entries.get(agent_name, {}) or {}
        agent_tts = agent_entry.get("tts", {}) or {}
        if agent_tts:
            global_tts = _deep_merge(global_tts, agent_tts)

    # 解析环境变量
    config = _resolve_env_vars(global_tts)

    # 设置默认值
    config.setdefault("auto", "off")
    config.setdefault("provider", "edge_tts")
    config.setdefault("mode", "final")
    config.setdefault("stream_mode", "one-way")
    config.setdefault("timeout_ms", 30000)
    config.setdefault("providers", {})
    config.setdefault("personas", {})
    config.setdefault("model_overrides", {"enabled": False, "allow_provider": False})

    return config


def resolve_persona(config: Dict[str, Any], persona_id: Optional[str] = None) -> Dict[str, Any]:
    """解析 persona 配置并深合并到 provider 配置

    Args:
        config: 完整 TTS 配置
        persona_id: persona ID (None 或空则不使用)

    Returns:
        合并 persona 后的配置副本
    """
    if not persona_id:
        return config

    personas = config.get("personas", {}) or {}
    persona = personas.get(persona_id)
    if not persona:
        logger.warning("TTS persona not found", persona_id=persona_id)
        return config

    result = copy.deepcopy(config)

    # persona 可覆盖 provider 选择
    if persona.get("provider"):
        result["provider"] = persona["provider"]

    # persona 内的 providers 覆盖合并到全局 providers
    persona_providers = persona.get("providers", {}) or {}
    for prov_name, prov_override in persona_providers.items():
        if prov_name in result.get("providers", {}):
            result["providers"][prov_name] = _deep_merge(
                result["providers"][prov_name], prov_override
            )
        else:
            result["providers"][prov_name] = prov_override

    # 存储 fallback_policy
    result["_persona_fallback_policy"] = persona.get("fallback_policy", "preserve-persona")
    result["_persona_id"] = persona_id

    return result


def get_provider_config(config: Dict[str, Any], provider_name: Optional[str] = None) -> Dict[str, Any]:
    """获取指定 provider 的配置"""
    name = provider_name or config.get("provider", "edge_tts")
    providers = config.get("providers", {}) or {}
    return providers.get(name, {})


def get_effective_stream_mode(provider_name: str, requested_mode: str) -> str:
    """根据 provider 能力降级 stream_mode

    - two-way: aliyun/minimax 支持; edge_tts 降级为 one-way
    - one-way: 所有 provider 支持
    - none: 所有 provider 支持
    """
    if requested_mode == "two-way":
        if provider_name == "edge_tts":
            return "one-way"
        return "two-way"
    return requested_mode


# ===== [[tts:...]] Tag 解析 =====

def parse_tts_tags(text: str, allow_provider: bool = False) -> Tuple[str, List[Dict[str, Any]]]:
    """解析文本中的 [[tts:...]] 指令

    Returns:
        (cleaned_text, segments)
        - cleaned_text: 移除 tts 标记和 tts:text 块后的纯文本
        - segments: [{config_overrides: dict, text: str, tts_only_text: str}]
            每段包含该段使用的配置覆盖、显示文本、仅语音文本
    """
    # 先检查是否包含 tts 标记
    if '[[tts:' not in text:
        return text, []

    segments: List[Dict[str, Any]] = []
    current_overrides: Dict[str, Any] = {}
    pos = 0
    cleaned_parts: List[str] = []

    # 按 [[tts:...]] 标记分割
    for match in _TTS_TAG_RE.finditer(text):
        tag_content = match.group(1)

        # 检查是否是 [[tts:text]] 块开始
        if tag_content.strip() == "text":
            # 找对应的 [[/tts:text]]
            block_start = match.end()
            block_end_match = re.search(r'\[\[/tts:text\]\]', text[block_start:])
            if block_end_match:
                tts_only = text[block_start:block_start + block_end_match.start()]
                # 前面的文本作为当前段
                before_text = text[pos:match.start()]
                cleaned_parts.append(before_text)
                segments.append({
                    "config_overrides": copy.deepcopy(current_overrides),
                    "text": before_text,
                    "tts_only_text": tts_only,
                })
                pos = block_start + block_end_match.end()
                continue

        # 普通 [[tts:key=value ...]] 指令
        before_text = text[pos:match.start()]
        if before_text.strip():
            cleaned_parts.append(before_text)
            segments.append({
                "config_overrides": copy.deepcopy(current_overrides),
                "text": before_text,
                "tts_only_text": "",
            })

        # 解析键值对
        new_overrides: Dict[str, Any] = {}
        for kv_match in _TTS_KV_RE.finditer(tag_content):
            key = kv_match.group(1)
            value = kv_match.group(2) or kv_match.group(3) or kv_match.group(4) or ""
            # provider 覆盖控制
            if key == "provider" and not allow_provider:
                continue
            # 尝试转换数字
            if value.replace('.', '').replace('-', '').isdigit():
                value = float(value) if '.' in value else int(value)
            new_overrides[key] = value

        current_overrides = _deep_merge(current_overrides, new_overrides)
        pos = match.end()

    # 剩余文本
    remaining = text[pos:]
    if remaining:
        cleaned_parts.append(remaining)
        segments.append({
            "config_overrides": copy.deepcopy(current_overrides),
            "text": remaining,
            "tts_only_text": "",
        })

    cleaned_text = "".join(cleaned_parts)
    return cleaned_text, segments
