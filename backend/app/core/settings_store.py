"""统一的 settings.json 读写接口

设计目标：
- 所有对 .happy-claw/settings.json 的写入都通过本模块，避免多处直接写文件；
- 写入采用「读-改-写」并加锁，防止并发更新引起配置不一致；
- 写入成功后刷新内存中的全局配置（settings.reload()）；
- 提供 agent 模型配置（agent.entries.{agent_name}.models/model）的便捷读写，
  以及根据 model.primary 解析默认模型参数的能力。
"""
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import structlog

from app.config import settings, _HAPPYCLAW_HOME

logger = structlog.get_logger()

# 全局可重入写锁：保护 settings.json 的读-改-写，避免并发不一致
_settings_lock = threading.RLock()


def get_settings_path() -> Path:
    """settings.json 的绝对路径"""
    return _HAPPYCLAW_HOME / "settings.json"


def _read_unlocked() -> Dict[str, Any]:
    """读取 settings.json 原始字典（调用方需持有锁）"""
    path = get_settings_path()
    if not path.exists():
        logger.warning("settings.json not found for read", path=str(path))
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to read settings.json", path=str(path), error=str(e), exc_info=True)
        return {}


def _write_unlocked(data: Dict[str, Any]) -> None:
    """原子写入 settings.json（调用方需持有锁）"""
    path = get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(str(tmp), str(path))
    except Exception as e:
        # 清理临时文件
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        logger.error("Failed to write settings.json", path=str(path), error=str(e), exc_info=True)
        raise


def read_settings() -> Dict[str, Any]:
    """线程安全地读取 settings.json 原始字典"""
    with _settings_lock:
        return _read_unlocked()


def update_settings(mutator: Callable[[Dict[str, Any]], None], reload: bool = True) -> Dict[str, Any]:
    """统一的配置更新接口（加锁的读-改-写）。

    Args:
        mutator: 接收完整配置字典并原地修改的回调（无需返回值）。
        reload: 是否在写入后刷新内存缓存（默认 True）。
                设为 False 时仅写入文件，不刷新 settings 对象（适用于需要重启应用才能生效的配置）。

    Returns:
        更新后的完整配置字典。

    说明：
        - 所有需要写 settings.json 的场景都应通过此接口，保证并发安全；
        - 写入成功后默认会刷新内存中的全局配置对象（reload=True 时）。
    """
    with _settings_lock:
        data = _read_unlocked()
        mutator(data)
        _write_unlocked(data)

    # 刷新内存中的全局配置（锁外执行，reload 内部只读文件）
    if reload:
        try:
            settings.reload()
        except Exception as e:
            logger.warning("Failed to reload settings after update", error=str(e))
    return data


# ===== agent 模型配置（agent.entries.{agent_name}.models/model） =====

def _resolve_model_alias(data: Dict[str, Any], model_ref: str) -> str:
    """根据 provider/model_id 在 agent.llm.providers 中查找模型显示名作为 alias。

    找不到时回退为 model_id。
    """
    provider, _, model_id = model_ref.partition("/")
    if not model_id:
        model_id = provider
        provider = ""
    providers = (data.get("agent", {}) or {}).get("llm", {}).get("providers", {}) or {}
    provider_cfg = providers.get(provider, {}) if provider else {}
    for m in provider_cfg.get("models", []) or []:
        if m.get("id") == model_id:
            return m.get("name") or model_id
    return model_id


def get_agent_models(agent_name: str) -> Tuple[List[str], Optional[str]]:
    """读取某 agent 的可用模型列表与默认主模型。

    Returns:
        (models, primary)：models 为 "provider/model_id" 字符串列表，primary 可能为 None。
    """
    data = settings.to_dict()  # ✅ 从内存缓存读取，避免每次读文件
    entries = (data.get("agent", {}) or {}).get("entries", {}) or {}
    entry = entries.get(agent_name) or {}
    models_cfg = entry.get("models", {}) or {}
    # DotDict 不是 dict 子类，用 hasattr 检查是否有 keys() 方法
    models = list(models_cfg.keys()) if hasattr(models_cfg, 'keys') else []
    model_cfg = entry.get("model", {}) or {}
    primary = model_cfg.get("primary") if hasattr(model_cfg, 'get') else None
    return models, primary


def set_agent_models(
    agent_name: str,
    models: List[str],
    primary: Optional[str] = None,
    old_name: Optional[str] = None,
) -> None:
    """更新 agent.entries.{agent_name}.models/model 配置。

    Args:
        agent_name: 目标 agent 名称（entries 的 key）。
        models: 可用模型列表，元素格式为 "provider/model_id"。
        primary: 默认主模型（"provider/model_id"）；为空时取 models 首个。
        old_name: 若 agent 重命名，传入旧名称以迁移/清理旧条目。
    """
    if not agent_name:
        return

    def _mutator(data: Dict[str, Any]) -> None:
        agent = data.setdefault("agent", {})
        if not isinstance(agent, dict):
            agent = {}
            data["agent"] = agent
        entries = agent.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            agent["entries"] = entries

        # 处理重命名：迁移旧条目并删除旧 key
        migrated: Optional[Dict[str, Any]] = None
        if old_name and old_name != agent_name and old_name in entries:
            migrated = entries.pop(old_name)

        entry = entries.get(agent_name)
        if not isinstance(entry, dict):
            entry = migrated if isinstance(migrated, dict) else {}
            entries[agent_name] = entry

        # models: 以 "provider/model_id" 为 key，alias 取自 providers 中的模型名
        models_dict: Dict[str, Any] = {}
        for m in models or []:
            if not m:
                continue
            existing = (entry.get("models", {}) or {}).get(m)
            alias = existing.get("alias") if isinstance(existing, dict) else None
            models_dict[m] = {"alias": alias or _resolve_model_alias(data, m)}
        entry["models"] = models_dict

        # model.primary
        primary_model = primary or (models[0] if models else None)
        if primary_model:
            model_cfg = entry.get("model")
            if not isinstance(model_cfg, dict):
                model_cfg = {}
            model_cfg["primary"] = primary_model
            entry["model"] = model_cfg

    update_settings(_mutator)
    logger.info(
        "Agent model config updated in settings.json",
        agent_name=agent_name,
        models=models,
        primary=primary,
    )


def remove_agent_entry(agent_name: str) -> None:
    """删除 agent.entries.{agent_name} 条目（agent 删除时清理）"""
    if not agent_name:
        return

    def _mutator(data: Dict[str, Any]) -> None:
        entries = (data.get("agent", {}) or {}).get("entries")
        if isinstance(entries, dict) and agent_name in entries:
            entries.pop(agent_name)

    update_settings(_mutator)
    logger.info("Agent entry removed from settings.json", agent_name=agent_name)


def resolve_llm_endpoint(provider: str, model_id: Optional[str] = None) -> Dict[str, Any]:
    """解析指定供应商（及模型）的接入信息。

    从 agent.llm.providers.{provider} 读取 baseUrl/apiKey，从该供应商的 models
    列表中匹配 model_id 读取 api/reasoning/compat/maxTokens/contextWindow 等。

    Args:
        provider: 供应商名称，如 "deepseek"、"openai"、"anthropic"。
        model_id: 模型 ID（可选），用于定位模型的 api 类型与能力标记。

    Returns:
        dict: {
            "provider": str,
            "model_id": str | None,
            "base_url": str | None,
            "api_key": str | None,
            "api": str,               # "openai-completions" | "anthropic-messages"
            "reasoning": bool,        # 模型是否允许推理
            "supports_reasoning_effort": bool,
            "max_tokens": int | None,
            "context_window": int | None,
            "params": {...},          # 完整模型配置
        }
    """
    data = settings.to_dict()  # ✅ 从内存缓存读取，避免每次读文件
    providers = (data.get("agent", {}) or {}).get("llm", {}).get("providers", {}) or {}
    provider_cfg = providers.get(provider, {}) or {}

    base_url = provider_cfg.get("baseUrl")
    api_key = provider_cfg.get("apiKey")

    model_params: Dict[str, Any] = {}
    if model_id:
        for m in provider_cfg.get("models", []) or []:
            if m.get("id") == model_id:
                model_params = m
                break

    api = model_params.get("api")
    if not api:
        # 未匹配到模型时按供应商名称推断默认 api 类型
        api = "anthropic-messages" if provider == "anthropic" else "openai-completions"

    compat = model_params.get("compat", {}) or {}

    return {
        "provider": provider,
        "model_id": model_id,
        "base_url": base_url,
        "api_key": api_key,
        "api": api,
        "reasoning": bool(model_params.get("reasoning", False)),
        "supports_reasoning_effort": bool(compat.get("supportsReasoningEffort", False)),
        "max_tokens": model_params.get("maxTokens"),
        "context_window": model_params.get("contextWindow"),
        "params": model_params,
    }


def resolve_agent_primary_model(agent_name: str) -> Optional[Dict[str, Any]]:
    """解析 agent 的默认主模型及其参数。

    读取 agent.entries.{agent_name}.model.primary（格式 "provider/model_id"），
    在 agent.llm.providers.{provider}.models 中查找对应模型，返回其参数。

    Returns:
        dict: {
            "model_ref": "provider/model_id",
            "provider": "provider",
            "model_id": "model_id",
            "max_tokens": int | None,       # 来自模型的 maxTokens
            "context_window": int | None,   # 来自模型的 contextWindow
            "params": {...},                # 完整模型配置
        }
        未配置 primary 时返回 None。
    """
    data = settings.to_dict()  # ✅ 从内存缓存读取，避免每次读文件
    entries = (data.get("agent", {}) or {}).get("entries", {}) or {}
    entry = entries.get(agent_name) or {}
    model_cfg = entry.get("model", {}) or {}
    primary = model_cfg.get("primary") if hasattr(model_cfg, 'get') else None
    if not primary:
        return None

    provider, _, model_id = primary.partition("/")
    if not model_id:
        model_id = provider
        provider = ""

    providers = (data.get("agent", {}) or {}).get("llm", {}).get("providers", {}) or {}
    provider_cfg = providers.get(provider, {}) if provider else {}
    model_params: Dict[str, Any] = {}
    for m in provider_cfg.get("models", []) or []:
        if m.get("id") == model_id:
            model_params = m
            break

    return {
        "model_ref": primary,
        "provider": provider,
        "model_id": model_id,
        "max_tokens": model_params.get("maxTokens"),
        "context_window": model_params.get("contextWindow"),
        "params": model_params,
    }
