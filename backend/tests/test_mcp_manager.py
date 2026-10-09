"""MCP 管理器测试

覆盖:
1. 工具命名规范化（mcp__<server>__<tool>）
2. 配置加载（settings.json + plugin-mcp/.mcp.json 变量展开）
3. 内置优先（重名跳过）
4. tools 变更后注册表同步（diff 增删改）
5. stdio 端到端（工具发现、调用、断开注销）
"""
import sys
from pathlib import Path

# 直接运行时确保 app 模块可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# 先按应用启动顺序导入（与 main.py 一致），避免 app.tools 包初始化时的循环导入；
# 同时确保内置工具已注册（内置优先测试依赖）
from app.api.v1 import agents, chat, tools, teams, tasks, events, memory, jobs  # noqa: F401

from mcp import types as mcp_types

from app.tools.mcp.config_loader import (
    MCPServerConfig,
    _expand_vars,
    load_global_config,
    load_server_configs,
    make_tool_name,
    normalize_name,
)
from app.tools.mcp.engine import MCPServerConnection, MCPManager
from app.tools.registry import tool_registry

ECHO_SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py"


def _make_tool(name: str, description: str = "test tool") -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}, "required": []},
    )


class _FakeSession:
    """模拟 ClientSession（仅 list_tools）"""

    def __init__(self, tools):
        self.tools = tools

    async def list_tools(self):
        return mcp_types.ListToolsResult(tools=list(self.tools))


def _make_conn(name: str) -> MCPServerConnection:
    cfg = MCPServerConfig(name=name, type="stdio", command="dummy")
    return MCPServerConnection(config=cfg)


class TestNaming:
    def test_normalize_name(self):
        assert normalize_name("my server!") == "my_server_"
        assert normalize_name("Server-A_1") == "Server-A_1"
        assert normalize_name("中文服务") == "____"

    def test_make_tool_name(self):
        assert make_tool_name("Server A", "tool.b") == "mcp__Server_A__tool_b"
        assert make_tool_name("db-tools", "query") == "mcp__db-tools__query"


class TestConfigLoading:
    def test_global_config(self):
        cfg = load_global_config()
        assert cfg.enabled is True
        assert cfg.max_consecutive_errors == 3

    def test_server_configs_merged(self):
        configs = load_server_configs()
        # settings.json 中的示例服务器
        assert "echo-demo" in configs
        assert configs["echo-demo"].type == "stdio"
        # enabled 为用户可配置的运行时状态，此处仅校验字段被正确解析为布尔值
        assert isinstance(configs["echo-demo"].enabled, bool)
        # plugin-mcp/.mcp.json 中的插件服务器（变量已展开）
        assert "database-tools" in configs
        plugin_cfg = configs["database-tools"]
        assert plugin_cfg.source == "plugin"
        assert "${HAPPY_CLAW_PLUGIN_ROOT}" not in plugin_cfg.command
        assert "plugin-mcp" in plugin_cfg.command.replace("\\", "/")

    def test_expand_vars(self):
        result = _expand_vars(
            {"a": "${HAPPY_CLAW_PLUGIN_ROOT}/x", "b": ["${HAPPY_CLAW_PLUGIN_ROOT}"]},
            {"HAPPY_CLAW_PLUGIN_ROOT": "/root"},
        )
        assert result["a"] == "/root/x"
        assert result["b"] == ["/root"]


class TestBuiltinPriority:
    def test_conflict_skipped(self):
        """与内置/扩展工具重名时跳过（内置优先）"""
        manager = MCPManager()
        conn = _make_conn("prio")

        # 预先注册一个非 MCP 来源的同名工具
        async def _dummy(**kwargs):
            return "builtin"

        reg_name = make_tool_name("prio", "foo")
        tool_registry.register(
            name=reg_name,
            description="existing",
            handler=_dummy,
            parameters={"type": "object", "properties": {}},
            metadata={"source": "extension"},
        )
        try:
            result = manager._register_tool(conn, _make_tool("foo"))
            assert result is None  # 跳过注册
            # 注册表中仍是原来的扩展工具
            existing = tool_registry.get(reg_name)
            assert existing is not None
            assert existing.metadata.get("source") == "extension"
        finally:
            tool_registry.unregister(reg_name)

    def test_normal_registration(self):
        manager = MCPManager()
        conn = _make_conn("reg")
        reg_name = manager._register_tool(conn, _make_tool("bar"))
        try:
            assert reg_name == "mcp__reg__bar"
            reg = tool_registry.get(reg_name)
            assert reg is not None
            assert reg.metadata.get("mcp_tool") is True
            assert reg.metadata.get("mcp_server") == "reg"
        finally:
            tool_registry.unregister(reg_name)


class TestToolsChangedSync:
    @pytest.mark.asyncio
    async def test_refresh_diff(self):
        """tools 变更后 diff 同步注册表（新增/移除/变更）"""
        manager = MCPManager()
        conn = _make_conn("chg")

        # 初始: t1, t2
        conn.session = _FakeSession([_make_tool("t1", "v1"), _make_tool("t2")])
        await manager._refresh_tools(conn)
        assert tool_registry.get("mcp__chg__t1") is not None
        assert tool_registry.get("mcp__chg__t2") is not None

        # 变更: t1 描述改变，t2 移除，t3 新增
        conn.session = _FakeSession([_make_tool("t1", "v2"), _make_tool("t3")])
        await manager._refresh_tools(conn)

        assert tool_registry.get("mcp__chg__t1") is not None
        assert tool_registry.get("mcp__chg__t1").description == "v2"
        assert tool_registry.get("mcp__chg__t2") is None  # 已移除
        assert tool_registry.get("mcp__chg__t3") is not None  # 新增

        # 清理
        manager._cleanup_registered_tools(conn)
        assert tool_registry.get("mcp__chg__t1") is None
        assert tool_registry.get("mcp__chg__t3") is None


class TestStdioEndToEnd:
    @pytest.mark.asyncio
    async def test_stdio_connect_call_disconnect(self):
        """stdio 端到端：连接 → 工具发现 → 调用 → 断开注销"""
        manager = MCPManager()
        cfg = MCPServerConfig(
            name="echo-e2e",
            type="stdio",
            command=sys.executable,
            args=[str(ECHO_SERVER)],
        )
        conn = MCPServerConnection(config=cfg)
        manager._connections["echo-e2e"] = conn

        try:
            ok = await manager.connect("echo-e2e", timeout=30)
            assert ok, f"connect failed: {conn.last_error}"
            assert conn.status == "connected"

            # 工具已注册（含命名规范）
            reg_name = make_tool_name("echo-e2e", "echo")
            assert tool_registry.get(reg_name) is not None
            assert tool_registry.get(make_tool_name("echo-e2e", "add")) is not None

            # 调用 echo
            result = await manager.call_tool("echo-e2e", "echo", {"text": "hello-mcp"})
            assert result["isError"] is False
            texts = [c.get("text", "") for c in result["content"]]
            assert any("hello-mcp" in t for t in texts)

            # 调用 add
            result = await manager.call_tool("echo-e2e", "add", {"a": 1, "b": 2})
            assert result["isError"] is False
            all_text = " ".join(c.get("text", "") for c in result["content"])
            structured = result.get("structuredContent")
            assert "3" in all_text or (structured and 3 in [structured.get("result"), structured.get("return")])

            # 断开后工具被注销
            await manager.disconnect("echo-e2e")
            assert conn.status == "disconnected"
            assert tool_registry.get(reg_name) is None
        finally:
            conn.shutting_down = True
            conn.stop_requested = True
            conn.wake_event.set()
            if conn.supervisor and not conn.supervisor.done():
                conn.supervisor.cancel()
            manager._cleanup_registered_tools(conn)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
