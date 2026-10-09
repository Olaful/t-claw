"""MinerU 精准解析 API Provider

文档: https://mineru.net/apiManage/docs （🎯 精准解析 API）
- 远程 URL: POST /api/v4/extract/task（单）或 /api/v4/extract/task/batch（多，≤50）
- 本地文件: POST /api/v4/file-urls/batch 申请上传链接 → PUT 上传（无 Content-Type）
- 轮询: GET /api/v4/extract/task/{task_id}（单）或 /api/v4/extract-results/batch/{batch_id}（批量）
- 结果: 下载 full_zip_url 解压，收集 markdown/json/docx/html/latex 产物
"""
import asyncio
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog

from .base import PdfParseRequest, PdfProviderBase, resolve_local_pdf_path

logger = structlog.get_logger()

_MINERU_BASE = "https://mineru.net/api/v4"
_POLL_INTERVAL = 3.0

# 错误码 -> 可读信息
_ERROR_MESSAGES: Dict[str, str] = {
    "A0202": "Token 错误（请检查 MINERU_API_KEY 是否正确）",
    "A0211": "Token 已过期（请更换新 Token）",
    "-500": "传参错误（请确保参数类型及 Content-Type 正确）",
    "-60001": "生成上传 URL 失败，请稍后再试",
    "-60002": "获取匹配的文件格式失败（请确保文件名/链接含正确后缀）",
    "-60003": "文件读取失败（文件可能已损坏）",
    "-60004": "空文件",
    "-60005": "文件大小超出限制（最大 200MB）",
    "-60006": "文件页数超过限制（最大 200 页）",
    "-60007": "模型服务暂时不可用，请稍后重试",
    "-60008": "文件读取超时（请检查 URL 可访问）",
    "-60009": "任务提交队列已满，请稍后再试",
    "-60010": "解析失败，请稍后再试",
    "-60018": "每日解析任务数量已达上限，明日再来",
}

# 收集产物的扩展名 -> format 标签（仅收集文档类，图片保留在磁盘但不列入 files）
_FORMAT_BY_EXT: Dict[str, str] = {
    ".md": "markdown",
    ".json": "json",
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
    ".tex": "latex",
    ".latex": "latex",
}

# Windows 文件名非法字符
_ILLEGAL_WIN_CHARS = re.compile(r'[<>:"|?*]')


def _sanitize_zip_member(name: str) -> Optional[str]:
    """规范化 zip 成员名，返回安全的相对路径（'/' 分隔）；无效则返回 None。

    处理：统一分隔符、过滤 '.'/'..' 防目录穿越、去除空段、
    替换 Windows 非法字符、剔除结尾的点/空格（Windows 不允许）。
    """
    if not name:
        return None
    name = name.replace("\\", "/")
    parts: List[str] = []
    for seg in name.split("/"):
        if seg in ("", ".", ".."):
            continue
        seg = _ILLEGAL_WIN_CHARS.sub("_", seg)
        seg = seg.rstrip(". ")
        if not seg:
            continue
        parts.append(seg)
    if not parts:
        return None
    return "/".join(parts)


def _win_long_path(p: str) -> str:
    r"""Windows 下返回带 \\?\ 前缀的绝对路径以支持超长路径（>260 字符）；其他平台原样返回。"""
    if os.name == "nt" and p:
        ap = os.path.abspath(p)
        if ap.startswith("\\\\?\\"):
            return ap
        return "\\\\?\\" + ap
    return p


def _strip_win_prefix(p: str) -> str:
    r"""去掉 Windows 长路径前缀 \\?\，返回可对外暴露的常规路径。"""
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


class MineruPdfProvider(PdfProviderBase):
    """MinerU 精准解析（支持 OCR / 公式 / 表格，不支持密码）"""

    provider_type = "mineru"
    supports_ocr = True
    supports_password = False
    supports_formula = True
    supports_table = True
    supported_extra_formats = ("docx", "html", "latex")
    default_extra_formats = ("markdown", "json")

    # ===== 主流程 =====

    async def parse(self, request: PdfParseRequest) -> Dict[str, Any]:
        cfg = request.provider_cfg or {}
        api_key = str(cfg.get("api_key") or "").strip()
        if not api_key:
            return {"success": False, "error": "MinerU api_key 未配置（请设置环境变量 MINERU_API_KEY）"}
        if not request.pdfs:
            return {"success": False, "error": "没有可解析的 PDF 文件"}

        headers = {"Authorization": f"Bearer {api_key}"}
        params = request.params or {}
        workspace = request.workspace_path or os.getcwd()
        timeout_s = max(30, int((request.timeout_ms or 300000) / 1000))
        common = self._build_common_params(params, cfg)

        url_refs = [r for r in request.pdfs if str(r).startswith(("http://", "https://"))]
        local_refs = [r for r in request.pdfs if not str(r).startswith(("http://", "https://"))]

        files: List[Dict[str, Any]] = []
        errors: List[str] = []

        timeout = httpx.Timeout(60.0, read=180.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # ===== 远程 URL 组 =====
            if url_refs:
                if len(url_refs) == 1:
                    ok, val = await self._submit_single_url(client, headers, url_refs[0], common, params)
                    if not ok:
                        errors.append(f"{url_refs[0]}: {val}")
                    else:
                        res = await self._poll_single(client, headers, val, timeout_s)
                        collected, err = await self._handle_single_result(client, res, workspace, url_refs[0])
                        files.extend(collected)
                        if err:
                            errors.append(err)
                else:
                    ok, val = await self._submit_url_batch(client, headers, url_refs, common, params)
                    if not ok:
                        errors.append(f"URL 批量提交失败: {val}")
                    else:
                        results = await self._poll_batch(client, headers, val, timeout_s)
                        collected, errs = await self._handle_batch_results(client, results, workspace)
                        files.extend(collected)
                        errors.extend(errs)

            # ===== 本地文件组（按 batch_max_files 分块）=====
            if local_refs:
                batch_max = int(cfg.get("batch_max_files", 50) or 50)
                max_mb = cfg.get("pdf_max_mb")
                resolved: List[Tuple[str, str]] = []
                for ref in local_refs:
                    path = resolve_local_pdf_path(ref)
                    if not path:
                        errors.append(f"{ref}: 文件不存在或无法访问")
                        continue
                    if isinstance(max_mb, (int, float)):
                        try:
                            size_mb = os.path.getsize(path) / (1024 * 1024)
                        except OSError:
                            size_mb = 0
                        if size_mb > max_mb:
                            errors.append(
                                f"{os.path.basename(path)}: 文件大小 {size_mb:.1f}MB 超过限制 {max_mb}MB"
                            )
                            continue
                    resolved.append((path, os.path.basename(path)))

                for i in range(0, len(resolved), batch_max):
                    chunk = resolved[i:i + batch_max]
                    ok, val = await self._submit_local_batch(client, headers, chunk, common, params)
                    if not ok:
                        errors.append(f"本地文件批量提交失败: {val}")
                        continue
                    batch_id, upload_errs = val
                    errors.extend(upload_errs)
                    results = await self._poll_batch(client, headers, batch_id, timeout_s)
                    collected, errs = await self._handle_batch_results(client, results, workspace)
                    files.extend(collected)
                    errors.extend(errs)

        if not files:
            return {"success": False, "error": "; ".join(errors) or "MinerU 解析失败"}
        result: Dict[str, Any] = {"success": True, "files": files}
        if errors:
            result["partial_errors"] = errors
        return result

    # ===== 参数构建 =====

    def _build_common_params(self, params: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        """构建 MinerU 公共请求参数（顶层字段）"""
        common: Dict[str, Any] = {
            "model_version": cfg.get("model_version", "pipeline"),
            "is_ocr": bool(params.get("is_ocr", False)),
            "enable_formula": bool(params.get("enable_formula", True)),
            "enable_table": bool(params.get("enable_table", True)),
            "language": params.get("language") or "ch",
        }
        extra = params.get("extra_formats") or []
        if extra:
            common["extra_formats"] = list(extra)
        if cfg.get("no_cache") is not None:
            common["no_cache"] = bool(cfg.get("no_cache"))
        if cfg.get("cache_tolerance") is not None:
            common["cache_tolerance"] = int(cfg.get("cache_tolerance"))
        return common

    def _top_level(self, common: Dict[str, Any]) -> Dict[str, Any]:
        """批量接口顶层参数（is_ocr/page_ranges 为逐文件字段，需剔除 is_ocr）"""
        return {k: v for k, v in common.items() if k != "is_ocr"}

    def _map_error(self, code: Any, msg: Any) -> str:
        key = str(code)
        mapped = _ERROR_MESSAGES.get(key)
        if mapped:
            return f"{mapped}（code={key}）"
        return f"MinerU API 错误: {msg or '未知'}（code={key}）"

    # ===== 提交 =====

    async def _submit_single_url(
        self, client: httpx.AsyncClient, headers: Dict[str, str],
        url_ref: str, common: Dict[str, Any], params: Dict[str, Any],
    ) -> Tuple[bool, str]:
        body = dict(common)
        body["url"] = url_ref
        if params.get("page_ranges"):
            body["page_ranges"] = params["page_ranges"]
        try:
            resp = await client.post(f"{_MINERU_BASE}/extract/task", headers=headers, json=body)
            data = resp.json()
        except Exception as e:
            return False, f"提交异常: {str(e)}"
        if data.get("code") != 0:
            return False, self._map_error(data.get("code"), data.get("msg"))
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            return False, "响应未包含 task_id"
        return True, task_id

    async def _submit_url_batch(
        self, client: httpx.AsyncClient, headers: Dict[str, str],
        url_refs: List[str], common: Dict[str, Any], params: Dict[str, Any],
    ) -> Tuple[bool, str]:
        body = self._top_level(common)
        files_arr: List[Dict[str, Any]] = []
        for ref in url_refs:
            item: Dict[str, Any] = {"url": ref, "is_ocr": common.get("is_ocr", False)}
            if params.get("page_ranges"):
                item["page_ranges"] = params["page_ranges"]
            files_arr.append(item)
        body["files"] = files_arr
        try:
            resp = await client.post(f"{_MINERU_BASE}/extract/task/batch", headers=headers, json=body)
            data = resp.json()
        except Exception as e:
            return False, f"提交异常: {str(e)}"
        if data.get("code") != 0:
            return False, self._map_error(data.get("code"), data.get("msg"))
        batch_id = (data.get("data") or {}).get("batch_id")
        if not batch_id:
            return False, "响应未包含 batch_id"
        return True, batch_id

    async def _submit_local_batch(
        self, client: httpx.AsyncClient, headers: Dict[str, str],
        chunk: List[Tuple[str, str]], common: Dict[str, Any], params: Dict[str, Any],
    ) -> Tuple[bool, Any]:
        """申请上传链接并 PUT 上传本地文件。成功返回 (batch_id, upload_errs)"""
        body = self._top_level(common)
        files_arr: List[Dict[str, Any]] = []
        for _path, name in chunk:
            item: Dict[str, Any] = {
                "name": name,
                "data_id": uuid.uuid4().hex[:16],
                "is_ocr": common.get("is_ocr", False),
            }
            if params.get("page_ranges"):
                item["page_ranges"] = params["page_ranges"]
            files_arr.append(item)
        body["files"] = files_arr
        try:
            resp = await client.post(f"{_MINERU_BASE}/file-urls/batch", headers=headers, json=body)
            data = resp.json()
        except Exception as e:
            return False, f"申请上传链接异常: {str(e)}"
        if data.get("code") != 0:
            return False, self._map_error(data.get("code"), data.get("msg"))
        d = data.get("data") or {}
        batch_id = d.get("batch_id")
        file_urls = d.get("file_urls") or []
        if not batch_id or not file_urls:
            return False, "响应未包含 batch_id/file_urls"

        # PUT 上传（不设置 Content-Type / Authorization，使用签名 URL）
        upload_errs: List[str] = []
        for (path, name), up_url in zip(chunk, file_urls):
            try:
                # 读入字节后再上传：AsyncClient 不能直接流式发送同步文件对象，
                # 否则报 "Attempted to send an sync request with an AsyncClient instance"
                with open(path, "rb") as f:
                    payload = f.read()
                put_resp = await client.put(up_url, content=payload)
                if put_resp.status_code not in (200, 201):
                    upload_errs.append(f"{name}: 上传失败 HTTP {put_resp.status_code}")
            except Exception as e:
                upload_errs.append(f"{name}: 上传异常 {str(e)}")
                return False, (batch_id, upload_errs)
        return True, (batch_id, upload_errs)

    # ===== 轮询 =====

    async def _poll_single(
        self, client: httpx.AsyncClient, headers: Dict[str, str], task_id: str, timeout_s: int,
    ) -> Dict[str, Any]:
        loop = asyncio.get_event_loop()
        start = loop.time()
        while True:
            if loop.time() - start > timeout_s:
                return {"state": "failed", "err_msg": f"轮询超时（{timeout_s}s），task_id={task_id}"}
            try:
                resp = await client.get(f"{_MINERU_BASE}/extract/task/{task_id}", headers=headers)
                data = resp.json()
            except Exception as e:
                return {"state": "failed", "err_msg": f"查询异常: {str(e)}"}
            if data.get("code") != 0:
                return {"state": "failed", "err_msg": self._map_error(data.get("code"), data.get("msg"))}
            d = data.get("data") or {}
            state = d.get("state")
            if state in ("done", "failed"):
                return d
            await asyncio.sleep(_POLL_INTERVAL)

    async def _poll_batch(
        self, client: httpx.AsyncClient, headers: Dict[str, str], batch_id: str, timeout_s: int,
    ) -> List[Dict[str, Any]]:
        loop = asyncio.get_event_loop()
        start = loop.time()
        while True:
            if loop.time() - start > timeout_s:
                return [{"file_name": "?", "state": "failed", "err_msg": f"批量轮询超时（{timeout_s}s）"}]
            try:
                resp = await client.get(f"{_MINERU_BASE}/extract-results/batch/{batch_id}", headers=headers)
                data = resp.json()
            except Exception as e:
                return [{"file_name": "?", "state": "failed", "err_msg": f"查询异常: {str(e)}"}]
            if data.get("code") != 0:
                return [{"file_name": "?", "state": "failed",
                         "err_msg": self._map_error(data.get("code"), data.get("msg"))}]
            results = (data.get("data") or {}).get("extract_result") or []
            if results and all(r.get("state") in ("done", "failed") for r in results):
                return results
            await asyncio.sleep(_POLL_INTERVAL)

    # ===== 结果落地 =====

    async def _handle_single_result(
        self, client: httpx.AsyncClient, res: Dict[str, Any], workspace: str, url_ref: str,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if res.get("state") != "done":
            return [], f"{url_ref}: {res.get('err_msg') or '解析失败'}"
        zip_url = res.get("full_zip_url")
        if not zip_url:
            return [], f"{url_ref}: 无结果下载链接"
        base = Path(url_ref.split("?")[0]).stem or "document"
        try:
            files = await self._download_and_extract(client, zip_url, workspace, base)
        except Exception as e:
            return [], f"{url_ref}: 结果下载/解压失败 {str(e)}"
        return files, None

    async def _handle_batch_results(
        self, client: httpx.AsyncClient, results: List[Dict[str, Any]], workspace: str,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        files: List[Dict[str, Any]] = []
        errs: List[str] = []
        for r in results:
            name = r.get("file_name") or "document"
            if r.get("state") != "done":
                errs.append(f"{name}: {r.get('err_msg') or '解析失败'}")
                continue
            zip_url = r.get("full_zip_url")
            if not zip_url:
                errs.append(f"{name}: 无结果下载链接")
                continue
            base = Path(name).stem or "document"
            try:
                collected = await self._download_and_extract(client, zip_url, workspace, base)
                files.extend(collected)
            except Exception as e:
                errs.append(f"{name}: 结果下载/解压失败 {str(e)}")
        return files, errs

    async def _download_and_extract(
        self, client: httpx.AsyncClient, zip_url: str, workspace: str, base: str,
    ) -> List[Dict[str, Any]]:
        """下载 full_zip_url 并解压到 {workspace}/parsed_pdfs/{base}_{uuid6}/，收集文档产物"""
        resp = await client.get(zip_url)
        resp.raise_for_status()
        out_dir = os.path.join(workspace, "parsed_pdfs", f"{base}_{uuid.uuid4().hex[:6]}")
        os.makedirs(out_dir, exist_ok=True)
        fd, tmp_zip = tempfile.mkstemp(suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(resp.content)
            self._extract_zip(tmp_zip, out_dir)
        finally:
            try:
                os.remove(tmp_zip)
            except OSError:
                pass

        collected: List[Dict[str, Any]] = []
        # 用长路径安全的根遍历：文件可能是用 \\?\ 前缀写出的超长路径，
        # 直接用 out_dir 做 walk/getsize 会在 Windows 上报“找不到路径”。
        for root, _dirs, filenames in os.walk(_win_long_path(out_dir)):
            for fn in filenames:
                ext = Path(fn).suffix.lower()
                fmt = _FORMAT_BY_EXT.get(ext)
                if not fmt:
                    continue
                full = os.path.join(root, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                # 对外暴露去掉 \\?\ 前缀的常规路径，保证 relpath/url 计算正确
                clean_full = _strip_win_prefix(full)
                rel = os.path.relpath(clean_full, out_dir).replace("\\", "/")
                collected.append({
                    "filename": rel,
                    "path": clean_full,
                    "format": fmt,
                    "size": size,
                })
        # markdown 优先，其余按文件名排序
        collected.sort(key=lambda x: (0 if x["format"] == "markdown" else 1, x["filename"]))
        logger.info("MinerU result extracted", base=base, out_dir=out_dir, files=len(collected))
        return collected

    @staticmethod
    def _extract_zip(tmp_zip: str, out_dir: str) -> None:
        r"""逐个成员解压，容忍单个成员失败，全部失败时才抛错。

        直接用 zf.extractall 是“全有或全无”：只要有一个成员解压失败（如
        图片名过长触发 Windows MAX_PATH、或含非法字符），整个调用即中断，
        导致 out_dir 为空。这里改为逐成员解压：
        - 规范化成员名（防穿越 + 替换 Windows 非法字符）
        - Windows 下对目标根目录加 \\?\ 前缀以支持超长路径
        - 单个成员失败仅记录并继续，保证 md/json 等文档产物能落地
        """
        root = _win_long_path(out_dir)
        extracted = 0
        failures: List[str] = []
        with zipfile.ZipFile(tmp_zip) as zf:
            infos = zf.infolist()
            total = len(infos)
            for info in infos:
                if info.is_dir():
                    continue
                rel = _sanitize_zip_member(info.filename)
                if not rel:
                    continue
                target = os.path.join(root, rel.replace("/", os.sep))
                try:
                    parent = os.path.dirname(target)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted += 1
                except Exception as e:  # noqa: BLE001 - 单成员失败不中断整体解压
                    failures.append(f"{info.filename}: {e}")
        if failures:
            logger.warning(
                "MinerU zip partial extract failure",
                out_dir=out_dir, failed=len(failures), sample=failures[:5],
            )
        if extracted == 0:
            detail = "; ".join(failures[:5]) or "zip 内无可解压成员"
            raise RuntimeError(f"解压失败（0/{total} 个成员）：{detail}")
