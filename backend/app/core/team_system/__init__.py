"""Agent 团队协作系统（Agent Teams）

核心概念：
- 团队负责人（Lead）：主 Agent，负责规划、分配任务、审批与关机协调
- 队友（Teammate）：由 Lead 通过 spawn_teammate 创建，多轮工作、可相互通信
- 共享任务：任务以团队 ID 作为 task_list_id 存于 .happy-claw/tasks/{team-id}/
- 收件箱：每个成员一个 JSON 文件，消息通过文件锁保证并发安全

与子 agent 的区别：
    生命周期：队友多轮存活（子 agent 一次性）
    通信：    队友异步收件箱双向通信（子 agent 只回传结论）
    上下文：  队友通过消息共享信息（子 agent 完全隔离）
"""
from app.core.team_system.models import (
    TeamContext,
    TeamMessageTypes,
    TEAM_MESSAGE_TYPES,
    PROTOCOL_MESSAGE_TYPES,
    TEAMMATE_COLOR_POOL,
    current_team_id,
    current_team_context,
    get_current_team_id,
    make_team_id,
    build_teammate_injection,
    make_inbox_message,
    is_protocol_message,
)
from app.core.team_system.store import team_store, TeamStore

__all__ = [
    "TeamContext",
    "TeamMessageTypes",
    "TEAM_MESSAGE_TYPES",
    "PROTOCOL_MESSAGE_TYPES",
    "TEAMMATE_COLOR_POOL",
    "current_team_id",
    "current_team_context",
    "get_current_team_id",
    "make_team_id",
    "build_teammate_injection",
    "make_inbox_message",
    "is_protocol_message",
    "team_store",
    "TeamStore",
]
