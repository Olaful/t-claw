"""视频生成参数标准化引擎与 endpoint API 配置解析（provider 无关的共享逻辑）"""
from typing import Any, Dict, List, Optional, Tuple


# ===== 参数标准化引擎 =====

def normalize_video_params(
    params: Dict[str, Any],
    api_config: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """
    根据 other.api.generate-all-in-one.param_limit 配置裁剪参数。

    Returns: (normalized_params, normalization_details, ignored_params)
    """
    normalized = dict(params)
    normalization = {}
    ignored = []

    param_limit = api_config.get("param_limit", {}) or {}

    for param_name, limit_cfg in param_limit.items():
        if param_name not in normalized:
            continue

        value = normalized[param_name]

        # max_length: 截断超长文本
        if "max_length" in limit_cfg:
            max_len = limit_cfg["max_length"]
            if isinstance(value, str) and len(value) > max_len:
                original_len = len(value)
                normalized[param_name] = value[:max_len]
                normalization[param_name] = {
                    "requested_length": original_len,
                    "applied_length": max_len,
                    "action": "truncated",
                }

        # enum: 不在列表内则丢弃
        if "enum" in limit_cfg:
            allowed = limit_cfg["enum"]
            if value not in allowed:
                ignored.append(f"{param_name}={value}(not in {allowed})")
                del normalized[param_name]
                normalization[param_name] = {
                    "requested": value,
                    "applied": None,
                    "action": "ignored_not_in_enum",
                }

        # max_value: clamp 数值
        if "max_value" in limit_cfg:
            max_val = limit_cfg["max_value"]
            if isinstance(value, (int, float)) and value > max_val:
                normalized[param_name] = max_val
                normalization[param_name] = {
                    "requested": value,
                    "applied": max_val,
                    "action": "clamped",
                }

        # min_value: clamp 数值
        if "min_value" in limit_cfg:
            min_val = limit_cfg["min_value"]
            if isinstance(value, (int, float)) and value < min_val:
                normalized[param_name] = min_val
                normalization[param_name] = {
                    "requested": value,
                    "applied": min_val,
                    "action": "clamped",
                }

        # range: [min, max] clamp
        if "range" in limit_cfg:
            bounds = limit_cfg["range"]
            if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                min_val, max_val = bounds
                if isinstance(value, (int, float)):
                    if value < min_val:
                        normalized[param_name] = min_val
                        normalization[param_name] = {
                            "requested": value, "applied": min_val, "action": "clamped",
                        }
                    elif value > max_val:
                        normalized[param_name] = max_val
                        normalization[param_name] = {
                            "requested": value, "applied": max_val, "action": "clamped",
                        }

    return normalized, normalization, ignored


# ===== endpoint API 配置解析 =====

def get_api_mode_config(endpoint: Dict[str, Any], mode: str = "generate-all-in-one") -> Optional[Dict[str, Any]]:
    """从 endpoint 的 other.api 配置中获取指定 mode 的配置"""
    model_params = endpoint.get("params", {}) or {}
    other = model_params.get("other", {}) or {}
    api_cfg = other.get("api", {}) or {}
    return api_cfg.get(mode)


def _join_base_url(endpoint: Dict[str, Any], url: str) -> str:
    """将相对 URL 拼接到 endpoint base_url（去除 /compatible-mode 路径）"""
    base_url = (endpoint.get("base_url", "") or "").rstrip("/")
    if "/compatible-mode" in base_url:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base_url}{url}"


def resolve_api_url(endpoint: Dict[str, Any], mode_config: Dict[str, Any]) -> str:
    """解析 API URL: 如果是全路径则直接使用，否则拼接 baseUrl"""
    url = mode_config.get("url", "")
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return _join_base_url(endpoint, url)


def get_query_task_url(endpoint: Dict[str, Any]) -> Optional[str]:
    """获取 query_task URL 模板（含 {task_id} 占位符）"""
    model_params = endpoint.get("params", {}) or {}
    other = model_params.get("other", {}) or {}
    api_cfg = other.get("api", {}) or {}
    tq = api_cfg.get("query_task")
    if tq:
        url = tq.get("url", "")
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return _join_base_url(endpoint, url)
    return None


def get_file_upload_url(endpoint: Dict[str, Any]) -> Optional[str]:
    """获取 file_upload URL"""
    model_params = endpoint.get("params", {}) or {}
    other = model_params.get("other", {}) or {}
    api_cfg = other.get("api", {}) or {}
    fu = api_cfg.get("file_upload")
    if fu:
        url = fu.get("url", "")
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return _join_base_url(endpoint, url)
    return None


# ===== 提供商识别 =====

def identify_provider(endpoint: Dict[str, Any]) -> str:
    """识别提供商类型: aliyun / minimax / unknown"""
    provider = endpoint.get("provider", "")
    if provider == "aliyun":
        return "aliyun"
    if provider == "minimax":
        return "minimax"
    # 根据 model_id 推断
    model_id = endpoint.get("model_id", "")
    if "wan3" in model_id or "wanx" in model_id:
        return "aliyun"
    if "MiniMax" in model_id or "minimax" in model_id.lower():
        return "minimax"
    return provider or "unknown"
