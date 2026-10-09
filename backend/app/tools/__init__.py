"""工具系统"""
from app.tools.registry import ToolRegistry, ToolResult, tool_registry, tool
from app.tools.permissions import PermissionManager, PermissionLevel, permission_manager
from app.tools.hooks import HookManager, HookContext, HookResult, hook_manager
from app.tools.mcp import MCPManager, mcp_manager
from app.tools.builtin_tools import register_builtin_tools
from app.tools.extension_loader import load_extensions, get_loaded_extensions
from app.tools.tool_search import (
    ToolSearchEngine, BM25Index, create_tool_search_handler,
    get_tool_search_definition, ensure_tool_search_registered, current_search_engine,
    current_conversation_id,
)

__all__ = [
    "ToolRegistry", "ToolResult", "tool_registry", "tool",
    "PermissionManager", "PermissionLevel", "permission_manager",
    "HookManager", "HookContext", "HookResult", "hook_manager",
    "MCPManager", "mcp_manager",
    "register_builtin_tools",
    "load_extensions", "get_loaded_extensions",
    "ToolSearchEngine", "BM25Index", "create_tool_search_handler",
    "get_tool_search_definition", "ensure_tool_search_registered", "current_search_engine",
    "current_conversation_id",
]
