"""测试分层工具注册表过滤功能"""
import sys
from pathlib import Path

# 直接运行时确保 app 模块可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# 先按应用启动顺序导入，避免 app.tools 包初始化时的循环导入
from app.api.v1 import agents, chat, tools, teams, tasks, events, memory, jobs  # noqa: F401

from app.tools.registry import (
    ToolRegistry,
    FilteredToolRegistry,
    create_filtered_tool_registry,
)


class TestFilteredToolRegistry:
    """测试 FilteredToolRegistry"""

    def setup_method(self):
        """创建测试用的基础注册表"""
        self.base = ToolRegistry()
        # 注册一些测试工具
        self.base.register(
            name="web_search",
            description="Search the web",
            handler=lambda: None,
            parameters={"type": "object", "properties": {}},
        )
        self.base.register(
            name="read_file",
            description="Read a file",
            handler=lambda: None,
            parameters={"type": "object", "properties": {}},
        )
        self.base.register(
            name="write_file",
            description="Write a file",
            handler=lambda: None,
            parameters={"type": "object", "properties": {}},
        )
        self.base.register(
            name="bash_command",
            description="Run bash command",
            handler=lambda: None,
            parameters={"type": "object", "properties": {}},
        )
        # MCP 工具
        self.base.register(
            name="mcp__demo__search",
            description="MCP demo search",
            handler=lambda: None,
            parameters={"type": "object", "properties": {}, "mcp_tool": True},
            metadata={"mcp_tool": True, "mcp_server": "demo"},
        )
        self.base.register(
            name="mcp__demo__fetch",
            description="MCP demo fetch",
            handler=lambda: None,
            parameters={"type": "object", "properties": {}, "mcp_tool": True},
            metadata={"mcp_tool": True, "mcp_server": "demo"},
        )
        self.base.register(
            name="mcp__other__tool1",
            description="MCP other tool",
            handler=lambda: None,
            parameters={"type": "object", "properties": {}, "mcp_tool": True},
            metadata={"mcp_tool": True, "mcp_server": "other"},
        )

    def test_no_filter_allows_all(self):
        """空过滤允许所有工具"""
        filtered = FilteredToolRegistry(self.base)
        tools = filtered.list_tools()
        assert len(tools) == 7

    def test_disallowed_exact_match(self):
        """disallowed 精确匹配"""
        filtered = FilteredToolRegistry(self.base, disallowed=["web_search"])
        tools = filtered.list_tools()
        tool_names = [t.name for t in tools]
        assert "web_search" not in tool_names
        assert "read_file" in tool_names

    def test_disallowed_mcp_server(self):
        """disallowed MCP 服务器维度排除"""
        filtered = FilteredToolRegistry(self.base, disallowed=["mcp__demo"])
        tools = filtered.list_tools()
        tool_names = [t.name for t in tools]
        # demo 服务器的所有工具都被排除
        assert "mcp__demo__search" not in tool_names
        assert "mcp__demo__fetch" not in tool_names
        # 其他 MCP 服务器的工具不受影响
        assert "mcp__other__tool1" in tool_names

    def test_allowed_whitelist(self):
        """allowed 白名单"""
        filtered = FilteredToolRegistry(self.base, allowed=["web_search", "read_file"])
        tools = filtered.list_tools()
        tool_names = [t.name for t in tools]
        assert set(tool_names) == {"web_search", "read_file"}

    def test_disallowed_priority_over_allowed(self):
        """disallowed 优先级高于 allowed"""
        filtered = FilteredToolRegistry(
            self.base,
            allowed=["web_search", "read_file"],
            disallowed=["web_search"],
        )
        tools = filtered.list_tools()
        tool_names = [t.name for t in tools]
        assert "web_search" not in tool_names
        assert "read_file" in tool_names

    def test_regex_pattern(self):
        """正则表达式匹配"""
        # 匹配以 _file 结尾的工具
        filtered = FilteredToolRegistry(self.base, disallowed=[".*_file"])
        tools = filtered.list_tools()
        tool_names = [t.name for t in tools]
        assert "read_file" not in tool_names
        assert "write_file" not in tool_names
        assert "web_search" in tool_names

    def test_wildcard_all(self):
        """通配符 * 匹配所有"""
        filtered = FilteredToolRegistry(self.base, disallowed=["*"])
        tools = filtered.list_tools()
        assert len(tools) == 0

    def test_get_filtered_tool(self):
        """get() 方法正确过滤"""
        filtered = FilteredToolRegistry(self.base, disallowed=["web_search"])
        assert filtered.get("web_search") is None
        assert filtered.get("read_file") is not None

    def test_get_handler_filtered(self):
        """get_handler() 方法正确过滤"""
        filtered = FilteredToolRegistry(self.base, disallowed=["web_search"])
        assert filtered.get_handler("web_search") is None
        assert filtered.get_handler("read_file") is not None

    def test_get_function_definitions_filtered(self):
        """get_function_definitions() 正确过滤"""
        filtered = FilteredToolRegistry(self.base, allowed=["web_search"])
        defs = filtered.get_function_definitions()
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "web_search"

    def test_regex_ending_pattern(self):
        """正则匹配以某结尾的工具名"""
        # file$ 表示以 file 结尾的工具
        filtered = FilteredToolRegistry(self.base, disallowed=["read_file$", "write_file$"])
        tools = filtered.list_tools()
        tool_names = [t.name for t in tools]
        assert "read_file" not in tool_names
        assert "write_file" not in tool_names
        assert "web_search" in tool_names

    def test_mcp_server_with_regex(self):
        """MCP 服务器模式与正则结合"""
        # 排除所有 mcp__ 开头的工具
        filtered = FilteredToolRegistry(self.base, disallowed=["mcp__.*"])
        tools = filtered.list_tools()
        tool_names = [t.name for t in tools]
        assert "mcp__demo__search" not in tool_names
        assert "mcp__demo__fetch" not in tool_names
        assert "mcp__other__tool1" not in tool_names
        assert "web_search" in tool_names


class TestCreateFilteredToolRegistry:
    """测试工厂函数"""

    def test_create_for_main_agent(self):
        """主 agent 过滤（基于 agent.entries 配置）"""
        # 由于 settings.json 中有 "Default Agent" 的配置，可以测试
        filtered = create_filtered_tool_registry(agent_name="Default Agent")
        assert isinstance(filtered, FilteredToolRegistry)
        # Default Agent 配置了 disallowed: ["web_search", "mcp__demo", "my*"]
        # 验证 web_search 被过滤
        assert filtered.get("web_search") is None

    def test_create_for_unknown_agent(self):
        """未知 agent 名称（无配置，不过滤）"""
        filtered = create_filtered_tool_registry(agent_name="Unknown Agent XYZ")
        assert isinstance(filtered, FilteredToolRegistry)
        # 没有配置，应该允许所有工具

    def test_create_for_subagent_level(self):
        """子 agent 按层级过滤"""
        filtered = create_filtered_tool_registry(subagent_level=0)
        assert isinstance(filtered, FilteredToolRegistry)
        # settings.json 中 level 0 的 disallowed 包含多个工具

    def test_create_for_team_mode(self):
        """团队成员模式过滤"""
        filtered = create_filtered_tool_registry(team_mode=True)
        assert isinstance(filtered, FilteredToolRegistry)
        # teams.tools 配置了 disallowed: ["web_search", "mcp__demo"]
        assert filtered.get("web_search") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
