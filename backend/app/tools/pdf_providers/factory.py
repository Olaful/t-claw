"""PDF 解析 Provider 工厂 - 新增 provider 只需在此注册"""
from typing import Dict, Optional

from .base import PdfProviderBase
from .mineru import MineruPdfProvider
from .local import LocalPdfProvider

# Provider 注册表: 提供商类型 -> Provider 类，新增 provider 时在此添加映射
_PROVIDER_MAP: Dict[str, type] = {
    MineruPdfProvider.provider_type: MineruPdfProvider,
    LocalPdfProvider.provider_type: LocalPdfProvider,
}


def get_provider(provider_type: str) -> Optional[PdfProviderBase]:
    """按提供商类型创建 Provider 实例，未注册的类型返回 None"""
    cls = _PROVIDER_MAP.get(provider_type)
    if not cls:
        return None
    return cls()


def supported_provider_types() -> list:
    """返回已注册的提供商类型列表"""
    return list(_PROVIDER_MAP.keys())
