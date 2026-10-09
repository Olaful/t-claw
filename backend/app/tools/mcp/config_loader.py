"""MCP 配置加载器

服务器配置来源（两处共用同一 schema）:
1. `.happy-claw/settings.json` 的 `mcp.servers` 段
2. `.happy-claw/plugin-mcp/.mcp.json` 自定义插件

同名时 settings.json 优先（并打警告）。
插件配置支持 `${ENV_VAR}` 与 `${HAPPY_CLAW_PLUGIN_ROOT}` 变量展开。
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# 支持的 transport 类型
SUPPORTED_TRANSPORTS = {"stdio", "sse", "http", "ws", "sdk"}

# 名称规范化：保留 [A-Za-z0-9_-]，其余替换为 _
_NAME_NORMALIZE_RE = re.compile(r"[^A-Za-z0-9_-]")
# 变量展开：${VAR}
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def normalize_name(name: str) -> str:
    """规范化服务器/工具名称（用于生成 mcp__<server>__<tool> 工具名）"""
    normalized = _NAME_NORMALIZE_RE.sub("_", name)
    return normalized or "_"


def make_tool_name(server_name: str, tool_name: str) -> str:
    """生成 MCP 工具在 tool_registry 中的名称: mcp__<normalizedServer>__<normalizedTool>"""
    return f"mcp__{normalize_name(server_name)}__{normalize_name(tool_name)}"


@dataclass
class MCPServerConfig:
    """单个 MCP 服务器配置"""
    name: str
    type: str = "stdio"  # stdio | sse | http | ws | sdk
    enabled: bool = True
    # stdio
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    # sse / http / ws
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    # sdk（进程内）: "module.path:factory_func"
    module: Optional[str] = None
    # OAuth
    oauth: Dict[str, Any] = field(default_factory=dict)
    # 单服务器超时覆盖（秒），未设置时用全局值
    call_timeout_seconds: Optional[float] = None
    connect_timeout_seconds: Optional[float] = None
    # 来源: settings | plugin
    source: str = "settings"

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.oauth.get("enabled", False))

    def validate(self) -> Optional[str]:
        """校验配置，返回错误信息；合法返回 None"""
        if self.type not in SUPPORTED_TRANSPORTS:
            return f"unsupported transport type: {self.type}"
        if self.type == "stdio" and not self.command:
            return "stdio server requires 'command'"
        if self.type in ("sse", "http", "ws") and not self.url:
            return f"{self.type} server requires 'url'"
        if self.type == "sdk" and not self.module:
            return "sdk server requires 'module' (format: 'pkg.mod:factory')"
        return None


@dataclass
class MCPGlobalConfig:
    """MCP 全局配置（来自 settings.json 的 mcp 段）"""
    enabled: bool = True
    call_timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    max_consecutive_errors: int = 3
    reconnect_delay_seconds: float = 5.0
    reconnect_backoff_max_seconds: float = 60.0


def _expand_vars(value: Any, extra_vars: Dict[str, str]) -> Any:
    """递归展开字符串中的 ${VAR} 变量（优先 extra_vars，其次环境变量）"""
    if isinstance(value, str):
        def _sub(m):
            var = m.group(1)
            if var in extra_vars:
                return extra_vars[var]
            env_val = os.environ.get(var)
            if env_val is None:
                logger.warning("MCP config variable not found, left empty", variable=var)
                return ""
            return env_val
        return _VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_vars(v, extra_vars) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_vars(v, extra_vars) for v in value]
    return value


def _parse_server(name: str, raw: Dict[str, Any], source: str) -> Optional[MCPServerConfig]:
    """解析单个服务器配置字典"""
    if not isinstance(raw, dict):
        logger.error("MCP server config is not an object", server=name)
        return None
    try:
        config = MCPServerConfig(
            name=name,
            type=raw.get("type", raw.get("transport", "stdio")),
            enabled=bool(raw.get("enabled", True)),
            command=raw.get("command"),
            args=list(raw.get("args", []) or []),
            env={str(k): str(v) for k, v in (raw.get("env", {}) or {}).items()},
            cwd=raw.get("cwd"),
            url=raw.get("url"),
            headers={str(k): str(v) for k, v in (raw.get("headers", {}) or {}).items()},
            module=raw.get("module"),
            oauth=dict(raw.get("oauth", {}) or {}),
            call_timeout_seconds=raw.get("call_timeout_seconds"),
            connect_timeout_seconds=raw.get("connect_timeout_seconds"),
            source=source,
        )
    except Exception as e:
        logger.error("Failed to parse MCP server config", server=name, error=str(e), exc_info=True)
        return None

    err = config.validate()
    if err:
        logger.error("Invalid MCP server config", server=name, error=err)
        return None
    return config


def _load_plugin_configs(plugin_dir: Path) -> Dict[str, MCPServerConfig]:
    """从 .happy-claw/plugin-mcp/.mcp.json 加载自定义插件服务器"""
    result: Dict[str, MCPServerConfig] = {}
    mcp_json = plugin_dir / ".mcp.json"
    if not mcp_json.exists():
        return result

    try:
        with open(mcp_json, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to parse plugin .mcp.json", path=str(mcp_json), error=str(e))
        return result

    # .mcp.json 允许两种结构: 直接是服务器映射，或包一层 "mcpServers"
    servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
    extra_vars = {"HAPPY_CLAW_PLUGIN_ROOT": str(plugin_dir)}

    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        expanded = _expand_vars(raw, extra_vars)
        config = _parse_server(name, expanded, source="plugin")
        if config:
            result[name] = config

    if result:
        logger.info("Loaded MCP plugin servers", count=len(result), path=str(mcp_json))
    return result


def _get_settings_mcp_section() -> Dict[str, Any]:
    """读取 settings.json 中的 mcp 段（原始 dict）"""
    try:
        from app.config import settings
        section = settings._json_config.get("mcp", {})
        if hasattr(section, "to_dict"):
            return section.to_dict()
        return dict(section) if isinstance(section, dict) else {}
    except Exception as e:
        logger.warning("Failed to read mcp section from settings.json", error=str(e))
        return {}


def load_global_config() -> MCPGlobalConfig:
    """加载 MCP 全局配置"""
    section = _get_settings_mcp_section()
    return MCPGlobalConfig(
        enabled=bool(section.get("enabled", True)),
        call_timeout_seconds=float(section.get("call_timeout_seconds", 30)),
        connect_timeout_seconds=float(section.get("connect_timeout_seconds", 10)),
        max_consecutive_errors=int(section.get("max_consecutive_errors", 3)),
        reconnect_delay_seconds=float(section.get("reconnect_delay_seconds", 5)),
        reconnect_backoff_max_seconds=float(section.get("reconnect_backoff_max_seconds", 60)),
    )


def load_server_configs() -> Dict[str, MCPServerConfig]:
    """
    加载所有 MCP 服务器配置。

    合并规则: 插件 (.mcp.json) 先加载，同名时 settings.json 优先并打警告。
    """
    merged: Dict[str, MCPServerConfig] = {}

    # 1. 插件目录
    try:
        from app.config import _HAPPYCLAW_HOME
        plugin_dir = _HAPPYCLAW_HOME / "plugin-mcp"
    except Exception:
        plugin_dir = Path.home() / ".happy-claw" / "plugin-mcp"

    if plugin_dir.exists():
        merged.update(_load_plugin_configs(plugin_dir))

    # 2. settings.json（同名覆盖插件配置）
    section = _get_settings_mcp_section()
    for name, raw in (section.get("servers", {}) or {}).items():
        if name in merged:
            logger.warning(
                "MCP server defined in both settings.json and plugin .mcp.json, "
                "settings.json takes priority",
                server=name,
            )
        config = _parse_server(name, raw, source="settings")
        if config:
            merged[name] = config

    return merged
