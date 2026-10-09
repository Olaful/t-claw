"""PDF 解析工具测试

覆盖：
- normalize_pdf_params: extra_formats 裁剪 / ignored 登记 / 数值 clamp
- expand_page_ranges: "2,4-6" / "2--2" 倒数页语义 / 越界裁剪 / 非法片段忽略
- _build_provider_chain: provider 强制 / primary+fallbacks 顺序 / provider 回退
- PdfProviderBase.prepare: 能力协商登记（local 忽略 ocr/formula/table；mineru 忽略 password）
- LocalPdfProvider.parse: fixtures PDF 端到端（markdown 产出 / page_ranges / txt / password）
- MineruPdfProvider: 无 api_key 报错 + _build_common_params/_map_error 单元（不联网）

运行方式: pytest tests/test_pdf_parse.py -v
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.tools.pdf_providers import (
    PdfParseRequest,
    normalize_pdf_params,
    expand_page_ranges,
    get_provider,
    supported_provider_types,
    MineruPdfProvider,
    LocalPdfProvider,
)
from app.tools.pdf_parse import _build_provider_chain


# ===== 工厂 =====

def test_factory_registers_both_providers():
    types = supported_provider_types()
    assert "mineru" in types
    assert "local" in types
    assert isinstance(get_provider("mineru"), MineruPdfProvider)
    assert isinstance(get_provider("local"), LocalPdfProvider)
    assert get_provider("nonexistent") is None


# ===== expand_page_ranges =====

class TestExpandPageRanges:
    def test_empty_returns_none(self):
        assert expand_page_ranges("", 10) is None
        assert expand_page_ranges(None, 10) is None
        assert expand_page_ranges("   ", 10) is None

    def test_single_and_range(self):
        # "2,4-6" → 页2 + 页4~6 → 0-based [1,3,4,5]
        assert expand_page_ranges("2,4-6", 10) == [1, 3, 4, 5]

    def test_negative_end_second_to_last(self):
        # "2--2" → 第2页到倒数第二页(total=10 → 第9页) → 0-based [1..8]
        assert expand_page_ranges("2--2", 10) == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_out_of_range_clipped(self):
        # 越界页码裁剪：total=3，请求 1-10 → [0,1,2]
        assert expand_page_ranges("1-10", 3) == [0, 1, 2]

    def test_invalid_fragment_ignored(self):
        # 非法片段忽略，合法片段保留
        assert expand_page_ranges("abc,2", 10) == [1]

    def test_dedup_and_sort(self):
        # 重复页去重并升序
        assert expand_page_ranges("5,1,5,1-2", 10) == [0, 1, 4]

    def test_zero_total_returns_empty(self):
        assert expand_page_ranges("1-3", 0) == []

    def test_reversed_range_normalized(self):
        # start > end 自动交换
        assert expand_page_ranges("6-4", 10) == [3, 4, 5]


# ===== normalize_pdf_params =====

class TestNormalizePdfParams:
    def test_extra_formats_mineru_filter(self):
        prov = MineruPdfProvider()
        params = {"extra_formats": ["markdown", "docx", "pdf", "html"]}
        norm, details, ignored = normalize_pdf_params(params, {}, prov)
        # markdown 为默认产物静默剔除；docx/html 支持保留；pdf 不支持登记 ignored
        assert norm["extra_formats"] == ["docx", "html"]
        assert "extra_formats=pdf(unsupported)" in ignored
        assert details["extra_formats"]["action"] == "filtered"
        assert details["extra_formats"]["applied"] == ["docx", "html"]

    def test_extra_formats_local_filter(self):
        prov = LocalPdfProvider()
        # local 支持 json/txt；docx 不支持应登记 ignored
        params = {"extra_formats": ["txt", "json", "docx"]}
        norm, details, ignored = normalize_pdf_params(params, {}, prov)
        assert norm["extra_formats"] == ["txt", "json"]
        assert "extra_formats=docx(unsupported)" in ignored

    def test_extra_formats_all_default_no_normalization_entry(self):
        prov = MineruPdfProvider()
        # 仅请求默认产物：applied 为空，且 requested_norm != applied 会记录 normalization
        norm, details, ignored = normalize_pdf_params({"extra_formats": ["markdown", "json"]}, {}, prov)
        assert norm["extra_formats"] == []
        assert ignored == []

    def test_numeric_clamp(self):
        prov = MineruPdfProvider()
        params = {"max_pages": 500, "max_mb": 100}
        cfg = {"pdf_max_pages": 200, "pdf_max_mb": 200}
        norm, details, ignored = normalize_pdf_params(params, cfg, prov)
        assert norm["max_pages"] == 200  # clamped
        assert norm["max_mb"] == 100  # 未超限，保持
        assert details["max_pages"]["action"] == "clamped"
        assert details["max_pages"]["requested"] == 500
        assert details["max_pages"]["applied"] == 200
        assert "max_mb" not in details

    def test_page_ranges_passthrough(self):
        norm, details, ignored = normalize_pdf_params({"page_ranges": "2,4-6"}, {}, None)
        assert norm["page_ranges"] == "2,4-6"
        assert details == {}
        assert ignored == []


# ===== _build_provider_chain =====

class TestBuildProviderChain:
    CFG = {
        "primary": "mineru",
        "fallbacks": ["local"],
        "providers": {
            "mineru": {"api_key": "k", "pdf_max_mb": 200},
            "local": {"pdf_max_mb": 10},
        },
    }

    def test_forced_provider_single_no_fallback(self):
        chain = _build_provider_chain("local", self.CFG)
        assert len(chain) == 1
        assert chain[0]["name"] == "local"
        assert chain[0]["force"] is True
        assert chain[0]["cfg"]["pdf_max_mb"] == 10

    def test_primary_then_fallbacks_order(self):
        chain = _build_provider_chain(None, self.CFG)
        assert [e["name"] for e in chain] == ["mineru", "local"]
        assert all(e["force"] is False for e in chain)

    def test_fallback_equal_primary_skipped(self):
        cfg = {"primary": "mineru", "fallbacks": ["mineru", "local"], "providers": self.CFG["providers"]}
        chain = _build_provider_chain(None, cfg)
        assert [e["name"] for e in chain] == ["mineru", "local"]

    def test_primary_falls_back_to_provider_key(self):
        cfg = {"provider": "local", "providers": self.CFG["providers"]}
        chain = _build_provider_chain(None, cfg)
        assert chain[0]["name"] == "local"

    def test_empty_config(self):
        assert _build_provider_chain(None, {"providers": {}}) == []


# ===== prepare 能力协商 =====

class TestPrepareCapabilityNegotiation:
    def test_local_ignores_formula_only(self):
        # pymupdf4llm 支持 OCR / 表格 / 密码，仅公式识别不支持
        prov = LocalPdfProvider()
        req = PdfParseRequest(
            provider_name="local", provider_cfg={},
            params={"is_ocr": True, "enable_formula": True, "enable_table": True},
            passwords=["secret"],
        )
        prov.prepare(req)
        joined = " ".join(req.ignored_params)
        assert "enable_formula not supported" in joined
        assert "is_ocr not supported" not in joined
        assert "enable_table not supported" not in joined
        assert "passwords not supported" not in joined

    def test_mineru_ignores_password_not_ocr(self):
        prov = MineruPdfProvider()
        req = PdfParseRequest(
            provider_name="mineru", provider_cfg={},
            params={"is_ocr": True, "enable_formula": True},
            passwords=["secret"],
        )
        prov.prepare(req)
        joined = " ".join(req.ignored_params)
        assert "passwords not supported" in joined
        assert "is_ocr not supported" not in joined

    def test_add_ignored_dedup(self):
        req = PdfParseRequest(provider_name="local", provider_cfg={})
        req.add_ignored("x")
        req.add_ignored("x")
        req.add_ignored("y")
        assert req.ignored_params == ["x", "y"]

    def test_password_for_out_of_range(self):
        req = PdfParseRequest(provider_name="local", provider_cfg={}, passwords=["a"])
        assert req.password_for(0) == "a"
        assert req.password_for(5) == ""


# ===== MinerU 单元（不联网）=====

class TestMineruUnits:
    def test_build_common_params(self):
        prov = MineruPdfProvider()
        params = {"is_ocr": True, "enable_formula": False, "enable_table": True,
                  "language": "en", "extra_formats": ["docx"]}
        cfg = {"model_version": "vlm", "no_cache": True, "cache_tolerance": 600}
        common = prov._build_common_params(params, cfg)
        assert common["model_version"] == "vlm"
        assert common["is_ocr"] is True
        assert common["enable_formula"] is False
        assert common["language"] == "en"
        assert common["extra_formats"] == ["docx"]
        assert common["no_cache"] is True
        assert common["cache_tolerance"] == 600

    def test_top_level_drops_is_ocr(self):
        prov = MineruPdfProvider()
        top = prov._top_level({"is_ocr": True, "model_version": "pipeline"})
        assert "is_ocr" not in top
        assert top["model_version"] == "pipeline"

    def test_map_error_known_and_unknown(self):
        prov = MineruPdfProvider()
        assert "Token 错误" in prov._map_error("A0202", "bad")
        assert "code=A0202" in prov._map_error("A0202", "bad")
        unknown = prov._map_error("99999", "boom")
        assert "boom" in unknown and "code=99999" in unknown

    @pytest.mark.asyncio
    async def test_parse_without_api_key(self):
        prov = MineruPdfProvider()
        req = PdfParseRequest(provider_name="mineru", provider_cfg={}, pdfs=["http://x/y.pdf"])
        res = await prov.parse(req)
        assert res["success"] is False
        assert "api_key" in res["error"]

    @pytest.mark.asyncio
    async def test_parse_without_pdfs(self):
        prov = MineruPdfProvider()
        req = PdfParseRequest(provider_name="mineru", provider_cfg={"api_key": "k"}, pdfs=[])
        res = await prov.parse(req)
        assert res["success"] is False


# ===== LocalPdfProvider 端到端（fixtures PDF）=====

def _make_pdf(path: str, pages: list, password: str = None):
    """用 pymupdf 生成一个含指定页文本的 PDF；password 非空则加密。"""
    import pymupdf
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    if password:
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 user_pw=password, owner_pw=password)
    else:
        doc.save(path)
    doc.close()


@pytest.fixture
def fitz_available():
    try:
        import pymupdf  # noqa: F401
        import pymupdf4llm  # noqa: F401
        return True
    except ImportError:
        pytest.skip("pymupdf4llm 未安装")


class TestLocalProviderParse:
    @pytest.mark.asyncio
    async def test_parse_produces_markdown(self, tmp_path, fitz_available):
        pdf = str(tmp_path / "doc.pdf")
        _make_pdf(pdf, ["Hello Page One", "Second Page Body", "Third Page Tail"])
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace, exist_ok=True)

        prov = LocalPdfProvider()
        req = PdfParseRequest(
            provider_name="local", provider_cfg={}, pdfs=[pdf],
            params={"page_ranges": "", "extra_formats": []}, workspace_path=workspace,
        )
        res = await prov.parse(req)
        assert res["success"] is True, res.get("error")
        files = res["files"]
        assert len(files) == 1
        md = files[0]
        assert md["filename"] == "full.md"
        assert md["format"] == "markdown"
        assert os.path.isfile(md["path"])
        content = Path(md["path"]).read_text(encoding="utf-8")
        assert "Hello Page One" in content
        assert "Third Page Tail" in content

    @pytest.mark.asyncio
    async def test_parse_page_ranges(self, tmp_path, fitz_available):
        pdf = str(tmp_path / "doc.pdf")
        _make_pdf(pdf, ["P1 content", "P2 content", "P3 content"])
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace, exist_ok=True)

        prov = LocalPdfProvider()
        req = PdfParseRequest(
            provider_name="local", provider_cfg={}, pdfs=[pdf],
            params={"page_ranges": "1-2", "extra_formats": []}, workspace_path=workspace,
        )
        res = await prov.parse(req)
        assert res["success"] is True
        content = Path(res["files"][0]["path"]).read_text(encoding="utf-8")
        assert "P1 content" in content
        assert "P2 content" in content
        assert "P3 content" not in content

    @pytest.mark.asyncio
    async def test_parse_extra_txt(self, tmp_path, fitz_available):
        pdf = str(tmp_path / "doc.pdf")
        _make_pdf(pdf, ["Some text"])
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace, exist_ok=True)

        prov = LocalPdfProvider()
        req = PdfParseRequest(
            provider_name="local", provider_cfg={}, pdfs=[pdf],
            params={"page_ranges": "", "extra_formats": ["txt"]}, workspace_path=workspace,
        )
        res = await prov.parse(req)
        assert res["success"] is True
        fmts = {f["format"] for f in res["files"]}
        assert fmts == {"markdown", "txt"}

    @pytest.mark.asyncio
    async def test_parse_extra_json(self, tmp_path, fitz_available):
        pdf = str(tmp_path / "doc.pdf")
        _make_pdf(pdf, ["JSON body content"])
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace, exist_ok=True)

        prov = LocalPdfProvider()
        req = PdfParseRequest(
            provider_name="local", provider_cfg={}, pdfs=[pdf],
            params={"page_ranges": "", "extra_formats": ["json", "txt"]}, workspace_path=workspace,
        )
        res = await prov.parse(req)
        assert res["success"] is True
        fmts = {f["format"] for f in res["files"]}
        assert fmts == {"markdown", "json", "txt"}
        # json 产物应为合法 JSON
        import json as _json
        json_file = next(f for f in res["files"] if f["format"] == "json")
        parsed = _json.loads(Path(json_file["path"]).read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        assert parsed.get("page_count") == 1

    @pytest.mark.asyncio
    async def test_parse_encrypted_requires_password(self, tmp_path, fitz_available):
        pdf = str(tmp_path / "secret.pdf")
        _make_pdf(pdf, ["Top secret"], password="s3cret")
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace, exist_ok=True)

        prov = LocalPdfProvider()
        # 无密码 → 失败
        req_nopw = PdfParseRequest(
            provider_name="local", provider_cfg={}, pdfs=[pdf],
            params={"page_ranges": "", "extra_formats": []}, workspace_path=workspace,
        )
        res = await prov.parse(req_nopw)
        assert res["success"] is False

        # 正确密码 → 成功
        req_pw = PdfParseRequest(
            provider_name="local", provider_cfg={}, pdfs=[pdf], passwords=["s3cret"],
            params={"page_ranges": "", "extra_formats": []}, workspace_path=workspace,
        )
        res2 = await prov.parse(req_pw)
        assert res2["success"] is True, res2.get("error")
        content = Path(res2["files"][0]["path"]).read_text(encoding="utf-8")
        assert "Top secret" in content

    @pytest.mark.asyncio
    async def test_parse_missing_file(self, tmp_path, fitz_available):
        prov = LocalPdfProvider()
        req = PdfParseRequest(
            provider_name="local", provider_cfg={}, pdfs=[str(tmp_path / "nope.pdf")],
            params={"page_ranges": "", "extra_formats": []}, workspace_path=str(tmp_path),
        )
        res = await prov.parse(req)
        assert res["success"] is False

    @pytest.mark.asyncio
    async def test_parse_size_limit(self, tmp_path, fitz_available):
        pdf = str(tmp_path / "doc.pdf")
        _make_pdf(pdf, ["content"])
        prov = LocalPdfProvider()
        # pdf_max_mb=0 → 任何文件都超限
        req = PdfParseRequest(
            provider_name="local", provider_cfg={"pdf_max_mb": 0}, pdfs=[pdf],
            params={"page_ranges": "", "extra_formats": []}, workspace_path=str(tmp_path),
        )
        res = await prov.parse(req)
        assert res["success"] is False
        assert "超过限制" in res["error"]

    @pytest.mark.asyncio
    async def test_parse_long_name_exports_short_image_paths(self, tmp_path, fitz_available):
        """超长文件名 + 含图片：图片名应短、md 引用为相对路径（回归 Windows MAX_PATH）"""
        import re as _re
        import pymupdf
        long_stem = (
            "38773db3-d94d-4cc1-babf-7c25cd89507b_"
            "网络工程师第6版带书签可搜索_张永刚王涛高振江_Z-Library-page-1-199"
        )
        pdf = str(tmp_path / f"{long_stem}.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 120))
        for y in range(120):
            for x in range(120):
                pix.set_pixel(x, y, (x % 256, y % 256, (x * y) % 256))
        page.insert_image(pymupdf.Rect(72, 72, 260, 260), pixmap=pix)
        page.insert_text((72, 320), "long name image body")
        doc.save(pdf)
        doc.close()

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace, exist_ok=True)
        prov = LocalPdfProvider()
        req = PdfParseRequest(
            provider_name="local", provider_cfg={}, pdfs=[pdf],
            params={"page_ranges": "", "extra_formats": []}, workspace_path=workspace,
        )
        res = await prov.parse(req)
        assert res["success"] is True, res.get("error")

        md = next(f for f in res["files"] if f["format"] == "markdown")
        out_dir = os.path.dirname(md["path"])
        # out_dir 目录名已截断，不含完整超长文件名；产物路径处于安全长度
        assert long_stem not in os.path.basename(out_dir)
        assert len(os.path.abspath(md["path"])) < 240

        content = Path(md["path"]).read_text(encoding="utf-8")
        for r in _re.findall(r"!\[[^\]]*\]\(([^)]+)\)", content):
            assert not os.path.isabs(r)
            assert long_stem not in r
        # 图片（若有）应为短名 doc-NNNN-NN.png，而非源文件名
        img_dir = os.path.join(out_dir, "images")
        if os.path.isdir(img_dir):
            for name in os.listdir(img_dir):
                assert name.startswith("doc-") and name.endswith(".png")
                assert len(os.path.abspath(os.path.join(img_dir, name))) < 240


# ===== local: markdown 图片引用本地化 =====

class TestLocalizeImageRefs:
    """_localize_image_refs：把 pymupdf4llm 生成的绝对/CWD 相对引用改写为相对 out_dir"""

    @staticmethod
    def _md(ref: str) -> str:
        return f"prefix\n\n![]({ref})\n\ntail"

    def test_absolute_ref_inside_out_dir_becomes_relative(self, tmp_path):
        from app.tools.pdf_providers.local import _localize_image_refs
        out_dir = tmp_path / "out"
        (out_dir / "images").mkdir(parents=True)
        img = out_dir / "images" / "x.png"
        img.write_bytes(b"png")
        result = _localize_image_refs(self._md(img.as_posix()), str(out_dir))
        assert "![](images/x.png)" in result
        assert img.as_posix() not in result

    def test_data_uri_and_http_unchanged(self, tmp_path):
        from app.tools.pdf_providers.local import _localize_image_refs
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        md = "a ![](data:image/png;base64,AAAA) b ![](https://ex.com/i.png) c"
        assert _localize_image_refs(md, str(out_dir)) == md

    def test_ref_outside_out_dir_unchanged(self, tmp_path):
        from app.tools.pdf_providers.local import _localize_image_refs
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        other = tmp_path / "other" / "y.png"
        # 不在 out_dir 下 → relpath 以 .. 开头 → 保持原样
        assert _localize_image_refs(self._md(other.as_posix()), str(out_dir)) == self._md(other.as_posix())
