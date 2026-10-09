"""视频生成 Provider 抽象基类"""
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class VideoGenerateRequest:
    """一次视频生成请求的统一载体

    orchestration 层（video_generate._run_video_generation）构建该对象，
    Provider 可通过 prepare() 登记不支持的参数，generate() 内部完成
    媒体构建 → 任务提交 → 轮询的完整流程。
    """

    def __init__(
        self,
        endpoint: Dict[str, Any],
        mode: str,
        prompt: str,
        params: Dict[str, Any],
        timeout_ms: int = 300000,
        job_id: str = "",
        images: Optional[List[str]] = None,
        image_roles: Optional[List[str]] = None,
        videos: Optional[List[str]] = None,
        video_roles: Optional[List[str]] = None,
        audios: Optional[List[str]] = None,
        audio_roles: Optional[List[str]] = None,
        files: Optional[List[str]] = None,
        file_roles: Optional[List[str]] = None,
        links: Optional[List[str]] = None,
        link_roles: Optional[List[str]] = None,
    ):
        self.endpoint = endpoint
        self.mode = mode
        self.prompt = prompt
        self.params = params
        self.timeout_ms = timeout_ms
        self.job_id = job_id
        self.images = images or []
        self.image_roles = image_roles or []
        self.videos = videos or []
        self.video_roles = video_roles or []
        self.audios = audios or []
        self.audio_roles = audio_roles or []
        self.files = files or []
        self.file_roles = file_roles or []
        self.links = links or []
        self.link_roles = link_roles or []
        self.ignored_params: List[str] = []

    def add_ignored(self, tag: str) -> None:
        """登记被忽略/不支持的参数（去重）"""
        if tag not in self.ignored_params:
            self.ignored_params.append(tag)


class VideoProviderBase(ABC):
    """视频生成提供商抽象基类

    子类须声明 `provider_type`，并在 factory.py 的 `_PROVIDER_MAP` 中注册。

    generate() 统一返回结构:
    - 成功: {"success": True, "video_url": str, "usage": {...}, "task_status": str, ...}
    - 失败: {"success": False, "error": "..."}
    """

    provider_type: str = ""
    # 该 provider 不支持的媒体类型（在 prepare 中登记到 ignored_params）
    unsupported_media: tuple = ()

    def prepare(self, request: VideoGenerateRequest) -> None:
        """生成前的能力协商：登记不支持的媒体类型。

        默认实现按 unsupported_media 声明登记，子类可覆写以处理特有逻辑。
        """
        for media_kind in self.unsupported_media:
            if getattr(request, media_kind, None):
                request.add_ignored(f"{self.provider_type}:{media_kind} not supported")

    @abstractmethod
    async def generate(self, request: VideoGenerateRequest) -> Dict[str, Any]:
        """执行视频生成（构建媒体项 → 提交任务 → 轮询结果）"""
        ...

    @abstractmethod
    async def upload_file(self, endpoint: Dict[str, Any], file_path: str) -> Optional[str]:
        """上传本地文件到提供商存储，返回提供商可访问的 URL（失败返回 None）"""
        ...

    # ===== 共享：媒体引用解析 =====

    async def resolve_media_url(self, endpoint: Dict[str, Any], media_ref: str) -> Optional[str]:
        """
        统一处理媒体引用：
        - 远程 URL (http/https) 或提供商 URL (oss:// / mm_file:// / data:) 直接返回
        - 本地文件路径或会话文件 URL → 先上传再返回
        """
        if media_ref.startswith(("http://", "https://", "oss://", "mm_file://", "data:")):
            return media_ref

        abs_path = resolve_local_media_path(media_ref)
        if not abs_path:
            logger.warning("Media file not found for upload", ref=media_ref)
            return None
        return await self.upload_file(endpoint, abs_path)


def resolve_local_media_path(media_ref: str) -> Optional[str]:
    """将媒体引用解析为本地绝对路径（provider 无关的共享逻辑）"""
    if media_ref.startswith("/api/v1/chat/files/"):
        rel = media_ref[len("/api/v1/chat/files/"):]
        try:
            from app.api.v1.chat import WORKSPACE_ROOT_PATH
            resolved = (WORKSPACE_ROOT_PATH / rel).resolve()
            if str(resolved).startswith(str(WORKSPACE_ROOT_PATH.resolve())) and os.path.isfile(str(resolved)):
                return str(resolved)
        except Exception:
            pass
        return None

    from app.core.workspace_isolation import resolve_ws_path
    candidate = resolve_ws_path(media_ref)
    if os.path.isfile(candidate):
        return candidate
    return None
