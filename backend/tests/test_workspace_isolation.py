"""工作区隔离功能测试 - workspace_isolation"""
import os
import subprocess

import pytest

from app.core.workspace_isolation import (
    WorkspaceIsolationManager,
    current_workspace,
    resolve_ws_path,
    sanitize_branch_name,
    sanitize_dir_name,
)


def _git_ok() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


requires_git = pytest.mark.skipif(not _git_ok(), reason="git not available")


# ===== 名称清洗 =====

def test_sanitize_dir_name():
    assert sanitize_dir_name("Default Agent") == "Default Agent"
    assert sanitize_dir_name("a/b\\c:d") == "a_b_c_d"
    assert sanitize_dir_name("数据分析协调者") == "数据分析协调者"
    assert sanitize_dir_name("") == "unnamed"


def test_sanitize_branch_name():
    assert sanitize_branch_name("my session") == "my_session"
    assert sanitize_branch_name("a..b") == "a_b"
    assert sanitize_branch_name("a//b") == "a_b"
    assert sanitize_branch_name("a~b^c:d?e*f[g]h") == "a_b_c_d_e_f_g_h"
    assert sanitize_branch_name("会话-1") == "会话-1"


# ===== 相对路径解析 =====

def test_resolve_ws_path_with_context(tmp_path):
    ws = str(tmp_path)
    token = current_workspace.set(ws)
    try:
        assert resolve_ws_path("config.json") == os.path.join(ws, "config.json")
        # 绝对路径不受工作区限制
        abs_p = os.path.join(str(tmp_path), "x.txt")
        assert resolve_ws_path(abs_p) == os.path.normpath(abs_p)
    finally:
        current_workspace.reset(token)


def test_resolve_ws_path_without_context():
    token = current_workspace.set(None)
    try:
        # 无上下文时回退进程当前目录
        assert resolve_ws_path("a.txt") == os.path.normpath(os.path.abspath("a.txt"))
    finally:
        current_workspace.reset(token)


# ===== scope 配置解析 =====

@pytest.mark.parametrize("value,expected", [
    ("session", "session"),
    ("agent", "agent"),
    ("shared", "shared"),
    ("SESSION", "session"),
    ("invalid", "session"),
])
def test_get_scope(value, expected, monkeypatch):
    from app.config import settings
    monkeypatch.setitem(settings._flat, "AGENT_DEFAULTS_SANDBOX_SCOPE", value)
    mgr = WorkspaceIsolationManager(root=None)
    assert mgr.get_scope() == expected


# ===== git 全流程 =====

@requires_git
@pytest.mark.asyncio
async def test_full_workspace_lifecycle(tmp_path):
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))

    # 1. 主agent工作区初始化（含 .git 与独立身份）
    agent_ws = await mgr.ensure_agent_workspace("Test Agent")
    assert os.path.isdir(os.path.join(agent_ws, ".git"))

    # 2. 会话工作区（worktree）
    session_path, session_branch = await mgr.create_session_workspace("Test Agent", "sess-001")
    assert os.path.isdir(session_path)
    assert session_branch == "session/sess-001"
    # worktree 特征：.git 是文件（指向主仓库元数据）
    assert os.path.exists(os.path.join(session_path, ".git"))

    # 3. 幂等：重复创建返回相同路径且不破坏已有内容
    with open(os.path.join(session_path, "hello.txt"), "w", encoding="utf-8") as f:
        f.write("hi")
    session_path2, _ = await mgr.create_session_workspace("Test Agent", "sess-001")
    assert session_path2 == session_path
    assert os.path.exists(os.path.join(session_path, "hello.txt"))

    # 4. 子agent工作区（嵌套 worktree 或降级位置）
    sub_path, sub_branch, repo = await mgr.create_subagent_workspace(
        "Test Agent", "sess-001", "Researcher"
    )
    assert os.path.isdir(sub_path)
    assert sub_branch  # git 模式下分支名非空

    # 5. 删除子工作区：目录与分支都清理
    await mgr.remove_workspace(sub_path, branch=sub_branch, repo=repo)
    assert not os.path.exists(sub_path)
    r = subprocess.run(
        ["git", "-C", repo, "branch", "--list", sub_branch],
        capture_output=True, text=True,
    )
    assert r.stdout.strip() == ""

    # 6. 删除会话工作区
    await mgr.remove_workspace(session_path, branch=session_branch, repo=agent_ws)
    assert not os.path.exists(session_path)
    r = subprocess.run(
        ["git", "-C", agent_ws, "branch", "--list", session_branch],
        capture_output=True, text=True,
    )
    assert r.stdout.strip() == ""


@requires_git
@pytest.mark.asyncio
async def test_remove_session_workspace_helper(tmp_path):
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))
    path, branch = await mgr.create_session_workspace("Agent-X", "conv-42")
    assert os.path.isdir(path)
    ok = await mgr.remove_session_workspace("Agent-X", "conv-42")
    assert ok
    assert not os.path.exists(path)


@requires_git
@pytest.mark.asyncio
async def test_global_repo_and_shared_subworkspace(tmp_path):
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))

    root = await mgr.ensure_global_repo()
    assert os.path.isdir(os.path.join(root, ".git"))

    # shared 场景：子工作区位于全局仓库下
    path, branch, repo = await mgr.create_subagent_workspace(None, None, "Helper")
    assert os.path.isdir(path)
    assert repo == root
    await mgr.remove_workspace(path, branch=branch, repo=repo)
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_fallback_without_git(tmp_path, monkeypatch):
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))
    monkeypatch.setattr(mgr, "_git_ok", False)
    monkeypatch.setattr(type(mgr), "git_available", lambda self: False)

    agent_ws = await mgr.ensure_agent_workspace("Plain Agent")
    assert os.path.isdir(agent_ws)

    path, branch = await mgr.create_session_workspace("Plain Agent", "s1")
    assert os.path.isdir(path)
    assert branch == ""  # 降级模式无分支

    ok = await mgr.remove_workspace(path)
    assert ok
    assert not os.path.exists(path)


# ===== forked agent 工作区 =====

@pytest.mark.asyncio
async def test_forked_workspace_lifecycle(tmp_path):
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))

    # 路径计算：{root}/forked-agents/{forked_id}
    expected = os.path.join(str(tmp_path / "ws"), "forked-agents", "fork-abc123")
    assert mgr.forked_workspace_path("fork-abc123") == expected

    path = await mgr.create_forked_workspace("fork-abc123")
    assert path == expected
    assert os.path.isdir(path)

    # 写入残留文件后删除
    with open(os.path.join(path, "scratch.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    ok = await mgr.remove_forked_workspace(path)
    assert ok
    assert not os.path.exists(path)

    # 也支持按 forked_id 删除（幂等：不存在时不报错）
    await mgr.create_forked_workspace("fork-xyz")
    ok = await mgr.remove_forked_workspace("fork-xyz")
    assert ok
    assert not os.path.exists(mgr.forked_workspace_path("fork-xyz"))


@pytest.mark.asyncio
async def test_remove_forked_workspace_rejects_outside_path(tmp_path):
    """安全边界：拒绝删除 forked-agents 目录之外的路径"""
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))
    outside = tmp_path / "important"
    outside.mkdir()
    (outside / "data.txt").write_text("keep me", encoding="utf-8")

    ok = await mgr.remove_forked_workspace(str(outside))
    assert not ok
    assert outside.exists()
    assert (outside / "data.txt").exists()


@pytest.mark.asyncio
async def test_cleanup_forked_workspaces_on_startup(tmp_path):
    """启动兜底：扫描 forked-agents 目录清空全部残留"""
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))

    # 模拟异常退出残留：两个工作区目录 + 一个散落文件
    await mgr.create_forked_workspace("fork-1")
    await mgr.create_forked_workspace("fork-2")
    with open(os.path.join(mgr.forked_dir, "stray.txt"), "w", encoding="utf-8") as f:
        f.write("residual")

    removed = await mgr.cleanup_forked_workspaces()
    assert removed == 3
    assert os.listdir(mgr.forked_dir) == []

    # 目录不存在时不报错
    mgr2 = WorkspaceIsolationManager(root=str(tmp_path / "ws2"))
    assert await mgr2.cleanup_forked_workspaces() == 0


def test_create_forked_agent_injects_workspace_path():
    """create_forked_agent 应将 forked 工作区路径注入 metadata"""
    from app.core.memory_system.fork import create_forked_agent
    from app.core.workspace_isolation import workspace_isolation

    agent = create_forked_agent(
        name="memory-extractor",
        system_prompt="test",
        llm_service=None,
    )
    ws_path = agent.config.metadata.get("workspace_path")
    assert ws_path == workspace_isolation.forked_workspace_path(agent.config.spawn_uuid)
    assert ws_path.startswith(workspace_isolation.forked_dir)
    # 仅预分配路径，不提前创建目录
    assert not os.path.exists(ws_path)


# ===== 孤儿会话工作区启动兜底清理 =====

@requires_git
@pytest.mark.asyncio
async def test_cleanup_orphan_session_workspaces(tmp_path):
    """会话已不存在的孤儿工作区被清理，存活会话保留（隐藏目录 .git 被跳过）"""
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))
    alive_path, _ = await mgr.create_session_workspace("Agent-O", "conv-alive")
    dead_path, dead_branch = await mgr.create_session_workspace("Agent-O", "conv-dead")

    existing = {"conv-alive"}

    async def conversation_exists(cid: str) -> bool:
        return cid in existing

    removed = await mgr.cleanup_orphan_session_workspaces(conversation_exists)
    assert removed == 1
    assert os.path.isdir(alive_path)
    assert not os.path.exists(dead_path)
    # 分支同步清理
    r = subprocess.run(
        ["git", "-C", os.path.join(mgr.agents_dir, "Agent-O"), "branch", "--list", dead_branch],
        capture_output=True, text=True,
    )
    assert r.stdout.strip() == ""


@pytest.mark.asyncio
async def test_cleanup_orphan_keeps_on_lookup_failure(tmp_path, monkeypatch):
    """会话存在性查询失败（如数据库不可用）时保守保留，不误删"""
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))
    monkeypatch.setattr(mgr, "_git_ok", False)
    monkeypatch.setattr(type(mgr), "git_available", lambda self: False)
    path, _ = await mgr.create_session_workspace("Agent-F", "conv-1")

    async def boom(cid: str) -> bool:
        raise RuntimeError("db down")

    removed = await mgr.cleanup_orphan_session_workspaces(boom)
    assert removed == 0
    assert os.path.isdir(path)


@pytest.mark.asyncio
async def test_cleanup_orphan_locked_dir_retried_next_startup(tmp_path, monkeypatch):
    """目录内文件被占用时删除失败不计入清理数，释放后重试成功"""
    if os.name != "nt":
        pytest.skip("file locking simulation requires Windows")
    mgr = WorkspaceIsolationManager(root=str(tmp_path / "ws"))
    monkeypatch.setattr(mgr, "_git_ok", False)
    monkeypatch.setattr(type(mgr), "git_available", lambda self: False)
    path, _ = await mgr.create_session_workspace("Agent-L", "conv-locked")

    async def conversation_exists(cid: str) -> bool:
        return False

    # 模拟文件被占用：句柄保持打开（Windows 下无法删除）
    lock_file = os.path.join(path, "locked.txt")
    with open(lock_file, "w", encoding="utf-8") as fh:
        fh.write("busy")
        removed = await mgr.cleanup_orphan_session_workspaces(conversation_exists)
        assert removed == 0
        assert os.path.isdir(path)

    # 句柄释放后（下次启动）重试成功
    removed = await mgr.cleanup_orphan_session_workspaces(conversation_exists)
    assert removed == 1
    assert not os.path.exists(path)
