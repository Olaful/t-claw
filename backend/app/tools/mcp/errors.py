"""MCP 异常类型定义"""


class McpError(Exception):
    """MCP 通用错误基类"""


class McpAuthError(McpError):
    """
    MCP 认证错误。

    触发场景:
    - 工具调用返回 401 / 令牌过期
    - OAuth 令牌刷新失败

    处理方式: 连接状态置为 auth_required，等待重新认证后重连。
    """


class McpConnectionError(McpError):
    """MCP 连接错误（终局性错误，如 ECONNRESET/ETIMEDOUT/EPIPE 等）"""


class McpTimeoutError(McpError):
    """MCP 工具调用超时"""


class McpConfigError(McpError):
    """MCP 配置错误（配置缺失、格式非法等）"""
