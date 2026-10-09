"""任务服务 - 管理Agent任务执行"""
import os
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

import structlog
from app.models.schemas.task import AsyncTask, TaskStatus, ScheduledTask
from app.models.schemas.agent import AgentConfig, AgentStatus
from app.agents.single import SingleAgent, create_single_agent
from app.core.memory import MemoryManager, create_memory_manager
from app.core.prompt_engine import PromptEngine, create_prompt_engine
from app.core.reflector import Reflector, create_reflector
from app.core.compressor import ContextCompressor, create_compressor
from app.tools.registry import tool_registry, create_filtered_tool_registry
from app.services.llm_service import LLMService, create_llm_service
from app.services.persistence import persistence
from app.config import settings, _HAPPYCLAW_HOME

logger = structlog.get_logger()


class TaskService:
    """任务服务"""
    
    def __init__(self):
        self._tasks: Dict[str, AsyncTask] = {}
        self._agents: Dict[str, SingleAgent] = {}
        self._scheduled_tasks: Dict[str, ScheduledTask] = {}
    
    async def initialize(self) -> None:
        """从数据库加载所有数据"""
        try:
            # 加载所有Agent
            agents_config = await persistence.load_all_agents()
            print(f"[DEBUG] Loaded {len(agents_config)} agents from DB")
            for config in agents_config:
                try:
                    agent = self.create_agent(config, save_to_db=False)
                    print(f"[DEBUG] Agent created successfully: {agent.id}")
                    # 加载该Agent的会话
                    conversations = await persistence.load_conversations_for_agent(config.id)
                    for conv in conversations:
                        agent._conversations[conv.id] = conv
                    logger.info("Agent loaded from DB", agent_id=config.id, conversations=len(conversations))
                except Exception as e:
                    import traceback
                    print(f"[DEBUG] Failed to load agent {config.id}: {str(e)}")
                    print(traceback.format_exc())
                    logger.error("Failed to load agent", agent_id=config.id, error=str(e), exc_info=True)
            
            print(f"[DEBUG] TaskService._agents has {len(self._agents)} agents")
            logger.info("TaskService initialized from DB", agents=len(self._agents))
        except Exception as e:
            import traceback
            print(f"[DEBUG] Failed to initialize from DB: {str(e)}")
            print(traceback.format_exc())
            logger.warning("Failed to initialize from DB, starting fresh", error=str(e), exc_info=True)
    
    async def execute_agent_task(
        self,
        agent_id: str,
        task_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """执行Agent任务"""
        task_id = str(uuid.uuid4())
        
        task = AsyncTask(
            id=task_id,
            task_type="agent_task",
            status="running",
        )
        self._tasks[task_id] = task
        
        try:
            # 获取或创建Agent
            agent = self._agents.get(agent_id)
            if not agent:
                raise ValueError(f"Agent not found: {agent_id}")
            
            # 执行任务
            task_description = task_data.get("task", "")
            context = task_data.get("context")
            
            result = await agent.execute_task(task_description, context)
            
            task.status = "completed"
            task.result = result
            task.completed_at = datetime.utcnow()
            task.progress = 1.0
            
            logger.info("Task completed", task_id=task_id, agent_id=agent_id)
            return {"task_id": task_id, "status": "completed", "result": result}
        
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            task.completed_at = datetime.utcnow()
            
            logger.error("Task failed", task_id=task_id, error=str(e), exc_info=True)
            return {"task_id": task_id, "status": "failed", "error": str(e)}
    
    async def run_scheduled_task(
        self,
        task_name: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """执行定时任务"""
        logger.info("Running scheduled task", task_name=task_name)
        
        # 根据任务名执行对应逻辑
        if task_name == "cleanup_old_conversations":
            return await self._cleanup_old_conversations(params)
        elif task_name == "health_check":
            return await self._health_check()
        else:
            logger.warning("Unknown scheduled task", task_name=task_name)
            return {"status": "unknown_task"}
    
    async def _cleanup_old_conversations(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """清理旧会话"""
        days = params.get("days", 30)
        # 实际实现应查询数据库清理旧会话
        return {"status": "completed", "cleaned": 0}
    
    async def _health_check(self) -> Dict[str, Any]:
        """健康检查"""
        return {
            "status": "healthy",
            "active_agents": len([a for a in self._agents.values() if a.status == AgentStatus.RUNNING]),
            "active_tasks": len([t for t in self._tasks.values() if t.status == "running"]),
        }
    
    def _create_agent_folders(self, agent_id: str, agent_name: str) -> None:
        """创建Agent的文件夹结构"""
        # 获取项目根目录（backend/ 的父目录）
        backend_dir = Path(__file__).resolve().parent.parent.parent
        project_root = backend_dir.parent
        
        # .happy-claw/agents/{agent_name}/ 用于存放Agent数据
        agents_data_dir = _HAPPYCLAW_HOME / "agents" / agent_name
        agents_data_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Agent data directory created", path=str(agents_data_dir))
    
    def _apply_primary_model_config(self, config: AgentConfig) -> Optional[Dict[str, Any]]:
        """根据 settings.json 的 agent.entries.{name}.model.primary 覆盖默认模型与参数。

        - 将 config.llm_model 设为 primary 指定的 model_id；
        - 从 agent.llm.providers.{provider}.models 中对应模型读取 maxTokens/contextWindow 覆盖参数；
        - 未配置 primary 或解析失败时保持原值不变。

        Returns:
            解析出的 primary 信息（含 provider/model_id 等），未配置时返回 None。
        """
        try:
            from app.core.settings_store import resolve_agent_primary_model
            resolved = resolve_agent_primary_model(config.name)
            if not resolved:
                return None
            if resolved.get("model_id"):
                config.llm_model = resolved["model_id"]
            if resolved.get("max_tokens"):
                config.max_tokens = int(resolved["max_tokens"])
            if resolved.get("context_window"):
                config.context_window = int(resolved["context_window"])
            logger.info(
                "Applied primary model config for agent",
                agent_name=config.name,
                model_ref=resolved.get("model_ref"),
                max_tokens=config.max_tokens,
                context_window=config.context_window,
            )
            return resolved
        except Exception as e:
            logger.warning(
                "Failed to apply primary model config",
                agent_name=config.name,
                error=str(e),
            )
            return None

    def create_agent(
        self,
        config: AgentConfig,
        save_to_db: bool = True,
    ) -> SingleAgent:
        """创建Agent"""
        # 如果config没有id，生成新的UUID
        if not config.id:
            config.id = str(uuid.uuid4())
        
        # ✅ 主agent：根据 settings.json 中 agent.entries.{name}.model.primary 应用默认模型及参数
        #    模型参数（maxTokens/contextWindow）来源于 agent.llm.providers.{provider}.models 中对应模型
        resolved_primary = None
        if not config.is_subagent:
            resolved_primary = self._apply_primary_model_config(config)
        
        # 创建Agent文件夹结构
        self._create_agent_folders(config.id, config.name)
        
        # ✅ 供应商以 primary 解析结果为准（如 deepseek），base_url/api_key 由 LLMService 按供应商自动解析
        provider_name = config.llm_provider.value
        if resolved_primary and resolved_primary.get("provider"):
            provider_name = resolved_primary["provider"]
        
        llm_service = create_llm_service(
            provider=provider_name,
            model=config.llm_model,
        )
        
        memory_manager = create_memory_manager()
        prompt_engine = create_prompt_engine()
        reflector = create_reflector(llm_service)
        compressor = create_compressor(llm_service)
        
        # 根据 agent 名称创建过滤后的工具注册表
        filtered_registry = create_filtered_tool_registry(agent_name=config.name)

        agent = create_single_agent(
            config=config,
            llm_service=llm_service,
            tool_registry=filtered_registry,
            memory_manager=memory_manager,
            prompt_engine=prompt_engine,
            reflector=reflector,
            compressor=compressor,
        )

        # ✅ 主 agent：根据 settings.json 中 agent.entries.{name}.skills.disallowed/allowed 计算技能拒绝列表
        if not config.is_subagent:
            try:
                from app.skills.loader import compute_agent_deny_skills
                deny_skills = compute_agent_deny_skills(config.name)
                if deny_skills:
                    agent.deny_skills = deny_skills
                    logger.info("Applied agent skill deny list", agent_name=config.name, deny_skills=deny_skills)
            except Exception as e:
                logger.warning("Failed to apply agent skill deny list", agent_name=config.name, error=str(e))

        self._agents[agent.id] = agent
        
        # 保存到数据库
        if save_to_db:
            import asyncio
            asyncio.create_task(persistence.save_agent(config, agent.status))
        
        logger.info("Agent created via TaskService", agent_id=agent.id)
        return agent
    
    def get_agent(self, agent_id: str) -> Optional[SingleAgent]:
        """获取Agent"""
        return self._agents.get(agent_id)
    
    def list_agents(self) -> List[Dict[str, Any]]:
        """列出所有Agent"""
        return [
            {
                "id": agent.id,
                "name": agent.config.name,
                "description": agent.config.description,
                "config": agent.config,  # 返回完整配置
                "status": agent.status,
                "created_at": agent.created_at,
            }
            for agent in self._agents.values()
        ]
    
    async def stop_agent(self, agent_id: str) -> bool:
        """停止Agent"""
        agent = self._agents.get(agent_id)
        if not agent:
            return False
        
        await agent.stop()
        await agent.cleanup()
        del self._agents[agent_id]
        
        # 从数据库删除
        await persistence.delete_agent(agent_id)
        
        logger.info("Agent stopped", agent_id=agent_id)
        return True
    
    def get_task(self, task_id: str) -> Optional[AsyncTask]:
        """获取任务"""
        return self._tasks.get(task_id)
    
    def list_tasks(self, status: Optional[str] = None) -> List[AsyncTask]:
        """列出任务"""
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks
    
    def register_scheduled_task(self, task: ScheduledTask):
        """注册定时任务"""
        self._scheduled_tasks[task.id] = task
        logger.info("Scheduled task registered", task_id=task.id, name=task.name)
    
    def list_scheduled_tasks(self) -> List[ScheduledTask]:
        """列出定时任务"""
        return list(self._scheduled_tasks.values())


# 全局任务服务实例
task_service = TaskService()
