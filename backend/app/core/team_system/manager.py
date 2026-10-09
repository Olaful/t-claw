"""团队协作 - 团队管理器（TeamManager）

职责：
    1. 团队创建与成员管理（复用 TeamStore 持久化）
    2. 队友（Teammate）多轮生命周期：工作循环、空闲轮询收件箱、自动认领任务
    3. 权限冒泡（双向轮询）：队友 permission_request → Lead 审批 → permission_response
    4. 关机协议与资源清理

队友与子 agent 的区别：
    - 子 agent：一次性、单向回传结论、完全隔离
    - 队友：多轮生命周期、异步收件箱双向通信、通过消息共享信息
"""
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import structlog

from app.core.team_system.models import (
    PROTOCOL_MESSAGE_TYPES,
    TeamContext,
    TeamMessageTypes,
    build_teammate_injection,
    current_team_context,
    current_team_id,
    make_team_id,
    parse_inbox_message_protocol,
)
from app.core.team_system.store import team_store

logger = structlog.get_logger()


def _setting(name: str, default: Any = None) -> Any:
    """从全局配置读取团队系统参数"""
    try:
        from app.config import settings
        val = settings.get(name)
        return default if val is None else val
    except Exception:
        return default


# 需要队友向 Lead 申请审批的高风险工具（权限冒泡触发点）
TEAM_PERMISSION_REQUIRED_TOOLS = {}


# ===== 运行时数据结构 =====

@dataclass
class TeammateRuntime:
    """单个队友的运行时状态"""
    name: str
    agent_id: str                                  # "name@team_name"
    agent: Any = None                              # SingleAgent 实例
    conversation_id: Optional[str] = None
    color: str = ""
    color_name: str = ""
    state: str = "starting"                        # starting/working/idle/stopped
    initial_task: str = ""
    role: str = ""
    stopping: bool = False
    work_task: Optional[asyncio.Task] = None
    # 待唤醒的请求-响应（request_id -> asyncio.Future）
    pending_responses: Dict[str, asyncio.Future] = field(default_factory=dict)
    # 临时工作区记录 (path, branch, repo)，队友退出时删除
    workspace_info: Optional[Tuple[str, str, str]] = None
    # 每个任务的认领/执行尝试次数（防止任务未完成时无限重新认领）
    task_attempts: Dict[str, int] = field(default_factory=dict)
    # 已上报过“卡住”的任务（避免重复通知 Lead）
    stuck_reported: set = field(default_factory=set)


@dataclass
class TeamRuntime:
    """单个团队的运行时状态"""
    team_id: str
    session_id: str
    team_name: str
    lead_name: str
    lead_agent_id: str
    members: Dict[str, TeammateRuntime] = field(default_factory=dict)
    shutting_down: bool = False


# ===== 团队管理器 =====

class TeamManager:
    """团队管理器（全局单例）"""

    def __init__(self):
        self._teams: Dict[str, TeamRuntime] = {}          # team_id -> TeamRuntime
        # session_id -> {team_name -> team_id}：同一会话可并存多个团队，互不覆盖。
        # 注意：不维护 agent_id -> team 的全局映射。
        #   - Lead 的 agent_id 跨会话复用，全局映射会被后建团队覆盖；
        #   - 队友 agent_id（name@team_name）跨会话同名团队会碰撞。
        # 后备查询一律走：队友 config.metadata.team_id / 会话映射。
        self._session_teams: Dict[str, Dict[str, str]] = {}

    # ===== 配置读取 =====

    @property
    def _inbox_poll(self) -> float:
        return max(0.2, float(_setting("TEAMS_INBOX_POLL_SECONDS", 1)))

    @property
    def _permission_poll(self) -> float:
        return max(0.1, float(_setting("TEAMS_PERMISSION_POLL_MS", 500)) / 1000.0)

    @property
    def _permission_timeout(self) -> float:
        return float(_setting("TEAMS_PERMISSION_TIMEOUT_SECONDS", 300))

    @property
    def _max_members(self) -> int:
        return int(_setting("TEAMS_MAX_MEMBERS", 10))

    @property
    def _shutdown_ack_timeout(self) -> float:
        return float(_setting("TEAMS_SHUTDOWN_ACK_TIMEOUT_SECONDS", 120))

    @property
    def _max_task_retries(self) -> int:
        return max(1, int(_setting("TEAMS_MAX_TASK_RETRIES", 3)))

    # ===== 查询（后备映射，ContextVar 跨请求不持久） =====
    #
    # 查询作用域原则（避免多会话/多团队相互覆盖）：
    #   - 队友：用自身 config.metadata.team_id（实例级精确）
    #   - Lead / 会话维度：用 session_id 映射（按会话隔离）
    # 不提供 agent_id -> team 的全局反查。

    def get_team(self, team_id: str) -> Optional[TeamRuntime]:
        return self._teams.get(team_id)

    def get_team_id_for_session(
        self,
        session_id: str,
        team_name: Optional[str] = None,
    ) -> Optional[str]:
        """会话 -> 团队 ID。指定 team_name 时精确查找；
        否则返回该会话最近创建的一个团队。"""
        teams = self._session_teams.get(session_id)
        if not teams:
            return None
        if team_name is not None:
            return teams.get(team_name)
        return next(reversed(teams.values()))

    def get_team_for_session(
        self,
        session_id: str,
        team_name: Optional[str] = None,
    ) -> Optional[TeamRuntime]:
        team_id = self.get_team_id_for_session(session_id, team_name)
        return self._teams.get(team_id) if team_id else None

    # ===== 团队创建 =====

    def ensure_team(
        self,
        session_id: str,
        team_name: str,
        lead_name: str,
        lead_agent_id: str,
    ) -> TeamRuntime:
        """确保团队存在（幂等）：创建目录、config、Lead 收件箱，并注册运行时映射"""
        team_id = make_team_id(session_id, team_name)

        if team_id in self._teams:
            return self._teams[team_id]

        # 持久化团队配置（含 Lead 成员）
        team_store.create_team(session_id, team_name, lead_name, lead_agent_id)

        team = TeamRuntime(
            team_id=team_id,
            session_id=session_id,
            team_name=team_name,
            lead_name=lead_name,
            lead_agent_id=lead_agent_id,
        )
        self._teams[team_id] = team
        self._session_teams.setdefault(session_id, {})[team_name] = team_id

        logger.info(
            "Team ensured",
            team_id=team_id,
            session_id=session_id,
            lead=lead_name,
        )
        return team

    # ===== 队友生成 =====

    async def spawn_teammates(
        self,
        session_id: str,
        team_name: str,
        lead_name: str,
        lead_agent_id: str,
        teammates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """生成一批队友并启动其工作循环

        Args:
            teammates: [{"name","role","description"?, "task"?}, ...]

        Returns:
            {"success": True, "team_id":..., "members":[...]}
        """
        team = self.ensure_team(session_id, team_name, lead_name, lead_agent_id)

        # 首个队友创建时设置 ContextVar（当前请求上下文内生效）
        current_team_id.set(team.team_id)
        current_team_context.set(TeamContext(
            team_id=team.team_id,
            session_id=session_id,
            team_name=team_name,
            member_name=lead_name,
            is_lead=True,
            lead_name=lead_name,
        ))

        created = []
        errors = []
        for spec in teammates:
            name = (spec.get("name") or "").strip()
            if not name:
                errors.append({"spec": spec, "error": "teammate name is required"})
                continue
            if name == lead_name:
                errors.append({"spec": spec, "error": "teammate name conflicts with lead"})
                continue
            if name in team.members:
                errors.append({"spec": spec, "error": f"teammate '{name}' already exists"})
                continue
            # 成员上限（含 Lead）
            if len(team.members) + 1 >= self._max_members:
                errors.append({"spec": spec, "error": f"team reached max members ({self._max_members})"})
                continue
            try:
                member = await self._spawn_one_teammate(
                    team=team,
                    name=name,
                    role=spec.get("role", ""),
                    description=spec.get("description", ""),
                    initial_task=spec.get("task", "") or spec.get("task_description", ""),
                )
                created.append({
                    "name": member.name,
                    "agentId": member.agent_id,
                    "color": member.color,
                    "colorName": member.color_name,
                    "role": member.role,
                })
            except Exception as e:
                logger.error("Failed to spawn teammate", name=name, error=str(e), exc_info=True)
                errors.append({"name": name, "error": str(e)})

        return {
            "success": len(created) > 0,
            "team_id": team.team_id,
            "team_name": team_name,
            "lead": lead_name,
            "members": [
                {
                    "name": m.name,
                    "agentId": m.agent_id,
                    "color": m.color,
                    "colorName": m.color_name,
                    "isLead": False,
                    "isActive": True,
                }
                for m in team.members.values()
            ],
            "created": created,
            "errors": errors,
        }

    async def _spawn_one_teammate(
        self,
        team: TeamRuntime,
        name: str,
        role: str,
        description: str,
        initial_task: str,
    ) -> TeammateRuntime:
        """创建单个队友实例并启动工作循环"""
        from app.agents.single import SingleAgent
        from app.core.memory import create_memory_manager
        from app.core.prompt_engine import create_prompt_engine
        from app.models.schemas.agent import AgentConfig
        from app.services.llm_service import create_llm_service
        from app.tools.registry import create_filtered_tool_registry

        agent_id = f"{name}@{team.team_name}"

        # 注册到团队配置（含颜色分配 + 收件箱初始化）
        member_entry = team_store.add_member(
            team.session_id, team.team_name, agent_id, name, agent_type="teammate"
        )

        # ✅ 为队友创建临时工作区（worktree，退出时删除）
        ws_info = await self._prepare_teammate_workspace(team, name)

        system_prompt = self._build_teammate_system_prompt(
            team=team, name=name, role=role, description=description
        )

        metadata = {
            "is_subagent": True,
            "is_teammate": True,
            "team_id": team.team_id,
            "team_name": team.team_name,
            "member_name": name,
        }
        if ws_info:
            metadata["workspace_path"] = ws_info[0]

        config = AgentConfig(
            id=agent_id,
            name=name,
            description=description or f"Teammate: {role}",
            system_prompt=system_prompt,
            is_subagent=True,           # 跳过入库与记忆钩子
            llm_model=_setting("DEFAULT_LLM_MODEL", "deepseek-v4-flash"),
            level=1,
            parent_agent_id=team.lead_agent_id,
            parent_agent_name=team.lead_name,
            parent_conversation_id=team.session_id,
            metadata=metadata,
        )

        # 根据团队配置创建过滤后的工具注册表
        filtered_registry = create_filtered_tool_registry(team_mode=True)

        agent = SingleAgent(
            config=config,
            llm_service=create_llm_service(),
            tool_registry=filtered_registry,
            memory_manager=create_memory_manager(),
            prompt_engine=create_prompt_engine(),
        )

        rt = TeammateRuntime(
            name=name,
            agent_id=agent_id,
            agent=agent,
            color=member_entry.get("color", ""),
            color_name=member_entry.get("colorName", ""),
            role=role,
            initial_task=initial_task,
            workspace_info=ws_info,
        )
        team.members[name] = rt

        # 启动工作循环
        rt.work_task = asyncio.create_task(
            self._teammate_loop(rt, team), name=f"teammate-{agent_id}"
        )

        logger.info(
            "Teammate spawned",
            team=team.team_name,
            teammate=name,
            agent_id=agent_id,
            color=rt.color_name,
        )
        return rt

    async def _prepare_teammate_workspace(
        self, team: TeamRuntime, name: str
    ) -> Optional[Tuple[str, str, str]]:
        """
        为队友创建临时工作区（git worktree），位于 lead 相应级别工作区下。
        
        按 agent.defaults.sandbox.scope 配置:
        - session: lead 会话工作区下 {lead}/{session}/{teammate}
        - agent:   lead 的 agent 工作区下 {lead}/{teammate}
        - shared:  全局共享工作区下 {teammate}
        """
        try:
            from app.core.workspace_isolation import workspace_isolation
            scope = workspace_isolation.get_scope()
            if scope == "shared":
                info = await workspace_isolation.create_subagent_workspace(None, None, name)
            elif scope == "agent":
                info = await workspace_isolation.create_subagent_workspace(
                    team.lead_name, None, name
                )
            else:
                info = await workspace_isolation.create_subagent_workspace(
                    team.lead_name, team.session_id, name
                )
            logger.info("Teammate workspace prepared", teammate=name, path=info[0], scope=scope)
            return info
        except Exception as e:
            logger.warning("Failed to prepare teammate workspace", teammate=name, error=str(e))
            return None

    def _remove_teammate_workspace(self, rt: TeammateRuntime) -> None:
        """删除队友临时工作区（worktree + 分支），幂等。同步执行以便在取消上下文中安全调用。"""
        if not rt.workspace_info:
            return
        path, branch, repo = rt.workspace_info
        rt.workspace_info = None
        try:
            from app.core.workspace_isolation import workspace_isolation
            workspace_isolation.remove_workspace_sync(path, branch=branch, repo=repo)
        except Exception as e:
            logger.warning("Failed to remove teammate workspace", path=path, error=str(e))

    def _build_teammate_system_prompt(
        self,
        team: TeamRuntime,
        name: str,
        role: str,
        description: str,
    ) -> str:
        """构建队友系统提示词（角色 + 团队协作指令）"""
        parts = [
            f"You are '{name}', a teammate in the agent team '{team.team_name}'.",
        ]
        if role:
            parts.append(f"Your role: {role}")
        if description:
            parts.append(f"About you: {description}")
        parts.append(
            "\n## Team Collaboration Rules\n"
            f"- The team lead is '{team.lead_name}'. You share a task board with the whole team.\n"
            "- Use `task_list` to inspect the shared task board, `task_create` to add tasks, "
            "and `task_update` to change task status (pending -> in_progress -> completed).\n"
            "- When you finish a unit of work, mark your task completed. You may then claim "
            "another unassigned task from the board, or wait for new instructions.\n"
            "- Use `send_team_message` to communicate. Set `to` to a teammate name, or `*` to "
            "broadcast. ALWAYS use plain text for questions, discussion and progress reports. "
            "Use a structured message (dict with 'type') ONLY for: `plan_approval_request`, "
            "`permission_request`, `shutdown_approved`/`shutdown_rejected`.\n"
            "- NEVER assign work via messages (no `task_assignment`): create a task on the board "
            "with `task_create` and assign it via `task_update` (owner).\n"
            "- Your idle/busy state is tracked by the system automatically — do NOT send "
            "`idle_notification` yourself. When you finish work, mark your task completed and "
            "report the result briefly.\n"
            "- You CANNOT spawn further teammates (teammates cannot nest).\n"
            "- When you receive a `shutdown_request`, finish your current work promptly and "
            "reply with a `shutdown_approved` structured message, then stop.\n"
            "- Be concise and focus on your assigned work."
        )
        return "\n".join(parts)

    # ===== 队友工作循环 =====

    async def _teammate_loop(self, rt: TeammateRuntime, team: TeamRuntime) -> None:
        """队友工作循环：初始任务 → 空闲通知 → 轮询收件箱 / 自动认领"""
        # 设置队友的上下文（任务落到团队任务板）
        ctx = TeamContext(
            team_id=team.team_id,
            session_id=team.session_id,
            team_name=team.team_name,
            member_name=rt.name,
            is_lead=False,
            lead_name=team.lead_name,
        )
        token_id = current_team_id.set(team.team_id)
        token_ctx = current_team_context.set(ctx)
        try:
            # 1. 执行初始任务（若有）
            if rt.initial_task:
                rt.state = "working"
                await self._run_turn(rt, rt.initial_task)

            # 2. 循环：空闲通知 → 等待下一份工作
            while not rt.stopping and not team.shutting_down:
                self._notify_idle(team, rt)
                rt.state = "idle"
                next_input = await self._wait_for_next_work(rt, team)
                if next_input is None:
                    break
                rt.state = "working"
                await self._run_turn(rt, next_input)
        except asyncio.CancelledError:
            logger.info("Teammate loop cancelled", teammate=rt.name)
        except Exception as e:
            logger.error("Teammate loop error", teammate=rt.name, error=str(e), exc_info=True)
        finally:
            current_team_id.reset(token_id)
            current_team_context.reset(token_ctx)
            rt.state = "stopped"
            team_store.set_member_active(team.session_id, team.team_name, rt.name, False)
            # ✅ 队友退出时删除其临时工作区与分支（同步执行，兼容取消上下文）
            self._remove_teammate_workspace(rt)
            logger.info("Teammate loop ended", teammate=rt.name, team=team.team_name)

    async def _run_turn(self, rt: TeammateRuntime, input_text: str) -> Optional[Any]:
        """消费队友一轮 process_message 流，返回最终 assistant 消息"""
        agent = rt.agent
        conv_id = rt.conversation_id
        final = None
        try:
            async for msg in agent.process_message(input_text, conversation_id=conv_id):
                if msg is not None:
                    final = msg
        except Exception as e:
            logger.error("Teammate turn failed", teammate=rt.name, error=str(e), exc_info=True)
        # 捕获会话 ID（首轮会创建新会话）
        rt.conversation_id = getattr(agent, "_current_conversation", None) or conv_id
        return final

    def _notify_idle(self, team: TeamRuntime, rt: TeammateRuntime) -> None:
        """队友轮次结束 → 向 Lead 发送 idle_notification"""
        try:
            team_store.send_team_message(
                session_id=team.session_id,
                team_name=team.team_name,
                from_name=rt.name,
                to=team.lead_name,
                message={
                    "type": TeamMessageTypes.IDLE_NOTIFICATION,
                    "from": rt.name,
                    "state": "idle",
                },
                summary=f"{rt.name} is idle",
            )
        except Exception as e:
            logger.warning("Failed to send idle notification", teammate=rt.name, error=str(e))

    async def _wait_for_next_work(
        self,
        rt: TeammateRuntime,
        team: TeamRuntime,
    ) -> Optional[str]:
        """空闲轮询：收件箱优先（关机 > 普通消息），无消息时尝试自动认领任务

        Returns:
            下一轮输入文本；None 表示应退出（关机/团队关闭）
        """
        while not rt.stopping and not team.shutting_down:
            await asyncio.sleep(self._inbox_poll)

            entries = team_store.read_unread(team.session_id, team.team_name, rt.name)
            if entries:
                # 关机请求优先级最高
                for e in entries:
                    proto = parse_inbox_message_protocol(e)
                    if proto and proto.get("type") == TeamMessageTypes.SHUTDOWN_REQUEST:
                        self._respond_shutdown(team, rt, proto)
                        rt.stopping = True
                        return None

                # 请求-响应唤醒（permission/plan approval 等）
                injectable = []
                for e in entries:
                    proto = parse_inbox_message_protocol(e)
                    if proto and self._maybe_wake_pending(rt, proto):
                        continue  # 已交给等待中的 Future，不注入对话
                    injectable.append(e)

                if injectable:
                    return build_teammate_injection(injectable)
                # 仅有唤醒类协议消息，继续轮询
                continue

            # 无消息：尝试自动认领未分配、未阻塞的任务
            claimed = await self._try_auto_claim(rt, team)
            if claimed:
                return claimed
        return None

    def _maybe_wake_pending(self, rt: TeammateRuntime, proto: Dict[str, Any]) -> bool:
        """若协议消息匹配等待中的请求-响应，则唤醒对应 Future，返回 True"""
        request_id = proto.get("request_id")
        if not request_id:
            return False
        fut = rt.pending_responses.get(request_id)
        if fut and not fut.done():
            rt.pending_responses.pop(request_id, None)
            fut.set_result(proto)
            return True
        return False

    # ===== 自动认领（Self-claim） =====

    async def _try_auto_claim(
        self,
        rt: TeammateRuntime,
        team: TeamRuntime,
    ) -> Optional[str]:
        """空闲队友自动认领任务，成功则返回任务输入文本

        可认领两类任务：
            1. 未分配（无 owner）的待办任务 —— 自由认领；
            2. Lead 通过 task_update(owner=...) 指派给本队友的任务 ——
               owner 匹配自己（name 或 agent_id）时接受并开工。
        """
        try:
            from app.core.task_system.store import task_store
            tasks = await task_store.list_tasks(task_list_id=team.team_id)
        except Exception as e:
            logger.debug("Auto-claim list failed", error=str(e))
            return None

        for t in tasks:
            owner = t.get("owner")
            status = t.get("status")
            assigned_to_me = bool(owner) and owner in (rt.name, rt.agent_id)
            if assigned_to_me:
                # Lead 指派给我的任务：pending / in_progress 都接受开工；
                # 已完成/已取消的跳过（避免重复消费）
                if status not in ("pending", "in_progress"):
                    continue
            else:
                # 非指派任务：仅自由认领无 owner 的 pending 任务
                if owner or status != "pending":
                    continue
            task_id = t.get("id")
            if not task_id:
                continue
            # 重试上限：任务尝试达上限仍未完成（如 process_message 反复出错、
            # 任务未被更新），停止重新认领，避免“认领→失败→空闲→再认领”
            # 死循环及向 Lead 刷屏 idle 通知；首次发现卡住时一次性上报。
            attempts = rt.task_attempts.get(task_id, 0)
            if attempts >= self._max_task_retries:
                if task_id not in rt.stuck_reported:
                    rt.stuck_reported.add(task_id)
                    self._report_stuck_task(team, rt, t, attempts)
                continue
            try:
                # 指派给本队友的任务用已有 owner 认领（保持归属一致），
                # 未分配任务用 agent_id 认领
                result = await task_store.claim_task(
                    task_id,
                    owner=owner if assigned_to_me else rt.agent_id,
                    task_list_id=team.team_id,
                )
            except Exception as e:
                logger.debug("Auto-claim failed", task_id=task_id, error=str(e))
                continue
            if result.get("ok"):
                rt.task_attempts[task_id] = attempts + 1
                task = result.get("task", {})
                logger.info(
                    "Teammate claimed task",
                    teammate=rt.name, task_id=task_id, assigned=assigned_to_me,
                )
                source = (
                    "This task was assigned to you by the team lead.\n"
                    if assigned_to_me
                    else "You have claimed a task from the shared team task board.\n"
                )
                return (
                    f"{source}"
                    f"Task ID: {task_id}\n"
                    f"Subject: {task.get('subject', '')}\n"
                    f"Description: {task.get('description', '') or '(none)'}\n\n"
                    f"Please complete this task. Use `task_update` to mark it "
                    f"`completed` when done."
                )
        return None

    def _report_stuck_task(
        self,
        team: TeamRuntime,
        rt: TeammateRuntime,
        task: Dict[str, Any],
        attempts: int,
    ) -> None:
        """任务多次尝试失败卡住时，一次性通知 Lead 重新处置"""
        task_id = task.get("id", "")
        try:
            team_store.send_team_message(
                session_id=team.session_id,
                team_name=team.team_name,
                from_name=rt.name,
                to=team.lead_name,
                message=(
                    f"I attempted task {task_id} ({task.get('subject', '')}) "
                    f"{attempts} time(s) but could not complete it "
                    f"(execution kept failing or the task was never updated). "
                    f"I have stopped retrying. Please reassign it or handle it yourself."
                ),
                summary=f"{rt.name} stuck on task {task_id}",
            )
            logger.warning(
                "Teammate stuck on task, lead notified",
                teammate=rt.name, task_id=task_id, attempts=attempts,
            )
        except Exception as e:
            logger.warning("Failed to report stuck task", error=str(e))

    # ===== 权限冒泡（队友侧，双向轮询） =====

    async def request_permission(
        self,
        team: TeamRuntime,
        member_name: str,
        operation: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """队友向 Lead 申请操作审批（阻塞等待响应）

        流程：
            写 permission_request 到 Lead 收件箱 → 500ms 轮询自己收件箱的
            permission_response（按 request_id 匹配）→ 超时则拒绝
        """
        request_id = str(uuid.uuid4())
        payload = {
            "type": TeamMessageTypes.PERMISSION_REQUEST,
            "request_id": request_id,
            "from": member_name,
            "operation": operation,
            "details": details or {},
        }
        try:
            team_store.send_team_message(
                session_id=team.session_id,
                team_name=team.team_name,
                from_name=member_name,
                to=team.lead_name,
                message=payload,
                summary=f"{member_name} requests permission: {operation}",
            )
        except Exception as e:
            logger.error("Failed to send permission request", error=str(e))
            return False

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        rt = team.members.get(member_name)
        if rt is not None:
            rt.pending_responses[request_id] = fut

        deadline = loop.time() + self._permission_timeout
        try:
            while not fut.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.warning(
                        "Permission request timed out",
                        teammate=member_name,
                        request_id=request_id,
                    )
                    return False
                # 主动轮询收件箱（被工具调用阻塞时，工作循环不在运行）
                await asyncio.sleep(min(self._permission_poll, remaining))
                entries = team_store.read_unread(team.session_id, team.team_name, member_name)
                for e in entries:
                    proto = parse_inbox_message_protocol(e)
                    if not proto:
                        continue
                    if (
                        proto.get("type") == TeamMessageTypes.PERMISSION_RESPONSE
                        and proto.get("request_id") == request_id
                    ):
                        approved = bool(proto.get("approved", False))
                        logger.info(
                            "Permission resolved",
                            teammate=member_name,
                            request_id=request_id,
                            approved=approved,
                        )
                        return approved
                    # 其他协议消息尝试唤醒（非本请求则忽略）
                    if rt is not None:
                        self._maybe_wake_pending(rt, proto)
        finally:
            if rt is not None:
                rt.pending_responses.pop(request_id, None)
        return False

    # ===== 关机协议 =====

    def _respond_shutdown(
        self,
        team: TeamRuntime,
        rt: TeammateRuntime,
        proto: Dict[str, Any],
    ) -> None:
        """队友响应关机请求：回复 shutdown_approved"""
        try:
            team_store.send_team_message(
                session_id=team.session_id,
                team_name=team.team_name,
                from_name=rt.name,
                to=team.lead_name,
                message={
                    "type": TeamMessageTypes.SHUTDOWN_APPROVED,
                    "request_id": proto.get("request_id", ""),
                    "from": rt.name,
                },
                summary=f"{rt.name} approved shutdown",
            )
        except Exception as e:
            logger.warning("Failed to send shutdown_approved", teammate=rt.name, error=str(e))

    async def shutdown_teammate(self, team: TeamRuntime, name: str) -> bool:
        """Lead 请求单个队友关机并等待确认"""
        rt = team.members.get(name)
        if not rt:
            return False
        request_id = str(uuid.uuid4())
        try:
            team_store.send_team_message(
                session_id=team.session_id,
                team_name=team.team_name,
                from_name=team.lead_name,
                to=name,
                message={
                    "type": TeamMessageTypes.SHUTDOWN_REQUEST,
                    "request_id": request_id,
                    "from": team.lead_name,
                },
                summary=f"Shutdown request to {name}",
            )
        except Exception as e:
            logger.warning("Failed to send shutdown_request", teammate=name, error=str(e))

        # 等待 shutdown_approved（或超时强制清理）
        deadline = asyncio.get_running_loop().time() + self._shutdown_ack_timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self._inbox_poll)
            entries = team_store.read_unread(team.session_id, team.team_name, team.lead_name)
            approved = False
            for e in entries:
                proto = parse_inbox_message_protocol(e)
                if (
                    proto
                    and proto.get("type") == TeamMessageTypes.SHUTDOWN_APPROVED
                    and proto.get("request_id") == request_id
                ):
                    approved = True
                    break
            if approved:
                break
            if rt.state == "stopped":
                break

        await self._finalize_teammate_removal(team, name)
        return True

    async def shutdown_team(self, team: TeamRuntime) -> Dict[str, Any]:
        """关闭整个团队：向所有队友发关机请求 → 清理"""
        team.shutting_down = True
        names = list(team.members.keys())
        results = {}
        for name in names:
            try:
                results[name] = await self.shutdown_teammate(team, name)
            except Exception as e:
                logger.warning("Shutdown teammate failed", teammate=name, error=str(e))
                results[name] = False
        return {"success": True, "team_id": team.team_id, "shutdown": results}

    async def _finalize_teammate_removal(self, team: TeamRuntime, name: str) -> None:
        """队友关机确认后：取消任务、广播终止通知、unassign 任务、移除成员"""
        rt = team.members.pop(name, None)

        # 取消工作循环
        if rt and rt.work_task and not rt.work_task.done():
            rt.work_task.cancel()
            try:
                await rt.work_task
            except (asyncio.CancelledError, Exception):
                pass

        # unassign 该队友名下的未完成任务（owner 置空 -> 用 ""，因 update 会跳过 None）
        try:
            from app.core.task_system.store import task_store
            agent_id = rt.agent_id if rt else f"{name}@{team.team_name}"
            owned = await task_store.list_tasks(owner=agent_id, task_list_id=team.team_id)
            for t in owned:
                if t.get("status") in ("pending", "in_progress"):
                    await task_store.update_task(
                        t.get("id"), {"owner": ""}, task_list_id=team.team_id
                    )
        except Exception as e:
            logger.warning("Failed to unassign teammate tasks", teammate=name, error=str(e))

        # 广播 teammate_terminated
        try:
            team_store.send_team_message(
                session_id=team.session_id,
                team_name=team.team_name,
                from_name=team.lead_name,
                to="*",
                message={
                    "type": TeamMessageTypes.TEAMMATE_TERMINATED,
                    "teammate": name,
                },
                summary=f"Teammate {name} terminated",
            )
        except Exception:
            pass

        logger.info("Teammate finalized", teammate=name, team=team.team_name)

    # ===== 清理 =====

    async def cleanup_team(self, session_id: str, team_name: str) -> None:
        """清理团队：停止所有队友并删除团队目录与团队任务目录"""
        team_id = make_team_id(session_id, team_name)
        team = self._teams.get(team_id)
        if team:
            team.shutting_down = True
            for name in list(team.members.keys()):
                rt = team.members.pop(name, None)
                if rt and rt.work_task and not rt.work_task.done():
                    rt.work_task.cancel()
                    try:
                        await rt.work_task
                    except (asyncio.CancelledError, Exception):
                        pass
            self._teams.pop(team_id, None)
            teams = self._session_teams.get(session_id)
            if teams and teams.get(team_name) == team_id:
                teams.pop(team_name, None)
                if not teams:
                    self._session_teams.pop(session_id, None)

        # 删除团队目录
        try:
            team_store.remove_team_dir(session_id, team_name)
        except Exception as e:
            logger.warning("Failed to remove team dir", team=team_name, error=str(e))

        # 删除团队任务目录
        try:
            from app.core.task_system.store import task_store
            task_store.remove_task_list(team_id)
        except Exception as e:
            logger.warning("Failed to remove team task list", team_id=team_id, error=str(e))

        logger.info("Team cleaned up", session_id=session_id, team=team_name)

    async def cleanup_session(self, session_id: str) -> List[str]:
        """会话删除时：停止该会话全部团队任务并清理目录"""
        cleaned = []
        # 运行时团队
        team_ids = [
            tid for tid, t in self._teams.items() if t.session_id == session_id
        ]
        for tid in team_ids:
            team = self._teams.get(tid)
            if team:
                await self.cleanup_team(session_id, team.team_name)
                cleaned.append(team.team_name)

        # 磁盘上残留的团队（应用重启后无运行时实例，从磁盘兜底）。
        # 复用 cleanup_team：无运行时实例时仅做文件清理（团队目录 + 任务目录）
        try:
            for team_name in team_store.list_teams_for_session(session_id):
                if team_name not in cleaned:
                    await self.cleanup_team(session_id, team_name)
                    cleaned.append(team_name)
            team_store.remove_session_teams(session_id)
        except Exception as e:
            logger.warning("Failed to clean session team dirs", session_id=session_id, error=str(e))

        if cleaned:
            logger.info("Session teams cleaned", session_id=session_id, teams=cleaned)
        return cleaned

    async def stop_all(self) -> None:
        """进程关闭：仅停止所有队友的工作并释放未完成任务，不删除团队目录与任务文件。
        目录/文件的彻底清理只在会话删除时由 cleanup_session / cleanup_team 负责。
        """
        for team in list(self._teams.values()):
            team.shutting_down = True
            for name, rt in list(team.members.items()):
                rt.state = "stopped"
                if rt.work_task and not rt.work_task.done():
                    rt.work_task.cancel()
                    try:
                        await rt.work_task
                    except (asyncio.CancelledError, Exception):
                        pass
                # 释放该队友名下未完成任务的 owner（任务文件保留，仅置空 owner）
                try:
                    from app.core.task_system.store import task_store
                    owned = await task_store.list_tasks(
                        owner=rt.agent_id, task_list_id=team.team_id
                    )
                    for t in owned:
                        if t.get("status") in ("pending", "in_progress"):
                            await task_store.update_task(
                                t.get("id"), {"owner": ""}, task_list_id=team.team_id
                            )
                except Exception as e:
                    logger.warning("Failed to unassign tasks on shutdown",
                                   teammate=name, error=str(e))
            logger.info("Team stopped (files retained)", team_id=team.team_id)


# 全局单例
team_manager = TeamManager()


# ===== 权限门控（工具 prehook 集成） =====

async def team_permission_gate(context, arguments):
    """队友高风险工具调用的权限门控（注册为全局 prehook）

    当队友（非 Lead）调用 TEAM_PERMISSION_REQUIRED_TOOLS 中的工具时，
    向 Lead 发起权限申请并阻塞等待审批结果；被拒绝则阻止工具执行。
    """
    from app.models.schemas.tool import HookResult

    # 仅团队功能开启时生效
    if not _setting("ENABLE_AGENT_TEAMS", False):
        return HookResult(success=True, should_continue=True)

    tool_name = getattr(context, "tool_name", "")
    if tool_name not in TEAM_PERMISSION_REQUIRED_TOOLS:
        return HookResult(success=True, should_continue=True)

    ctx = current_team_context.get()
    if ctx is None or ctx.is_lead:
        # 非团队上下文或 Lead 本人：直接放行
        return HookResult(success=True, should_continue=True)

    team = team_manager.get_team(ctx.team_id)
    if team is None:
        return HookResult(success=True, should_continue=True)

    # 队友需要审批
    summary = _summarize_tool_call(tool_name, arguments)
    approved = await team_manager.request_permission(
        team=team,
        member_name=ctx.member_name,
        operation=tool_name,
        details={"summary": summary, "arguments": arguments},
    )
    if approved:
        logger.info(
            "Team permission approved",
            teammate=ctx.member_name,
            tool=tool_name,
        )
        return HookResult(success=True, should_continue=True)

    logger.warning(
        "Team permission denied",
        teammate=ctx.member_name,
        tool=tool_name,
    )
    return HookResult(
        success=False,
        should_continue=False,
        error=(
            f"[PERMISSION DENIED] Team lead denied '{ctx.member_name}' permission to "
            f"run '{tool_name}'. Do not retry the same operation; inform the lead or "
            f"choose a safer alternative."
        ),
    )


def _summarize_tool_call(tool_name: str, arguments: Dict[str, Any]) -> str:
    """生成工具调用摘要，便于 Lead 审批"""
    if tool_name == "bash_command":
        return f"bash: {str(arguments.get('command', ''))[:200]}"
    if tool_name == "write_file":
        return f"write file: {arguments.get('path', '') or arguments.get('file_path', '')}"
    return str(arguments)[:200]


_TEAM_HOOKS_REGISTERED = False


def register_team_hooks() -> None:
    """注册团队权限门控 prehook（仅在团队功能开启时，幂等）"""
    global _TEAM_HOOKS_REGISTERED
    if _TEAM_HOOKS_REGISTERED:
        return
    if not _setting("ENABLE_AGENT_TEAMS", False):
        return
    try:
        from app.tools.hooks import hook_manager
        hook_manager.register_pre_hook(
            team_permission_gate,
            name="team_permission_gate",
            priority=150,  # 在危险命令检查(200)之后、审计(100)之前
        )
        _TEAM_HOOKS_REGISTERED = True
        logger.info("Team permission gate hook registered")
    except Exception as e:
        logger.warning("Failed to register team hooks", error=str(e))
