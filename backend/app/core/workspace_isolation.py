"""工作区隔离管理 - 基于 Git worktree 的分层工作区体系

目录结构:
    .happy-claw/workspace                                    # 全局共享工作区（git init）
    .happy-claw/workspace/agents/{agent_name}                # 主agent工作区（git init，独立作者身份）
    .happy-claw/workspace/agents/{agent_name}/{session_id}   # 会话 worktree
    .happy-claw/workspace/agents/{agent_name}/{session_id}/{sub_agent_name}  # 子agent/队友 worktree
    .happy-claw/workspace/forked-agents/{forked_id}          # 记忆提取/整理等后台 forked agent 临时工作区

设计要点:
    - 全局工作区/主agent工作区不存在 .git 时自动执行 git init 初始化
    - 会话/子agent/队友工作区使用 `git worktree add` 创建，
      使用 `git worktree remove` + `git branch -D` 删除（完全清理工作区与分支）
    - 每个主agent仓库写入独立的 git 作者身份（任务隔离/身份隔离）
    - forked agent 工作区为普通目录，任务退出时删除；应用启动时扫描
      forked-agents 目录清空残留（异常退出兜底）
    - git 不可用时自动降级为普通目录管理
"""
import asyncio
import hashlib
import os
import re
import shutil
import subprocess
from contextvars import ContextVar
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

from app.config import settings, _HAPPYCLAW_HOME

logger = structlog.get_logger()

# ===== 当前工作区上下文变量 =====
# 工具调用（read_file / write_file / bash_command 等）解析相对路径时，
# 以该上下文变量指向的目录为基准，实现"工作区切换"。
# 不使用 os.chdir（进程级、并发不安全），天然支持主agent与子agent并发时各用各的工作区。
current_workspace: ContextVar[Optional[str]] = ContextVar("current_workspace", default=None)


def get_current_workspace() -> Optional[str]:
    """获取当前工作区路径（无上下文时返回 None）"""
    return current_workspace.get()


def resolve_ws_path(path: str) -> str:
    """
    将工具参数中的路径解析为绝对路径。

    - 绝对路径：原样返回（工作区切换不限制跨工作区访问）
    - 相对路径：拼接当前工作区；无工作区上下文时回退到进程当前目录
    """
    if os.path.isabs(path):
        return os.path.normpath(os.path.abspath(path))
    ws = current_workspace.get()
    base = ws if ws else os.getcwd()
    return os.path.normpath(os.path.abspath(os.path.join(base, path)))


# ===== 名称清洗 =====

# 目录名非法字符：路径分隔符与保留符号
_DIR_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# git 分支名非法字符/序列
_BRANCH_INVALID = re.compile(r'[\\\s~^:?*\[\]<>\|\x00-\x1f]')
_BRANCH_BAD_SEQ = re.compile(r"(\.\.|//|@{|\.lock$|^/|/$|\.$)")


def sanitize_dir_name(name: str) -> str:
    """清洗目录名：替换非法字符为下划线，保留中文"""
    name = (name or "").strip() or "unnamed"
    return _DIR_INVALID.sub("_", name)


def sanitize_branch_name(name: str) -> str:
    """清洗 git 分支名：替换非法字符/序列，保留中文"""
    name = (name or "").strip() or "unnamed"
    name = _BRANCH_INVALID.sub("_", name)
    # 迭代消除非法序列（.., //, @{ 等）
    while True:
        new_name = _BRANCH_BAD_SEQ.sub("_", name)
        if new_name == name:
            break
        name = new_name
    return name


def _short_hash(*parts) -> str:
    """生成短哈希后缀，用于避免同名冲突"""
    raw = "/".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:8]


class WorkspaceIsolationManager:
    """工作区隔离管理器（全局单例）"""

    def __init__(self, root: Optional[str] = None):
        self.root = os.path.abspath(root or settings.get("WORKSPACE_ROOT", str(_HAPPYCLAW_HOME / "workspace")))
        self.agents_dir = os.path.join(self.root, "agents")
        self.forked_dir = os.path.join(self.root, "forked-agents")
        self._git_ok: Optional[bool] = None
        # 每个仓库一把锁，避免并发 git 操作冲突
        self._repo_locks: Dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    # ---------- 基础能力 ----------

    def get_scope(self) -> str:
        """读取隔离粒度配置: session(默认) / agent / shared"""
        scope = settings.get("AGENT_DEFAULTS_SANDBOX_SCOPE", "session")
        scope = str(scope).lower()
        return scope if scope in ("session", "agent", "shared") else "session"

    def git_available(self) -> bool:
        """检测 git 是否可用（结果缓存）"""
        if self._git_ok is None:
            try:
                r = subprocess.run(
                    ["git", "--version"], capture_output=True, timeout=10
                )
                self._git_ok = r.returncode == 0
            except Exception:
                self._git_ok = False
            if not self._git_ok:
                logger.warning("git not available, workspace isolation falls back to plain directories")
        return self._git_ok

    async def _repo_lock(self, repo: str) -> asyncio.Lock:
        key = os.path.normcase(os.path.abspath(repo))
        async with self._locks_guard:
            lock = self._repo_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._repo_locks[key] = lock
            return lock

    @staticmethod
    def _git_sync(args, cwd: Optional[str] = None, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            errors="replace",
        )

    async def _git(self, args, cwd: Optional[str] = None, timeout: int = 60) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(self._git_sync, args, cwd, timeout)

    def _ensure_exclude(self, repo: str, entry: str):
        """将目录写入仓库 .git/info/exclude，避免 worktree 目录产生 untracked 噪音"""
        exclude_dir = os.path.join(repo, ".git", "info")
        exclude_file = os.path.join(exclude_dir, "exclude")
        try:
            os.makedirs(exclude_dir, exist_ok=True)
            existing = ""
            if os.path.exists(exclude_file):
                with open(exclude_file, "r", encoding="utf-8") as f:
                    existing = f.read()
            if entry not in existing.splitlines():
                with open(exclude_file, "a", encoding="utf-8") as f:
                    f.write(f"\n{entry}\n")
        except Exception as e:
            logger.debug("Failed to update git exclude", repo=repo, entry=entry, error=str(e))

    async def ensure_repo(self, path: str, identity_name: Optional[str] = None) -> bool:
        """
        确保目录是一个 git 仓库：无 .git 时执行 git init，
        配置本地作者身份并建立初始提交（worktree 要求至少一个提交）。

        Returns:
            True 表示已是可用的 git 仓库；False 表示降级为普通目录
        """
        os.makedirs(path, exist_ok=True)
        if not self.git_available():
            return False

        lock = await self._repo_lock(path)
        async with lock:
            if os.path.exists(os.path.join(path, ".git")):
                return True
            try:
                r = await self._git(["git", "init", "-b", "main"], cwd=path)
                if r.returncode != 0:
                    # 旧版 git 不支持 -b，退化为 init + 重命名
                    r = await self._git(["git", "init"], cwd=path)
                    if r.returncode != 0:
                        raise RuntimeError(r.stderr.strip())
                    await self._git(["git", "checkout", "-b", "main"], cwd=path)

                name = identity_name or os.path.basename(os.path.abspath(path))
                slug = sanitize_dir_name(name)
                await self._git(["git", "config", "user.name", name], cwd=path)
                await self._git(
                    ["git", "config", "user.email", f"{slug}@agents.happy-claw.local"], cwd=path
                )
                # 初始空提交：worktree 需要至少一个提交作为起点
                r = await self._git(
                    ["git", "commit", "--allow-empty", "-m", "chore: init workspace"], cwd=path
                )
                if r.returncode != 0:
                    raise RuntimeError(r.stderr.strip())
                logger.info("Workspace repo initialized", path=path, identity=name)
                return True
            except Exception as e:
                logger.warning("git init failed, fallback to plain directory", path=path, error=str(e))
                return False

    # ---------- 全局 / 主agent 工作区 ----------

    async def ensure_global_repo(self) -> str:
        """初始化全局共享工作区，返回路径"""
        await self.ensure_repo(self.root, identity_name="happy-claw-global")
        return self.root

    async def ensure_agent_workspace(self, agent_name: str) -> str:
        """初始化主agent工作区（含独立 git 身份），返回路径"""
        dir_name = sanitize_dir_name(agent_name)
        path = os.path.join(self.agents_dir, dir_name)
        await self.ensure_repo(path, identity_name=agent_name)
        return path

    # ---------- worktree 创建/删除 ----------

    async def _worktree_add(self, repo: str, branch: str, path: str) -> bool:
        """执行 git worktree add，处理分支已存在的冲突，失败返回 False"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 目标目录已存在（可能是残留）：先尝试清理
        if os.path.exists(path):
            await self._worktree_remove(repo, path, branch)

        r = await self._git(["git", "-C", repo, "worktree", "add", "-b", branch, path])
        if r.returncode == 0:
            return True

        stderr = (r.stderr or "") + (r.stdout or "")
        if "already exists" in stderr:
            # 分支已存在：删除旧分支后重试一次
            await self._git(["git", "-C", repo, "branch", "-D", branch])
            r = await self._git(["git", "-C", repo, "worktree", "add", "-b", branch, path])
            if r.returncode == 0:
                return True
            stderr = (r.stderr or "") + (r.stdout or "")
        logger.warning("git worktree add failed", repo=repo, branch=branch, path=path, error=stderr.strip())
        return False

    async def _worktree_remove(self, repo: str, path: str, branch: Optional[str] = None):
        """移除 worktree 及其分支（尽力而为，失败时目录兜底删除）"""
        try:
            await self._git(["git", "-C", repo, "worktree", "remove", "--force", path])
        except Exception as e:
            logger.debug("worktree remove error (ignored)", path=path, error=str(e))
        # 清理 worktree 元数据残留
        try:
            await self._git(["git", "-C", repo, "worktree", "prune"])
        except Exception:
            pass
        # 目录兜底删除
        if os.path.exists(path):
            try:
                shutil.rmtree(path)
            except Exception as e:
                logger.warning("Failed to rmtree workspace dir", path=path, error=str(e))
        # 删除分支
        if branch:
            try:
                await self._git(["git", "-C", repo, "branch", "-D", branch])
            except Exception as e:
                logger.debug("branch delete error (ignored)", branch=branch, error=str(e))

    async def create_session_workspace(
        self, agent_name: str, session_id: str
    ) -> Tuple[str, str]:
        """
        创建会话工作区（git worktree）。

        Returns:
            (workspace_path, branch)，branch 在降级模式下为空字符串
        """
        repo = await self.ensure_agent_workspace(agent_name)
        session_dir = sanitize_dir_name(session_id)
        path = os.path.join(repo, session_dir)
        branch = f"session/{sanitize_branch_name(session_id)}"

        # 幂等：已存在的会话工作区直接复用（避免误删已有内容）
        if os.path.isdir(path):
            return path, branch if self.git_available() else ""

        # 会话 worktree 目录位于主仓库工作区内，写入 exclude 规避 untracked 噪音
        self._ensure_exclude(repo, f"/{session_dir}/")

        ok = False
        if self.git_available():
            lock = await self._repo_lock(repo)
            async with lock:
                ok = await self._worktree_add(repo, branch, path)
        if not ok:
            os.makedirs(path, exist_ok=True)
            branch = ""
        logger.info("Session workspace ready", agent=agent_name, session_id=session_id, path=path)
        return path, branch

    async def remove_session_workspace(self, agent_name: str, session_id: str) -> bool:
        """删除会话工作区及其分支（会话删除时联动清理）"""
        repo = os.path.join(self.agents_dir, sanitize_dir_name(agent_name))
        path = os.path.join(repo, sanitize_dir_name(session_id))
        branch = f"session/{sanitize_branch_name(session_id)}"
        return await self.remove_workspace(path, branch=branch, repo=repo)

    # ---------- 孤儿会话工作区启动兜底清理 ----------

    def list_session_workspaces(self) -> List[Tuple[str, str, str]]:
        """
        同步扫描 agents 目录，枚举各 agent 仓库下的会话级子目录。

        Returns:
            [(agent_dir_name, session_dir_name, path)]，已排除隐藏目录
            （.git/.worktrees 等非会话目录）
        """
        results: List[Tuple[str, str, str]] = []
        if not os.path.isdir(self.agents_dir):
            return results
        try:
            agent_names = os.listdir(self.agents_dir)
        except Exception:
            return results
        for agent_name in agent_names:
            agent_dir = os.path.join(self.agents_dir, agent_name)
            if not os.path.isdir(agent_dir):
                continue
            try:
                entries = os.listdir(agent_dir)
            except Exception:
                continue
            for entry in entries:
                if entry.startswith("."):
                    continue  # .git / .worktrees 等
                p = os.path.join(agent_dir, entry)
                if os.path.isdir(p):
                    results.append((agent_name, entry, p))
        return results

    async def cleanup_orphan_session_workspaces(
        self, conversation_exists: Callable[[str], Awaitable[bool]]
    ) -> int:
        """
        启动兜底：检查会话工作区对应的会话是否仍存在，不存在则尝试删除。

        场景：删除会话时工作区目录内文件被占用导致 remove_session_workspace
        失败，残留目录在下次启动时重试清理（此时占用通常已释放）。
        删除仍可能失败（文件依然被引用），但每次启动都会重试，形成检查保障。

        Args:
            conversation_exists: 异步判定会话是否存在的回调（如查数据库），
                抛异常时保守跳过该目录（无法确认时不删）

        Returns:
            成功清理的孤儿工作区数量
        """
        entries = await asyncio.to_thread(self.list_session_workspaces)
        removed = 0
        for agent_name, session_dir, path in entries:
            try:
                exists = await conversation_exists(session_dir)
            except Exception as e:
                # 无法确认会话是否存在（如数据库不可用）：保守保留
                logger.debug(
                    "Orphan check skipped (conversation lookup failed)",
                    path=path, error=str(e),
                )
                continue
            if exists:
                continue
            logger.info("Removing orphan session workspace", agent=agent_name, session=session_dir, path=path)
            if await self.remove_session_workspace(agent_name, session_dir):
                if not os.path.exists(path):
                    removed += 1
                else:
                    logger.warning(
                        "Orphan session workspace still present after removal (files may be locked), will retry on next startup",
                        path=path,
                    )
        return removed

    async def create_subagent_workspace(
        self,
        agent_name: Optional[str],
        session_id: Optional[str],
        sub_name: str,
    ) -> Tuple[str, str, str]:
        """
        创建子agent/队友工作区（git worktree，临时）。
        
        agent_name=None 时在全局共享工作区下创建（对应 scope=shared）；
        session_id=None 时直接位于 agent/全局工作区下（对应 scope=agent）。

        优先嵌套在会话工作区下；若 git 拒绝（如某些版本禁止 worktree 嵌套），
        降级到 {repo}/.worktrees/{session}/{sub}，最终回退普通目录。

        Returns:
            (workspace_path, branch, repo)
        """
        if agent_name:
            repo = await self.ensure_agent_workspace(agent_name)
        else:
            repo = await self.ensure_global_repo()
        sub_dir = sanitize_dir_name(sub_name)
        session_dir = sanitize_dir_name(session_id) if session_id else None

        if session_dir:
            path = os.path.join(repo, session_dir, sub_dir)
            branch = f"sub/{sanitize_branch_name(session_id)}/{sanitize_branch_name(sub_name)}"
        else:
            path = os.path.join(repo, sub_dir)
            branch = f"sub/{sanitize_branch_name(sub_name)}"
        # 分支唯一性兜底：不同会话可能产生同名子agent
        branch = f"{branch}-{_short_hash(agent_name, str(session_id), sub_name)}"

        if self.git_available():
            lock = await self._repo_lock(repo)
            async with lock:
                if await self._worktree_add(repo, branch, path):
                    logger.info("Subagent workspace ready (worktree)", path=path, branch=branch)
                    return path, branch, repo
                # 降级：放到 .worktrees 下（规避嵌套限制）
                alt_dir = os.path.join(repo, ".worktrees", session_dir or "_", sub_dir)
                if await self._worktree_add(repo, branch, alt_dir):
                    logger.info("Subagent workspace ready (fallback worktree)", path=alt_dir, branch=branch)
                    return alt_dir, branch, repo

        os.makedirs(path, exist_ok=True)
        logger.info("Subagent workspace ready (plain dir)", path=path)
        return path, "", repo

    # ---------- forked agent 工作区（记忆提取/整理等后台任务） ----------

    def forked_workspace_path(self, forked_id: str) -> str:
        """计算 forked agent 工作区路径：{root}/forked-agents/{forked_id}"""
        return os.path.join(self.forked_dir, sanitize_dir_name(forked_id))

    def _is_forked_path(self, path: str) -> bool:
        """安全边界：判断路径是否位于 forked-agents 目录下"""
        prefix = os.path.normcase(os.path.abspath(self.forked_dir)) + os.sep
        return os.path.normcase(os.path.abspath(path)).startswith(prefix)

    async def create_forked_workspace(self, forked_id: str) -> str:
        """创建 forked agent 工作区（普通目录，无需 git），返回路径"""
        path = self.forked_workspace_path(forked_id)

        def _create():
            os.makedirs(path, exist_ok=True)
            # 全局仓库已初始化时，将 forked-agents 写入 exclude 避免 untracked 噪音
            if os.path.exists(os.path.join(self.root, ".git")):
                self._ensure_exclude(self.root, "/forked-agents/")

        await asyncio.to_thread(_create)
        logger.debug("Forked agent workspace ready", forked_id=forked_id, path=path)
        return path

    async def remove_forked_workspace(self, forked: str) -> bool:
        """删除 forked agent 工作区（参数可为 forked_id 或绝对路径）"""
        path = forked if os.path.isabs(forked) else self.forked_workspace_path(forked)
        if not self._is_forked_path(path):
            logger.warning("Refuse to remove path outside forked-agents dir", path=path)
            return False
        await asyncio.to_thread(shutil.rmtree, path, True)
        logger.debug("Forked agent workspace removed", path=path)
        return True

    async def cleanup_forked_workspaces(self) -> int:
        """启动兜底：扫描 forked-agents 目录，清空全部残留工作区（异常退出未清理），返回清理数量"""
        def _sweep() -> int:
            if not os.path.isdir(self.forked_dir):
                return 0
            removed = 0
            for name in os.listdir(self.forked_dir):
                p = os.path.join(self.forked_dir, name)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                    removed += 1
                except Exception as e:
                    logger.warning("Failed to remove residual forked workspace", path=p, error=str(e))
            return removed

        removed = await asyncio.to_thread(_sweep)
        if removed:
            logger.info("Residual forked agent workspaces cleaned", removed=removed)
        return removed

    async def remove_workspace(self, path: str, branch: str = "", repo: str = "") -> bool:
        """
        删除工作区：git worktree remove + git branch -D，失败时目录兜底删除。
            
        Args:
            path: 工作区目录
            branch: 关联分支（降级创建时为空）
            repo: 所属主仓库（worktree 元数据所在仓库）
        """
        try:
            await asyncio.to_thread(self.remove_workspace_sync, path, branch, repo)
            logger.info("Workspace removed", path=path, branch=branch or None)
            return True
        except Exception as e:
            logger.error("Failed to remove workspace", path=path, error=str(e), exc_info=True)
            return False
        
    def remove_workspace_sync(self, path: str, branch: str = "", repo: str = "") -> bool:
        """同步版工作区删除（供关闭清理等同步上下文使用）"""
        if not os.path.exists(path):
            return True
        if branch and repo and self.git_available():
            try:
                self._git_sync(["git", "-C", repo, "worktree", "remove", "--force", path])
            except Exception as e:
                logger.debug("worktree remove error (ignored)", path=path, error=str(e))
            try:
                self._git_sync(["git", "-C", repo, "worktree", "prune"])
            except Exception:
                pass
            try:
                self._git_sync(["git", "-C", repo, "branch", "-D", branch])
            except Exception as e:
                logger.debug("branch delete error (ignored)", branch=branch, error=str(e))
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
        return True


# 全局单例
workspace_isolation = WorkspaceIsolationManager()
