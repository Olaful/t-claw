"""本地 PDF 解析 Provider - 基于 pymupdf4llm

pymupdf4llm 在 pymupdf 之上提供更强的版面分析、表格识别与 OCR 能力：
- 导出格式：markdown（full.md，默认）、json（full.json）、txt（full.txt）
- 图片：markdown 转换时 write_images=True，图片存于 out_dir/images，
  并把 md 中的引用改写为相对路径（images/xxx.png），保证产物自包含
- OCR：通过 use_ocr/force_ocr/ocr_language 支持扫描件识别（is_ocr=True 时启用）
- 加密 PDF：先用 pymupdf 打开并 authenticate 密码，再把 Document 交给 pymupdf4llm

远程 URL 引用会先下载到临时文件再解析。
"""
import asyncio
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import structlog

from .base import PdfParseRequest, PdfProviderBase, resolve_local_pdf_path
from .params import expand_page_ranges

logger = structlog.get_logger()

# 工具 language 参数（MinerU 风格）→ Tesseract OCR 语言码映射
_OCR_LANGUAGE_MAP: Dict[str, str] = {
    "auto": "eng",
    "ch": "chi_sim", "zh": "chi_sim", "chinese": "chi_sim", "chi_sim": "chi_sim",
    "chinese_cht": "chi_tra", "chi_tra": "chi_tra",
    "en": "eng", "english": "eng", "eng": "eng",
    "japan": "jpn", "ja": "jpn", "japanese": "jpn", "jpn": "jpn",
    "korean": "kor", "ko": "kor", "kor": "kor",
    "latin": "eng", "cyrillic": "rus", "arabic": "ara",
}


def _map_ocr_language(lang: Any) -> str:
    """将工具 language 参数映射为 Tesseract 语言码；未知值原样透传（可能已是合法码）"""
    if not lang:
        return "eng"
    key = str(lang).strip().lower()
    return _OCR_LANGUAGE_MAP.get(key, key)


# markdown 图片引用：![alt](ref)
_IMG_REF_RE = re.compile(r"(!\[[^\]]*\]\()([^)]+)(\))")
# Windows 路径非法字符（用于规范化目录名）
_ILLEGAL_WIN = re.compile(r'[<>:"/\\|?*]')


def _localize_image_refs(md_text: str, out_dir: str, img_subdir: str = "images") -> str:
    """将 markdown 中的图片引用改写为相对 out_dir 的 images/<basename>。

    pymupdf4llm 的 utils.md_path 生成的引用可能是“相对 CWD”“绝对路径”或
    含 8.3 短名，且不一定相对 md 文件本身。这里只取引用的 basename，并在
    out_dir/images 下确认文件存在后改写为 images/<basename>，使产物自包含且
    与引用原始形态无关；data URI / 外部 URL / 找不到对应文件的引用保持原样。
    """
    if not md_text:
        return md_text
    img_dir = os.path.join(out_dir, img_subdir)

    def _repl(m: "re.Match") -> str:
        ref = m.group(2)
        if ref.startswith(("data:", "http://", "https://", "//")):
            return m.group(0)
        name = os.path.basename(ref.replace("/", os.sep))
        if name and os.path.isfile(os.path.join(img_dir, name)):
            return m.group(1) + f"{img_subdir}/{name}" + m.group(3)
        return m.group(0)

    return _IMG_REF_RE.sub(_repl, md_text)


class LocalPdfProvider(PdfProviderBase):
    """pymupdf4llm 本地解析（离线，支持 OCR / markdown / json / txt / 密码）"""

    provider_type = "local"
    supports_ocr = True
    supports_password = True
    supports_formula = False
    supports_table = True
    supported_extra_formats = ("json", "txt")
    default_extra_formats = ("markdown",)

    async def parse(self, request: PdfParseRequest) -> Dict[str, Any]:
        try:
            import pymupdf  # noqa: F401
            import pymupdf4llm  # noqa: F401
        except ImportError:
            return {
                "success": False,
                "error": "pymupdf4llm 未安装，无法使用 local 解析。请执行: pip install pymupdf4llm",
            }

        if not request.pdfs:
            return {"success": False, "error": "没有可解析的 PDF 文件"}

        workspace = request.workspace_path or os.getcwd()
        params = request.params or {}
        max_mb = (request.provider_cfg or {}).get("pdf_max_mb")

        files: List[Dict[str, Any]] = []
        errors: List[str] = []
        temp_downloads: List[str] = []

        for idx, ref in enumerate(request.pdfs):
            local_path = resolve_local_pdf_path(ref)
            # 远程 URL：下载到临时文件后解析
            if not local_path and str(ref).startswith(("http://", "https://")):
                local_path = await self._download_to_temp(ref, temp_downloads)
            if not local_path:
                errors.append(f"{ref}: 文件不存在或无法访问")
                continue

            # 大小校验
            if isinstance(max_mb, (int, float)):
                try:
                    size_mb = os.path.getsize(local_path) / (1024 * 1024)
                except OSError:
                    size_mb = 0
                if size_mb > max_mb:
                    errors.append(
                        f"{os.path.basename(local_path)}: 文件大小 {size_mb:.1f}MB 超过限制 {max_mb}MB"
                    )
                    continue

            try:
                produced = await asyncio.to_thread(
                    self._parse_one, local_path, request.password_for(idx), params, workspace,
                )
                files.extend(produced)
            except Exception as e:
                errors.append(f"{os.path.basename(local_path)}: {str(e)}")
                logger.warning("Local PDF parse failed", ref=ref, error=str(e))

        # 清理临时下载文件
        for tp in temp_downloads:
            try:
                os.remove(tp)
            except OSError:
                pass

        if not files:
            return {"success": False, "error": "; ".join(errors) or "本地解析失败"}
        result: Dict[str, Any] = {"success": True, "files": files}
        if errors:
            result["partial_errors"] = errors
        return result

    # ===== 同步解析（在线程池执行）=====

    def _parse_one(
        self, path: str, password: str, params: Dict[str, Any], workspace: str,
    ) -> List[Dict[str, Any]]:
        import pymupdf
        import pymupdf4llm

        is_ocr = bool(params.get("is_ocr"))
        ocr_language = _map_ocr_language(params.get("language"))
        page_ranges = params.get("page_ranges") or ""
        extra_formats = [str(f).strip().lower() for f in (params.get("extra_formats") or [])]

        # 从字节流打开：使 doc.name 为空，从而让 pymupdf4llm 采用我们传入的短 filename
        # 命名导出图片（否则图片名 = 源 PDF 的超长文件名，叠加深层 out_dir 会突破
        # Windows MAX_PATH，令 pix.save 抛 "code=2: cannot open file"）。
        with open(path, "rb") as f:
            pdf_bytes = f.read()
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            if getattr(doc, "needs_pass", False) or getattr(doc, "is_encrypted", False):
                if password:
                    if not doc.authenticate(password):
                        raise ValueError("PDF 密码错误")
                else:
                    raise ValueError("PDF 已加密，需要提供密码")

            total = doc.page_count
            indices = expand_page_ranges(page_ranges, total)
            if indices is None:
                indices = list(range(total))
            indices = [i for i in indices if 0 <= i < total]
            # pymupdf4llm 的 pages 参数为 0-based 页索引列表；None 表示全部页
            pages_arg: Optional[List[int]] = indices or None

            # OCR 选项：is_ocr=True 时对需要的页启用 OCR（use_ocr），并指定语言
            ocr_kwargs: Dict[str, Any] = {"use_ocr": is_ocr, "force_ocr": False}
            if is_ocr:
                ocr_kwargs["ocr_language"] = ocr_language

            base = Path(path).stem or "document"
            # 截断+规范化 base，避免超长 PDF 文件名将 out_dir 撑破 Windows MAX_PATH
            short_base = _ILLEGAL_WIN.sub("_", base)[:24].rstrip(". ") or "document"
            out_dir = os.path.join(workspace, "parsed_pdfs", f"{short_base}_{uuid.uuid4().hex[:6]}")
            os.makedirs(out_dir, exist_ok=True)

            produced: List[Dict[str, Any]] = []

            # markdown（默认产物）：导出图片并保证 md 引用可解析。
            # 图片先写入“短绝对路径”的临时目录：pymupdf4llm 内部 pix.save 用的是
            # utils.md_path 计算的引用——当目标在 CWD 之下时它是“相对 CWD”路径，
            # 在深层工作区下会触发 "code=2: cannot open file"(ENOENT)。临时目录与 CWD
            # 不同根，md_path 会给出绝对引用，pix.save 稳定可靠；之后再把图片搬进
            # out_dir/images 并按 basename 改写 md 引用。
            img_dir = os.path.join(out_dir, "images")
            tmp_img_dir = tempfile.mkdtemp(prefix="pdfimg_")
            try:
                md_kwargs = dict(ocr_kwargs)
                # filename="doc" → 图片名形如 doc-0001-00.png（短），规避超长路径
                md_kwargs.update(write_images=True, image_path=tmp_img_dir,
                                 image_format="png", filename="doc")
                md_text = pymupdf4llm.to_markdown(doc, pages=pages_arg, **md_kwargs)
                self._relocate_images(tmp_img_dir, img_dir)
            finally:
                shutil.rmtree(tmp_img_dir, ignore_errors=True)
            produced.append(self._write_output(
                out_dir, "full.md", "markdown",
                _localize_image_refs(md_text, out_dir),
            ))
            # json（额外产物）
            if "json" in extra_formats:
                produced.append(self._write_output(
                    out_dir, "full.json", "json",
                    pymupdf4llm.to_json(doc, pages=pages_arg, **ocr_kwargs),
                ))
            # txt（额外产物）
            if "txt" in extra_formats:
                produced.append(self._write_output(
                    out_dir, "full.txt", "txt",
                    pymupdf4llm.to_text(doc, pages=pages_arg, **ocr_kwargs),
                ))

            logger.info(
                "Local PDF parsed", base=base, pages=len(indices),
                ocr=is_ocr, formats=[p["format"] for p in produced], out_dir=out_dir,
            )
            return produced
        finally:
            doc.close()

    @staticmethod
    def _write_output(out_dir: str, filename: str, fmt: str, content: Any) -> Dict[str, Any]:
        """将解析文本落地为文件，返回统一文件描述"""
        if not isinstance(content, str):
            content = str(content)
        full = os.path.join(out_dir, filename)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return {
            "filename": filename,
            "path": full,
            "format": fmt,
            "size": os.path.getsize(full),
        }

    @staticmethod
    def _relocate_images(src_dir: str, dst_dir: str) -> int:
        """把 pymupdf4llm 写入临时目录的图片搬到 out_dir/images，返回搬运数量。"""
        try:
            names = os.listdir(src_dir)
        except OSError:
            return 0
        moved = 0
        for name in names:
            src = os.path.join(src_dir, name)
            if not os.path.isfile(src):
                continue
            os.makedirs(dst_dir, exist_ok=True)
            shutil.move(src, os.path.join(dst_dir, name))
            moved += 1
        return moved

    async def _download_to_temp(self, url: str, temp_downloads: List[str]) -> Optional[str]:
        """下载远程 PDF 到临时文件，返回路径（失败返回 None）"""
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
            suffix = Path(url.split("?")[0]).suffix or ".pdf"
            fd, tmp = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as f:
                f.write(resp.content)
            temp_downloads.append(tmp)
            return tmp
        except Exception as e:
            logger.warning("Failed to download PDF for local parse", url=url, error=str(e))
            return None
