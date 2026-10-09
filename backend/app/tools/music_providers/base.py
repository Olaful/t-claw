"""音乐生成 Provider 抽象基类"""
from abc import ABC, abstractmethod
from typing import Any, Dict


class MusicGenerateRequest:
    """一次音乐生成请求的统一载体

    orchestration 层（music_generate._run_music_generation）构建该对象，
    Provider 可通过 prepare() 就地修正 mode/stream，并登记不支持的参数。
    """

    def __init__(
        self,
        endpoint: Dict[str, Any],
        mode: str,
        prompt: str,
        params: Dict[str, Any],
        timeout_ms: int = 180000,
        stream: bool = False,
        conversation_id: str = "",
        job_id: str = "",
        reference: str = None,
        references: list = None,
    ):
        self.endpoint = endpoint
        self.mode = mode
        self.prompt = prompt
        self.params = params
        self.timeout_ms = timeout_ms
        self.stream = stream
        self.conversation_id = conversation_id
        self.job_id = job_id
        self.reference = reference
        self.references = references
        self.ignored_params: list = []

    def add_ignored(self, tag: str) -> None:
        """登记被忽略/不支持的参数（去重）"""
        if tag not in self.ignored_params:
            self.ignored_params.append(tag)


class MusicProviderBase(ABC):
    """音乐生成提供商抽象基类

    子类须声明 `provider_type`，并在 factory.py 的 `_PROVIDER_MAP` 中注册。

    generate() 统一返回结构:
    - 成功: {"success": True, "audios": [{"url", "id", "title", "lyrics"}...],
             "lyrics": str, "response_meta": {...}}
      流式追加: "streamed": True, "streamed_songs": {song_id: [base64 chunks]}
      gen_lyric: {"success": True, "lyrics": str, "title": str, "response_meta": {...}}
    - 失败: {"success": False, "error": "..."}
    """

    provider_type: str = ""
    # 该 provider 不支持的通用参数（在 prepare 中登记到 ignored_params）
    unsupported_params: tuple = ()
    # 支持流式输出的生成模式
    stream_modes: tuple = ()

    def prepare(self, request: MusicGenerateRequest) -> None:
        """生成前的能力协商：修正 mode/stream，登记不支持的参数。

        默认实现：stream 仅在 stream_modes 内生效，并登记 unsupported_params。
        子类可覆写以处理 mode 降级等 provider 特有逻辑。
        """
        if request.stream and self.stream_modes and request.mode not in self.stream_modes:
            request.stream = False
        for param in self.unsupported_params:
            request.add_ignored(f"{self.provider_type}:{param}")

    @abstractmethod
    async def generate(self, request: MusicGenerateRequest) -> Dict[str, Any]:
        """执行音乐生成"""
        ...
