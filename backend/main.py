"""FastAPI应用入口"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import structlog

from app.config import settings
from app.api.v1 import agents, chat, tools, teams, tasks, events, memory, jobs, slash_commands
from app.api.v1 import settings as settings_router
from app.utils.database import engine, Base, AsyncSessionLocal
from app.utils.redis import redis_client
from app.utils.logging import setup_logging
from app.services.task_service import task_service


# 配置日志
setup_logging()
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("Starting AI Agent Framework", version=settings.APP_VERSION)
    
    # 加载子代理配置
    from app.core.config_loader import load_subagent_config
    load_subagent_config()
    
    # 加载扩展工具（从 .happy-claw/extensions/ 目录）
    from app.tools.extension_loader import load_extensions
    load_extensions()
    
    # 加载并连接 MCP 服务器（配置来自 settings.json + .happy-claw/plugin-mcp/.mcp.json）
    try:
        from app.tools.mcp import mcp_manager
        await mcp_manager.load_and_connect()
    except Exception as e:
        logger.warning("MCP manager init failed", error=str(e), exc_info=True)
    
    # 初始化记忆系统目录（.happy-claw/memory/）
    try:
        from app.core.memory_system.store import memory_store
        if memory_store.enabled:
            memory_store.ensure_dirs()
            logger.info("Memory system initialized", root=str(memory_store.memory_root()))
    except Exception as e:
        logger.warning("Memory system init failed", error=str(e))
    
    # 清理残留的 forked agent 工作区（第二层保障：应用异常退出时
    # 记忆提取/整理等 forked agent 未能自行清理 .happy-claw/workspace/forked-agents/）
    try:
        from app.core.workspace_isolation import workspace_isolation
        removed = await workspace_isolation.cleanup_forked_workspaces()
        if removed:
            logger.info("Residual forked agent workspaces cleaned on startup", removed=removed)
    except Exception as e:
        logger.warning("Forked workspace startup cleanup failed", error=str(e))
    
    # 初始化文件型任务系统（启动清理过期任务 + 启动文件监听器）
    try:
        from app.core.task_system.store import task_store
        from app.core.task_system.watcher import task_watcher
        removed = await task_store.cleanup_expired()
        if removed:
            logger.info("Expired tasks cleaned on startup", removed=removed)
        await task_watcher.start()
    except Exception as e:
        logger.warning("Task system init failed", error=str(e), exc_info=True)
    
    # 初始化后台作业系统（启动清理过期作业）
    try:
        from app.core.background_jobs import background_job_manager
        await background_job_manager.startup_cleanup()
        if not background_job_manager.disabled:
            logger.info("Background job manager initialized", max_concurrent=background_job_manager.max_concurrent)
        else:
            logger.info("Background job manager disabled via env var")
    except Exception as e:
        logger.warning("Background job manager init failed", error=str(e), exc_info=True)
    
    # 初始化定时任务调度器（启动清理过期任务 + 恢复调度 + 1秒轮询）
    try:
        from app.core.scheduler.scheduler import scheduler
        await scheduler.start()
    except Exception as e:
        logger.warning("Scheduler init failed", error=str(e), exc_info=True)
    
    # 创建数据库表（如果数据库可用）
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created")
    except Exception as e:
        logger.warning("Database not available, running without DB", error=str(e))
    
    # 初始化Redis连接（如果Redis可用）
    try:
        await redis_client.initialize()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning("Redis not available, running without cache", error=str(e))
    
    # 从数据库加载数据
    await task_service.initialize()
    
    # 清理孤儿会话工作区（兜底保障：删除会话时工作区内文件被占用导致删除失败，
    # 启动时检查会话已不存在则重试删除其工作区；仍被占用则下次启动继续重试）
    try:
        from sqlalchemy import select
        from app.core.workspace_isolation import workspace_isolation
        from app.models.orm.models import ConversationORM

        async def _conversation_exists(conversation_id: str) -> bool:
            async with AsyncSessionLocal() as session:
                row = (await session.execute(
                    select(ConversationORM.id).where(ConversationORM.id == conversation_id)
                )).first()
                return row is not None

        removed = await workspace_isolation.cleanup_orphan_session_workspaces(_conversation_exists)
        if removed:
            logger.info("Orphan session workspaces cleaned on startup", removed=removed)
    except Exception as e:
        logger.warning("Orphan session workspace cleanup failed", error=str(e), exc_info=True)
    
    yield
    
    # 关闭时
    # 关闭 MCP 服务器连接
    try:
        from app.tools.mcp import mcp_manager
        await mcp_manager.shutdown()
    except Exception as e:
        logger.warning("MCP manager shutdown failed", error=str(e))
    
    # 停止定时任务调度器
    try:
        from app.core.scheduler.scheduler import scheduler
        await scheduler.stop()
    except Exception as e:
        logger.warning("Scheduler stop failed", error=str(e))
    
    # 停止任务文件监听器
    try:
        from app.core.task_system.watcher import task_watcher
        await task_watcher.stop()
    except Exception as e:
        logger.warning("Task watcher stop failed", error=str(e))
    
    # 清理子agent资源
    try:
        from app.core.subagent_manager import subagent_manager
        subagent_manager.cleanup()
        logger.info("SubAgentManager cleaned up")
    except Exception as e:
        logger.error("Error cleaning up SubAgentManager", error=str(e))
    
    # 停止团队协作后台任务（全部队友工作循环）
    try:
        from app.core.team_system.manager import team_manager
        await team_manager.stop_all()
        logger.info("TeamManager stopped")
    except Exception as e:
        logger.warning("TeamManager stop failed", error=str(e))
    
    # 关闭Redis连接
    try:
        await redis_client.close()
    except Exception:
        pass
    
    # 关闭数据库连接
    try:
        await engine.dispose()
    except Exception:
        pass
    
    logger.info("Application shutdown complete")


# 创建FastAPI应用
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="生产级AI Agent开发框架",
    lifespan=lifespan,
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发环境允许所有来源
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(agents.router, prefix=f"{settings.API_PREFIX}/agents", tags=["agents"])
app.include_router(chat.router, prefix=f"{settings.API_PREFIX}/chat", tags=["chat"])
app.include_router(tools.router, prefix=f"{settings.API_PREFIX}/tools", tags=["tools"])
app.include_router(teams.router, prefix=f"{settings.API_PREFIX}/teams", tags=["teams"])
app.include_router(tasks.router, prefix=f"{settings.API_PREFIX}/tasks", tags=["tasks"])
app.include_router(memory.router, prefix=f"{settings.API_PREFIX}/memory", tags=["memory"])
app.include_router(events.router, prefix=f"{settings.API_PREFIX}/events", tags=["events"])
app.include_router(jobs.router, prefix=f"{settings.API_PREFIX}/jobs", tags=["jobs"])
app.include_router(settings_router.router, prefix=settings.API_PREFIX, tags=["settings"])
app.include_router(slash_commands.router, prefix=f"{settings.API_PREFIX}/slash-commands", tags=["slash-commands"])


# 健康检查端点
@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "database": "connected" if engine else "disconnected",
    }


# 根路径
@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "AI Agent Framework API",
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
