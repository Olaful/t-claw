"""子Agent - 由主Agent动态创建"""
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, AsyncIterator

import structlog
from app.models.schemas.agent import AgentConfig, SubAgentConfig, AgentStatus
from app.models.schemas.chat import Message, MessageRole, MessageType
from app.agents.single import SingleAgent
from app.core.memory import MemoryManager
from app.core.prompt_engine import PromptEngine
from app.tools.registry import ToolRegistry
from app.services.llm_service import LLMService

logger = structlog.get_logger()


class SubAgentManager:
    """子Agent管理器"""
    
    def __init__(
        self,
        parent_agent_id: str,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        prompt_engine: PromptEngine,
    ):
        self.parent_agent_id = parent_agent_id
        self.llm_service = llm_service
        self.tool_registry = tool_registry
        self.prompt_engine = prompt_engine
        
        self._sub_agents: Dict[str, SingleAgent] = {}
        self._sub_configs: Dict[str, SubAgentConfig] = {}
    
    async def spawn(
        self,
        config: SubAgentConfig,
    ) -> SingleAgent:
        """创建子Agent"""
        config.parent_agent_id = self.parent_agent_id
        
        # 创建子Agent
        sub_agent = SingleAgent(
            config=config.config,
            llm_service=self.llm_service,
            tool_registry=self.tool_registry,
            memory_manager=MemoryManager(),
            prompt_engine=self.prompt_engine,
        )
        
        self._sub_agents[sub_agent.id] = sub_agent
        self._sub_configs[sub_agent.id] = config
        
        await sub_agent.start()
        
        logger.info(
            "Sub-agent spawned",
            parent_id=self.parent_agent_id,
            sub_agent_id=sub_agent.id,
            name=config.name,
            role=config.role,
        )
        
        return sub_agent
    
    async def spawn_from_description(
        self,
        name: str,
        role: str,
        description: str,
        capabilities: Optional[List[str]] = None,
    ) -> SingleAgent:
        """根据描述动态创建子Agent配置"""
        # 使用LLM生成系统提示词
        prompt = f"""为一个AI助手创建系统提示词。

名称: {name}
角色: {role}
描述: {description}
{f'能力: {", ".join(capabilities)}' if capabilities else ''}

请生成一个详细的系统提示词，定义这个助手的角色、能力和行为准则。
"""
        
        system_prompt = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
        )
        
        config = SubAgentConfig(
            name=name,
            role=role,
            capabilities=capabilities or [],
            parent_agent_id=self.parent_agent_id,
            config=AgentConfig(
                name=name,
                description=description,
                system_prompt=system_prompt,
            ),
        )
        
        return await self.spawn(config)
    
    async def dispatch(
        self,
        sub_agent_id: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """向子Agent分派任务"""
        sub_agent = self._sub_agents.get(sub_agent_id)
        if not sub_agent:
            raise ValueError(f"Sub-agent not found: {sub_agent_id}")
        
        # 添加父Agent上下文
        full_context = context or {}
        full_context["parent_agent_id"] = self.parent_agent_id
        
        result = await sub_agent.execute_task(task, context=full_context)
        
        logger.info(
            "Task dispatched to sub-agent",
            sub_agent_id=sub_agent_id,
            task_preview=task[:50],
        )
        
        return result
    
    async def dispatch_to_role(
        self,
        role: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """向指定角色的子Agent分派任务"""
        # 查找匹配角色的子Agent
        for sub_id, config in self._sub_configs.items():
            if config.role == role:
                return await self.dispatch(sub_id, task, context)
        
        raise ValueError(f"No sub-agent with role: {role}")
    
    async def broadcast(
        self,
        message: str,
        exclude_ids: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """向所有子Agent广播消息"""
        exclude = set(exclude_ids or [])
        results = {}
        
        for sub_id, sub_agent in self._sub_agents.items():
            if sub_id not in exclude:
                try:
                    result = await sub_agent.chat(message)
                    results[sub_id] = result.content
                except Exception as e:
                    results[sub_id] = f"Error: {str(e)}"
        
        return results
    
    async def terminate(self, sub_agent_id: str) -> bool:
        """终止子Agent"""
        sub_agent = self._sub_agents.get(sub_agent_id)
        if not sub_agent:
            return False
        
        await sub_agent.stop()
        await sub_agent.cleanup()
        
        del self._sub_agents[sub_agent_id]
        del self._sub_configs[sub_agent_id]
        
        logger.info("Sub-agent terminated", sub_agent_id=sub_agent_id)
        return True
    
    async def terminate_all(self):
        """终止所有子Agent"""
        for sub_id in list(self._sub_agents.keys()):
            await self.terminate(sub_id)
    
    def get_sub_agent(self, sub_agent_id: str) -> Optional[SingleAgent]:
        """获取子Agent"""
        return self._sub_agents.get(sub_agent_id)
    
    def list_sub_agents(self) -> List[Dict[str, Any]]:
        """列出所有子Agent"""
        result = []
        for sub_id, agent in self._sub_agents.items():
            config = self._sub_configs[sub_id]
            result.append({
                "id": sub_id,
                "name": config.name,
                "role": config.role,
                "capabilities": config.capabilities,
                "status": agent.status,
            })
        return result
    
    def get_status_summary(self) -> Dict[str, Any]:
        """获取状态摘要"""
        total = len(self._sub_agents)
        active = sum(1 for a in self._sub_agents.values() if a.status == AgentStatus.RUNNING)
        idle = sum(1 for a in self._sub_agents.values() if a.status == AgentStatus.IDLE)
        
        return {
            "total": total,
            "active": active,
            "idle": idle,
            "agents": self.list_sub_agents(),
        }
