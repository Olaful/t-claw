"""PDF 解析工具 - 支持多提供商（mineru 精准解析 / local pymupdf4llm）的文档解析

本文件为编排/兼容层，负责：上下文解析、配置合并、提供商链构建、参数标准化、
同步/后台解析编排、结果落地、事件推送与消息持久化、工具注册。各 provider 的具体
实现（文件读取、任务提交与轮询、结果解压）已拆分至 app.tools.pdf_providers 包，
新增 provider 只需在该包新建文件并在 factory.py 注册。

提供商尝试顺序：
1. 工具 `provider` 参数（强制，失败不 fallback）
2. agent `pdf.primary`（无则回退全局 `pdf.provider`）
3. agent `pdf.fallbacks`（按序）
"""
import asyncio
import contextvars
import copy
import json as _json
import os
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog

from app.config import settings
from app.core.workspace_isolation import get_current_workspace
from app.tools.registry import tool_registry, ToolPermissionEnum, current_tool_call_id
from app.tools.pdf_providers import (
    PdfParseRequest,
    get_provider,
    normalize_pdf_params,
    supported_provider_types,
)

logger = structlog.get_logger()

# ===== 活动任务跟踪（用于 action=status）=====
_active_jobs: Dict[str, Dict[str, Any]] = {}

# ===== 外部上下文注入（斜杠命令等非 agent 调用场景）=====
_external_agent_context: contextvars.ContextVar[Optional[Tuple[str, str, Any]]] = contextvars.ContextVar(
    '_external_pdf_agent_context', default=None
)


# ===== 辅助：获取当前 agent 上下文 =====

def _get_agent_context() -> Tuple[str, str, Any]:
    """获取当前 agent_name, conversation_id, agent_self。"""
    ctx = _external_agent_context.get()
    if ctx is not None:
        return ctx
    import inspect
    frame = inspect.currentframe()
    for _ in range(15):
        frame = frame.f_back
        if frame is None:
            break
        if 'self' in frame.f_locals:
            caller_self = frame.f_locals['self']
            if hasattr(caller_self, 'id') and hasattr(caller_self, '_current_conversation'):
                agent_name = getattr(caller_self.config, 'name', 'unknown') if hasattr(caller_self, 'config') else 'unknown'
                conversation_id = caller_self._current_conversation or "default"
                return agent_name, conversation_id, caller_self
    return "unknown", "default", None


# ===== 配置解析 =====

def _resolve_env_vars(value: Any) -> Any:
    """递归解析配置中的 ${ENV_VAR} 占位符"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
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


def resolve_pdf_config(agent_name: Optional[str] = None) -> Dict[str, Any]:
    """解析并合并 PDF 配置: 全局 pdf + agent 级覆盖（已解析环境变量）

    Returns: {provider, primary, fallbacks, providers: {mineru:{...}, local:{...}}}
    """
    raw = settings.to_dict() if hasattr(settings, 'to_dict') else {}
    global_pdf = dict(raw.get("pdf", {}) or {})

    if agent_name:
        entries = (raw.get("agent", {}) or {}).get("entries", {}) or {}
        entry = entries.get(agent_name, {}) or {}
        agent_pdf = entry.get("pdf", {}) or {}
        if agent_pdf:
            global_pdf = _deep_merge(global_pdf, agent_pdf)

    config = _resolve_env_vars(global_pdf)
    config.setdefault("providers", {})
    config.setdefault("fallbacks", [])
    return config


def _build_provider_chain(
    provider_param: Optional[str],
    pdf_cfg: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """构建提供商尝试链：
    1. provider_param（强制，失败不 fallback）
    2. primary（无则回退全局 provider）
    3. fallbacks
    """
    providers_cfg = pdf_cfg.get("providers", {}) or {}
    chain: List[Dict[str, Any]] = []

    if provider_param:
        chain.append({
            "name": provider_param,
            "cfg": providers_cfg.get(provider_param, {}) or {},
            "force": True,
        })
        return chain

    primary = pdf_cfg.get("primary") or pdf_cfg.get("provider")
    if primary:
        chain.append({"name": primary, "cfg": providers_cfg.get(primary, {}) or {}, "force": False})

    for fb in (pdf_cfg.get("fallbacks") or []):
        if fb and fb != primary:
            chain.append({"name": fb, "cfg": providers_cfg.get(fb, {}) or {}, "force": False})

    return chain


# ===== 结果落地 =====

def _save_pdf_outputs(provider_result: Dict[str, Any], workspace_path: str) -> List[Dict[str, Any]]:
    """将 provider 返回的已落地文件规范化为 {filename,url,path,size,format}

    url 为 `/api/v1/chat/files/{rel}`（rel 相对 WORKSPACE_ROOT），供前端下载。
    """
    files = provider_result.get("files", []) or []
    workspace_root = str(Path(settings.get("WORKSPACE_ROOT", str(Path(".happy-claw") / "workspace"))).resolve())
    outputs: List[Dict[str, Any]] = []
    for f in files:
        path = f.get("path", "")
        if not path:
            continue
        try:
            rel_path = os.path.relpath(path, workspace_root)
        except ValueError:
            rel_path = path
        rel_url = rel_path.replace("\\", "/")
        size = f.get("size")
        if size is None:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        outputs.append({
            "filename": f.get("filename") or os.path.basename(path),
            "url": f"/api/v1/chat/files/{rel_url}",
            "path": path,
            "size": size,
            "format": f.get("format", ""),
        })
    return outputs


# ===== 提供商链执行（同步/后台共用）=====

async def _execute_chain(
    chain: List[Dict[str, Any]],
    pdfs: List[str],
    passwords: List[str],
    params: Dict[str, Any],
    workspace_path: str,
    timeout_ms: int,
    job_id: str = "",
) -> Dict[str, Any]:
    """按链顺序执行 prepare + normalize + parse，成功即停（force 失败不 fallback）"""
    last_error: Optional[str] = None
    for entry in chain:
        name = entry["name"]
        cfg = entry["cfg"]
        force = entry["force"]
        provider = get_provider(name)

        if not provider:
            last_error = f"Unsupported pdf provider: {name}"
            logger.warning("PDF parse failed, trying next provider",
                           job_id=job_id, provider=name, error=last_error, force=force)
            if force:
                break
            continue

        # 参数标准化（extra_formats 裁剪 + 限额 clamp）
        norm_params, norm_details, norm_ignored = normalize_pdf_params(params, cfg, provider)

        request = PdfParseRequest(
            provider_name=name, provider_cfg=cfg, pdfs=pdfs, passwords=passwords,
            params=norm_params, workspace_path=workspace_path, job_id=job_id,
            timeout_ms=timeout_ms or 300000,
        )
        # 能力协商（登记不支持的参数）
        provider.prepare(request)
        ignored = list(norm_ignored) + list(request.ignored_params)

        logger.info("Attempting PDF parse", job_id=job_id, provider=name, files=len(pdfs))
        parse_result = await provider.parse(request)

        if parse_result.get("success"):
            saved_files = _save_pdf_outputs(parse_result, workspace_path)
            out: Dict[str, Any] = {
                "success": True,
                "provider": name,
                "files": saved_files,
                "details": {"normalization": norm_details, "ignored_overrides": ignored},
            }
            if parse_result.get("partial_errors"):
                out["partial_errors"] = parse_result["partial_errors"]
            return out

        last_error = parse_result.get("error", "Unknown error")
        logger.error("PDF parse failed, trying next provider",
                       job_id=job_id, provider=name, error=last_error, force=force, traceback=True)
        if force:
            break

    return {"success": False, "error": last_error or "PDF 解析失败"}


# ===== 后台解析任务 =====

async def _run_pdf_parse(
    job_id: str,
    conversation_id: str,
    agent_name: str,
    chain: List[Dict[str, Any]],
    pdfs: List[str],
    passwords: List[str],
    params: Dict[str, Any],
    workspace_path: str,
    timeout_ms: int,
    tool_call_id: str,
    agent_self: Any = None,
):
    """后台执行 PDF 解析任务，完成后推送事件并持久化消息"""
    job_info = _active_jobs.get(job_id)
    if job_info:
        job_info["status"] = "running"
        job_info["started_at"] = datetime.now(timezone.utc).isoformat()

    result = await _execute_chain(chain, pdfs, passwords, params, workspace_path, timeout_ms, job_id)

    status = "completed" if result.get("success") else "failed"
    files = result.get("files", []) if result.get("success") else []
    details = result.get("details", {}) or {}
    used_provider = result.get("provider")
    error = result.get("error") if status == "failed" else None

    if job_info:
        job_info["status"] = status
        job_info["completed_at"] = datetime.now(timezone.utc).isoformat()
        job_info["result"] = {"files": files, "provider": used_provider, "error": error}

    # 推送最终事件到前端
    message_id = f"pdf_{job_id}"
    try:
        from app.core.event_bus import event_bus
        event_data = {
            "message_id": message_id,
            "job_id": job_id,
            "conversation_id": conversation_id,
            "tool_call_id": tool_call_id,
            "status": status,
            "files": files,
            "provider": used_provider,
            "details": details,
        }
        if error:
            event_data["error"] = error
        await event_bus.emit_event(
            type="pdf_parsed",
            source=conversation_id,
            data=event_data,
            priority="normal",
            dimensions=[f"conversation:{conversation_id}"],
        )
        logger.info("PDF parse event emitted", job_id=job_id, status=status, conversation_id=conversation_id)
    except Exception as e:
        logger.error("Failed to emit pdf_parsed event", job_id=job_id, error=str(e), exc_info=True)

    # 持久化消息到数据库
    try:
        from app.services.persistence import persistence
        from app.models.schemas.chat import Message, MessageRole, MessageType

        msg_metadata = {
            "job_id": job_id,
            "tool_call_id": tool_call_id,
            "status": status,
            "provider": used_provider,
            "files": files,
            "normalization": details.get("normalization", {}),
            "ignored_overrides": details.get("ignored_overrides", []),
        }
        if error:
            msg_metadata["error"] = error

        if status == "completed":
            content = _json.dumps({
                "status": "completed",
                "provider": used_provider,
                "files": [{"filename": f.get("filename"), "url": f.get("url"), "format": f.get("format")} for f in files],
            }, ensure_ascii=False)
        else:
            content = _json.dumps({"status": "failed", "error": error, "provider": used_provider}, ensure_ascii=False)

        msg = Message(
            id=message_id,
            role=MessageRole.TOOL,
            content=content,
            message_type=MessageType.PDF_PARSED,
            tool_call_id=tool_call_id,
            name="pdf_parse",
            metadata=msg_metadata,
        )
        await persistence.save_message(conversation_id, msg)

        if agent_self and hasattr(agent_self, 'get_conversation'):
            conv = agent_self.get_conversation(conversation_id)
            if conv:
                conv.messages.append(msg)

        logger.info("PDF parse message persisted", job_id=job_id, message_id=message_id)
    except Exception as e:
        logger.error("Failed to persist pdf_parsed message", job_id=job_id, error=str(e), exc_info=True)

    _cleanup_old_jobs()


def _cleanup_old_jobs():
    """清理过期的任务记录"""
    now = time.time()
    to_remove = []
    for jid, info in _active_jobs.items():
        completed = info.get("completed_at")
        if completed:
            try:
                ct = datetime.fromisoformat(completed.replace("Z", "+00:00")).timestamp()
                if now - ct > 600:
                    to_remove.append(jid)
            except Exception:
                to_remove.append(jid)
    for jid in to_remove:
        _active_jobs.pop(jid, None)


# ===== 工具 Handler =====

def _ensure_list(val: Any) -> List[Any]:
    """列表参数容错：兼容 JSON 字符串 / 逗号分隔字符串 / 列表 / None"""
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                return _json.loads(s)
            except Exception:
                return [x.strip().strip("'\"") for x in s[1:-1].split(",") if x.strip()]
        elif s:
            return [x.strip() for x in s.split(",") if x.strip()]
        return []
    if isinstance(val, list):
        return val
    return []


async def pdf_parse(
    pdfs: list = None,
    provider: str = "",
    is_ocr: bool = False,
    enable_formula: bool = True,
    enable_table: bool = True,
    language: str = "ch",
    extra_formats: list = None,
    page_ranges: str = "",
    passwords: list = None,
    background: bool = False,
    timeoutMs: int = 0,
    action: str = "parse",
    **kwargs,
) -> Dict[str, Any]:
    """PDF 解析工具 handler"""
    try:
        timeoutMs = int(timeoutMs) if timeoutMs not in (None, "") else 0
    except (ValueError, TypeError):
        timeoutMs = 0

    pdfs_list = [str(p) for p in _ensure_list(pdfs) if str(p).strip()]
    passwords_list = [str(p) if p is not None else "" for p in _ensure_list(passwords)]
    extra_formats_list = [str(f) for f in _ensure_list(extra_formats) if str(f).strip()]

    # 获取 agent 上下文与配置
    agent_name, conversation_id, agent_self = _get_agent_context()
    pdf_cfg = resolve_pdf_config(agent_name)
    if not pdf_cfg or not pdf_cfg.get("providers"):
        return {
            "success": False,
            "error": f"Agent '{agent_name}' has no pdf providers configured. Cannot parse PDF.",
        }

    # ===== action: list =====
    if action == "list":
        return _handle_list(pdf_cfg)

    # ===== action: status =====
    if action == "status":
        return _handle_status(conversation_id)

    # ===== action: parse =====
    if not pdfs_list:
        return {"success": False, "error": "Parameter 'pdfs' is required for action='parse'"}

    chain = _build_provider_chain(provider or None, pdf_cfg)
    if not chain:
        return {"success": False, "error": "No pdf provider available. Check pdf configuration (primary/fallbacks/providers)."}

    params: Dict[str, Any] = {
        "is_ocr": bool(is_ocr),
        "enable_formula": bool(enable_formula),
        "enable_table": bool(enable_table),
        "language": language or "ch",
        "extra_formats": extra_formats_list,
        "page_ranges": page_ranges or "",
    }

    # 获取工作区路径
    workspace_path = get_current_workspace()
    if not workspace_path and agent_self:
        try:
            workspace_path = await agent_self.ensure_workspace(conversation_id)
        except Exception:
            pass
    if not workspace_path:
        workspace_path = os.getcwd()

    # ===== 后台模式 =====
    if background:
        job_id = f"pdf_{uuid.uuid4().hex[:8]}"
        tool_call_id = current_tool_call_id.get()
        _active_jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "conversation_id": conversation_id,
            "agent_name": agent_name,
            "provider": chain[0]["name"],
            "files": len(pdfs_list),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "started_at": None,
            "completed_at": None,
            "result": None,
        }
        asyncio.create_task(
            _run_pdf_parse(
                job_id=job_id,
                conversation_id=conversation_id,
                agent_name=agent_name,
                chain=chain,
                pdfs=pdfs_list,
                passwords=passwords_list,
                params=params,
                workspace_path=workspace_path,
                timeout_ms=timeoutMs,
                tool_call_id=tool_call_id,
                agent_self=agent_self,
            )
        )
        return {
            "success": True,
            "status": "started",
            "job_id": job_id,
            "message": f"PDF 解析任务已提交后台执行（提供商: {chain[0]['name']}，文件数: {len(pdfs_list)}）。完成后将自动推送到会话界面。",
            "provider": chain[0]["name"],
            "available_providers": [e["name"] for e in chain],
        }

    # ===== 同步模式（默认）=====
    result = await _execute_chain(chain, pdfs_list, passwords_list, params, workspace_path, timeoutMs)
    if result.get("success"):
        result["message"] = f"PDF 解析完成（提供商: {result.get('provider')}，产出 {len(result.get('files', []))} 个文件）"
    return result


def _handle_list(pdf_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """列出可用的 PDF 解析提供商与限额"""
    providers_cfg = pdf_cfg.get("providers", {}) or {}
    primary = pdf_cfg.get("primary") or pdf_cfg.get("provider")
    fallbacks = pdf_cfg.get("fallbacks", []) or []

    providers = []
    for name, cfg in providers_cfg.items():
        prov = get_provider(name)
        if name == primary:
            role = "primary"
        elif name in fallbacks:
            role = "fallback"
        else:
            role = "available"
        providers.append({
            "name": name,
            "role": role,
            "registered": prov is not None,
            "supports_ocr": bool(getattr(prov, "supports_ocr", False)) if prov else False,
            "supports_password": bool(getattr(prov, "supports_password", False)) if prov else False,
            "supported_extra_formats": list(getattr(prov, "supported_extra_formats", ())) if prov else [],
            "pdf_max_mb": (cfg or {}).get("pdf_max_mb"),
            "pdf_max_pages": (cfg or {}).get("pdf_max_pages"),
        })

    return {
        "success": True,
        "primary": primary,
        "fallbacks": fallbacks,
        "supported_provider_types": supported_provider_types(),
        "providers": providers,
    }


def _handle_status(conversation_id: str) -> Dict[str, Any]:
    """查询当前会话的活动 PDF 解析任务"""
    jobs = []
    for jid, info in _active_jobs.items():
        if info.get("conversation_id") == conversation_id:
            job_data = {
                "job_id": jid,
                "status": info["status"],
                "provider": info.get("provider"),
                "files": info.get("files"),
                "created_at": info.get("created_at"),
            }
            if info.get("completed_at"):
                job_data["completed_at"] = info["completed_at"]
            if info.get("result"):
                job_data["result"] = info["result"]
            jobs.append(job_data)

    return {
        "success": True,
        "conversation_id": conversation_id,
        "active_jobs": len([j for j in jobs if j["status"] in ("pending", "running")]),
        "jobs": jobs,
    }


# ===== 工具注册 =====

_PDF_PARSE_PARAMS = {
    "type": "object",
    "properties": {
        "pdfs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of PDF references: remote http(s) URLs, local absolute paths, or session file URLs (/api/v1/chat/files/...). Required for action='parse'.",
        },
        "provider": {
            "type": "string",
            "description": "Force a specific provider (e.g. 'mineru', 'local'). When set, this provider is used exclusively without fallback.",
        },
        "is_ocr": {
            "type": "boolean",
            "description": "Enable OCR for scanned/image-based PDFs. Supported by both mineru and local (pymupdf4llm). Default false.",
        },
        "enable_formula": {
            "type": "boolean",
            "description": "Enable formula recognition (mineru only). Default true.",
        },
        "enable_table": {
            "type": "boolean",
            "description": "Enable table recognition (mineru only). Default true.",
        },
        "language": {
            "type": "string",
            "description": "Document language hint, e.g. 'ch' (default), 'en', 'japan'.",
        },
        "extra_formats": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extra export formats. mineru supports docx/html/latex (markdown+json always produced); local supports json/txt (markdown always produced). Unsupported entries are dropped and reported in ignored_overrides.",
        },
        "page_ranges": {
            "type": "string",
            "description": "Page range, comma-separated. e.g. '2,4-6' = pages [2,4,5,6]; '2--2' = page 2 to second-to-last. Empty = all pages.",
        },
        "passwords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Per-file PDF passwords, aligned with pdfs by index (local provider only).",
        },
        "background": {
            "type": "boolean",
            "description": "Run in background and push result via event when done. Default false (synchronous).",
        },
        "timeoutMs": {
            "type": "number",
            "description": "Optional provider operation timeout in milliseconds (default 300000).",
        },
        "action": {
            "type": "string",
            "enum": ["parse", "list", "status"],
            "description": "'list' returns available providers and limits; 'status' returns current session tasks; 'parse' parses the PDFs. Defaults to 'parse'.",
        },
    },
    "required": [],
}


def register_pdf_parse_tool():
    """注册 pdf_parse 工具到全局注册表"""
    tool_registry.register(
        name="pdf_parse",
        description=(
            "Parse PDF documents into markdown/structured files using configured providers "
            "(mineru precise parsing with OCR/formula/table, or local pymupdf4llm offline with OCR/markdown/json/txt). "
            "Provider order: 'provider' param (forced) -> agent pdf.primary -> pdf.fallbacks. "
            "Results are saved to the conversation workspace and returned as downloadable files. "
            "Use action='list' to see available providers, action='status' to check running tasks. "
            "Set background=true for large documents to avoid blocking."
        ),
        handler=pdf_parse,
        parameters=_PDF_PARSE_PARAMS,
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=600,
        metadata={"system_tool": False},
    )
    logger.info("pdf_parse tool registered")
