"""团队协作工具（Agent Teams）

提供三个工具：
    - spawn_teammate:  由 Lead 创建队友（队友不能嵌套）
    - send_team_message: 成员间通信（指定队友 / 广播 / 结构化协议消息）
    - team_status:     查看团队成员与任务进度

仅在 `ENABLE_AGENT_TEAMS` 开启时注册。
"""
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


def _resolve_caller_team_context(caller_self) -> Optional[Dict[str, Any]]:
    """解析调用者所属团队上下文

    Returns:
        {"team_id","session_id","team_name","member_name","is_lead"} 或 None
    """
    from app.core.team_system.models import current_team_context

    # 1. 优先使用当前请求的 ContextVar
    ctx = current_team_context.get()
    if ctx is not None:
        return {
            "team_id": ctx.team_id,
            "session_id": ctx.session_id,
            "team_name": ctx.team_name,
            "member_name": ctx.member_name,
            "is_lead": ctx.is_lead,
        }

    # 2. 后备：按调用者自身作用域解析（不用 agent_id 全局映射，避免多会话互相覆盖）
    try:
        from app.core.team_system.manager import team_manager
        metadata = getattr(getattr(caller_self, "config", None), "metadata", {}) or {}
        # 2a. 队友：实例创建时 metadata 已写入 team_id，精确匹配自身团队
        team_id = metadata.get("team_id")
        if team_id:
            team = team_manager.get_team(team_id)
            if team:
                return {
                    "team_id": team.team_id,
                    "session_id": team.session_id,
                    "team_name": team.team_name,
                    "member_name": metadata.get("member_name") or "",
                    "is_lead": False,
                }
        # 2b. Lead：按当前会话查团队（支持同会话多团队并存，取最近创建者）
        conv = getattr(caller_self, "_current_conversation", None)
        if conv:
            team = team_manager.get_team_for_session(conv)
            if team:
                return {
                    "team_id": team.team_id,
                    "session_id": team.session_id,
                    "team_name": team.team_name,
                    "member_name": team.lead_name,
                    "is_lead": True,
                }
    except Exception as e:
        logger.debug("Failed to resolve team context via manager", error=str(e))
    return None


# ===== spawn_teammate =====

async def spawn_teammate(
    teammates: List[Dict[str, Any]],
    team_name: str = "team",
) -> Dict[str, Any]:
    """
    创建团队成员（队友）。仅团队负责人（Lead）可调用；队友不能嵌套创建队友。

    队友是多轮生命周期的 Agent：执行分配的任务后进入空闲，通过收件箱接收新消息，
    并可自动认领共享任务板上的未分配任务。团队共享任务存于 .happy-claw/tasks/{team-id}/。

    Args:
        teammates: 队友列表，每项为对象:
            - name (str): 队友名称（必填，团队内唯一）
            - role (str): 角色描述，定义其专业领域
            - description (str, 可选): 更详细的行为描述
            - task (str, 可选): 分配给该队友的初始任务
        team_name (str): 团队名称（同一会话内唯一，默认 "team"）

    Returns:
        {"success": True, "team_id": ..., "members": [...]} 或 {"success": False, "error": ...}

    Example:
        ```json
        {
            "team_name": "refactor-team",
            "teammates": [
                {"name": "researcher", "role": "Code research expert",
                 "task": "Analyze the auth module and list its public APIs"},
                {"name": "implementer", "role": "Senior developer",
                 "task": "Refactor the login flow based on researcher's findings"}
            ]
        }
        ```
    """
    try:
        from app.tools.builtin_tools import _get_parent_agent_info

        _, parent_conversation_id, _, caller_self = _get_parent_agent_info()

        # 队友不能嵌套
        metadata = getattr(getattr(caller_self, "config", None), "metadata", {}) or {}
        if metadata.get("is_teammate"):
            return {
                "success": False,
                "error": "Teammates cannot spawn further teammates (no nesting allowed).",
            }

        if not isinstance(teammates, list) or not teammates:
            return {"success": False, "error": "teammates must be a non-empty list"}

        from app.core.team_system.manager import team_manager

        lead_name = getattr(caller_self.config, "name", "team-lead")
        lead_agent_id = getattr(caller_self, "id", "")
        session_id = parent_conversation_id

        result = await team_manager.spawn_teammates(
            session_id=session_id,
            team_name=team_name,
            lead_name=lead_name,
            lead_agent_id=lead_agent_id,
            teammates=teammates,
        )
        return result
    except Exception as e:
        logger.error("spawn_teammate failed", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


# ===== send_team_message =====

async def send_team_message(
    to: str,
    message: Any,
    summary: str = "",
) -> Dict[str, Any]:
    """
    向团队成员发送消息。日常沟通一律用纯文本字符串。
    仅以下需要状态机处理的场景才用结构化协议消息（dict，须含 "type" 字段）:
        - plan_approval_request  提交计划待审（队友→Lead）
        - plan_approval_response 审批计划（Lead→队友，含 approved）
        - permission_request     请求操作审批（队友→Lead，含 request_id）
        - permission_response    审批操作（Lead→队友，含 request_id 与 approved）
        - shutdown_approved / shutdown_rejected  关机确认/拒绝（队友→Lead）
        请求-响应类消息需携带相同的 "request_id" 以配对。
    注意：
        - 分配任务请用任务板（task_create / task_update 指派 owner），不要发消息指派，
          禁止使用 `task_assignment` 类型。
        - `idle_notification`、`teammate_terminated`、`shutdown_request`、
          `team_permission_update` 由系统运行时自动发送，不接受手动发送。

    Args:
        to: 目标成员名称；"*" 表示广播给除自己外的所有活跃成员
        message: 消息内容。日常沟通用纯文本字符串；仅上述协议场景用 dict
        summary (str, 可选): 消息摘要（用于收件箱展示）

    Returns:
        {"success": True, "delivered_to": [...]} 或 {"success": False, "error": ...}

    Example:
        ```json
        {"to": "researcher", "message": "Please also cover the payment module."}
        ```
        ```json
        {"to": "team-lead",
         "message": {"type": "plan_approval_request", "request_id": "r1",
                     "plan": "Step 1: ..."}}
        ```
    """
    try:
        from app.tools.builtin_tools import _get_parent_agent_info

        _, _, _, caller_self = _get_parent_agent_info()
        ctx = _resolve_caller_team_context(caller_self)
        if ctx is None:
            return {
                "success": False,
                "error": "Not in a team context. Use spawn_teammate to create a team first.",
            }

        from app.core.team_system.store import team_store
        from app.core.team_system.models import TeamMessageTypes

        # 系统驱动的协议消息由运行时状态机发送，禁止 LLM 手动伪造
        #（如队友自行判断“我空闲了”不可靠，空闲以系统状态为准；
        #  任务指派走任务板，不走消息）
        if isinstance(message, dict) and message.get("type") in (
            TeamMessageTypes.IDLE_NOTIFICATION,
            TeamMessageTypes.TEAMMATE_TERMINATED,
            TeamMessageTypes.SHUTDOWN_REQUEST,
            TeamMessageTypes.TASK_ASSIGNMENT,
            TeamMessageTypes.TEAM_PERMISSION_UPDATE,
            TeamMessageTypes.MODE_SET_REQUEST,
        ):
            hint = (
                "Assign work via the task board (task_create / task_update owner) instead."
                if message.get("type") == TeamMessageTypes.TASK_ASSIGNMENT
                else "Report your progress as plain text instead."
            )
            return {
                "success": False,
                "error": (
                    f"'{message.get('type')}' is system-managed or not a valid manual message type; "
                    f"you cannot send it manually. {hint}"
                ),
            }

        result = team_store.send_team_message(
            session_id=ctx["session_id"],
            team_name=ctx["team_name"],
            from_name=ctx["member_name"],
            to=to,
            message=message,
            summary=summary,
        )
        return result
    except Exception as e:
        logger.error("send_team_message failed", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


# ===== team_status =====

async def team_status() -> Dict[str, Any]:
    """
    查看当前团队的状态：成员列表、活跃状态、颜色、实时运行状态（working/idle），
    以及共享任务板进度。Lead 可据此了解哪些队友空闲、可以接受新任务。
    （队友的空闲状态由系统状态机维护，本接口返回的 state 为唯一可靠来源。）

    Returns:
        {"success": True, "team_id":..., "members":[...], "tasks":{...}}
    """
    try:
        from app.tools.builtin_tools import _get_parent_agent_info

        _, _, _, caller_self = _get_parent_agent_info()
        ctx = _resolve_caller_team_context(caller_self)
        if ctx is None:
            return {"success": False, "error": "Not in a team context."}

        from app.core.team_system.manager import team_manager
        from app.core.team_system.store import team_store

        members = team_store.list_members(ctx["session_id"], ctx["team_name"])

        # 任务进度
        tasks_summary = {}
        try:
            from app.core.task_system.store import task_store
            tasks = await task_store.list_tasks(task_list_id=ctx["team_id"])
            for t in tasks:
                st = t.get("status", "unknown")
                tasks_summary[st] = tasks_summary.get(st, 0) + 1
            tasks_summary["total"] = len(tasks)
        except Exception:
            pass

        # 运行时状态补充
        team = team_manager.get_team(ctx["team_id"])
        runtime_states = {}
        if team:
            for name, rt in team.members.items():
                runtime_states[name] = rt.state

        enriched = []
        for m in members:
            name = m.get("name")
            enriched.append({
                **m,
                "runtimeState": runtime_states.get(name, "unknown"),
            })

        return {
            "success": True,
            "team_id": ctx["team_id"],
            "team_name": ctx["team_name"],
            "members": enriched,
            "tasks": tasks_summary,
        }
    except Exception as e:
        logger.error("team_status failed", error=str(e), exc_info=True)
        return {"success": False, "error": str(e)}


# ===== 注册 =====

def register_team_tools() -> int:
    """注册团队工具（仅在团队功能开启时），返回注册数量"""
    try:
        from app.config import settings
        from app.tools.registry import tool_registry, ToolPermissionEnum

        if not settings.get("ENABLE_AGENT_TEAMS", False):
            logger.info("Agent teams disabled, skip team tools registration")
            return 0

        tool_registry.register(
            name="spawn_teammate",
            description=(
                "Spawn one or more teammates (long-lived agents) to collaborate on a task. "
                "Only the team lead may call this; teammates cannot spawn nested teammates. "
                "Teammates share a task board and communicate via an async inbox."
            ),
            handler=spawn_teammate,
            parameters={
                "type": "object",
                "properties": {
                    "teammates": {
                        "type": "array",
                        "description": "List of teammates to create",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Unique teammate name"},
                                "role": {"type": "string", "description": "Role / expertise"},
                                "description": {"type": "string", "description": "Detailed behavior description"},
                                "task": {"type": "string", "description": "Initial task to assign"},
                            },
                            "required": ["name"],
                        },
                    },
                    "team_name": {
                        "type": "string",
                        "description": "Team name (unique within the session)",
                        "default": "team",
                    },
                },
                "required": ["teammates"],
            },
            required_role=ToolPermissionEnum.PUBLIC,
            timeout_seconds=120,
            metadata={"system_tool": False},
        )

        tool_registry.register(
            name="send_team_message",
            description=(
                "Send a message to a teammate or broadcast to the whole team. "
                "Use to='*' to broadcast. ALWAYS send plain text for everyday communication, "
                "questions and progress reports. Assign work via the task board (task_create / "
                "task_update), NOT via messages. Only use a structured dict with 'type' for: "
                "plan_approval_request/response, permission_request/response, "
                "shutdown_approved/rejected. Never use task_assignment, idle_notification or "
                "shutdown_request manually."
            ),
            handler=send_team_message,
            parameters={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Target teammate name, or '*' to broadcast",
                    },
                    "message": {
                        "description": "Message content: plain text string, or a structured protocol dict with 'type'",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Optional short summary for the inbox",
                    },
                },
                "required": ["to", "message"],
            },
            required_role=ToolPermissionEnum.PUBLIC,
            timeout_seconds=30,
            metadata={"system_tool": False},
        )

        tool_registry.register(
            name="team_status",
            description=(
                "Show the current team's members, active state, colors, and shared task board progress."
            ),
            handler=team_status,
            parameters={"type": "object", "properties": {}},
            required_role=ToolPermissionEnum.PUBLIC,
            timeout_seconds=15,
            metadata={"system_tool": False},
        )

        logger.info("Team tools registered", count=3)
        return 3
    except Exception as e:
        logger.error("Failed to register team tools", error=str(e), exc_info=True)
        return 0
