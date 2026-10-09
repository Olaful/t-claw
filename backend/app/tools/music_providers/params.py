"""音乐生成参数标准化引擎与 endpoint API 配置解析（provider 无关的共享逻辑）"""
from typing import Any, Dict, List, Optional, Tuple


# ===== 参数标准化引擎 =====

def normalize_music_params(
    params: Dict[str, Any],
    mode: str,
    api_config: Dict[str, Any],
    stream: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """
    根据 other.api.{mode}.param_limit 配置裁剪参数。

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

        # length_range: clamp 到范围内 (支持 stream/no_stream 区分)
        if "length_range" in limit_cfg:
            lr = limit_cfg["length_range"]
            if isinstance(lr, dict):
                range_key = "stream" if stream else "no_stream"
                bounds = lr.get(range_key) or lr.get("no_stream") or lr.get("stream")
            else:
                bounds = lr
            if bounds and isinstance(bounds, (list, tuple)) and len(bounds) == 2:
                min_len, max_len = bounds
                if isinstance(value, str):
                    original_len = len(value)
                    if original_len > max_len:
                        normalized[param_name] = value[:max_len]
                        normalization[param_name] = {
                            "requested_length": original_len,
                            "applied_length": max_len,
                            "action": "truncated",
                        }
                    elif original_len < min_len:
                        # 太短，记录但不修改（让 API 报错）
                        normalization[param_name] = {
                            "requested_length": original_len,
                            "min_required": min_len,
                            "action": "below_minimum",
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

    return normalized, normalization, ignored


# ===== endpoint API 配置解析 =====

def get_api_mode_config(endpoint: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    """从 endpoint 的 other.api 配置中获取指定 mode 的配置"""
    model_params = endpoint.get("params", {}) or {}
    other = model_params.get("other", {}) or {}
    api_cfg = other.get("api", {}) or {}
    return api_cfg.get(mode)


def resolve_api_url(endpoint: Dict[str, Any], mode_config: Dict[str, Any]) -> str:
    """解析 API URL: 如果是全路径则直接使用，否则拼接 baseUrl"""
    url = mode_config.get("url", "")
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base_url = endpoint.get("base_url", "") or ""
    # 去掉 base_url 末尾的 /
    base_url = base_url.rstrip("/")
    # 对于音乐 API，不使用 compatible-mode 路径
    # 如果 base_url 含 /compatible-mode，取协议+域名部分
    if "/compatible-mode" in base_url:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
    return f"{base_url}{url}"


# ===== 提供商识别 =====

def identify_provider(endpoint: Dict[str, Any]) -> str:
    """识别提供商类型: yinchao / aliyun / unknown"""
    provider = endpoint.get("provider", "")
    if provider == "yinchao":
        return "yinchao"
    if provider == "aliyun":
        return "aliyun"
    # 根据 model_id 推断
    model_id = endpoint.get("model_id", "")
    if "fun-music" in model_id:
        return "aliyun"
    return provider or "unknown"
