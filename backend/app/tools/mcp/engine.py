"""MCP 引擎 - 基于官方 mcp SDK 的 MCP 服务器连接与工具管理

职责:
- 从 settings.json / plugin-mcp/.mcp.json 加载服务器配置
- 支持 5 种 transport: stdio(自实现,优雅杀进程) / sse / http(Streamable HTTP) / ws / sdk(进程内)
- initialize 握手 + tools/list 发现（保留 annotations）
- 工具注册进全局 tool_registry（命名: mcp__<server>__<tool>，内置优先）
- notifications/tools/list_changed → 自动刷新工具缓存并同步注册表
- 错误处理: 终局性错误连续 3 次 → 关闭+重连；401 → McpAuthError → 重认证；
  工具调用超时可配置（asyncio.wait_for，等价 Promise.race）
"""
import asyncio
import importlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

import anyio
import httpx
import structlog
from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.client.websocket import websocket_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, create_mcp_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from app.tools.mcp.config_loader import (
    MCPGlobalConfig,
    MCPServerConfig,
    load_global_config,
    load_server_configs,
    make_tool_name,
)
from app.tools.mcp.errors import McpAuthError, McpConnectionError, McpTimeoutError
from app.tools.mcp.oauth import McpOAuthClient
from app.tools.mcp.transports import graceful_stdio_client

logger = structlog.get_logger()

# websocket_client 在 mcp 1.x 已被 @deprecated 装饰（将于 2.0 移除），每次调用会触发
# DeprecationWarning。本项目规范要求支持 ws 传输，且 1.29.1 仍可正常工作，
# 故取 __wrapped__ 得到未装饰的原上下文管理器工厂，避免日志噪音。
_websocket_client = getattr(websocket_client, "__wrapped__", websocket_client)

# 连接状态
STATUS_DISCONNECTED = "disconnected"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_ERROR = "error"
STATUS_AUTH_REQUIRED = "auth_required"

# 终局性错误类型
_TERMINAL_ERROR_TYPES = (
    ConnectionResetError,
    ConnectionAbortedError,
    ConnectionRefusedError,
    BrokenPipeError,
    EOFError,
    TimeoutError,
    asyncio.TimeoutError,
)
# 终局性错误关键词（Windows socket 错误等）
_TERMINAL_ERROR_TOKENS = (
    "econnreset", "etimedout", "epipe", "econnrefused", "ehostunreach",
    "connection reset", "broken pipe", "connection closed", "connection aborted",
    "end of stream", "closedresourceerror", "endofstream", "brokenresourceerror",
    "streamablehttperror", "remotedisconnected", "server disconnected",
)


def _is_terminal_error(exc: BaseException) -> bool:
    """判断是否为终局性连接错误（ECONNRESET/ETIMEDOUT/EPIPE 等）"""
    if isinstance(exc, _TERMINAL_ERROR_TYPES):
        return True
    try:
        import anyio as _anyio
        if isinstance(exc, (_anyio.ClosedResourceError, _anyio.EndOfStream, _anyio.BrokenResourceError)):
            return True
    except Exception:
        pass
    # httpx 网络错误
    try:
        import httpx
        if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.WriteError,
                            httpx.RemoteProtocolError, httpx.PoolTimeout, httpx.ConnectTimeout,
                            httpx.ReadTimeout)):
            return True
    except Exception:
        pass
    msg = str(exc).lower()
    return any(tok in msg for tok in _TERMINAL_ERROR_TOKENS)


def _is_auth_error(exc: BaseException) -> bool:
    """判断是否为认证错误（401 / 令牌过期）"""
    if isinstance(exc, McpAuthError):
        return True
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    msg = str(exc).lower()
    return "401" in msg or "unauthorized" in msg or ("authentication" in msg and "fail" in msg)


class _ReconnectSignal(Exception):
    """内部信号：请求重连"""


@dataclass
class MCPServerConnection:
    """单个 MCP 服务器的连接状态"""
    config: MCPServerConfig
    status: str = STATUS_DISCONNECTED
    session: Optional[ClientSession] = None
    tools: Dict[str, Any] = field(default_factory=dict)  # 原始工具名 -> mcp Tool
    registered_names: List[str] = field(default_factory=list)  # 已注册的注册表工具名
    consecutive_errors: int = 0
    reconnect_attempts: int = 0
    last_error: Optional[str] = None
    stop_requested: bool = False
    reconnect_requested: bool = False
    shutting_down: bool = False
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    connected_event: asyncio.Event = field(default_factory=asyncio.Event)
    supervisor: Optional[asyncio.Task] = None
    oauth: Optional[McpOAuthClient] = None

    @property
    def name(self) -> str:
        return self.config.name


class MCPManager:
    """MCP 管理器：连接生命周期 + 工具注册同步"""

    def __init__(self):
        self._connections: Dict[str, MCPServerConnection] = {}
        self._global: MCPGlobalConfig = load_global_config()

    # ================= 启动与关闭 =================

    async def load_and_connect(self):
        """启动时调用：加载配置并连接所有启用的服务器"""
        self._global = load_global_config()
        if not self._global.enabled:
            logger.info("MCP disabled by settings.json (mcp.enabled=false)")
            return

        configs = load_server_configs()
        started = 0
        for name, cfg in configs.items():
            if not cfg.enabled:
                logger.info("MCP server disabled by config, skipped", server=name)
                continue
            conn = MCPServerConnection(config=cfg)
            self._connections[name] = conn
            conn.supervisor = asyncio.create_task(
                self._supervise(conn), name=f"mcp-supervisor-{name}"
            )
            started += 1

        logger.info("MCP manager started", servers=started)

    async def shutdown(self):
        """关闭所有连接（应用退出时调用）"""
        for conn in list(self._connections.values()):
            conn.shutting_down = True
            conn.stop_requested = True
            conn.wake_event.set()
        for conn in list(self._connections.values()):
            if conn.supervisor and not conn.supervisor.done():
                try:
                    await asyncio.wait_for(conn.supervisor, timeout=10)
                except Exception:
                    conn.supervisor.cancel()
                    try:
                        await conn.supervisor
                    except Exception:
                        pass
            self._cleanup_registered_tools(conn)
        self._connections.clear()
        logger.info("MCP manager shut down")

    # ================= 连接监控 =================

    async def _supervise(self, conn: MCPServerConnection):
        """单个服务器的连接监控循环（连接 → 发现工具 → 保活 → 错误重连）"""
        cfg = conn.config
        while not conn.shutting_down and not conn.stop_requested:
            conn.status = STATUS_CONNECTING
            conn.connected_event.clear()
            try:
                if cfg.type == "sdk":
                    await self._run_session(conn, use_sdk=True)
                else:
                    await self._run_session(conn, use_sdk=False)
                # 正常退出（收到 disconnect 请求）
                break
            except McpAuthError as e:
                conn.status = STATUS_AUTH_REQUIRED
                conn.last_error = str(e)
                self._cleanup_registered_tools(conn)
                logger.warning(
                    "MCP server requires authentication",
                    server=cfg.name,
                    error=str(e),
                    hint=f"POST /api/v1/tools/mcp/servers/{cfg.name}/auth",
                )
                return
            except asyncio.CancelledError:
                self._cleanup_registered_tools(conn)
                conn.status = STATUS_DISCONNECTED
                raise
            except _ReconnectSignal:
                self._cleanup_registered_tools(conn)
                continue
            except Exception as e:
                self._cleanup_registered_tools(conn)
                conn.consecutive_errors += 1
                conn.last_error = str(e)
                terminal = _is_terminal_error(e)
                logger.error(
                    "MCP connection error",
                    server=cfg.name,
                    error=str(e),
                    terminal=terminal,
                    consecutive_errors=conn.consecutive_errors,
                )
                if conn.consecutive_errors >= self._global.max_consecutive_errors:
                    # 连续终局性错误达到阈值 → 关闭 + 重连
                    conn.status = STATUS_ERROR
                    conn.reconnect_attempts += 1
                    delay = min(
                        self._global.reconnect_delay_seconds * (2 ** (conn.reconnect_attempts - 1)),
                        self._global.reconnect_backoff_max_seconds,
                    )
                    logger.warning(
                        "MCP consecutive errors reached threshold, closing and reconnecting",
                        server=cfg.name,
                        threshold=self._global.max_consecutive_errors,
                        reconnect_delay=delay,
                        attempts=conn.reconnect_attempts,
                    )
                    await asyncio.sleep(delay)
                    continue
                # 未达阈值：短暂等待后重试
                await asyncio.sleep(self._global.reconnect_delay_seconds)
                continue

        conn.status = STATUS_DISCONNECTED
        conn.session = None
        self._cleanup_registered_tools(conn)

    async def _run_session(self, conn: MCPServerConnection, use_sdk: bool):
        """建立传输 + 会话，完成握手与工具发现，保活直到被唤醒"""
        cfg = conn.config
        read_timeout = timedelta(
            seconds=cfg.connect_timeout_seconds or self._global.connect_timeout_seconds
        )
        message_handler = self._make_message_handler(conn)

        if use_sdk:
            server_obj = self._import_sdk_server(cfg.module)
            ctx = create_connected_server_and_client_session(
                server_obj,
                read_timeout_seconds=read_timeout,
                message_handler=message_handler,
            )
            async with ctx as session:
                await self._session_up(conn, session)
        else:
            headers = await self._build_headers(conn)
            async with self._transport_context(conn, headers) as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=read_timeout,
                    message_handler=message_handler,
                ) as session:
                    await self._session_up(conn, session)

    async def _session_up(self, conn: MCPServerConnection, session: ClientSession):
        """握手 + 工具发现 + 保活等待"""
        conn.session = session
        await session.initialize()
        await self._refresh_tools(conn)

        conn.status = STATUS_CONNECTED
        conn.consecutive_errors = 0
        conn.reconnect_attempts = 0
        conn.last_error = None
        conn.connected_event.set()
        logger.info(
            "MCP server connected",
            server=conn.name,
            transport=conn.config.type,
            tools=len(conn.tools),
        )

        # 保活：等待 disconnect 或 reconnect 信号（在 anyio 取消作用域内安全等待）
        async with anyio.create_task_group() as tg:

            async def _wake():
                await conn.wake_event.wait()
                tg.cancel_scope.cancel()

            tg.start_soon(_wake)

        conn.wake_event.clear()
        conn.session = None
        if conn.reconnect_requested:
            conn.reconnect_requested = False
            raise _ReconnectSignal()
        # stop_requested 或 shutting_down → 正常退出

    @asynccontextmanager
    async def _transport_context(self, conn: MCPServerConnection, headers: Dict[str, str]):
        """按 transport 类型构造上下文管理器

        统一以异步上下文管理器形式产出流元组 (read_stream, write_stream[, get_session_id])。
        """
        cfg = conn.config
        connect_timeout = cfg.connect_timeout_seconds or self._global.connect_timeout_seconds

        if cfg.type == "stdio":
            params = StdioServerParameters(
                command=cfg.command,
                args=cfg.args or [],
                env=cfg.env or None,
                cwd=cfg.cwd,
            )
            async with graceful_stdio_client(params) as streams:
                yield streams
        elif cfg.type == "sse":
            async with sse_client(
                cfg.url, headers=headers or None, timeout=connect_timeout
            ) as streams:
                yield streams
        elif cfg.type == "http":
            # 新版 streamable_http_client 不再接受 headers/timeout 参数，改为传入预配置的
            # httpx.AsyncClient。传入自定义 client 时 SDK 不负责关闭它，需自行管理生命周期。
            # 超时: 通用阶段用 connect_timeout，读阶段(SSE 长连接)用 SDK 推荐的 300s。
            http_client = create_mcp_http_client(
                headers=headers or None,
                timeout=httpx.Timeout(connect_timeout, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
            )
            try:
                async with streamable_http_client(cfg.url, http_client=http_client) as streams:
                    yield streams
            finally:
                await http_client.aclose()
        elif cfg.type == "ws":
            async with _websocket_client(cfg.url) as streams:
                yield streams
        else:
            raise McpConnectionError(f"Unsupported transport: {cfg.type}")

    def _import_sdk_server(self, module_spec: str):
        """导入 sdk 类型服务器: 'pkg.mod:factory' → 返回 FastMCP/Server 实例"""
        if not module_spec or ":" not in module_spec:
            raise McpConnectionError(
                f"sdk server requires 'module' in format 'pkg.mod:factory', got: {module_spec!r}"
            )
        module_path, factory_name = module_spec.rsplit(":", 1)
        try:
            module = importlib.import_module(module_path)
            factory = getattr(module, factory_name)
            server = factory()
        except Exception as e:
            raise McpConnectionError(f"Failed to import sdk server '{module_spec}': {e}")
        return server

    async def _build_headers(self, conn: MCPServerConnection) -> Dict[str, str]:
        """构造请求头（静态配置 + OAuth Bearer 令牌）"""
        headers = dict(conn.config.headers or {})
        cfg = conn.config

        if cfg.oauth_enabled:
            if cfg.type == "ws":
                logger.warning("OAuth over ws transport is not supported (no header injection)",
                               server=cfg.name)
                return headers
            if conn.oauth is None:
                conn.oauth = McpOAuthClient(cfg.name, cfg.url, cfg.oauth)
            status = conn.oauth.auth_status
            if status in ("authenticated", "expired"):
                token = await conn.oauth.get_access_token()
                headers["Authorization"] = f"Bearer {token}"
            elif status == "pending":
                raise McpAuthError(f"OAuth flow for '{cfg.name}' is still pending")
            else:
                raise McpAuthError(
                    f"Server '{cfg.name}' requires OAuth authentication. "
                    f"Call POST /api/v1/tools/mcp/servers/{cfg.name}/auth first."
                )
        return headers

    def _make_message_handler(self, conn: MCPServerConnection):
        """构造会话消息处理器（捕获 notifications/tools/list_changed）"""

        async def handler(message):
            try:
                if isinstance(message, mcp_types.ServerNotification):
                    notification = message.root
                    if isinstance(notification, mcp_types.ToolListChangedNotification):
                        logger.info(
                            "notifications/tools/list_changed received, refreshing tools",
                            server=conn.name,
                        )
                        asyncio.create_task(self._refresh_tools(conn))
            except Exception as e:
                logger.warning("MCP message handler error", server=conn.name, error=str(e))

        return handler

    # ================= 工具发现与注册 =================

    async def _refresh_tools(self, conn: MCPServerConnection):
        """重新执行 tools/list 并同步工具缓存与 tool_registry（diff 增删改）"""
        session = conn.session
        if not session:
            return
        try:
            result = await session.list_tools()
        except Exception as e:
            if _is_auth_error(e):
                conn.status = STATUS_AUTH_REQUIRED
                conn.last_error = str(e)
                logger.warning("Tool list refresh got auth error", server=conn.name, error=str(e))
                return
            logger.error("Tool list refresh failed", server=conn.name, error=str(e))
            return

        new_tools = {t.name: t for t in result.tools}
        old_tools = conn.tools

        removed = [n for n in old_tools if n not in new_tools]
        added = [n for n in new_tools if n not in old_tools]
        changed = [n for n in new_tools if n in old_tools and new_tools[n] != old_tools[n]]

        from app.tools.registry import tool_registry

        # 移除/变更的先注销
        for tool_name in removed + changed:
            reg_name = make_tool_name(conn.name, tool_name)
            if reg_name in conn.registered_names:
                tool_registry.unregister(reg_name)
                conn.registered_names.remove(reg_name)

        # 新增/变更的重新注册
        for tool_name in added + changed:
            reg_name = self._register_tool(conn, new_tools[tool_name])
            if reg_name and reg_name not in conn.registered_names:
                conn.registered_names.append(reg_name)

        if removed or added or changed:
            logger.info(
                "MCP tools synced",
                server=conn.name,
                added=len(added),
                removed=len(removed),
                changed=len(changed),
                total=len(new_tools),
            )
            # ✅ 通知 ToolSearch 引擎重建索引
            try:
                from app.tools.tool_search import notify_tools_changed
                notify_tools_changed()
            except Exception:
                pass
        conn.tools = new_tools

    def _register_tool(self, conn: MCPServerConnection, tool) -> Optional[str]:
        """将 MCP 工具注册进 tool_registry（内置优先：重名时跳过）"""
        from app.tools.registry import tool_registry

        reg_name = make_tool_name(conn.name, tool.name)

        existing = tool_registry.get(reg_name)
        if existing and existing.metadata.get("source") != "mcp":
            logger.warning(
                "MCP tool name conflicts with built-in/extension tool, skipped (built-in takes priority)",
                server=conn.name,
                tool=tool.name,
                registry_name=reg_name,
            )
            return None
        if existing:
            # 同名 MCP 工具更新：先注销
            tool_registry.unregister(reg_name)
            if reg_name in conn.registered_names:
                conn.registered_names.remove(reg_name)

        annotations = None
        if getattr(tool, "annotations", None):
            try:
                annotations = tool.annotations.model_dump(exclude_none=True)
            except Exception:
                annotations = None

        server_name = conn.name
        original_name = tool.name

        async def _handler(**kwargs):
            return await self.call_tool(server_name, original_name, kwargs)

        parameters = tool.inputSchema if isinstance(tool.inputSchema, dict) else {
            "type": "object", "properties": {},
        }

        tool_registry.register(
            name=reg_name,
            description=(tool.description or f"MCP tool '{tool.name}' from server '{server_name}'"),
            handler=_handler,
            parameters=parameters,
            metadata={
                "mcp_tool": True,
                "source": "mcp",
                "mcp_server": server_name,
                "mcp_tool_name": original_name,
                "annotations": annotations,
            },
        )
        logger.debug("MCP tool registered", registry_name=reg_name, server=server_name)
        return reg_name

    def _cleanup_registered_tools(self, conn: MCPServerConnection):
        """从 tool_registry 注销该服务器的所有工具"""
        if not conn.registered_names:
            return
        from app.tools.registry import tool_registry
        for reg_name in list(conn.registered_names):
            tool_registry.unregister(reg_name)
        conn.registered_names.clear()
        conn.tools = {}

    # ================= 工具调用 =================

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        调用 MCP 工具（带可配置超时，等价 Promise.race）。

        错误处理:
        - 超时 → McpTimeoutError
        - 401/令牌过期 → McpAuthError（状态置 auth_required）
        - 终局性错误连续达阈值 → 触发关闭+重连
        """
        conn = self._connections.get(server_name)
        if not conn:
            raise McpConnectionError(f"Unknown MCP server: {server_name}")
        if conn.status != STATUS_CONNECTED or not conn.session:
            raise McpConnectionError(
                f"MCP server '{server_name}' is not connected (status={conn.status})"
            )

        timeout = conn.config.call_timeout_seconds or self._global.call_timeout_seconds
        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(tool_name, arguments or {}),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise McpTimeoutError(
                f"MCP tool call timed out after {timeout}s: {server_name}.{tool_name}"
            )
        except Exception as e:
            if _is_auth_error(e):
                conn.status = STATUS_AUTH_REQUIRED
                conn.last_error = str(e)
                raise McpAuthError(
                    f"MCP tool call got 401/token expired on '{server_name}', "
                    f"re-authentication required"
                )
            if _is_terminal_error(e):
                conn.consecutive_errors += 1
                if conn.consecutive_errors >= self._global.max_consecutive_errors:
                    logger.warning(
                        "MCP consecutive errors reached threshold during tool call, "
                        "closing and reconnecting",
                        server=server_name,
                        consecutive_errors=conn.consecutive_errors,
                    )
                    self._request_reconnect(conn)
                raise McpConnectionError(f"MCP tool call failed: {e}")
            raise

        # 调用成功，重置错误计数
        conn.consecutive_errors = 0

        return self._format_call_result(result)

    def _format_call_result(self, result) -> Dict[str, Any]:
        """将 CallToolResult 转为可序列化结构"""
        content = []
        for item in (result.content or []):
            try:
                content.append(item.model_dump(exclude_none=True))
            except Exception:
                content.append({"type": "text", "text": str(item)})
        formatted = {
            "content": content,
            "isError": bool(getattr(result, "isError", False)),
        }
        if getattr(result, "structuredContent", None):
            formatted["structuredContent"] = result.structuredContent
        return formatted

    def _request_reconnect(self, conn: MCPServerConnection):
        """请求连接监控循环关闭当前连接并重连"""
        conn.reconnect_requested = True
        conn.wake_event.set()

    # ================= 对外操作接口 =================

    async def connect(self, server_name: str, timeout: float = 30.0) -> bool:
        """连接指定服务器（若监控循环未运行则启动）"""
        conn = self._connections.get(server_name)
        if not conn:
            return False
        if conn.status == STATUS_CONNECTED:
            return True
        conn.stop_requested = False
        if not conn.supervisor or conn.supervisor.done():
            conn.supervisor = asyncio.create_task(
                self._supervise(conn), name=f"mcp-supervisor-{server_name}"
            )
        else:
            # 已在运行（可能处于重连等待或 auth_required 后重启）
            self._request_reconnect(conn)
        try:
            await asyncio.wait_for(conn.connected_event.wait(), timeout=timeout)
            return conn.status == STATUS_CONNECTED
        except asyncio.TimeoutError:
            return False

    async def disconnect(self, server_name: str) -> bool:
        """断开指定服务器"""
        conn = self._connections.get(server_name)
        if not conn:
            return False
        conn.stop_requested = True
        conn.wake_event.set()
        if conn.supervisor and not conn.supervisor.done():
            try:
                await asyncio.wait_for(conn.supervisor, timeout=15)
            except Exception:
                conn.supervisor.cancel()
        conn.status = STATUS_DISCONNECTED
        self._cleanup_registered_tools(conn)
        return True

    async def refresh_tools(self, server_name: str) -> Dict[str, Any]:
        """手动刷新指定服务器的工具列表"""
        conn = self._connections.get(server_name)
        if not conn or conn.status != STATUS_CONNECTED:
            raise McpConnectionError(f"MCP server '{server_name}' is not connected")
        await self._refresh_tools(conn)
        return {"server": server_name, "tools": len(conn.tools)}

    async def start_auth(
        self,
        server_name: str,
        id_token: Optional[str] = None,
        jwt_assertion: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        发起 OAuth 认证。

        - 传入 id_token / jwt_assertion 时走 XAA（RFC 8693 / RFC 7523），直接交换令牌
        - 否则返回授权 URL，本地回调服务器在后台等待授权码
        """
        conn = self._connections.get(server_name)
        if not conn:
            raise McpConnectionError(f"Unknown MCP server: {server_name}")
        cfg = conn.config
        if not cfg.url:
            raise McpConnectionError(f"Server '{server_name}' has no url for OAuth")

        if conn.oauth is None:
            conn.oauth = McpOAuthClient(cfg.name, cfg.url, cfg.oauth)

        if id_token or jwt_assertion:
            # XAA: 直接交换令牌并重连
            await conn.oauth.exchange_external_token(
                id_token=id_token, jwt_assertion=jwt_assertion
            )
            conn.stop_requested = False
            asyncio.create_task(self._reconnect_after_auth(conn))
            return {"status": "authenticated", "mode": "xaa"}

        info = await conn.oauth.start_authorization()
        asyncio.create_task(self._complete_auth(conn))
        return {
            "status": "pending",
            "mode": "authorization_code",
            "authorization_url": info["authorization_url"],
            "callback_port": info["callback_port"],
        }

    async def _complete_auth(self, conn: MCPServerConnection):
        """后台等待授权码完成并自动重连"""
        try:
            await conn.oauth.complete_authorization()
            await self._reconnect_after_auth(conn)
        except Exception as e:
            conn.status = STATUS_AUTH_REQUIRED
            conn.last_error = str(e)
            logger.error("OAuth authorization failed", server=conn.name, error=str(e))

    async def _reconnect_after_auth(self, conn: MCPServerConnection):
        """认证成功后重连"""
        conn.stop_requested = False
        if conn.supervisor and not conn.supervisor.done():
            self._request_reconnect(conn)
        else:
            conn.supervisor = asyncio.create_task(
                self._supervise(conn), name=f"mcp-supervisor-{conn.name}"
            )
        try:
            await asyncio.wait_for(conn.connected_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("Reconnect after auth timed out", server=conn.name)

    def get_auth_status(self, server_name: str) -> Dict[str, Any]:
        """查询认证状态"""
        conn = self._connections.get(server_name)
        if not conn:
            return {"server": server_name, "oauth_configured": False, "status": "unknown"}
        if not conn.config.oauth_enabled:
            return {"server": server_name, "oauth_configured": False, "status": "not_required"}
        if conn.oauth is None:
            conn.oauth = McpOAuthClient(conn.config.name, conn.config.url, conn.config.oauth)
        return {
            "server": server_name,
            "oauth_configured": True,
            "status": conn.oauth.auth_status,
            "connection_status": conn.status,
        }

    # ================= 查询接口 =================

    def list_servers(self) -> List[Dict[str, Any]]:
        """列出所有服务器状态"""
        result = []
        for conn in self._connections.values():
            result.append({
                "name": conn.name,
                "transport": conn.config.type,
                "status": conn.status,
                "tools_count": len(conn.tools),
                "consecutive_errors": conn.consecutive_errors,
                "last_error": conn.last_error,
                "oauth_required": conn.config.oauth_enabled,
                "source": conn.config.source,
            })
        return result

    def get_connection(self, server_name: str) -> Optional[MCPServerConnection]:
        return self._connections.get(server_name)

    def list_tools(self, server_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出工具（含 annotations）"""
        tools = []
        conns = ([self._connections[server_name]] if server_name and server_name in self._connections
                 else self._connections.values())
        for conn in conns:
            for tool in conn.tools.values():
                annotations = None
                if getattr(tool, "annotations", None):
                    try:
                        annotations = tool.annotations.model_dump(exclude_none=True)
                    except Exception:
                        annotations = None
                tools.append({
                    "server": conn.name,
                    "name": tool.name,
                    "registry_name": make_tool_name(conn.name, tool.name),
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                    "annotations": annotations,
                })
        return tools

    # ================= 配置热重载 =================

    async def reload_config(self) -> Dict[str, Any]:
        """对比配置文件与运行状态，差量重载 MCP 服务器。

        - 新增/启用的服务器：创建连接并启动监控循环
        - 删除/禁用的服务器：断开并移除
        - 配置变更的服务器：断开旧连接，用新配置重连
        - 全局配置同步更新

        Returns:
            {"removed": int, "added": int, "reloaded": int}
        """
        new_global = load_global_config()
        new_configs = load_server_configs()
        current_names = set(self._connections.keys())
        new_names = {k for k, v in new_configs.items() if v.enabled}

        removed_names = current_names - new_names
        added_names = new_names - current_names
        common_names = current_names & new_names

        # 1. 移除已删除/禁用的服务器
        removed = 0
        for name in removed_names:
            await self.disconnect(name)
            self._connections.pop(name, None)
            removed += 1

        # 2. 新增服务器
        added = 0
        for name in added_names:
            cfg = new_configs[name]
            conn = MCPServerConnection(config=cfg)
            self._connections[name] = conn
            conn.supervisor = asyncio.create_task(
                self._supervise(conn), name=f"mcp-supervisor-{name}"
            )
            added += 1

        # 3. 配置变更的服务器：断开后用新配置重连
        reloaded = 0
        for name in common_names:
            old_conn = self._connections[name]
            new_cfg = new_configs[name]
            # dataclass 默认 __eq__ 比较所有字段
            if old_conn.config != new_cfg:
                await self.disconnect(name)
                conn = MCPServerConnection(config=new_cfg)
                self._connections[name] = conn
                conn.supervisor = asyncio.create_task(
                    self._supervise(conn), name=f"mcp-supervisor-{name}"
                )
                reloaded += 1

        # 4. 同步全局配置
        self._global = new_global

        logger.info(
            "MCP config reloaded",
            removed=removed,
            added=added,
            reloaded=reloaded,
        )
        return {"removed": removed, "added": added, "reloaded": reloaded}


# 全局 MCP 管理器实例
mcp_manager = MCPManager()
