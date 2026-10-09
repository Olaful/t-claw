"""PDF 解析参数标准化引擎与 provider 无关的共享解析逻辑

- normalize_pdf_params: 依据 provider 能力与限额裁剪参数，产出 normalization/ignored 报告
- expand_page_ranges: 将页码范围字符串展开为 0-based 页索引（local provider 使用）
"""
import re
from typing import Any, Dict, List, Optional, Tuple


# 数值类参数 -> provider_cfg 中的限额键（超出则 clamp 到限额）
_NUMERIC_LIMITS: Dict[str, str] = {
    "max_pages": "pdf_max_pages",
    "max_mb": "pdf_max_mb",
}


def normalize_pdf_params(
    params: Dict[str, Any],
    provider_cfg: Dict[str, Any],
    provider: Any = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """根据 provider 能力与限额裁剪 PDF 解析参数。

    Args:
        params: 原始请求参数（is_ocr/enable_formula/enable_table/language/extra_formats/page_ranges 等）
        provider_cfg: 该 provider 的配置（含 pdf_max_mb/pdf_max_pages 等限额）
        provider: PdfProviderBase 实例（可选），用于读取 supported_extra_formats/default_extra_formats

    Returns: (normalized_params, normalization_details, ignored_params)
        - normalization_details: {param: {requested, applied, action}} 记录请求值→应用值的转换
        - ignored_params: 被丢弃参数的可读标记列表
    """
    normalized = dict(params or {})
    normalization: Dict[str, Any] = {}
    ignored: List[str] = []
    provider_cfg = provider_cfg or {}

    # ===== extra_formats 裁剪：仅保留 provider 支持的额外格式 =====
    if "extra_formats" in normalized:
        requested = normalized.get("extra_formats") or []
        if isinstance(requested, str):
            requested = [requested]
        supported = tuple(getattr(provider, "supported_extra_formats", ()) or ())
        defaults = tuple(getattr(provider, "default_extra_formats", ()) or ())
        applied: List[str] = []
        for fmt in requested:
            f = str(fmt).strip().lower()
            if not f:
                continue
            if supported and f in supported:
                if f not in applied:
                    applied.append(f)
            elif f in defaults:
                # markdown/json 等为默认产物，静默剔除（无需登记 ignored）
                continue
            else:
                ignored.append(f"extra_formats={f}(unsupported)")
        requested_norm = [str(x).strip().lower() for x in requested]
        if applied != requested_norm:
            normalization["extra_formats"] = {
                "requested": requested,
                "applied": applied,
                "action": "filtered",
            }
        normalized["extra_formats"] = applied

    # ===== 数值/范围类裁剪：依据 provider_cfg 限额 clamp =====
    for param_name, limit_key in _NUMERIC_LIMITS.items():
        if param_name not in normalized:
            continue
        limit_val = provider_cfg.get(limit_key)
        value = normalized[param_name]
        if isinstance(value, (int, float)) and isinstance(limit_val, (int, float)) and value > limit_val:
            normalized[param_name] = limit_val
            normalization[param_name] = {
                "requested": value,
                "applied": limit_val,
                "action": "clamped",
            }

    # page_ranges 原样透传（格式校验/展开交由提供商处理）
    return normalized, normalization, ignored


# ===== 页码范围解析（provider 无关共享逻辑）=====

_RANGE_RE = re.compile(r'^\s*(-?\d+)\s*-\s*(-?\d+)\s*$')
_SINGLE_RE = re.compile(r'^\s*(-?\d+)\s*$')


def _resolve_page_number(n: int, total_pages: int) -> int:
    """将可能为负（倒数）的 1-based 页码解析为实际 1-based 页码。

    负数语义：-1 表示最后一页，-2 表示倒数第二页。
    """
    if n < 0:
        return total_pages + n + 1
    return n


def expand_page_ranges(page_ranges: Optional[str], total_pages: int) -> Optional[List[int]]:
    """解析页码范围字符串为升序去重的 0-based 页索引列表。

    支持格式（1-based，含端点）：
    - "2,4-6" → 第2页 + 第4~6页
    - "2--2"  → 第2页到倒数第二页（"-2" 表示倒数第二页）
    - 空/None → None（表示全部页，由调用方决定）

    越界页码自动裁剪到 [1, total_pages]；非法片段忽略。
    total_pages <= 0 时返回空列表。
    """
    if page_ranges is None or not str(page_ranges).strip():
        return None
    if total_pages <= 0:
        return []

    collected: List[int] = []
    for part in str(page_ranges).split(","):
        part = part.strip()
        if not part:
            continue
        m = _RANGE_RE.match(part)
        if m:
            start = _resolve_page_number(int(m.group(1)), total_pages)
            end = _resolve_page_number(int(m.group(2)), total_pages)
            if start > end:
                start, end = end, start
            pages: Any = range(start, end + 1)
        else:
            sm = _SINGLE_RE.match(part)
            if not sm:
                continue  # 非法片段忽略
            pages = [_resolve_page_number(int(sm.group(1)), total_pages)]
        for p in pages:
            if 1 <= p <= total_pages:
                idx = p - 1
                if idx not in collected:
                    collected.append(idx)
    collected.sort()
    return collected
