"""图片生成参数标准化引擎

根据模型 compat 配置对请求参数进行校验、值映射与参数名映射，
供各 Provider 在发起调用前统一处理。
"""
import re
from typing import Any, Dict, List, Optional, Tuple

_SIZE_RANGE_PATTERN = re.compile(r'\[(\d+),(\d+)\]x\[(\d+),(\d+)\]')


def _parse_size_support(supports_size: List[str]) -> Tuple[List[str], Optional[Tuple[int, int, int, int]]]:
    """解析 supportsSize 配置，返回 (精确尺寸列表, 范围 (w_min, w_max, h_min, h_max) 或 None)"""
    exact = []
    size_range = None
    for s in supports_size:
        m = _SIZE_RANGE_PATTERN.match(s)
        if m:
            size_range = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        else:
            exact.append(s)
    return exact, size_range


def _find_closest_size(requested: str, supported_exact: List[str], supported_range: Optional[Tuple]) -> Optional[str]:
    """找到最接近的受支持尺寸"""
    if requested in supported_exact:
        return requested

    # 解析请求尺寸
    parts = requested.split("x")
    if len(parts) != 2:
        return supported_exact[0] if supported_exact else None
    try:
        req_w, req_h = int(parts[0]), int(parts[1])
    except ValueError:
        return supported_exact[0] if supported_exact else None

    # 范围表达：在范围内则直接使用
    if supported_range:
        w_min, w_max, h_min, h_max = supported_range
        if w_min <= req_w <= w_max and h_min <= req_h <= h_max:
            return requested

    # 找最接近的精确尺寸（面积比）
    if supported_exact:
        best = None
        best_ratio = float('inf')
        req_ratio = req_w / req_h if req_h > 0 else 1
        for s in supported_exact:
            sp = s.split("x")
            if len(sp) == 2:
                try:
                    sw, sh = int(sp[0]), int(sp[1])
                    s_ratio = sw / sh if sh > 0 else 1
                    ratio_diff = abs(req_ratio - s_ratio)
                    if ratio_diff < best_ratio:
                        best_ratio = ratio_diff
                        best = s
                except ValueError:
                    continue
        return best

    # 范围模式但超出范围：clamp 到最近边界
    if supported_range:
        w_min, w_max, h_min, h_max = supported_range
        clamped_w = max(w_min, min(w_max, req_w))
        clamped_h = max(h_min, min(h_max, req_h))
        return f"{clamped_w}x{clamped_h}"

    return None


def normalize_params(
    requested: Dict[str, Any],
    model_compat: Dict[str, Any],
    model_params_config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """
    标准化参数：
    - 检查模型 compat 支持
    - 值映射 (params.properties.{name}.mapping)
    - 参数名映射 (params.tool_param_map)
    - 不支持的参数丢弃

    Returns: (mapped_params, normalization_details, ignored_overrides)
    """
    mapped = {}
    normalization = {}
    ignored = []

    model_params_config = model_params_config or {}
    properties = model_params_config.get("properties", {}) or {}
    tool_param_map = model_params_config.get("tool_param_map", {}) or {}

    # size 处理
    size = requested.get("size")
    if size:
        supports_size = model_compat.get("supportsSize", [])
        if supports_size:
            exact_sizes, size_range = _parse_size_support(supports_size)
            resolved = _find_closest_size(size, exact_sizes, size_range)
            if resolved:
                if resolved != size:
                    normalization["size"] = {"requested": size, "applied": resolved}
                mapped["size"] = resolved
            else:
                ignored.append("size")
                normalization["size"] = {"requested": size, "applied": None, "reason": "no supported size found"}
        else:
            ignored.append("size")

    # quality 处理
    quality = requested.get("quality")
    if quality:
        supports_quality = model_compat.get("supportsQuality", [])
        if supports_quality and quality in supports_quality:
            # 检查是否有值映射
            quality_prop = properties.get("quality", {})
            quality_mapping = quality_prop.get("mapping", {})
            if quality_mapping and quality in quality_mapping:
                mapped_quality = quality_mapping[quality]
                normalization["quality"] = {"requested": quality, "applied": mapped_quality}
                mapped["quality"] = mapped_quality
            else:
                mapped["quality"] = quality
        elif supports_quality:
            # 尝试映射
            quality_prop = properties.get("quality", {})
            quality_mapping = quality_prop.get("mapping", {})
            if quality_mapping and quality in quality_mapping:
                mapped_quality = quality_mapping[quality]
                normalization["quality"] = {"requested": quality, "applied": mapped_quality}
                mapped["quality"] = mapped_quality
            else:
                ignored.append("quality")
        else:
            ignored.append("quality")

    # aspectRatio
    aspect_ratio = requested.get("aspectRatio")
    if aspect_ratio:
        supports_ar = model_compat.get("supportsAspectRatio", [])
        if supports_ar and aspect_ratio in supports_ar:
            mapped["aspectRatio"] = aspect_ratio
        elif supports_ar:
            ignored.append("aspectRatio")
        else:
            ignored.append("aspectRatio")

    # resolution
    resolution = requested.get("resolution")
    if resolution:
        supports_res = model_compat.get("supportsResolution", [])
        if supports_res and resolution in supports_res:
            mapped["resolution"] = resolution
        elif supports_res:
            ignored.append("resolution")
        else:
            ignored.append("resolution")

    # outputFormat
    output_format = requested.get("outputFormat")
    if output_format:
        supports_fmt = model_compat.get("supportsOutputFormat", [])
        if supports_fmt and output_format in supports_fmt:
            mapped["outputFormat"] = output_format
        elif supports_fmt:
            ignored.append("outputFormat")
        else:
            ignored.append("outputFormat")

    # background
    background = requested.get("background")
    if background:
        supports_bg = model_compat.get("supportsBackground", [])
        if supports_bg and background in supports_bg:
            mapped["background"] = background
        elif supports_bg:
            ignored.append("background")
        else:
            ignored.append("background")

    # 应用 tool_param_map 重命名
    if tool_param_map:
        final_mapped = {}
        for key, value in mapped.items():
            new_key = tool_param_map.get(key, key)
            final_mapped[new_key] = value
        mapped = final_mapped

    return mapped, normalization, ignored
