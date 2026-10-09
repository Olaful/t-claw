"""团队协作 - 消息协议、上下文变量与公共模型

消息协议（队友间通信）：
    纯文本消息双向通信；结构化协议消息通过 request_id 进行请求-响应配对。

消息类型一览：
    类型                          方向          用途
    plain text                   双向          普通队友间通信
    idle_notification            队友→Lead     队友完成一轮工作进入空闲
    permission_request           队友→Lead     队友需要操作审批
    permission_response          Lead→队友     Lead 审批结果
    plan_approval_request        队友→Lead     队友提交计划待审
    plan_approval_response       Lead→队友     Lead 审批计划
    shutdown_request             Lead→队友     请求体面关机（优先级最高）
    shutdown_approved            队友→Lead     确认关机
    shutdown_rejected            队友→Lead     拒绝关机（附原因）
    task_assignment              Lead→队友     分配任务
    team_permission_update       Lead→队友     广播权限变更
    mode_set_request             Lead→队友     修改队友的权限模式
    sandbox_permission_request   双向          网络权限请求
    sandbox_permission_response  双向          网络权限回复
    teammate_terminated          系统          队友被移除通知
"""
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ===== 消息类型常量 =====

class TeamMessageTypes:
    """团队消息类型常量"""
    PLAIN = "plain"
    IDLE_NOTIFICATION = "idle_notification"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_RESPONSE = "permission_response"
    PLAN_APPROVAL_REQUEST = "plan_approval_request"
    PLAN_APPROVAL_RESPONSE = "plan_approval_response"
    SHUTDOWN_REQUEST = "shutdown_request"
    SHUTDOWN_APPROVED = "shutdown_approved"
    SHUTDOWN_REJECTED = "shutdown_rejected"
    TASK_ASSIGNMENT = "task_assignment"
    TEAM_PERMISSION_UPDATE = "team_permission_update"
    MODE_SET_REQUEST = "mode_set_request"
    SANDBOX_PERMISSION_REQUEST = "sandbox_permission_request"
    SANDBOX_PERMISSION_RESPONSE = "sandbox_permission_response"
    TEAMMATE_TERMINATED = "teammate_terminated"


TEAM_MESSAGE_TYPES = (
    TeamMessageTypes.PLAIN,
    TeamMessageTypes.IDLE_NOTIFICATION,
    TeamMessageTypes.PERMISSION_REQUEST,
    TeamMessageTypes.PERMISSION_RESPONSE,
    TeamMessageTypes.PLAN_APPROVAL_REQUEST,
    TeamMessageTypes.PLAN_APPROVAL_RESPONSE,
    TeamMessageTypes.SHUTDOWN_REQUEST,
    TeamMessageTypes.SHUTDOWN_APPROVED,
    TeamMessageTypes.SHUTDOWN_REJECTED,
    TeamMessageTypes.TASK_ASSIGNMENT,
    TeamMessageTypes.TEAM_PERMISSION_UPDATE,
    TeamMessageTypes.MODE_SET_REQUEST,
    TeamMessageTypes.SANDBOX_PERMISSION_REQUEST,
    TeamMessageTypes.SANDBOX_PERMISSION_RESPONSE,
    TeamMessageTypes.TEAMMATE_TERMINATED,
)

# 结构化协议消息（非纯文本），收件箱注入时不直接进入对话，而是驱动状态机
PROTOCOL_MESSAGE_TYPES = frozenset({
    TeamMessageTypes.IDLE_NOTIFICATION,
    TeamMessageTypes.PERMISSION_REQUEST,
    TeamMessageTypes.PERMISSION_RESPONSE,
    TeamMessageTypes.PLAN_APPROVAL_REQUEST,
    TeamMessageTypes.PLAN_APPROVAL_RESPONSE,
    TeamMessageTypes.SHUTDOWN_REQUEST,
    TeamMessageTypes.SHUTDOWN_APPROVED,
    TeamMessageTypes.SHUTDOWN_REJECTED,
    TeamMessageTypes.TASK_ASSIGNMENT,
    TeamMessageTypes.TEAM_PERMISSION_UPDATE,
    TeamMessageTypes.MODE_SET_REQUEST,
    TeamMessageTypes.SANDBOX_PERMISSION_REQUEST,
    TeamMessageTypes.SANDBOX_PERMISSION_RESPONSE,
    TeamMessageTypes.TEAMMATE_TERMINATED,
})

# 需要请求-响应配对（request_id）的类型
REQUEST_RESPONSE_PAIRS = {
    TeamMessageTypes.PERMISSION_REQUEST: TeamMessageTypes.PERMISSION_RESPONSE,
    TeamMessageTypes.PLAN_APPROVAL_REQUEST: TeamMessageTypes.PLAN_APPROVAL_RESPONSE,
    TeamMessageTypes.SANDBOX_PERMISSION_REQUEST: TeamMessageTypes.SANDBOX_PERMISSION_RESPONSE,
}


# ===== 队友颜色池 =====

TEAMMATE_COLOR_POOL = [
    "#3498db",  # blue
    "#2ecc71",  # green
    "#e67e22",  # orange
    "#9b59b6",  # purple
    "#e74c3c",  # red
    "#1abc9c",  # cyan
    "#f1c40f",  # yellow
    "#34495e",  # dark slate
]

TEAMMATE_COLOR_NAMES = [
    "blue", "green", "orange", "purple", "red", "cyan", "yellow", "slate",
]


# ===== 上下文变量（多主 Agent 并发创建多团队时隔离） =====

# 当前调用链上的团队 ID（如 team-{session}-{team_name}），
# 任务系统据此将任务落到 .happy-claw/tasks/{team_id}/
current_team_id: ContextVar[Optional[str]] = ContextVar("current_team_id", default=None)

# 当前调用链上的团队完整上下文（见 TeamContext）
current_team_context: ContextVar[Optional["TeamContext"]] = ContextVar(
    "current_team_context", default=None
)


def get_current_team_id() -> Optional[str]:
    """获取当前上下文中的团队 ID（无则返回 None）"""
    return current_team_id.get()


# ===== 团队上下文模型 =====

@dataclass
class TeamContext:
    """团队成员的运行时上下文（存放于 ContextVar）"""
    team_id: str                        # 团队 ID（作为共享任务的 task_list_id）
    session_id: str                     # 归属会话 ID（Lead 的 conversation_id）
    team_name: str                      # 团队名称
    member_name: str                    # 当前成员名称（lead 名称或队友名称）
    is_lead: bool = False               # 是否为团队负责人
    lead_name: str = "team-lead"        # 负责人名称

    def to_dict(self) -> Dict[str, Any]:
        return {
            "team_id": self.team_id,
            "session_id": self.session_id,
            "team_name": self.team_name,
            "member_name": self.member_name,
            "is_lead": self.is_lead,
            "lead_name": self.lead_name,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TeamContext":
        return cls(
            team_id=data.get("team_id", ""),
            session_id=data.get("session_id", ""),
            team_name=data.get("team_name", ""),
            member_name=data.get("member_name", ""),
            is_lead=bool(data.get("is_lead", False)),
            lead_name=data.get("lead_name", "team-lead"),
        )


def make_team_id(session_id: str, team_name: str) -> str:
    """生成团队 ID：作为共享任务列表的 task_list_id"""
    return f"team-{session_id}-{team_name}"


# ===== 收件箱消息构建 =====

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_protocol_message(message: Any) -> bool:
    """判断消息是否为结构化协议消息（dict 且 type 在协议表内）"""
    return (
        isinstance(message, dict)
        and message.get("type") in PROTOCOL_MESSAGE_TYPES
    )


def make_inbox_message(
    from_name: str,
    message: Any,
    color: str = "",
    summary: str = "",
) -> Dict[str, Any]:
    """构建标准收件箱消息

    格式：
        {
          "from": "worker-1",
          "text": "纯文本" 或 协议消息 JSON 字符串,
          "timestamp": "2026-08-30T10:15:30Z",
          "read": false,
          "summary": "摘要",
          "color": "#3498db"
        }

    协议消息时额外携带 "protocol": dict（解析后的结构化消息）。
    """
    if is_protocol_message(message):
        text = json.dumps(message, ensure_ascii=False)
        msg_type = message.get("type", "")
        auto_summary = summary or f"[{msg_type}]"
    else:
        text = str(message)
        auto_summary = summary or (text[:60] + ("..." if len(text) > 60 else ""))

    entry = {
        "from": from_name,
        "text": text,
        "timestamp": _now_iso(),
        "read": False,
        "summary": auto_summary,
        "color": color,
    }
    if is_protocol_message(message):
        entry["protocol"] = message
    return entry


def parse_inbox_message_protocol(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析收件箱消息中的协议内容（非协议消息返回 None）"""
    if isinstance(entry.get("protocol"), dict):
        return entry["protocol"]
    text = entry.get("text", "")
    if isinstance(text, str) and text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("type") in PROTOCOL_MESSAGE_TYPES:
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ===== 队友消息注入构建 =====

def build_teammate_injection(entries: List[Dict[str, Any]]) -> str:
    """将收件箱消息构建为 <teammate-message> 包裹的注入文本

    每条消息格式：
        <teammate-message from="worker-1" time="...">
        消息内容
        </teammate-message>
    """
    blocks = []
    for entry in entries:
        protocol = parse_inbox_message_protocol(entry)
        if protocol:
            # 协议消息也以结构化形式注入，便于 LLM 理解上下文
            content = json.dumps(protocol, ensure_ascii=False, indent=2)
        else:
            content = entry.get("text", "")
        from_name = entry.get("from", "unknown")
        ts = entry.get("timestamp", "")
        blocks.append(
            f'<teammate-message from="{from_name}" time="{ts}">\n'
            f"{content}\n"
            f"</teammate-message>"
        )

    if not blocks:
        return ""

    header = (
        f"以下是你的队友发来的 {len(blocks)} 条新消息，请阅读并据此继续工作"
        f"（如需回复请使用 send_team_message 工具）：\n"
    )
    return header + "\n\n".join(blocks)
