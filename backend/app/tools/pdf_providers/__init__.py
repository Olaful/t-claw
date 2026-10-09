"""PDF 解析 Provider 包

架构（参照 tts/、image_providers/、music_providers/、video_providers/ 包的模块化拆分模式）:
- base: PdfProviderBase 抽象基类 + PdfParseRequest 请求载体
        （统一 parse 接口、能力协商 prepare、共享本地路径解析 resolve_local_pdf_path）
- params: 参数标准化引擎（normalize_pdf_params）与页码范围解析（expand_page_ranges）
- mineru: MinerU 精准解析 API Provider（支持 OCR/公式/表格，不支持密码）
- local: pymupdf4llm 本地解析 Provider（离线，支持 OCR / markdown / json / txt / 密码）
- factory: Provider 工厂（新增 provider 只需在 factory.py 注册）
"""

from .base import PdfParseRequest, PdfProviderBase, resolve_local_pdf_path
from .params import normalize_pdf_params, expand_page_ranges
from .factory import get_provider, supported_provider_types
from .mineru import MineruPdfProvider
from .local import LocalPdfProvider

__all__ = [
    "PdfParseRequest",
    "PdfProviderBase",
    "resolve_local_pdf_path",
    "normalize_pdf_params",
    "expand_page_ranges",
    "get_provider",
    "supported_provider_types",
    "MineruPdfProvider",
    "LocalPdfProvider",
]
