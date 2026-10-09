"""MCP OAuth 认证客户端

流程:
1. 通过受保护资源元数据（RFC 9728）与授权服务器元数据（RFC 8414）发现 OAuth 端点
2. 动态客户端注册（RFC 7591），公共客户端 + PKCE（S256）
3. 本地回调服务器（127.0.0.1 随机端口）接收授权码
4. 令牌通过 get_secure_storage() 持久化
5. 过期前 5 分钟自动刷新（refresh_token）
6. XAA 跨应用访问: 浏览器获取 id_token → RFC 8693 token-exchange / RFC 7523 JWT Bearer
"""
import asyncio
import base64
import hashlib
import secrets
import time
import urllib.parse
from typing import Any, Dict, Optional

import httpx
import structlog

from app.tools.mcp.errors import McpAuthError
from app.tools.mcp.storage import get_secure_storage

logger = structlog.get_logger()

# 令牌提前刷新窗口（秒）：过期前 5 分钟
REFRESH_AHEAD_SECONDS = 300

# OAuth 授权码流程等待用户完成授权的最长时间（秒）
AUTH_FLOW_TIMEOUT_SECONDS = 300

# RFC 8693 / RFC 7523 常量
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class McpOAuthClient:
    """单个 MCP 服务器的 OAuth 客户端"""

    def __init__(
        self,
        server_name: str,
        server_url: str,
        oauth_config: Optional[Dict[str, Any]] = None,
    ):
        self.server_name = server_name
        self.server_url = server_url
        self.config = oauth_config or {}
        self._storage = get_secure_storage()
        # 从持久化存储加载状态
        self._state = self._storage.load(server_name) or {}
        # 进行中的授权流程
        self._pending: Optional[Dict[str, Any]] = None

    # ---------- 状态持久化 ----------

    def _save_state(self):
        self._storage.save(self.server_name, self._state)

    @property
    def client_id(self) -> Optional[str]:
        return self._state.get("client_id")

    @property
    def auth_status(self) -> str:
        """返回认证状态: none | authenticated | expired | pending"""
        if self._pending:
            return "pending"
        token = self._state.get("token")
        if not token or not token.get("access_token"):
            return "none"
        expires_at = token.get("expires_at", 0)
        if expires_at and expires_at <= time.time():
            return "expired" if not token.get("refresh_token") else "authenticated"
        return "authenticated"

    # ---------- 元数据发现 ----------

    async def discover_metadata(self) -> Dict[str, Any]:
        """
        发现 OAuth 元数据:
        1. RFC 9728: /.well-known/oauth-protected-resource 获取授权服务器列表
        2. RFC 8414: /.well-known/oauth-authorization-server 获取端点
        """
        if self._state.get("metadata"):
            return self._state["metadata"]

        parsed = urllib.parse.urlparse(self.server_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            auth_server_origin = origin

            # 1. RFC 9728 受保护资源元数据
            try:
                resource_url = self.config.get("resource_metadata_url") or \
                    f"{origin}/.well-known/oauth-protected-resource{parsed.path}"
                resp = await client.get(resource_url)
                if resp.status_code == 200:
                    resource_meta = resp.json()
                    auth_servers = resource_meta.get("authorization_servers", [])
                    if auth_servers:
                        auth_server_origin = auth_servers[0].rstrip("/")
                        logger.info(
                            "Discovered authorization server via RFC 9728",
                            server=self.server_name,
                            auth_server=auth_server_origin,
                        )
            except Exception as e:
                logger.debug("RFC 9728 resource metadata not available", error=str(e))

            # 2. RFC 8414 授权服务器元数据
            meta_url = self.config.get("authorization_server_metadata_url") or \
                f"{auth_server_origin}/.well-known/oauth-authorization-server"
            resp = await client.get(meta_url)
            if resp.status_code != 200:
                # 部分服务器把元数据放在根路径
                fallback = f"{auth_server_origin}/.well-known/openid-configuration"
                resp = await client.get(fallback)
            if resp.status_code != 200:
                raise McpAuthError(
                    f"Cannot discover OAuth metadata for server '{self.server_name}' "
                    f"(tried {meta_url}): HTTP {resp.status_code}"
                )
            metadata = resp.json()

        required = ["authorization_endpoint", "token_endpoint"]
        for key in required:
            if key not in metadata:
                raise McpAuthError(f"OAuth metadata missing required field: {key}")

        self._state["metadata"] = metadata
        self._save_state()
        logger.info("OAuth metadata discovered", server=self.server_name)
        return metadata

    # ---------- 客户端注册（RFC 7591） ----------

    async def ensure_client(self, redirect_uri: str) -> str:
        """确保已有注册客户端，返回 client_id（公共客户端）"""
        if self.client_id:
            return self.client_id

        metadata = await self.discover_metadata()
        registration_endpoint = metadata.get("registration_endpoint")
        if not registration_endpoint:
            # 无动态注册端点时，允许配置静态 client_id
            static_id = self.config.get("client_id")
            if static_id:
                self._state["client_id"] = static_id
                self._state["client_secret"] = self.config.get("client_secret")
                self._save_state()
                return static_id
            raise McpAuthError(
                f"No registration_endpoint in OAuth metadata and no static client_id configured "
                f"for server '{self.server_name}'"
            )

        client_meta = {
            "client_name": f"happy-claw-mcp-{self.server_name}",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token",
                            TOKEN_EXCHANGE_GRANT, JWT_BEARER_GRANT],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",  # 公共客户端 + PKCE
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(registration_endpoint, json=client_meta)
            if resp.status_code not in (200, 201):
                raise McpAuthError(
                    f"Dynamic client registration failed: HTTP {resp.status_code} {resp.text[:200]}"
                )
            reg = resp.json()

        self._state["client_id"] = reg["client_id"]
        self._state["client_secret"] = reg.get("client_secret")
        self._save_state()
        logger.info("OAuth client registered", server=self.server_name, client_id=reg["client_id"])
        return reg["client_id"]

    # ---------- 授权码流程（PKCE + 本地回调） ----------

    async def start_authorization(self) -> Dict[str, Any]:
        """
        发起授权码流程。

        返回:
            {"authorization_url": str, "callback_port": int}
        授权码由内部启动的本地回调服务器接收。
        """
        metadata = await self.discover_metadata()

        # 启动本地回调服务器（随机端口）
        code_future: asyncio.Future = asyncio.get_running_loop().create_future()
        server = await asyncio.start_server(
            self._make_callback_handler(code_future),
            host="127.0.0.1",
            port=0,
        )
        port = server.sockets[0].getsockname()[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"

        client_id = await self.ensure_client(redirect_uri)

        # PKCE
        code_verifier = _b64url(secrets.token_bytes(48))
        code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(16)

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        scopes = self.config.get("scopes") or metadata.get("scopes_supported")
        if scopes:
            params["scope"] = " ".join(scopes) if isinstance(scopes, list) else scopes

        authorization_url = f"{metadata['authorization_endpoint']}?{urllib.parse.urlencode(params)}"

        self._pending = {
            "code_verifier": code_verifier,
            "state": state,
            "redirect_uri": redirect_uri,
            "code_future": code_future,
            "callback_server": server,
            "started_at": time.time(),
        }
        self._state["pending_auth"] = True
        self._save_state()

        logger.info("OAuth authorization started", server=self.server_name, callback_port=port)
        return {"authorization_url": authorization_url, "callback_port": port}

    def _make_callback_handler(self, code_future: asyncio.Future):
        """构造本地回调 HTTP 处理器"""
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                request_line = await asyncio.wait_for(reader.readline(), timeout=10)
                # 读完请求头
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=10)
                    if line in (b"\r\n", b"\n", b""):
                        break

                path = request_line.decode("utf-8", errors="replace").split(" ")[1]
                query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)

                if "error" in query:
                    body = f"<html><body><h1>Authorization failed</h1><p>{query['error'][0]}</p></body></html>"
                    if not code_future.done():
                        code_future.set_exception(
                            McpAuthError(f"Authorization denied: {query['error'][0]}")
                        )
                elif "code" in query:
                    body = ("<html><body><h1>Authorization successful</h1>"
                            "<p>You can close this page and return to Happy-Claw.</p></body></html>")
                    if not code_future.done():
                        code_future.set_result({
                            "code": query["code"][0],
                            "state": query.get("state", [""])[0],
                        })
                else:
                    body = "<html><body><h1>Invalid callback</h1></body></html>"

                response = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body.encode('utf-8'))}\r\n"
                    "Connection: close\r\n\r\n" + body
                )
                writer.write(response.encode("utf-8"))
                await writer.drain()
            except Exception as e:
                logger.warning("OAuth callback handler error", error=str(e))
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        return handler

    async def complete_authorization(self) -> Dict[str, Any]:
        """
        等待本地回调收到授权码并交换令牌（阻塞直到用户在浏览器完成授权）。
        """
        if not self._pending:
            raise McpAuthError("No pending authorization flow")

        pending = self._pending
        metadata = await self.discover_metadata()

        try:
            callback_data = await asyncio.wait_for(
                pending["code_future"],
                timeout=self.config.get("auth_timeout_seconds", AUTH_FLOW_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            raise McpAuthError("OAuth authorization timed out waiting for callback")
        finally:
            await self._stop_callback_server()

        if callback_data["state"] != pending["state"]:
            raise McpAuthError("OAuth state mismatch (possible CSRF)")

        token = await self._exchange_code(
            metadata["token_endpoint"],
            code=callback_data["code"],
            code_verifier=pending["code_verifier"],
            redirect_uri=pending["redirect_uri"],
        )
        self._store_token(token)
        self._state.pop("pending_auth", None)
        self._save_state()
        logger.info("OAuth authorization completed", server=self.server_name)
        return token

    async def _stop_callback_server(self):
        if self._pending and self._pending.get("callback_server"):
            try:
                self._pending["callback_server"].close()
                await self._pending["callback_server"].wait_closed()
            except Exception:
                pass
        self._pending = None

    async def _exchange_code(
        self,
        token_endpoint: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        """授权码换令牌"""
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if self._state.get("client_secret"):
            data["client_secret"] = self._state["client_secret"]
        return await self._token_request(token_endpoint, data)

    async def _token_request(self, token_endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """通用令牌请求"""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            raise McpAuthError(
                f"Token request failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()

    def _store_token(self, token: Dict[str, Any]):
        """保存令牌（计算绝对过期时间）"""
        expires_in = token.get("expires_in")
        token["expires_at"] = time.time() + float(expires_in) if expires_in else 0
        self._state["token"] = {
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token") or self._state.get("token", {}).get("refresh_token"),
            "expires_at": token["expires_at"],
            "token_type": token.get("token_type", "Bearer"),
            "scope": token.get("scope"),
        }
        self._save_state()

    # ---------- 令牌获取与刷新 ----------

    async def get_access_token(self, force_refresh: bool = False) -> str:
        """
        获取有效的 access_token。

        - 过期前 5 分钟自动刷新
        - 刷新失败抛出 McpAuthError（触发重认证）
        """
        token = self._state.get("token") or {}
        access_token = token.get("access_token")
        expires_at = token.get("expires_at", 0)

        if access_token and not force_refresh:
            # 未过期（含 5 分钟提前量）直接返回
            if not expires_at or expires_at - REFRESH_AHEAD_SECONDS > time.time():
                return access_token

        # 需要刷新
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise McpAuthError(
                f"Access token expired and no refresh_token available for server '{self.server_name}'"
            )

        metadata = await self.discover_metadata()
        try:
            new_token = await self._token_request(
                metadata["token_endpoint"],
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                },
            )
        except McpAuthError as e:
            # 刷新失败 → 需要重新认证
            self._state.pop("token", None)
            self._save_state()
            raise McpAuthError(f"Token refresh failed, re-authentication required: {e}")

        self._store_token(new_token)
        logger.info("OAuth token refreshed", server=self.server_name)
        return new_token["access_token"]

    # ---------- XAA 跨应用访问 ----------

    async def exchange_external_token(
        self,
        id_token: Optional[str] = None,
        jwt_assertion: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        XAA: 用浏览器获取的 id_token（RFC 8693 token-exchange）
        或 JWT 断言（RFC 7523）交换本服务器的访问令牌，避免反复弹浏览器。
        """
        metadata = await self.discover_metadata()
        token_endpoint = metadata["token_endpoint"]

        # 确保有 client（XAA 可能无需重定向，注册时使用占位符）
        if not self.client_id:
            await self.ensure_client("http://127.0.0.1:0/xaa")

        if jwt_assertion:
            # RFC 7523 JWT Bearer
            data = {
                "grant_type": JWT_BEARER_GRANT,
                "assertion": jwt_assertion,
                "client_id": self.client_id,
            }
        elif id_token:
            # RFC 8693 Token Exchange
            data = {
                "grant_type": TOKEN_EXCHANGE_GRANT,
                "subject_token": id_token,
                "subject_token_type": ID_TOKEN_TYPE,
                "requested_token_type": ACCESS_TOKEN_TYPE,
                "client_id": self.client_id,
            }
        else:
            raise McpAuthError("exchange_external_token requires id_token or jwt_assertion")

        if self._state.get("client_secret"):
            data["client_secret"] = self._state["client_secret"]

        token = await self._token_request(token_endpoint, data)
        # RFC 8693 返回 access_token；部分服务器可能返回 JWT
        self._store_token(token)
        logger.info("XAA token exchanged", server=self.server_name)
        return token

    # ---------- 清理 ----------

    def logout(self):
        """清除本地令牌与客户端信息"""
        self._state = {}
        self._storage.delete(self.server_name)
        logger.info("OAuth credentials cleared", server=self.server_name)
