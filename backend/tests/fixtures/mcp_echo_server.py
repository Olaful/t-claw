"""MCP stdio 回声测试服务器（供测试与示例配置使用）"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """回声工具：原样返回输入文本"""
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """加法工具：返回两数之和"""
    return a + b


if __name__ == "__main__":
    mcp.run()
