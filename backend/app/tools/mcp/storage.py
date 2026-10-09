"""MCP OAuth 令牌安全存储

等价于 getSecureStorage():
- 优先 keyring（macOS Keychain / Windows 凭据管理器 / Linux Secret Service）
- keyring 不可用时回退本地加密文件（Fernet，.happy-claw/memory/mcp-tokens/）
"""
import base64
import getpass
import hashlib
import json
import platform
import socket
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger()

_KEYRING_SERVICE = "happy-claw-mcp"


def _get_happyclaw_home() -> Path:
    try:
        from app.config import _HAPPYCLAW_HOME
        return _HAPPYCLAW_HOME
    except Exception:
        return Path.home() / ".happy-claw"


class _EncryptedFileStorage:
    """Fernet 加密文件存储（keyring 回退方案）"""

    def __init__(self):
        home = _get_happyclaw_home()
        self._dir = home / "memory" / "mcp-tokens"
        self._key_file = home / ".token_key"
        self._fernet = self._init_fernet()

    def _derive_key(self) -> bytes:
        """从机器名 + 用户名派生 Fernet 密钥"""
        material = f"{platform.node()}::{socket.gethostname()}::{getpass.getuser()}::happy-claw-mcp"
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            material.encode("utf-8"),
            b"happy-claw-mcp-salt",
            iterations=100_000,
        )
        return base64.urlsafe_b64encode(digest)

    def _init_fernet(self):
        from cryptography.fernet import Fernet
        # 已有密钥文件则复用，否则派生并保存
        if self._key_file.exists():
            try:
                key = self._key_file.read_bytes().strip()
                return Fernet(key)
            except Exception as e:
                logger.warning("Invalid token key file, regenerating", error=str(e))
        key = self._derive_key()
        try:
            self._key_file.parent.mkdir(parents=True, exist_ok=True)
            self._key_file.write_bytes(key)
            try:
                import os as _os
                if hasattr(_os, "chmod"):
                    _os.chmod(self._key_file, 0o600)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Cannot persist token key file (in-memory only)", error=str(e))
        return Fernet(key)

    def _path_for(self, server_name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in server_name)
        return self._dir / f"{safe}.enc"

    def load(self, server_name: str) -> Optional[Dict[str, Any]]:
        path = self._path_for(server_name)
        if not path.exists():
            return None
        try:
            token = self._fernet.decrypt(path.read_bytes())
            return json.loads(token.decode("utf-8"))
        except Exception as e:
            logger.warning("Failed to decrypt token file", server=server_name, error=str(e))
            return None

    def save(self, server_name: str, data: Dict[str, Any]):
        path = self._path_for(server_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = self._fernet.encrypt(json.dumps(data).encode("utf-8"))
        path.write_bytes(encrypted)

    def delete(self, server_name: str):
        path = self._path_for(server_name)
        if path.exists():
            try:
                path.unlink()
            except Exception as e:
                logger.warning("Failed to delete token file", server=server_name, error=str(e))


class _KeyringStorage:
    """系统凭据存储（Windows 凭据管理器 / macOS Keychain / Linux Secret Service）"""

    def load(self, server_name: str) -> Optional[Dict[str, Any]]:
        import keyring
        raw = keyring.get_password(_KEYRING_SERVICE, server_name)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupted keyring entry", server=server_name)
            return None

    def save(self, server_name: str, data: Dict[str, Any]):
        import keyring
        keyring.set_password(_KEYRING_SERVICE, server_name, json.dumps(data))

    def delete(self, server_name: str):
        import keyring
        try:
            keyring.delete_password(_KEYRING_SERVICE, server_name)
        except Exception:
            # 不存在时 keyring 会抛异常，忽略
            pass


class SecureStorage:
    """安全存储统一接口: load / save / delete(server_name)"""

    def __init__(self):
        self._backend = None
        self._backend_name = "none"
        self._init_backend()

    def _init_backend(self):
        # 1. 尝试 keyring
        try:
            import keyring
            from keyring.errors import KeyringError  # noqa: F401
            # 探测可用性（某些平台默认 backend 会抛异常）
            backend = keyring.get_keyring()
            if backend.priority <= 0:
                raise RuntimeError(f"keyring backend not usable: {backend}")
            self._backend = _KeyringStorage()
            self._backend_name = f"keyring({backend.name})"
            logger.info("MCP token storage backend selected", backend=self._backend_name)
            return
        except Exception as e:
            logger.info("keyring unavailable, falling back to encrypted file", error=str(e))

        # 2. 回退加密文件
        try:
            self._backend = _EncryptedFileStorage()
            self._backend_name = "encrypted-file"
            logger.info("MCP token storage backend selected", backend=self._backend_name)
        except Exception as e:
            logger.error("No secure storage backend available", error=str(e), exc_info=True)

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def load(self, server_name: str) -> Optional[Dict[str, Any]]:
        if self._backend is None:
            return None
        try:
            return self._backend.load(server_name)
        except Exception as e:
            logger.warning("Secure storage load failed", server=server_name, error=str(e))
            return None

    def save(self, server_name: str, data: Dict[str, Any]):
        if self._backend is None:
            logger.warning("No secure storage available, token not persisted", server=server_name)
            return
        try:
            self._backend.save(server_name, data)
        except Exception as e:
            logger.error("Secure storage save failed", server=server_name, error=str(e), exc_info=True)

    def delete(self, server_name: str):
        if self._backend is None:
            return
        try:
            self._backend.delete(server_name)
        except Exception as e:
            logger.warning("Secure storage delete failed", server=server_name, error=str(e))


# 全局单例（惰性创建，避免导入期副作用）
_storage_instance: Optional[SecureStorage] = None


def get_secure_storage() -> SecureStorage:
    """获取安全存储单例（等价 getSecureStorage()）"""
    global _storage_instance
    if _storage_instance is None:
        _storage_instance = SecureStorage()
    return _storage_instance
