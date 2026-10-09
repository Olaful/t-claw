"""记忆系统单元测试

覆盖：
- store frontmatter 读写、索引生成与截断
- scanner 排序/上限/去噪过滤
- extractor 节流与游标逻辑（mock forked agent）
- consolidator 四层门控与锁过期
- session_memory 触发阈值判断
- 统一记忆写入锁（主 Agent/提取/整理）
- resolve_memory_llm（记忆后台任务独立模型/低成本模型配置）
"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# 直接运行时确保 app 模块可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.config import settings
from app.core.memory_system.store import memory_store
from app.core.memory_system.scanner import MemoryScanner
from app.core.memory_system import extractor as extractor_mod
from app.core.memory_system import consolidator as consolidator_mod
from app.core.memory_system.session_memory import SessionMemoryManager, _SessionState
from app.models.schemas.chat import MessageRole


# ===== fixtures =====

@pytest.fixture
def memory_root(tmp_path, monkeypatch):
    """将记忆根目录重定向到临时目录"""
    root = tmp_path / "memory"
    monkeypatch.setattr(settings, "MEMORY_ROOT", str(root))
    memory_store.set_root_override(root)
    memory_store.ensure_dirs()
    yield root
    memory_store.set_root_override(None)


def _write_mem(root: Path, filename: str, name: str, description: str,
               mem_type: str = "user", mtime: float = None):
    """快速创建一个记忆文件"""
    path = root / filename
    memory_store.write_memory(path, {
        "name": name,
        "description": description,
        "type": mem_type,
        "scope": "personal",
    }, f"content of {name}")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _make_msg(idx: int, role, content: str):
    return SimpleNamespace(
        id=f"msg-{idx}",
        role=role,
        content=content,
        message_type=None,
        tool_calls=None,
        name=None,
    )


def _make_agent(messages):
    """构造测试用 fake agent 与 conversation"""
    conversation = SimpleNamespace(messages=messages)
    agent = SimpleNamespace(
        id="test-agent",
        llm_service=SimpleNamespace(provider="openai", model="gpt-4o"),
        config=SimpleNamespace(llm_provider="openai", llm_model="gpt-4o"),
        get_conversation=lambda cid: conversation,
    )
    return agent


# ===== Store =====

class TestStore:

    def test_frontmatter_roundtrip(self, memory_root):
        path = memory_root / "a.md"
        memory_store.write_memory(path, {
            "name": "偏好",
            "description": "用户喜欢简洁回答",
            "type": "user",
        }, "正文内容")

        fm = memory_store.read_frontmatter(path)
        assert fm["name"] == "偏好"
        assert fm["description"] == "用户喜欢简洁回答"
        assert fm["type"] == "user"
        assert "created_at" in fm and "updated_at" in fm

        fm2, body, truncated = memory_store.read_memory(path)
        assert fm2["name"] == "偏好"
        assert "正文内容" in body
        assert truncated is False

    def test_read_memory_truncation_by_lines(self, memory_root):
        path = memory_root / "big.md"
        path.write_text("---\nname: big\n---\n" + "\n".join(f"line{i}" for i in range(300)),
                        encoding="utf-8")
        fm, body, truncated = memory_store.read_memory(path, max_lines=10, max_bytes=100000)
        assert truncated is True
        assert len(body.splitlines()) <= 11  # frontmatter 展开后行数受限

    def test_index_generation_and_truncation(self, memory_root, monkeypatch):
        for i in range(5):
            _write_mem(memory_root, f"m{i}.md", f"记忆{i}", f"描述{i}")

        memory_store.update_index("personal")
        index_text = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
        for i in range(5):
            assert f"[记忆{i}](m{i}.md)" in index_text
            assert f"描述{i}" in index_text

        # 截断：限制 3 行
        monkeypatch.setattr(settings, "MEMORY_INDEX_MAX_LINES", 3)
        content = memory_store.load_index_content("personal")
        assert "... (index truncated)" in content
        assert len(content.splitlines()) == 4  # 3 行 + 截断标注

    def test_iter_excludes_index_and_dotfiles(self, memory_root):
        _write_mem(memory_root, "ok.md", "正常", "d")
        (memory_root / ".hidden.md").write_text("x", encoding="utf-8")
        (memory_root / "note.txt").write_text("x", encoding="utf-8")
        names = {p.name for p in memory_store.iter_memory_files("personal")}
        assert names == {"ok.md"}

    def test_is_within_memory_root(self, memory_root):
        assert memory_store.is_within_memory_root(str(memory_root / "a.md"))
        assert memory_store.is_within_memory_root(str(memory_root / "team" / "b.md"))
        assert not memory_store.is_within_memory_root(str(memory_root.parent / "evil.md"))


# ===== Scanner =====

class TestScanner:

    def test_sort_by_mtime_desc_and_filter(self, memory_root):
        now = time.time()
        _write_mem(memory_root, "old.md", "旧", "d", mtime=now - 3600)
        _write_mem(memory_root, "new.md", "新", "d", mtime=now)
        _write_mem(memory_root, "mid.md", "中", "d", mtime=now - 1800)

        scanner = MemoryScanner(store=memory_store)
        entries = scanner.scan_manifest("personal", force=True)
        assert [e.filename for e in entries] == ["new.md", "mid.md", "old.md"]
        assert entries[0].name == "新"

    def test_scan_max_files_limit(self, memory_root, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_SCAN_MAX_FILES", 2)
        now = time.time()
        for i in range(5):
            _write_mem(memory_root, f"f{i}.md", f"n{i}", "d", mtime=now - i)

        scanner = MemoryScanner(store=memory_store)
        entries = scanner.scan_manifest("personal", force=True)
        assert len(entries) == 2
        # 保留的是最新的两个
        assert {e.filename for e in entries} == {"f0.md", "f1.md"}

    def test_cache_hit_when_unchanged(self, memory_root):
        _write_mem(memory_root, "a.md", "A", "d")
        scanner = MemoryScanner(store=memory_store)
        first = scanner.scan_manifest("personal", force=True)
        second = scanner.scan_manifest("personal", force=False)
        assert first is second  # 缓存命中返回同一列表对象


# ===== Extractor =====

class TestExtractor:

    @pytest.mark.asyncio
    async def test_throttle(self, memory_root, monkeypatch):
        memory_store.update_session_meta("conv-1", last_extracted_at=time.time())
        monkeypatch.setattr(settings, "MEMORY_EXTRACT_MIN_INTERVAL_SECONDS", 300)

        agent = _make_agent([_make_msg(1, MessageRole.USER, "hello" * 100)])
        result = await extractor_mod.extract_memories(agent, "conv-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_increment_by_cursor(self, memory_root, monkeypatch):
        memory_store.update_session_meta(
            "conv-1",
            last_extracted_at=0,
            last_memory_message_uuid="msg-2",
        )
        from app.core.memory_system.prefetch import memory_prefetch_manager
        monkeypatch.setattr(memory_prefetch_manager, "agent_wrote_memory", lambda cid: False)

        messages = [
            _make_msg(1, MessageRole.USER, "q1" * 200),
            _make_msg(2, MessageRole.ASSISTANT, "a1" * 200),
        ]
        agent = _make_agent(messages)
        result = await extractor_mod.extract_memories(agent, "conv-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_skip_when_increment_too_small(self, memory_root, monkeypatch):
        memory_store.update_session_meta("conv-1", last_extracted_at=0)
        from app.core.memory_system.prefetch import memory_prefetch_manager
        monkeypatch.setattr(memory_prefetch_manager, "agent_wrote_memory", lambda cid: False)
        monkeypatch.setattr(settings, "MEMORY_EXTRACT_MIN_CONTENT_CHARS", 200)

        messages = [_make_msg(1, MessageRole.USER, "hi")]
        agent = _make_agent(messages)
        result = await extractor_mod.extract_memories(agent, "conv-1")
        assert result is False
        # 跳过提取时不推进游标，等待下轮累积更多内容
        assert "last_memory_message_uuid" not in memory_store.load_session_meta("conv-1")

    @pytest.mark.asyncio
    async def test_full_flow_with_mocked_fork(self, memory_root, monkeypatch):
        memory_store.update_session_meta("conv-1", last_extracted_at=0)
        from app.core.memory_system.prefetch import memory_prefetch_manager
        monkeypatch.setattr(memory_prefetch_manager, "agent_wrote_memory", lambda cid: False)
        monkeypatch.setattr(settings, "MEMORY_EXTRACT_MIN_USER_MESSAGES", 1)

        async def fake_run(forked, prompt, lock_holder=None):
            return "SKIP"

        monkeypatch.setattr(extractor_mod, "run_forked_task", fake_run)
        monkeypatch.setattr(extractor_mod, "create_forked_agent",
                            lambda **kwargs: SimpleNamespace(name=kwargs.get("name")))

        messages = [
            _make_msg(1, MessageRole.USER, "请帮我配置项目的部署流程，要求使用 Docker 并部署到 K8s 集群，同时配置 CI/CD 流水线。" * 5),
            _make_msg(2, MessageRole.ASSISTANT, "好的，已完成部署配置。" * 5),
        ]
        agent = _make_agent(messages)
        result = await extractor_mod.extract_memories(agent, "conv-1")
        assert result is True

        meta = memory_store.load_session_meta("conv-1")
        assert meta["last_memory_message_uuid"] == "msg-2"
        assert meta.get("last_extracted_at") > 0

    @pytest.mark.asyncio
    async def test_meta_isolation_between_conversations(self, memory_root, monkeypatch):
        """会话级 meta 隔离：conv-1 的游标/节流不影响 conv-2"""
        from app.core.memory_system.prefetch import memory_prefetch_manager
        monkeypatch.setattr(memory_prefetch_manager, "agent_wrote_memory", lambda cid: False)
        monkeypatch.setattr(settings, "MEMORY_EXTRACT_MIN_USER_MESSAGES", 1)

        # conv-1 处于节流中，游标已推进到 msg-2
        memory_store.update_session_meta(
            "conv-1",
            last_extracted_at=time.time(),
            last_memory_message_uuid="msg-2",
        )

        messages = [
            _make_msg(1, MessageRole.USER, "请帮我配置项目的部署流程，要求使用 Docker 并部署到 K8s 集群，同时配置 CI/CD 流水线。" * 5),
            _make_msg(2, MessageRole.ASSISTANT, "好的，已完成部署配置。" * 5),
        ]
        agent = _make_agent(messages)

        async def fake_run(forked, prompt, lock_holder=None):
            return "SKIP"

        monkeypatch.setattr(extractor_mod, "run_forked_task", fake_run)
        monkeypatch.setattr(extractor_mod, "create_forked_agent",
                            lambda **kwargs: SimpleNamespace(name=kwargs.get("name")))

        # conv-2 不受 conv-1 节流/游标影响：从头处理全部消息并正常执行
        result = await extractor_mod.extract_memories(agent, "conv-2")
        assert result is True
        assert memory_store.load_session_meta("conv-2")["last_memory_message_uuid"] == "msg-2"
        # conv-1 的 meta 未被改动（仍在节流）
        meta1 = memory_store.load_session_meta("conv-1")
        assert time.time() - meta1["last_extracted_at"] < 300


# ===== Consolidator =====

class TestConsolidator:

    @pytest.mark.asyncio
    async def test_conversation_gate(self, memory_root, monkeypatch):
        """会话门控：统计所有非子 agent 的新会话数量"""
        from datetime import datetime, timedelta
        from app.services.task_service import task_service
        
        # 构造两个 mock agent：一个主 agent（2 个新会话），一个子 agent（不计入）
        main_agent = SimpleNamespace(
            is_subagent=False,
            _conversations={
                "conv-1": SimpleNamespace(created_at=datetime.utcnow() - timedelta(hours=1)),
                "conv-2": SimpleNamespace(created_at=datetime.utcnow() - timedelta(minutes=30)),
            }
        )
        sub_agent = SimpleNamespace(
            is_subagent=True,
            _conversations={
                "conv-sub-1": SimpleNamespace(created_at=datetime.utcnow() - timedelta(minutes=10)),
            }
        )
        monkeypatch.setattr(task_service, "_agents", {"main": main_agent, "sub": sub_agent})
        
        reason = await consolidator_mod._check_conversation_gate()
        assert reason and "conversation gate" in reason
        assert "2 new conversations" in reason

    def test_interval_gate(self, memory_root, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_MIN_INTERVAL_HOURS", 24)
        # 锁文件 mtime 为当前时间 → last_consolidated_at = now → 间隔门控拒绝
        lock = memory_store.consolidate_lock_path()
        lock.write_text("locked_at=now\n", encoding="utf-8")
        reason = consolidator_mod._check_gates()
        assert reason and "interval gate" in reason
        lock.unlink()

    def test_lock_gate(self, memory_root, monkeypatch):
        # 间隔阈值设为 0，避免锁 mtime 先触发 interval gate
        monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_MIN_INTERVAL_HOURS", 0)
        assert memory_store.try_acquire_consolidate_lock() is True
        # 锁已被占用
        reason = consolidator_mod._check_gates()
        # _check_gates 现在只检查时间和扫描节流，不检查锁
        # 锁门控在 auto_dream 中检查
        assert reason is None  # 同步门控通过
        memory_store.release_consolidate_lock()

    def test_gates_pass(self, memory_root, monkeypatch):
        memory_store.update_meta(
            last_consolidate_scan_attempt=0,
        )
        monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_SCAN_THROTTLE_SECONDS", 600)
        reason = consolidator_mod._check_gates()
        assert reason is None  # 同步门控通过

    def test_lock_expiry_takeover(self, memory_root, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_CONSOLIDATE_LOCK_TTL_SECONDS", 3600)
        lock = memory_store.consolidate_lock_path()
        lock.write_text("locked_at=old\n", encoding="utf-8")
        old = time.time() - 7200  # 2 小时前
        os.utime(lock, (old, old))

        # 过期锁可被抢占
        assert memory_store.try_acquire_consolidate_lock() is True
        memory_store.release_consolidate_lock()

    @pytest.mark.asyncio
    async def test_conversation_gate_pass(self, memory_root, monkeypatch):
        """会话门控通过：所有非子 agent 共有足够数量的新会话"""
        from datetime import datetime, timedelta
        from app.services.task_service import task_service
        
        # 两个主 agent，合计 10 个新会话（大于默认阈值 5）
        agent_a = SimpleNamespace(
            is_subagent=False,
            _conversations={
                f"conv-a-{i}": SimpleNamespace(created_at=datetime.utcnow() - timedelta(minutes=i * 10))
                for i in range(6)
            }
        )
        agent_b = SimpleNamespace(
            is_subagent=False,
            _conversations={
                f"conv-b-{i}": SimpleNamespace(created_at=datetime.utcnow() - timedelta(minutes=i * 5))
                for i in range(4)
            }
        )
        monkeypatch.setattr(task_service, "_agents", {"a": agent_a, "b": agent_b})
        
        reason = await consolidator_mod._check_conversation_gate()
        assert reason is None  # 门控通过


# ===== Session Memory =====

class TestSessionMemory:

    def _manager(self):
        return SessionMemoryManager()

    def test_should_update_by_first_token_threshold(self, memory_root, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_SESSION_MEMORY_FIRST_UPDATE_TOKENS", 10000)
        mgr = self._manager()
        mgr._states["c1"] = _SessionState(total_tokens=9999)
        should, _ = mgr.should_update("c1")
        assert should is False

        mgr._states["c1"].total_tokens = 10000
        should, reason = mgr.should_update("c1")
        assert should is True
        assert "token" in reason

    def test_should_update_by_increment_threshold(self, memory_root, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_SESSION_MEMORY_INCREMENT_TOKENS", 5000)
        mgr = self._manager()
        # 已有基线（非首次）：增量 4999 不触发，5000 触发
        mgr._states["c1"] = _SessionState(total_tokens=14999, baseline_tokens=10000)
        assert mgr.should_update("c1")[0] is False
        mgr._states["c1"].total_tokens = 15000
        assert mgr.should_update("c1")[0] is True

    def test_should_update_by_tool_calls(self, memory_root, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_SESSION_MEMORY_TOOL_CALLS_THRESHOLD", 3)
        mgr = self._manager()
        mgr._states["c1"] = _SessionState(total_tokens=100, tool_calls_since_update=2)
        assert mgr.should_update("c1")[0] is False
        mgr._states["c1"].tool_calls_since_update = 3
        assert mgr.should_update("c1")[0] is True

    def test_on_message_added_tracking(self, memory_root):
        mgr = self._manager()
        mgr.on_message_added("c1", _make_msg(1, MessageRole.USER, "x" * 300))
        # assistant 带工具调用：计数工具调用并清零停顿轮
        msg_tool = _make_msg(2, MessageRole.ASSISTANT, "y")
        msg_tool.tool_calls = [1, 2]
        mgr.on_message_added("c1", msg_tool)
        # assistant 无工具调用：停顿轮 +1
        mgr.on_message_added("c1", _make_msg(3, MessageRole.ASSISTANT, "z"))

        state = mgr.get_state("c1")
        assert state is not None
        assert state.total_tokens > 0
        assert state.tool_calls_since_update == 2
        assert state.assistant_pause_turns == 1

    def test_get_state_readonly(self, memory_root):
        mgr = self._manager()
        assert mgr.get_state("nonexistent") is None


# ===== 统一记忆写入锁 =====

class TestMemoryLock:

    def test_acquire_release_and_holder(self, memory_root):
        assert memory_store.get_memory_lock_holder() is None
        assert memory_store.try_acquire_memory_lock("extractor") is True
        assert memory_store.get_memory_lock_holder() == "extractor"
        memory_store.release_memory_lock("extractor")
        assert memory_store.get_memory_lock_holder() is None

    def test_reentrant(self, memory_root):
        assert memory_store.try_acquire_memory_lock("extractor") is True
        # 同一持有者重入成功
        assert memory_store.try_acquire_memory_lock("extractor") is True
        memory_store.release_memory_lock("extractor")

    def test_mutual_exclusion(self, memory_root):
        assert memory_store.try_acquire_memory_lock("extractor") is True
        # 他人持有且未过期 → 获取失败（三方互斥）
        assert memory_store.try_acquire_memory_lock("consolidator") is False
        assert memory_store.try_acquire_memory_lock("main-agent") is False
        assert memory_store.get_memory_lock_holder() == "extractor"
        memory_store.release_memory_lock("extractor")

    def test_expiry_takeover(self, memory_root, monkeypatch):
        monkeypatch.setattr(settings, "MEMORY_LOCK_TTL_SECONDS", 60)
        assert memory_store.try_acquire_memory_lock("extractor") is True
        # 将锁 mtime 设为 2 分钟前（超过 TTL）
        old = time.time() - 120
        os.utime(memory_store.memory_lock_path(), (old, old))
        assert memory_store.try_acquire_memory_lock("consolidator") is True
        assert memory_store.get_memory_lock_holder() == "consolidator"
        memory_store.release_memory_lock("consolidator")

    def test_release_only_matching_holder(self, memory_root):
        assert memory_store.try_acquire_memory_lock("extractor") is True
        # 持有者不匹配时不得误删他人锁（防止主 Agent 强占后被后台任务误释放）
        memory_store.release_memory_lock("consolidator")
        assert memory_store.get_memory_lock_holder() == "extractor"
        memory_store.release_memory_lock("extractor")
        assert memory_store.get_memory_lock_holder() is None

    def test_force_acquire(self, memory_root):
        assert memory_store.try_acquire_memory_lock("extractor") is True
        # 主 Agent 优先级最高：强制覆盖锁
        assert memory_store.force_acquire_memory_lock("main-agent") is True
        assert memory_store.get_memory_lock_holder() == "main-agent"
        memory_store.release_memory_lock("main-agent")

    @pytest.mark.asyncio
    async def test_write_file_lock_integration(self, memory_root, monkeypatch):
        """builtin write_file 写入记忆目录时的锁行为"""
        from app.tools.builtin_tools import write_file
        from app.core.memory_system.store import (
            current_memory_lock_holder, LOCK_HOLDER_MAIN_AGENT,
        )

        target = str(memory_root / "locked-write.md")

        # 1. 无锁时主 Agent 写入成功，写入后锁已释放（写入级锁）
        result = await write_file(target, "hello")
        assert result["success"] is True
        assert memory_store.get_memory_lock_holder() is None

        # 2. 后台任务上下文且任务锁仍由自己持有 → 写入通过（重入）
        assert memory_store.try_acquire_memory_lock("extractor") is True
        token = current_memory_lock_holder.set("extractor")
        try:
            result = await write_file(target, "from extractor")
            assert result["success"] is True
        finally:
            current_memory_lock_holder.reset(token)
            memory_store.release_memory_lock("extractor")

        # 3. 后台任务上下文但锁已被主 Agent 抢占 → 写入被拒绝（不抛异常）
        assert memory_store.try_acquire_memory_lock(LOCK_HOLDER_MAIN_AGENT) is True
        token = current_memory_lock_holder.set("extractor")
        try:
            result = await write_file(target, "should be rejected")
            assert result["success"] is False
            assert "lock" in result["error"].lower()
        finally:
            current_memory_lock_holder.reset(token)
            memory_store.release_memory_lock(LOCK_HOLDER_MAIN_AGENT)

    @pytest.mark.asyncio
    async def test_main_agent_force_takeover_on_write(self, memory_root):
        """主 Agent 写入被后台任务持锁阻塞时，重试后强制获取（最高优先级）"""
        from app.tools.builtin_tools import write_file

        assert memory_store.try_acquire_memory_lock("extractor") is True
        target = str(memory_root / "force-write.md")
        result = await write_file(target, "main agent wins")  # 重试 3 次后强制获取（约 1.5s）
        assert result["success"] is True
        # 写入完成后主 Agent 锁已释放；原 extractor 锁已被覆盖后释放，最终无锁或持有者为 extractor 均已无效
        # 关键：锁未被遗留为 main-agent
        assert memory_store.get_memory_lock_holder() != "main-agent"
        memory_store.release_memory_lock("extractor")

    @pytest.mark.asyncio
    async def test_non_memory_path_not_locked(self, memory_root, tmp_path):
        """非记忆目录写入不受锁影响"""
        from app.tools.builtin_tools import write_file

        assert memory_store.try_acquire_memory_lock("extractor") is True
        outside = str(tmp_path / "outside.md")
        result = await write_file(outside, "no lock needed")
        assert result["success"] is True
        assert memory_store.get_memory_lock_holder() == "extractor"  # 锁未被影响
        memory_store.release_memory_lock("extractor")


class TestResolveMemoryLLM:
    """resolve_memory_llm：记忆后台任务 LLM 栈解析"""

    def _svc(self, provider="openai", model="gpt-4o"):
        from app.services.llm_service import create_llm_service
        return create_llm_service(provider=provider, model=model)

    def test_reuse_main_service_when_unconfigured(self, monkeypatch):
        """未配置 memory.llm 时，复用主服务且模型回退到主服务模型"""
        monkeypatch.setattr(settings, "MEMORY_LLM_PROVIDER", "")
        monkeypatch.setattr(settings, "MEMORY_LLM_MODEL", "")
        from app.core.memory_system.fork import resolve_memory_llm
        svc = self._svc()
        out_svc, out_provider, out_model = resolve_memory_llm(svc)
        assert out_svc is svc
        assert out_provider == "openai"
        assert out_model == "gpt-4o"  # 回退到主服务模型，避免回退到硬编码默认值

    def test_model_override_reuses_service(self, monkeypatch):
        """仅覆盖模型（同 provider）时，复用主服务实例"""
        monkeypatch.setattr(settings, "MEMORY_LLM_PROVIDER", "")
        monkeypatch.setattr(settings, "MEMORY_LLM_MODEL", "claude-sonnet-4")
        from app.core.memory_system.fork import resolve_memory_llm
        svc = self._svc(provider="anthropic")
        out_svc, out_provider, out_model = resolve_memory_llm(svc)
        assert out_svc is svc
        assert out_model == "claude-sonnet-4"

    def test_provider_switch_creates_dedicated_service(self, monkeypatch):
        """provider 不同时，创建独立专用服务实例"""
        monkeypatch.setattr(settings, "MEMORY_LLM_PROVIDER", "anthropic")
        monkeypatch.setattr(settings, "MEMORY_LLM_MODEL", "claude-sonnet-4")
        from app.core.memory_system.fork import resolve_memory_llm
        svc = self._svc(provider="openai")
        out_svc, out_provider, out_model = resolve_memory_llm(svc)
        assert out_svc is not svc
        assert out_svc.provider == "anthropic"
        assert out_provider == "anthropic"
        assert out_model == "claude-sonnet-4"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
