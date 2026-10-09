"""测试 ToolSearch 延迟加载机制"""
import sys
from pathlib import Path

# 直接运行时确保 app 模块可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# 先按应用启动顺序导入，避免 app.tools 包初始化时的循环导入
from app.api.v1 import agents, chat, tools, teams, tasks, events, memory, jobs  # noqa: F401

from app.tools.registry import ToolRegistry, FilteredToolRegistry
from app.tools.tool_search import (
    BM25Index,
    ToolSearchEngine,
    _tokenize,
    create_tool_search_handler,
    get_tool_search_definition,
    notify_tools_changed,
    _active_engines,
)


# =============================================================================
# BM25Index 测试
# =============================================================================

class TestTokenize:
    """测试分词函数"""

    def test_basic_split(self):
        assert _tokenize("hello world") == ["hello", "world"]

    def test_underscore_split(self):
        assert _tokenize("web_search") == ["web", "search"]

    def test_mixed_separators(self):
        tokens = _tokenize("mcp__github--create_issue")
        assert "mcp" in tokens
        assert "github" in tokens
        assert "create" in tokens
        assert "issue" in tokens

    def test_lowercase(self):
        assert _tokenize("WebSearch") == ["websearch"]

    def test_empty(self):
        assert _tokenize("") == []
        assert _tokenize("___") == []


class TestBM25Index:
    """测试 BM25 检索引擎"""

    def setup_method(self):
        self.documents = [
            {
                "name": "web_search",
                "description": "Search the web for information using multiple providers",
                "param_names": ["query", "num_results"],
            },
            {
                "name": "task_create",
                "description": "Create a new persistent task with dependencies",
                "param_names": ["subject", "description", "blocks", "blocked_by"],
            },
            {
                "name": "cron_create",
                "description": "Create a scheduled task using cron expression",
                "param_names": ["schedule", "prompt", "name", "recurring"],
            },
            {
                "name": "mcp__github__create_issue",
                "description": "Create a new issue in a GitHub repository",
                "param_names": ["owner", "repo", "title", "body"],
            },
            {
                "name": "spawn_teammate",
                "description": "Spawn teammates to collaborate on tasks",
                "param_names": ["teammates", "team_name"],
            },
        ]
        self.index = BM25Index(self.documents)

    def test_exact_name_match(self):
        """精确名称匹配应排第一"""
        results = self.index.search("web_search", top_k=3)
        assert len(results) > 0
        assert results[0][0] == "web_search"

    def test_description_match(self):
        """描述关键词匹配"""
        results = self.index.search("scheduled cron job", top_k=3)
        assert len(results) > 0
        assert results[0][0] == "cron_create"

    def test_param_name_match(self):
        """参数名匹配"""
        results = self.index.search("github repository issue", top_k=3)
        assert len(results) > 0
        assert results[0][0] == "mcp__github__create_issue"

    def test_fuzzy_match(self):
        """模糊匹配 - 多个工具包含 create"""
        results = self.index.search("create task", top_k=5)
        assert len(results) >= 2
        names = [r[0] for r in results]
        assert "task_create" in names

    def test_no_results_fallback(self):
        """无结果时兜底子串匹配"""
        results = self.index.search("xyznonexistent", top_k=5)
        # 应该没有匹配
        assert len(results) == 0

    def test_partial_name_fallback(self):
        """部分名称兜底匹配"""
        results = self.index.search("teammate", top_k=5)
        assert len(results) > 0
        assert results[0][0] == "spawn_teammate"

    def test_empty_index(self):
        """空索引不报错"""
        empty_index = BM25Index([])
        results = empty_index.search("anything")
        assert results == []

    def test_empty_query(self):
        """空查询不报错"""
        results = self.index.search("")
        assert results == []

    def test_top_k_limit(self):
        """top_k 限制返回数量"""
        results = self.index.search("create", top_k=2)
        assert len(results) <= 2

    def test_rebuild(self):
        """重建索引"""
        new_docs = [
            {"name": "new_tool", "description": "A brand new tool", "param_names": ["arg1"]},
        ]
        self.index.build(new_docs)
        results = self.index.search("new tool", top_k=5)
        assert len(results) > 0
        assert results[0][0] == "new_tool"


# =============================================================================
# ToolSearchEngine 测试
# =============================================================================

class TestToolSearchEngine:
    """测试 ToolSearchEngine"""

    def setup_method(self):
        """创建测试用的 registry 和 engine"""
        self.registry = ToolRegistry()

        # 注册 always-load 工具
        for name in ["read_file", "write_file", "bash_command", "glob", "grep"]:
            self.registry.register(
                name=name,
                description=f"{name} tool",
                handler=lambda: None,
                parameters={"type": "object", "properties": {}},
            )

        # 注册 deferred 工具
        self.registry.register(
            name="web_search",
            description="Search the web for information",
            handler=lambda: None,
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        self.registry.register(
            name="task_create",
            description="Create a new persistent task",
            handler=lambda: None,
            parameters={"type": "object", "properties": {"subject": {"type": "string"}}},
        )
        self.registry.register(
            name="cron_create",
            description="Create a scheduled cron task",
            handler=lambda: None,
            parameters={"type": "object", "properties": {"schedule": {"type": "string"}}},
        )
        self.registry.register(
            name="mcp__github__create_issue",
            description="Create GitHub issue",
            handler=lambda: None,
            parameters={"type": "object", "properties": {"title": {"type": "string"}}},
            metadata={"mcp_tool": True},
        )

        self.config = {
            "always_load": ["read_file", "write_file", "bash_command", "glob", "grep", "tool_search"],
            "default_limit": 5,
            "max_limit": 10,
        }

        self.engine = ToolSearchEngine(registry=self.registry, config=self.config)

    def teardown_method(self):
        """清理全局引擎列表"""
        self.engine.dispose()

    def test_initial_state(self):
        """初始状态：deferred 工具未加载"""
        assert self.engine.deferred_count == 4  # web_search, task_create, cron_create, mcp__github__create_issue
        assert len(self.engine.loaded_tools) == 0

    def test_get_tools_for_llm_initial(self):
        """初始时只返回 always_load 工具"""
        tools = self.engine.get_tools_for_llm()
        names = [t["function"]["name"] for t in tools]
        assert "read_file" in names
        assert "write_file" in names
        assert "bash_command" in names
        # deferred 工具不应出现
        assert "web_search" not in names
        assert "task_create" not in names

    def test_search_loads_tool(self):
        """搜索后工具被加载到上下文"""
        result = self.engine.search("web search")
        assert result["loaded_count"] > 0
        assert any(m["name"] == "web_search" for m in result["matches"])

        # 搜索后 get_tools_for_llm 应包含该工具
        tools = self.engine.get_tools_for_llm()
        names = [t["function"]["name"] for t in tools]
        assert "web_search" in names

    def test_search_multiple(self):
        """多次搜索累积加载"""
        self.engine.search("web search")
        self.engine.search("create task")

        tools = self.engine.get_tools_for_llm()
        names = [t["function"]["name"] for t in tools]
        assert "web_search" in names
        assert "task_create" in names

    def test_search_no_duplicate_load(self):
        """重复搜索不会重复加载"""
        self.engine.search("web search")
        result = self.engine.search("web search")
        # 第二次搜索时 web_search 已加载，不会再 newly_loaded
        assert result["loaded_count"] == 0

    def test_search_limit(self):
        """搜索限制返回数量"""
        result = self.engine.search("create", limit=1)
        assert len(result["matches"]) <= 1

    def test_deferred_summary(self):
        """deferred 摘要包含未加载工具"""
        summary = self.engine.get_deferred_summary()
        assert "web_search" in summary
        assert "task_create" in summary
        assert "ToolSearch" in summary

        # 加载后摘要中不再出现
        self.engine.search("web search")
        summary = self.engine.get_deferred_summary()
        assert "web_search" not in summary
        assert "task_create" in summary

    def test_rebuild_index(self):
        """重建索引：新增工具后可被搜索"""
        # 注册新工具
        self.registry.register(
            name="new_mcp_tool",
            description="A new MCP tool for testing",
            handler=lambda: None,
            parameters={"type": "object", "properties": {}},
        )
        # 重建前搜不到
        result = self.engine.search("new mcp testing")
        names = [m["name"] for m in result["matches"]]
        assert "new_mcp_tool" not in names

        # 重建后可搜到
        self.engine.rebuild_index()
        result = self.engine.search("new mcp testing")
        names = [m["name"] for m in result["matches"]]
        assert "new_mcp_tool" in names

    def test_rebuild_preserves_loaded(self):
        """重建索引保留已加载状态"""
        self.engine.search("web search")
        assert "web_search" in self.engine.loaded_tools

        self.engine.rebuild_index()
        assert "web_search" in self.engine.loaded_tools

    def test_rebuild_removes_deleted_tools(self):
        """重建索引清理已删除的工具"""
        self.engine.search("web search")
        assert "web_search" in self.engine.loaded_tools

        # 注销工具
        self.registry.unregister("web_search")
        self.engine.rebuild_index()

        assert "web_search" not in self.engine.loaded_tools
        assert "web_search" not in self.engine.get_deferred_summary()


# =============================================================================
# 会话级隔离测试（同一 Agent 的多个会话 loaded 状态互不污染）
# =============================================================================

class TestConversationIsolation:
    """验证延迟工具加载状态按会话隔离"""

    def setup_method(self):
        self.registry = ToolRegistry()
        self.registry.register(
            name="read_file", description="Read file",
            handler=lambda: None, parameters={"type": "object", "properties": {}},
        )
        self.registry.register(
            name="web_search", description="Search the web for information",
            handler=lambda: None, parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        self.engine = ToolSearchEngine(
            registry=self.registry,
            config={"always_load": ["read_file", "tool_search"]},
        )

    def teardown_method(self):
        self.engine.dispose()

    def test_loaded_not_leaked_across_conversations(self):
        """会话 A 加载的工具不应出现在会话 B 的可用工具列表中"""
        # 会话 A 通过 tool_search 加载 web_search
        self.engine.search("web search", conversation_id="conv_A")
        names_a = [t["function"]["name"] for t in self.engine.get_tools_for_llm("conv_A")]
        assert "web_search" in names_a

        # 会话 B 未加载任何延迟工具，不应看到 web_search
        names_b = [t["function"]["name"] for t in self.engine.get_tools_for_llm("conv_B")]
        assert "web_search" not in names_b
        assert "read_file" in names_b  # always_load 仍然可用

    def test_deferred_summary_per_conversation(self):
        """deferred 摘要按会话生成：A 已加载则不列出，B 仍列出"""
        self.engine.search("web search", conversation_id="conv_A")
        assert "web_search" not in self.engine.get_deferred_summary("conv_A")
        assert "web_search" in self.engine.get_deferred_summary("conv_B")

    def test_loaded_tools_for(self):
        """loaded_tools_for 返回指定会话的加载集合"""
        self.engine.search("web search", conversation_id="conv_A")
        assert "web_search" in self.engine.loaded_tools_for("conv_A")
        assert "web_search" not in self.engine.loaded_tools_for("conv_B")

    def test_contextvar_dispatch(self):
        """未显式传 conversation_id 时通过 ContextVar 解析会话"""
        from app.tools.tool_search import current_conversation_id
        token = current_conversation_id.set("conv_ctx")
        try:
            self.engine.search("web search")
            assert "web_search" in self.engine.loaded_tools_for("conv_ctx")
        finally:
            current_conversation_id.reset(token)


# =============================================================================
# FilteredToolRegistry + ToolSearch 联动测试
# =============================================================================

class TestFilteredToolSearch:
    """测试 FilteredToolRegistry 与 ToolSearch 的联动"""

    def setup_method(self):
        self.base = ToolRegistry()
        # always-load
        self.base.register(
            name="read_file", description="Read file",
            handler=lambda: None, parameters={"type": "object", "properties": {}},
        )
        self.base.register(
            name="bash_command", description="Run command",
            handler=lambda: None, parameters={"type": "object", "properties": {}},
        )
        # deferred
        self.base.register(
            name="web_search", description="Search the web",
            handler=lambda: None, parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        self.base.register(
            name="task_create", description="Create a task",
            handler=lambda: None, parameters={"type": "object", "properties": {}},
        )
        self.base.register(
            name="forbidden_tool", description="This tool is forbidden",
            handler=lambda: None, parameters={"type": "object", "properties": {}},
        )

        # 创建过滤注册表：禁止 forbidden_tool
        self.filtered = FilteredToolRegistry(
            base=self.base,
            allowed=[],
            disallowed=["forbidden_tool"],
        )

        self.engine = ToolSearchEngine(
            registry=self.filtered,
            config={"always_load": ["read_file", "bash_command", "tool_search"]},
        )

    def teardown_method(self):
        self.engine.dispose()

    def test_filtered_tool_not_in_deferred(self):
        """被过滤的工具不出现在 deferred 目录中"""
        summary = self.engine.get_deferred_summary()
        assert "forbidden_tool" not in summary
        assert "web_search" in summary

    def test_search_skips_filtered_tool(self):
        """搜索结果不包含被过滤的工具"""
        result = self.engine.search("forbidden")
        names = [m["name"] for m in result["matches"]]
        assert "forbidden_tool" not in names

    def test_search_returns_allowed_deferred(self):
        """搜索结果包含允许的 deferred 工具"""
        result = self.engine.search("web search")
        names = [m["name"] for m in result["matches"]]
        assert "web_search" in names


# =============================================================================
# tool_search handler 测试
# =============================================================================

class TestToolSearchHandler:
    """测试 tool_search handler 工厂"""

    def setup_method(self):
        self.registry = ToolRegistry()
        self.registry.register(
            name="read_file", description="Read file",
            handler=lambda: None, parameters={"type": "object", "properties": {}},
        )
        self.registry.register(
            name="web_search", description="Search the web for info",
            handler=lambda: None, parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        self.engine = ToolSearchEngine(
            registry=self.registry,
            config={"always_load": ["read_file", "tool_search"]},
        )
        self.handler = create_tool_search_handler(self.engine)

    def teardown_method(self):
        self.engine.dispose()

    @pytest.mark.asyncio
    async def test_handler_returns_matches(self):
        """handler 返回搜索结果"""
        result = await self.handler(query="web search", limit=5)
        assert "matches" in result
        assert "loaded_count" in result
        assert len(result["matches"]) > 0
        assert result["matches"][0]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_handler_loads_tool(self):
        """handler 调用后工具被加载"""
        await self.handler(query="web search")
        tools = self.engine.get_tools_for_llm()
        names = [t["function"]["name"] for t in tools]
        assert "web_search" in names


# =============================================================================
# 全局通知机制测试
# =============================================================================

class TestNotifyToolsChanged:
    """测试全局通知机制"""

    def setup_method(self):
        self.registry = ToolRegistry()
        self.registry.register(
            name="read_file", description="Read file",
            handler=lambda: None, parameters={"type": "object", "properties": {}},
        )
        self.engine = ToolSearchEngine(
            registry=self.registry,
            config={"always_load": ["read_file", "tool_search"]},
        )

    def teardown_method(self):
        self.engine.dispose()

    def test_engine_registered_in_active_list(self):
        """引擎创建后自动注册到全局列表"""
        assert self.engine in _active_engines

    def test_dispose_removes_from_active_list(self):
        """dispose 后从全局列表移除"""
        self.engine.dispose()
        assert self.engine not in _active_engines

    def test_notify_rebuilds_all_engines(self):
        """notify_tools_changed 重建所有活跃引擎"""
        # 新增工具
        self.registry.register(
            name="new_tool", description="A new tool",
            handler=lambda: None, parameters={"type": "object", "properties": {}},
        )
        # 通知
        notify_tools_changed()
        # 验证索引已更新
        assert self.engine.deferred_count == 1
        result = self.engine.search("new tool")
        assert any(m["name"] == "new_tool" for m in result["matches"])


# =============================================================================
# get_tool_search_definition 测试
# =============================================================================

class TestToolSearchDefinition:
    """测试 tool_search 工具定义"""

    def test_definition_structure(self):
        """定义结构正确"""
        defn = get_tool_search_definition()
        assert defn["name"] == "tool_search"
        assert "description" in defn
        assert "parameters" in defn
        assert defn["parameters"]["type"] == "object"
        assert "query" in defn["parameters"]["properties"]
        assert "limit" in defn["parameters"]["properties"]
        assert "query" in defn["parameters"]["required"]
