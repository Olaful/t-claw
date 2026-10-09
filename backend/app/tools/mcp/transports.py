"""MCP 自定义传输实现

包含自实现的 stdio 传输（支持 SIGINT → SIGTERM → SIGKILL 顺序优雅终止子进程）。
其余传输（sse / http / ws / sdk）直接使用官方 mcp SDK。
"""
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Tuple

import anyio
import structlog
from mcp.client.stdio import StdioServerParameters, get_default_environment
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from app.tools.mcp.errors import McpConnectionError

logger = structlog.get_logger()


async def _wait_exit(process, grace_seconds: float) -> bool:
    """等待进程退出，超时返回 False"""
    try:
        with anyio.fail_after(grace_seconds):
            await process.wait()
        return True
    except TimeoutError:
        return False


async def kill_process_gracefully(process, grace_seconds: float = 2.0):
    """
    按 SIGINT → SIGTERM → SIGKILL 顺序终止 stdio 子进程。

    Windows 无 SIGINT，直接 terminate（TerminateProcess），再 kill 兜底。
    """
    if process.returncode is not None:
        return

    is_windows = sys.platform == "win32"

    # 1. SIGINT（Windows 跳过，直接 terminate）
    try:
        if is_windows:
            process.terminate()
        else:
            import signal as _signal
            process.send_signal(_signal.SIGINT)
        if await _wait_exit(process, grace_seconds):
            return
    except (ProcessLookupError, OSError, ValueError):
        return

    # 2. SIGTERM
    try:
        process.terminate()
        if await _wait_exit(process, grace_seconds):
            return
    except (ProcessLookupError, OSError, ValueError):
        return

    # 3. SIGKILL 兜底
    try:
        process.kill()
    except (ProcessLookupError, OSError, ValueError):
        return
    await _wait_exit(process, grace_seconds)


def _build_command(params: StdioServerParameters) -> list:
    """构造启动命令（Windows 下用 cmd.exe 包装以支持 .cmd/.bat）"""
    if sys.platform == "win32":
        try:
            from mcp.client.stdio import get_windows_executable_command
            return get_windows_executable_command(params.command, params.args)
        except Exception:
            pass
    return [params.command, *params.args]


@asynccontextmanager
async def graceful_stdio_client(
    params: StdioServerParameters,
    kill_grace_seconds: float = 2.0,
) -> AsyncGenerator[Tuple[anyio.abc.ObjectReceiveStream, anyio.abc.ObjectSendStream], None]:
    """
    自定义 stdio 传输（newline-delimited JSON-RPC）。

    与 mcp SDK stdio_client 接口兼容，额外提供优雅进程终止:
    退出时按 SIGINT → SIGTERM → SIGKILL 顺序杀进程。
    """
    # 环境变量合并：默认安全环境 + 用户配置
    env = get_default_environment()
    env.update(params.env or {})

    process = await anyio.open_process(
        _build_command(params),
        env=env,
        cwd=params.cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    logger.info(
        "MCP stdio process started",
        command=params.command,
        pid=getattr(process, "pid", None),
    )

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async def _read_loop():
        """读取子进程 stdout（换行分隔的 JSON）→ read_stream"""
        try:
            async with read_stream_writer:
                buffer = b""
                while True:
                    try:
                        chunk = await process.stdout.receive(65536)
                    except anyio.EndOfStream:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            message = JSONRPCMessage.model_validate(obj)
                            await read_stream_writer.send(SessionMessage(message=message))
                        except Exception as e:
                            logger.warning("MCP stdio invalid message", error=str(e))
        except anyio.ClosedResourceError:
            pass
        finally:
            # EOF：通知会话连接已关闭
            try:
                await read_stream_writer.send(
                    SessionMessage(message=McpConnectionError("stdio stream closed (EOF)"))
                )
            except Exception:
                pass

    async def _write_loop():
        """write_stream → 子进程 stdin"""
        try:
            async with write_stream_reader:
                async for item in write_stream_reader:
                    data = item.message.model_dump(
                        by_alias=True, exclude_none=True, mode="json"
                    )
                    await process.stdin.send(json.dumps(data).encode("utf-8") + b"\n")
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_read_loop)
            tg.start_soon(_write_loop)
            try:
                yield read_stream, write_stream
            finally:
                # 优雅终止子进程: SIGINT → SIGTERM → SIGKILL
                await kill_process_gracefully(process, kill_grace_seconds)
                tg.cancel_scope.cancel()
    except BaseException:
        # 任务组异常退出时也要确保进程被清理
        await kill_process_gracefully(process, kill_grace_seconds)
        raise
