"""工具管理API"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.schemas.tool import (
    CreateToolRequest, UpdateToolRequest, ToolResponse, ToolListResponse,
    ToolStatus, ToolCall,
)
from app.tools.registry import tool_registry
from app.tools.mcp import mcp_manager

router = APIRouter()


@router.post("/", response_model=ToolResponse)
async def create_tool(request: CreateToolRequest):
    """注册新工具"""
    # 工具需要通过代码注册，这里返回一个提示
    raise HTTPException(
        status_code=400,
        detail="Tools must be registered via code. Use the @tool decorator or tool_registry.register()",
    )


@router.get("/", response_model=ToolListResponse)
async def list_tools(status: str = None):
    """列出所有工具"""
    status_enum = ToolStatus(status) if status else None
    tools = tool_registry.list_tools(status=status_enum)
    
    return ToolListResponse(
        tools=[
            ToolResponse(
                id=t.id,
                name=t.name,
                description=t.description,
                function_definition=t.function_definition,
                required_role=t.required_role,
                status=t.status,
                timeout_seconds=t.timeout_seconds,
                created_at=t.created_at,
            )
            for t in tools
        ],
        total=len(tools),
    )


@router.get("/{tool_name}")
async def get_tool(tool_name: str):
    """获取工具详情"""
    tool = tool_registry.get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    return {
        "id": tool.id,
        "name": tool.name,
        "description": tool.description,
        "function_definition": tool.function_definition.model_dump(),
        "required_role": tool.required_role,
        "status": tool.status,
        "timeout_seconds": tool.timeout_seconds,
        "metadata": tool.metadata,
        "created_at": tool.created_at.isoformat(),
    }


@router.post("/{tool_name}/execute")
async def execute_tool(tool_name: str, arguments: dict):
    """执行工具"""
    tool_call = ToolCall(
        id="api_call",
        name=tool_name,
        arguments=arguments,
    )
    
    result = await tool_registry.execute(tool_call)
    
    return {
        "tool_call_id": result.tool_call_id,
        "name": result.name,
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "execution_time_ms": result.execution_time_ms,
    }


@router.get("/{tool_name}/function-definition")
async def get_function_definition(tool_name: str):
    """获取工具的Function Calling定义"""
    tool = tool_registry.get(tool_name)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    return {
        "type": "function",
        "function": {
            "name": tool.function_definition.name,
            "description": tool.function_definition.description,
            "parameters": tool.function_definition.parameters,
        },
    }


@router.put("/{tool_name}/status")
async def update_tool_status(tool_name: str, status: str):
    """更新工具状态"""
    status_enum = ToolStatus(status)
    success = tool_registry.update_status(tool_name, status_enum)
    
    if not success:
        raise HTTPException(status_code=404, detail="Tool not found")
    
    return {"message": "Status updated", "tool_name": tool_name, "status": status}


# ===== MCP服务器管理 =====

class McpAuthRequest(BaseModel):
    """OAuth 认证请求（支持 XAA）"""
    id_token: Optional[str] = None       # RFC 8693 token-exchange
    jwt_assertion: Optional[str] = None  # RFC 7523 JWT Bearer


@router.get("/mcp/servers")
async def list_mcp_servers():
    """列出 MCP 服务器（状态、工具数、认证状态）"""
    servers = mcp_manager.list_servers()
    return {"servers": servers, "total": len(servers)}


@router.get("/mcp/servers/{server_name}/tools")
async def list_mcp_server_tools(server_name: str):
    """列出指定服务器的工具（含 annotations）"""
    conn = mcp_manager.get_connection(server_name)
    if not conn:
        raise HTTPException(status_code=404, detail=f"MCP server not found: {server_name}")
    tools = mcp_manager.list_tools(server_name)
    return {
        "server": server_name,
        "status": conn.status,
        "tools": tools,
        "total": len(tools),
    }


@router.post("/mcp/servers/{server_name}/connect")
async def connect_mcp_server(server_name: str):
    """连接 MCP 服务器"""
    conn = mcp_manager.get_connection(server_name)
    if not conn:
        raise HTTPException(status_code=404, detail=f"MCP server not found: {server_name}")
    success = await mcp_manager.connect(server_name)
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Connection failed: {conn.last_error or 'timeout'}",
        )
    return {"message": "Connected", "server": server_name, "tools": len(conn.tools)}


@router.post("/mcp/servers/{server_name}/disconnect")
async def disconnect_mcp_server(server_name: str):
    """断开 MCP 服务器"""
    success = await mcp_manager.disconnect(server_name)
    if not success:
        raise HTTPException(status_code=404, detail=f"MCP server not found: {server_name}")
    return {"message": "Disconnected", "server": server_name}


@router.post("/mcp/servers/{server_name}/refresh-tools")
async def refresh_mcp_tools(server_name: str):
    """手动刷新工具列表（等价收到 notifications/tools/list_changed 后的行为）"""
    try:
        result = await mcp_manager.refresh_tools(server_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.post("/mcp/servers/{server_name}/auth")
async def start_mcp_auth(server_name: str, request: McpAuthRequest = None):
    """
    发起 OAuth 认证。

    - 不传参数: 返回授权 URL，本地回调服务器在后台等待授权码（PKCE）
    - 传 id_token: RFC 8693 token-exchange（XAA，无需弹浏览器）
    - 传 jwt_assertion: RFC 7523 JWT Bearer（XAA）
    """
    req = request or McpAuthRequest()
    try:
        result = await mcp_manager.start_auth(
            server_name,
            id_token=req.id_token,
            jwt_assertion=req.jwt_assertion,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.get("/mcp/servers/{server_name}/auth-status")
async def get_mcp_auth_status(server_name: str):
    """查询认证状态"""
    return mcp_manager.get_auth_status(server_name)


@router.get("/mcp/tools")
async def list_mcp_tools():
    """列出所有 MCP 工具（含 annotations）"""
    tools = mcp_manager.list_tools()
    return {"tools": tools, "total": len(tools)}
