"""后台作业管理器 - 管理长时间运行的后台任务"""
import asyncio
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Any, Optional, List

import structlog

from app.config import settings, _HAPPYCLAW_HOME

logger = structlog.get_logger()


# ===== 枚举定义 =====

class JobType(str, Enum):
    """后台任务类型"""
    LOCAL_BASH = "local_bash"           # 本地 Shell 命令 (前缀 b)
    LOCAL_AGENT = "local_agent"         # 本地子进程 Agent (前缀 a)
    REMOTE_AGENT = "remote_agent"       # 远程服务器 Agent (前缀 r)
    IN_PROCESS_TEAMMATE = "in_process_teammate"  # 进程内协作 Agent (前缀 t)
    LOCAL_WORKFLOW = "local_workflow"   # 本地工作流 (前缀 w)
    MONITOR_MCP = "monitor_mcp"         # MCP 资源监控 (前缀 m)
    DREAM = "dream"                     # 后台记忆整合 (前缀 d)


# 类型前缀映射
JOB_TYPE_PREFIXES = {
    JobType.LOCAL_BASH: "b",
    JobType.LOCAL_AGENT: "a",
    JobType.REMOTE_AGENT: "r",
    JobType.IN_PROCESS_TEAMMATE: "t",
    JobType.LOCAL_WORKFLOW: "w",
    JobType.MONITOR_MCP: "m",
    JobType.DREAM: "d",
}


class JobStatus(str, Enum):
    """作业状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ===== 数据模型 =====

class BackgroundJob:
    """后台作业"""

    def __init__(
        self,
        job_id: str,
        session_id: str,
        job_type: JobType,
        intent: str,
        tool_use_id: str = "",
        name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.job_id = job_id
        self.session_id = session_id
        self.job_type = job_type
        self.status = JobStatus.PENDING
        self.intent = intent
        self.tool_use_id = tool_use_id
        self.name = name or f"{job_type.value}:{job_id}"
        self.metadata = metadata or {}
        self.process: Optional[asyncio.subprocess.Process] = None
        self.timestamps = {
            "created": datetime.now(timezone.utc).isoformat(),
            "started": None,
            "completed": None,
        }
        self.exit_code: Optional[int] = None
        self._asyncio_task: Optional[asyncio.Task] = None

    @property
    def job_dir(self) -> Path:
        """作业目录: .happy-claw/jobs/{session_id}/{job_id}/"""
        return _HAPPYCLAW_HOME / "jobs" / self.session_id / self.job_id

    @property
    def state_file(self) -> Path:
        return self.job_dir / "state.json"

    @property
    def output_file(self) -> Path:
        return self.job_dir / ".output"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "job_type": self.job_type.value,
            "status": self.status.value,
            "intent": self.intent,
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "metadata": self.metadata,
            "timestamps": self.timestamps,
            "exit_code": self.exit_code,
        }

    async def save_state(self):
        """持久化状态到 state.json"""
        self.job_dir.mkdir(parents=True, exist_ok=True)
        state = self.to_dict()
        self.state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ===== 后台作业管理器 =====

class BackgroundJobManager:
    """后台作业管理器"""

    def __init__(self):
        self._jobs: Dict[str, BackgroundJob] = {}  # job_id -> BackgroundJob
        self._pending_notifications: Dict[str, List[Dict[str, Any]]] = {}  # session_id -> [notification]
        self._disabled = os.environ.get("HAPPY_CLAW_DISABLE_BACKGROUND_TASKS", "0") == "1"
        self._semaphore: Optional[asyncio.Semaphore] = None

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def max_concurrent(self) -> int:
        try:
            return settings.background_jobs.max_concurrent
        except Exception:
            return 5

    @property
    def cleanup_days(self) -> int:
        try:
            return settings.background_jobs.cleanup_days
        except Exception:
            return 7

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None or self._semaphore._value != self.max_concurrent:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        return self._semaphore

    # ----- 启动后台 bash 命令 -----

    async def launch_bash_background(
        self,
        command: str,
        session_id: str,
        tool_use_id: str = "",
        cwd: Optional[str] = None,
        timeout_seconds: int = 300,
    ) -> BackgroundJob:
        """启动一个后台 bash 命令并立即返回作业对象"""
        if self._disabled:
            raise RuntimeError("Background tasks are disabled (HAPPY_CLAW_DISABLE_BACKGROUND_TASKS=1)")

        # 生成 job_id: b_{uuid8}
        prefix = JOB_TYPE_PREFIXES[JobType.LOCAL_BASH]
        job_id = f"{prefix}_{uuid.uuid4().hex[:8]}"

        job = BackgroundJob(
            job_id=job_id,
            session_id=session_id,
            job_type=JobType.LOCAL_BASH,
            intent=command,
            tool_use_id=tool_use_id,
            name=f"bash: {command[:60]}",
            metadata={"timeout_seconds": timeout_seconds, "cwd": cwd or os.getcwd()},
        )

        # 创建作业目录
        job.job_dir.mkdir(parents=True, exist_ok=True)

        # 清空输出文件
        job.output_file.write_text("", encoding="utf-8")

        # 保存初始状态
        await job.save_state()

        # 注册到内存
        self._jobs[job_id] = job

        # 启动后台 asyncio Task
        job._asyncio_task = asyncio.create_task(
            self._run_bash_job(job, command, cwd, timeout_seconds)
        )

        logger.info(
            "Background bash job launched",
            job_id=job_id,
            command=command[:80],
            session_id=session_id,
        )

        # 推送事件
        try:
            from app.core.event_bus import event_bus
            await event_bus.emit_event(
                type="background_job_update",
                source=session_id,
                data={"job_id": job_id, "status": "running", "name": job.name},
                dimensions=[f"conversation:{session_id}"],
            )
        except Exception as e:
            logger.debug("Failed to emit background job event", error=str(e))

        return job

    async def _run_bash_job(
        self,
        job: BackgroundJob,
        command: str,
        cwd: Optional[str],
        timeout_seconds: int,
    ):
        """执行后台 bash 命令（在 asyncio.Task 中运行）"""
        import platform

        job.status = JobStatus.RUNNING
        job.timestamps["started"] = datetime.now(timezone.utc).isoformat()
        await job.save_state()

        try:
            # 构建命令
            system = platform.system().lower()
            if system == "windows":
                cmd_args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
            else:
                cmd_args = ["bash", "-c", command]

            work_dir = cwd or os.getcwd()

            # 启动子进程，stdout/stderr 写入 .output 文件
            output_fh = open(job.output_file, "a", encoding="utf-8")

            job.process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=output_fh,
                stderr=output_fh,
                cwd=work_dir,
            )

            try:
                await asyncio.wait_for(job.process.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                job.process.kill()
                await job.process.wait()
                output_fh.write(f"\n[TIMEOUT after {timeout_seconds}s]\n")
                job.exit_code = -1
                job.status = JobStatus.FAILED
                job.timestamps["completed"] = datetime.now(timezone.utc).isoformat()
                await job.save_state()
                output_fh.close()
                await self._on_job_finished(job)
                return

            output_fh.close()
            job.exit_code = job.process.returncode

            if job.exit_code == 0:
                job.status = JobStatus.COMPLETED
            else:
                job.status = JobStatus.FAILED

            job.timestamps["completed"] = datetime.now(timezone.utc).isoformat()
            await job.save_state()

            await self._on_job_finished(job)

        except Exception as e:
            logger.error("Background bash job failed", job_id=job.job_id, error=str(e), exc_info=True)
            job.status = JobStatus.FAILED
            job.exit_code = -1
            job.timestamps["completed"] = datetime.now(timezone.utc).isoformat()
            # 写入错误到输出文件
            try:
                with open(job.output_file, "a", encoding="utf-8") as f:
                    f.write(f"\n[ERROR] {str(e)}\n")
            except Exception:
                pass
            await job.save_state()
            await self._on_job_finished(job)

    async def _on_job_finished(self, job: BackgroundJob):
        """作业完成回调：构造 task-notification 并加入待注入队列"""
        # 读取输出文件尾部作为摘要
        output_summary = ""
        try:
            if job.output_file.exists():
                content = job.output_file.read_text(encoding="utf-8", errors="replace")
                # 取最后 2000 字符
                output_summary = content[-2000:] if len(content) > 2000 else content
        except Exception:
            pass

        status_text = "completed" if job.status == JobStatus.COMPLETED else "failed"
        summary = f'Background command "{job.intent[:60]}" {status_text} (exit code {job.exit_code})'

        notification = {
            "job_id": job.job_id,
            "tool_use_id": job.tool_use_id,
            "output_file": str(job.output_file),
            "status": job.status.value,
            "summary": summary,
            "output_summary": output_summary,
            "exit_code": job.exit_code,
            "timestamps": job.timestamps,
        }

        # 入队到 session 的待注入通知
        if job.session_id not in self._pending_notifications:
            self._pending_notifications[job.session_id] = []
        self._pending_notifications[job.session_id].append(notification)

        logger.info("Background job finished, notification queued", job_id=job.job_id, status=job.status.value)

        # 推送事件
        try:
            from app.core.event_bus import event_bus
            await event_bus.emit_event(
                type="background_job_update",
                source=job.session_id,
                data={
                    "job_id": job.job_id,
                    "status": job.status.value,
                    "name": job.name,
                    "summary": summary,
                },
                dimensions=[f"conversation:{job.session_id}"],
            )
        except Exception as e:
            logger.debug("Failed to emit background job completion event", error=str(e))

    # ----- 通知消费 -----

    def consume_notifications(self, session_id: str) -> List[Dict[str, Any]]:
        """消费指定会话的所有待注入通知（取出后清空）"""
        notifications = self._pending_notifications.pop(session_id, [])
        return notifications

    def has_pending_notifications(self, session_id: str) -> bool:
        return bool(self._pending_notifications.get(session_id))

    # ----- 查询 -----

    def get_job(self, job_id: str) -> Optional[BackgroundJob]:
        return self._jobs.get(job_id)

    def list_jobs(self, session_id: Optional[str] = None) -> List[BackgroundJob]:
        if session_id:
            return [j for j in self._jobs.values() if j.session_id == session_id]
        return list(self._jobs.values())

    def get_job_output(self, job_id: str, tail_chars: int = 0) -> Optional[str]:
        """获取作业输出内容"""
        job = self._jobs.get(job_id)
        if not job:
            return None
        try:
            if job.output_file.exists():
                content = job.output_file.read_text(encoding="utf-8", errors="replace")
                if tail_chars > 0 and len(content) > tail_chars:
                    return content[-tail_chars:]
                return content
        except Exception:
            pass
        return ""

    # ----- 取消 -----

    async def cancel_job(self, job_id: str) -> bool:
        """取消一个正在运行的后台作业"""
        job = self._jobs.get(job_id)
        if not job:
            return False

        if job.status not in (JobStatus.RUNNING, JobStatus.PENDING):
            return False

        # 杀进程
        if job.process and job.process.returncode is None:
            try:
                job.process.kill()
            except Exception:
                pass

        # 取消 asyncio task
        if job._asyncio_task and not job._asyncio_task.done():
            job._asyncio_task.cancel()

        job.status = JobStatus.CANCELLED
        job.timestamps["completed"] = datetime.now(timezone.utc).isoformat()
        await job.save_state()

        logger.info("Background job cancelled", job_id=job_id)
        return True

    # ----- 清理 -----

    async def cleanup_session(self, session_id: str):
        """清理指定会话的所有后台作业"""
        jobs = self.list_jobs(session_id)
        for job in jobs:
            await self.cancel_job(job.job_id)
            # 从内存移除
            self._jobs.pop(job.job_id, None)

        # 删除作业目录
        session_jobs_dir = _HAPPYCLAW_HOME / "jobs" / session_id
        if session_jobs_dir.exists():
            try:
                shutil.rmtree(session_jobs_dir)
                logger.info("Session jobs directory cleaned", session_id=session_id)
            except Exception as e:
                logger.warning("Failed to clean session jobs directory", session_id=session_id, error=str(e))

        # 清除通知
        self._pending_notifications.pop(session_id, None)

    async def startup_cleanup(self):
        """应用启动时自动清理过期作业"""
        jobs_dir = _HAPPYCLAW_HOME / "jobs"
        if not jobs_dir.exists():
            return

        cleanup_days = self.cleanup_days
        now = time.time()
        cleaned = 0
        marked_failed = 0

        for session_dir in jobs_dir.iterdir():
            if not session_dir.is_dir():
                continue
            for job_dir in session_dir.iterdir():
                if not job_dir.is_dir():
                    continue
                state_file = job_dir / "state.json"
                if not state_file.exists():
                    continue

                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    created_str = state.get("timestamps", {}).get("created", "")
                    status = state.get("status", "unknown")

                    # 检查是否过期
                    if created_str:
                        created_dt = datetime.fromisoformat(created_str)
                        age_seconds = now - created_dt.timestamp()
                        if age_seconds > cleanup_days * 86400:
                            shutil.rmtree(job_dir)
                            cleaned += 1
                            continue

                    # 标记 running 状态的作业为 failed（进程已丢失）
                    if status in ("running", "pending"):
                        state["status"] = "failed"
                        state["timestamps"]["completed"] = datetime.now(timezone.utc).isoformat()
                        state_file.write_text(
                            json.dumps(state, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        marked_failed += 1

                except Exception as e:
                    logger.warning("Failed to process job dir on startup", path=str(job_dir), error=str(e))

        if cleaned or marked_failed:
            logger.info(
                "Background jobs startup cleanup complete",
                cleaned_expired=cleaned,
                marked_failed=marked_failed,
            )

    # ----- 构造注入消息 -----

    @staticmethod
    def build_notification_message(notifications: List[Dict[str, Any]]) -> str:
        """将通知列表构造为 <task-notification> XML 格式消息"""
        parts = []
        for n in notifications:
            part = (
                f"<task-notification>\n"
                f"  <task-id>{n['job_id']}</task-id>\n"
                f"  <tool-use-id>{n.get('tool_use_id', '')}</tool-use-id>\n"
                f"  <output-file>{n.get('output_file', '')}</output-file>\n"
                f"  <status>{n['status']}</status>\n"
                f"  <summary>{n['summary']}</summary>\n"
                f"  <exit-code>{n.get('exit_code', '')}</exit-code>\n"
                f"</task-notification>"
            )
            parts.append(part)
        return "\n".join(parts)


# 全局单例
background_job_manager = BackgroundJobManager()
