"""视频生成 Provider 包

架构（参照 tts/、image_providers/、music_providers/ 包的模块化拆分模式）:
- base: VideoProviderBase 抽象基类 + VideoGenerateRequest 请求载体
        （统一 generate/upload_file 接口、共享媒体引用解析与本地路径解析）
- params: 参数标准化引擎（normalize_video_params）与 endpoint API 配置解析（provider 无关共享逻辑）
- aliyun: aliyun/wan3.0-video Provider（DashScope 异步任务 + OSS 上传）
- minimax: minimax/MiniMax-H3 Provider（异步任务 + 文件上传，不支持 file/link）
- factory: Provider 工厂（新增 provider 只需在 factory.py 注册）
"""

from .base import VideoProviderBase, VideoGenerateRequest, resolve_local_media_path
from .params import (
    normalize_video_params,
    get_api_mode_config,
    resolve_api_url,
    get_query_task_url,
    get_file_upload_url,
    identify_provider,
)
from .factory import get_provider, supported_provider_types
from .aliyun import AliyunVideoProvider
from .minimax import MinimaxVideoProvider

__all__ = [
    "VideoProviderBase",
    "VideoGenerateRequest",
    "resolve_local_media_path",
    "normalize_video_params",
    "get_api_mode_config",
    "resolve_api_url",
    "get_query_task_url",
    "get_file_upload_url",
    "identify_provider",
    "get_provider",
    "supported_provider_types",
    "AliyunVideoProvider",
    "MinimaxVideoProvider",
]
