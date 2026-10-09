"""团队协作系统单元测试

覆盖：
- TeamStore：config 创建/读取、成员添加与颜色分配、收件箱并发追加（文件锁）、
  未读/已读消费、广播排除发送者
- 协议消息：request_id 请求-响应配对、关机协议消息构建
- _resolve_task_list_id_from_context 的团队 ContextVar 优先级

不触发真实 LLM 调用。
"""
import sys
import threading
from pathlib import Path

# 直接运行时确保 app 模块可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core.team_system import store as store_mod
from app.core.team_system.store import TeamStore
from app.core.team_system.models import (
    TeamMessageTypes,
    build_teammate_injection,
    current_team_id,
    is_protocol_message,
    make_inbox_message,
    make_team_id,
    parse_inbox_message_protocol,
)


# ===== fixtures =====

@pytest.fixture
def team_store(tmp_path, monkeypatch):
    """将团队根目录重定向到临时目录，并返回干净的 TeamStore 实例"""
    monkeypatch.setattr(store_mod, "_HAPPYCLAW_HOME", tmp_path / ".happy-claw")
    return TeamStore()


SESSION = "sess-1"
TEAM = "alpha"
LEAD = "lead"


def _make_team(store: TeamStore):
    return store.create_team(SESSION, TEAM, LEAD, lead_agent_id="lead-agent-id")


# ===== config 与成员 =====

class TestTeamConfig:

    def test_create_team_idempotent(self, team_store):
        cfg1 = _make_team(team_store)
        cfg2 = _make_team(team_store)
        assert cfg1["name"] == TEAM
        assert cfg1["leadAgentId"] == "lead-agent-id"
        # 幂等：不重复添加 Lead
        assert len(cfg2["members"]) == 1
        assert cfg2["members"][0]["isLead"] is True

    def test_add_member_allocates_distinct_colors(self, team_store):
        _make_team(team_store)
        m1 = team_store.add_member(SESSION, TEAM, "a1@alpha", "worker-1")
        m2 = team_store.add_member(SESSION, TEAM, "a2@alpha", "worker-2")
        assert m1["colorName"] != m2["colorName"]
        assert m1["isActive"] is True
        # 收件箱已初始化
        assert team_store.inbox_path(SESSION, TEAM, "worker-1").exists()

    def test_remove_member(self, team_store):
        _make_team(team_store)
        team_store.add_member(SESSION, TEAM, "a1@alpha", "worker-1")
        assert team_store.remove_member(SESSION, TEAM, "worker-1") is True
        names = [m["name"] for m in team_store.list_members(SESSION, TEAM)]
        assert "worker-1" not in names
        assert LEAD in names


# ===== 收件箱 =====

class TestInbox:

    def test_append_and_read_unread_marks_read(self, team_store):
        _make_team(team_store)
        team_store.add_member(SESSION, TEAM, "a1@alpha", "worker-1")

        team_store.append_inbox_message(SESSION, TEAM, "worker-1", LEAD, "hello")
        unread = team_store.read_unread(SESSION, TEAM, "worker-1")
        assert len(unread) == 1
        assert unread[0]["text"] == "hello"
        assert unread[0]["from"] == LEAD

        # 已标记已读，再次读取为空
        assert team_store.read_unread(SESSION, TEAM, "worker-1") == []

    def test_concurrent_append_no_loss(self, team_store):
        """并发追加收件箱（文件锁保护），无消息丢失"""
        _make_team(team_store)
        team_store.add_member(SESSION, TEAM, "a1@alpha", "worker-1")

        n_per_thread = 10
        n_threads = 5

        def worker(i):
            for j in range(n_per_thread):
                team_store.append_inbox_message(
                    SESSION, TEAM, "worker-1", f"sender-{i}", f"msg-{i}-{j}"
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        msgs = team_store.peek_messages(SESSION, TEAM, "worker-1", limit=1000)
        assert len(msgs) == n_per_thread * n_threads

    def test_broadcast_excludes_sender(self, team_store):
        _make_team(team_store)
        team_store.add_member(SESSION, TEAM, "a1@alpha", "worker-1")
        team_store.add_member(SESSION, TEAM, "a2@alpha", "worker-2")

        result = team_store.send_team_message(
            SESSION, TEAM, from_name="worker-1", to="*", message="hi all"
        )
        assert result["success"] is True
        # 广播排除发送者自己，也不发给 Lead？（Lead 是活跃成员，应收到）
        assert "worker-1" not in result["delivered_to"]
        assert "worker-2" in result["delivered_to"]
        assert LEAD in result["delivered_to"]

    def test_send_to_unknown_member_fails(self, team_store):
        _make_team(team_store)
        result = team_store.send_team_message(
            SESSION, TEAM, from_name=LEAD, to="ghost", message="x"
        )
        assert result["success"] is False
        assert "not found" in result["error"]


# ===== 协议消息 =====

class TestProtocol:

    def test_make_and_parse_protocol_message(self):
        req = {
            "type": TeamMessageTypes.PERMISSION_REQUEST,
            "request_id": "req-123",
            "from": "worker-1",
            "operation": "bash_command",
        }
        assert is_protocol_message(req) is True
        entry = make_inbox_message("worker-1", req)
        # text 存 JSON 字符串 + 额外 protocol 字段
        assert entry["protocol"] == req

        parsed = parse_inbox_message_protocol(entry)
        assert parsed is not None
        assert parsed["type"] == TeamMessageTypes.PERMISSION_REQUEST
        assert parsed["request_id"] == "req-123"

    def test_plain_message_has_no_protocol(self):
        entry = make_inbox_message("worker-1", "just text")
        assert parse_inbox_message_protocol(entry) is None

    def test_request_response_pair_roundtrip(self, team_store):
        """permission_request → Lead 收件箱；permission_response → 队友收件箱，按 request_id 配对"""
        _make_team(team_store)
        team_store.add_member(SESSION, TEAM, "a1@alpha", "worker-1")

        request_id = "req-abc"
        team_store.send_team_message(
            SESSION, TEAM, from_name="worker-1", to=LEAD,
            message={
                "type": TeamMessageTypes.PERMISSION_REQUEST,
                "request_id": request_id,
                "operation": "bash_command",
            },
        )
        lead_msgs = team_store.read_unread(SESSION, TEAM, LEAD)
        assert len(lead_msgs) == 1
        proto = parse_inbox_message_protocol(lead_msgs[0])
        assert proto["type"] == TeamMessageTypes.PERMISSION_REQUEST

        # Lead 回复（按相同 request_id）
        team_store.send_team_message(
            SESSION, TEAM, from_name=LEAD, to="worker-1",
            message={
                "type": TeamMessageTypes.PERMISSION_RESPONSE,
                "request_id": request_id,
                "approved": True,
            },
        )
        worker_msgs = team_store.read_unread(SESSION, TEAM, "worker-1")
        assert len(worker_msgs) == 1
        resp = parse_inbox_message_protocol(worker_msgs[0])
        assert resp["type"] == TeamMessageTypes.PERMISSION_RESPONSE
        assert resp["request_id"] == request_id
        assert resp["approved"] is True

    def test_shutdown_protocol_flow(self, team_store):
        _make_team(team_store)
        team_store.add_member(SESSION, TEAM, "a1@alpha", "worker-1")

        team_store.send_team_message(
            SESSION, TEAM, from_name=LEAD, to="worker-1",
            message={"type": TeamMessageTypes.SHUTDOWN_REQUEST, "request_id": "sd-1"},
        )
        msg = team_store.read_unread(SESSION, TEAM, "worker-1")[0]
        assert parse_inbox_message_protocol(msg)["type"] == TeamMessageTypes.SHUTDOWN_REQUEST

        team_store.send_team_message(
            SESSION, TEAM, from_name="worker-1", to=LEAD,
            message={"type": TeamMessageTypes.SHUTDOWN_APPROVED, "request_id": "sd-1"},
        )
        ack = parse_inbox_message_protocol(team_store.read_unread(SESSION, TEAM, LEAD)[0])
        assert ack["type"] == TeamMessageTypes.SHUTDOWN_APPROVED

    def test_build_teammate_injection(self):
        entries = [
            make_inbox_message("worker-1", "I finished the review"),
            make_inbox_message("worker-2", {"type": TeamMessageTypes.IDLE_NOTIFICATION}),
        ]
        text = build_teammate_injection(entries)
        assert '<teammate-message from="worker-1"' in text
        assert "I finished the review" in text
        assert '<teammate-message from="worker-2"' in text


# ===== 任务上下文解析（团队 ContextVar 优先级） =====

class TestTaskListResolution:

    def test_team_contextvar_priority(self, monkeypatch):
        """设置 current_team_id 时，_resolve 优先返回团队 ID"""
        monkeypatch.delenv("HAPPY_CLAW_TASK_LIST_ID", raising=False)
        from app.tools.builtin_tools import _resolve_task_list_id_from_context

        team_id = make_team_id("conv-xyz", "alpha")
        token = current_team_id.set(team_id)
        try:
            assert _resolve_task_list_id_from_context() == team_id
        finally:
            current_team_id.reset(token)

    def test_env_var_overrides_team_contextvar(self, monkeypatch):
        """环境变量优先级最高"""
        monkeypatch.setenv("HAPPY_CLAW_TASK_LIST_ID", "env-list")
        from app.tools.builtin_tools import _resolve_task_list_id_from_context

        token = current_team_id.set(make_team_id("conv-xyz", "alpha"))
        try:
            assert _resolve_task_list_id_from_context() == "env-list"
        finally:
            current_team_id.reset(token)


# ===== 自动认领（含 Lead 指派任务的接受） =====

class TestAutoClaim:

    @pytest.fixture
    def claim_env(self, tmp_path, monkeypatch):
        """重定向任务存储根目录，构造团队/队友运行时"""
        monkeypatch.delenv("HAPPY_CLAW_TASK_LIST_ID", raising=False)
        from app.core.task_system import store as task_store_mod
        monkeypatch.setattr(task_store_mod, "_HAPPYCLAW_HOME", tmp_path / ".happy-claw")
        from app.core.task_system.store import task_store
        from app.core.team_system.manager import TeamManager, TeamRuntime, TeammateRuntime

        team = TeamRuntime(
            team_id=make_team_id(SESSION, TEAM),
            session_id=SESSION,
            team_name=TEAM,
            lead_name=LEAD,
            lead_agent_id="lead-agent-id",
        )
        rt = TeammateRuntime(name="worker-1", agent_id="worker-1@alpha")
        return TeamManager(), team, rt, task_store

    @pytest.mark.asyncio
    async def test_accept_task_assigned_by_lead(self, claim_env):
        """Lead 通过 task_update(owner=...) 指派的任务，队友应接受开工"""
        manager, team, rt, task_store = claim_env
        task = await task_store.create_task(
            "assigned work", description="do it",
            owner="worker-1", task_list_id=team.team_id,
        )
        result = await manager._try_auto_claim(rt, team)
        assert result is not None
        assert "assigned to you by the team lead" in result
        updated = await task_store.get_task(task["id"], task_list_id=team.team_id)
        assert updated["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_accept_assigned_in_progress_task(self, claim_env):
        """Lead 指派且已置为 in_progress 的任务也应接受"""
        manager, team, rt, task_store = claim_env
        task = await task_store.create_task(
            "assigned work", owner="worker-1", task_list_id=team.team_id,
        )
        await task_store.update_task(
            task["id"], {"status": "in_progress"}, task_list_id=team.team_id
        )
        result = await manager._try_auto_claim(rt, team)
        assert result is not None
        assert "assigned to you by the team lead" in result

    @pytest.mark.asyncio
    async def test_skip_assigned_to_others_claim_unowned(self, claim_env):
        """他人任务跳过，只认领无 owner 的任务"""
        manager, team, rt, task_store = claim_env
        await task_store.create_task(
            "for other", owner="worker-2", task_list_id=team.team_id,
        )
        free = await task_store.create_task("free work", task_list_id=team.team_id)
        result = await manager._try_auto_claim(rt, team)
        assert result is not None
        assert "claimed a task from the shared team task board" in result
        claimed = await task_store.get_task(free["id"], task_list_id=team.team_id)
        assert claimed["owner"] == rt.agent_id
        assert claimed["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_skip_completed_assigned_task(self, claim_env):
        """已完成的指派任务不重复消费"""
        manager, team, rt, task_store = claim_env
        task = await task_store.create_task(
            "done work", owner="worker-1", task_list_id=team.team_id,
        )
        await task_store.update_task(
            task["id"], {"status": "completed"}, task_list_id=team.team_id
        )
        result = await manager._try_auto_claim(rt, team)
        assert result is None

    @pytest.mark.asyncio
    async def test_retry_cap_stops_reclaim_and_reports_once(self, claim_env, tmp_path, monkeypatch):
        """任务未更新时：达到重试上限后停止重新认领，并一次性通知 Lead"""
        from app.core.team_system import manager as manager_mod
        from app.core.team_system import store as store_mod
        from app.core.team_system.store import team_store as global_store

        # 全局团队存储也重定向到临时目录（上报消息经 manager 内置单例投递）
        monkeypatch.setattr(store_mod, "_HAPPYCLAW_HOME", tmp_path / ".happy-claw")
        monkeypatch.setattr(
            manager_mod, "_setting",
            lambda name, default=None: {"TEAMS_MAX_TASK_RETRIES": 2}.get(name, default),
        )
        manager, team, rt, task_store = claim_env
        # 团队 config 与收件箱（供卡住上报投递）
        global_store.create_team(SESSION, TEAM, LEAD, lead_agent_id="lead-agent-id")

        await task_store.create_task("stuck work", task_list_id=team.team_id)

        # 上限内允许尝试（认领后任务仍 in_progress，模拟执行失败未更新）
        assert await manager._try_auto_claim(rt, team) is not None
        assert await manager._try_auto_claim(rt, team) is not None
        # 达上限：停止认领并上报一次
        assert await manager._try_auto_claim(rt, team) is None
        msgs = global_store.read_unread(SESSION, TEAM, LEAD)
        assert len(msgs) == 1
        assert "stopped retrying" in msgs[0]["text"]
        # 后续轮询不再认领，也不重复上报
        assert await manager._try_auto_claim(rt, team) is None
        assert global_store.read_unread(SESSION, TEAM, LEAD) == []


# ===== 会话清理（含重启后磁盘兜底） =====

class TestCleanupSession:

    @pytest.mark.asyncio
    async def test_cleanup_session_from_disk_after_restart(self, tmp_path, monkeypatch):
        """应用重启后内存无团队实例，删除会话时应从磁盘兜底清理团队目录与任务目录"""
        from app.core.team_system import manager as manager_mod
        from app.core.team_system import store as store_mod
        from app.core.team_system.store import team_store as global_store
        from app.core.task_system import store as task_store_mod
        from app.core.task_system.store import task_store

        monkeypatch.setattr(store_mod, "_HAPPYCLAW_HOME", tmp_path / ".happy-claw")
        monkeypatch.setattr(task_store_mod, "_HAPPYCLAW_HOME", tmp_path / ".happy-claw")

        # 磁盘上残留团队目录与任务目录（模拟重启前状态）
        team_id = make_team_id(SESSION, TEAM)
        global_store.create_team(SESSION, TEAM, LEAD, lead_agent_id="lead-agent-id")
        await task_store.create_task("leftover work", task_list_id=team_id)
        assert global_store.session_dir(SESSION).exists()
        assert task_store.list_dir(team_id).exists()

        # 重启后的全新 manager：内存中无任何团队
        manager = manager_mod.TeamManager()
        assert manager._teams == {}

        cleaned = await manager.cleanup_session(SESSION)
        assert TEAM in cleaned
        # 团队目录与任务目录均被删除
        assert not global_store.session_dir(SESSION).exists()
        assert not task_store.list_dir(team_id).exists()


