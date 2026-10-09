"""PDF 解析 Provider 抽象基类与统一请求载体

架构参照 video_providers/、music_providers/ 包的模块化拆分模式：
- PdfParseRequest: 一次解析请求的统一载体（provider 配置 + pdf 引用列表 + 参数 + 工作区）
- PdfProviderBase: 抽象基类，声明能力（ocr/password/formula/table/extra_formats），
  prepare() 做能力协商（登记不支持的参数），parse() 由各 provider 实现
- resolve_local_pdf_path: provider 无关的本地路径解析共享函数
"""
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class PdfParseRequest:
    """一次 PDF 解析请求的统一载体

    orchestration 层（pdf_parse）构建该对象，Provider 可通过 prepare()
    登记不支持的参数，parse() 内部完成「文件解析 → 结果落地」的完整流程。
    """

    def __init__(
        self,
        provider_name: str,
        provider_cfg: Dict[str, Any],
        pdfs: Optional[List[str]] = None,
        passwords: Optional[List[str]] = None,
        params: Optional[Dict[str, Any]] = None,
        workspace_path: str = "",
        job_id: str = "",
        timeout_ms: int = 300000,
    ):
        self.provider_name = provider_name
        self.provider_cfg = provider_cfg or {}
        # pdf 引用列表：远程 URL / 本地绝对路径 / `/api/v1/chat/files/{rel}` 会话文件 URL
        self.pdfs = pdfs or []
        # 与 pdfs 一一对应的密码列表（可含空串占位）
        self.passwords = passwords or []
        # 已标准化的解析参数：is_ocr/enable_formula/enable_table/language/extra_formats/page_ranges
        self.params = params or {}
        self.workspace_path = workspace_path
        self.job_id = job_id
        self.timeout_ms = timeout_ms
        self.ignored_params: List[str] = []

    def add_ignored(self, tag: str) -> None:
        """登记被忽略/不支持的参数（去重）"""
        if tag not in self.ignored_params:
            self.ignored_params.append(tag)

    def password_for(self, index: int) -> str:
        """获取第 index 个 PDF 对应的密码（越界或空返回空串）"""
        if 0 <= index < len(self.passwords):
            return self.passwords[index] or ""
        return ""


class PdfProviderBase(ABC):
    """PDF 解析提供商抽象基类

    子类须声明 `provider_type`，并在 factory.py 的 `_PROVIDER_MAP` 中注册。

    parse() 统一返回结构:
    - 成功: {"success": True, "files": [{"filename","path","format","size"?}...], "usage"?: {...}}
             （files 中的 path 为已落地的本地绝对路径，url 由编排层 _save_pdf_outputs 补全）
    - 失败: {"success": False, "error": "..."}
    """

    provider_type: str = ""
    # ===== 能力声明（prepare 依据这些标志登记不支持的参数）=====
    supports_ocr: bool = False
    supports_password: bool = False
    supports_formula: bool = False
    supports_table: bool = False
    # 支持的额外导出格式（markdown/json 为默认产物，不在此列）
    supported_extra_formats: tuple = ()
    # 默认始终产出的格式（无需在 extra_formats 中显式声明）
    default_extra_formats: tuple = ("markdown", "json")

    def prepare(self, request: PdfParseRequest) -> None:
        """解析前的能力协商：登记不支持的参数到 request.ignored_params。

        默认实现按能力声明登记 is_ocr / passwords / enable_formula / enable_table。
        extra_formats 的裁剪与登记由 normalize_pdf_params 统一处理。
        子类可覆写以补充特有逻辑（应先调用 super().prepare(request)）。
        """
        params = request.params or {}
        if not self.supports_ocr and params.get("is_ocr"):
            request.add_ignored(f"{self.provider_type}:is_ocr not supported")
        if not self.supports_password and any(p for p in (request.passwords or [])):
            request.add_ignored(f"{self.provider_type}:passwords not supported")
        if not self.supports_formula and params.get("enable_formula"):
            request.add_ignored(f"{self.provider_type}:enable_formula not supported")
        if not self.supports_table and params.get("enable_table"):
            request.add_ignored(f"{self.provider_type}:enable_table not supported")

    @abstractmethod
    async def parse(self, request: PdfParseRequest) -> Dict[str, Any]:
        """执行 PDF 解析（文件读取 → 解析 → 结果落地），返回统一结构"""
        ...


def resolve_local_pdf_path(pdf_ref: str) -> Optional[str]:
    """将 PDF 引用解析为本地绝对路径（provider 无关的共享逻辑）

    - 远程 URL（http/https）→ 返回 None（由 provider 走 URL 提交分支）
    - `/api/v1/chat/files/{rel}` → 校验位于 WORKSPACE_ROOT 内后返回绝对路径
    - 其他（本地绝对路径 / 相对工作区路径）→ resolve_ws_path
    找不到文件时返回 None。
    """
    if not pdf_ref:
        return None
    if pdf_ref.startswith(("http://", "https://")):
        return None
    if pdf_ref.startswith("/api/v1/chat/files/"):
        rel = pdf_ref[len("/api/v1/chat/files/"):]
        try:
            from app.api.v1.chat import WORKSPACE_ROOT_PATH
            resolved = (WORKSPACE_ROOT_PATH / rel).resolve()
            if str(resolved).startswith(str(WORKSPACE_ROOT_PATH.resolve())) and os.path.isfile(str(resolved)):
                return str(resolved)
        except Exception:
            pass
        return None

    from app.core.workspace_isolation import resolve_ws_path
    candidate = resolve_ws_path(pdf_ref)
    if os.path.isfile(candidate):
        return candidate
    return None
