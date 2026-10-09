"""多Agent团队 - 协作与通信"""
import uuid
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any, AsyncIterator

import structlog
from app.models.schemas.team import (
    TeamConfig, Team, TeamMember, TeamMessage, MessageType as TeamMessageType,
    TeamStatus, MemberStatus, TaskItem, TaskStatus, TaskPriority,
)
from app.models.schemas.agent import AgentConfig, AgentStatus
from app.models.schemas.chat import Message, MessageRole
from app.agents.base import BaseAgent
from app.agents.single import SingleAgent
from app.services.llm_service import LLMService
from app.tools.registry import ToolRegistry
from app.core.memory import MemoryManager
from app.core.prompt_engine import PromptEngine
from app.utils.redis import redis_client

logger = structlog.get_logger()


class AgentTeam:
    """Agent团队"""
    
    def __init__(
        self,
        config: TeamConfig,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        memory_manager: MemoryManager,
        prompt_engine: PromptEngine,
    ):
        self.id = str(uuid.uuid4())
        self.config = config
        self.llm_service = llm_service
        self.tool_registry = tool_registry
        self.memory_manager = memory_manager
        self.prompt_engine = prompt_engine
        
        self.status = TeamStatus.ACTIVE
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        
        # 成员管理
        self._members: Dict[str, TeamMember] = {}
        self._agents: Dict[str, SingleAgent] = {}
        
        # 任务白板
        self._tasks: Dict[str, TaskItem] = {}
        
        # 通信频道
        self._channel = f"team:{self.id}"
    
    async def add_member(
        self,
        agent_config: AgentConfig,
        role: str,
        capabilities: Optional[List[str]] = None,
    ) -> TeamMember:
        """添加团队成员"""
        if len(self._members) >= self.config.max_members:
            raise ValueError(f"Team is full (max {self.config.max_members} members)")
        
        # 创建Agent
        agent = SingleAgent(
            config=agent_config,
            llm_service=self.llm_service,
            tool_registry=self.tool_registry,
            memory_manager=MemoryManager(),
            prompt_engine=self.prompt_engine,
        )
        
        member = TeamMember(
            agent_id=agent.id,
            role=role,
            status=MemberStatus.IDLE,
            capabilities=capabilities or [],
        )
        
        self._members[agent.id] = member
        self._agents[agent.id] = agent
        
        # 广播成员加入
        await self._broadcast(
            TeamMessageType.EVENT,
            {"type": "member_joined", "agent_id": agent.id, "role": role},
        )
        
        logger.info("Member added", team_id=self.id, agent_id=agent.id, role=role)
        return member
    
    async def remove_member(self, agent_id: str) -> bool:
        """移除团队成员"""
        if agent_id not in self._members:
            return False
        
        agent = self._agents.get(agent_id)
        if agent:
            await agent.stop()
            del self._agents[agent_id]
        
        del self._members[agent_id]
        
        await self._broadcast(
            TeamMessageType.EVENT,
            {"type": "member_left", "agent_id": agent_id},
        )
        
        logger.info("Member removed", team_id=self.id, agent_id=agent_id)
        return True
    
    async def send_message(
        self,
        from_agent_id: str,
        to_agent_id: Optional[str],
        content: Dict[str, Any],
        message_type: TeamMessageType = TeamMessageType.REQUEST,
    ) -> TeamMessage:
        """发送团队消息"""
        message = TeamMessage(
            id=str(uuid.uuid4()),
            team_id=self.id,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            message_type=message_type,
            content=content,
        )
        
        # 通过Redis Pub/Sub发布消息
        if to_agent_id:
            # 定向消息
            channel = f"team:{self.id}:agent:{to_agent_id}"
            await redis_client.publish(channel, message.model_dump_json())
        else:
            # 广播
            await self._broadcast(message.message_type, message.content, from_agent_id)
        
        logger.debug(
            "Team message sent",
            from_agent=from_agent_id,
            to_agent=to_agent_id,
            msg_type=message_type,
        )
        
        return message
    
    async def _broadcast(
        self,
        message_type: TeamMessageType,
        content: Dict[str, Any],
        from_agent_id: Optional[str] = None,
    ):
        """广播消息"""
        message = TeamMessage(
            id=str(uuid.uuid4()),
            team_id=self.id,
            from_agent_id=from_agent_id or "system",
            message_type=message_type,
            content=content,
        )
        await redis_client.publish(self._channel, message.model_dump_json())
    
    # ===== 白板任务管理 =====
    
    async def create_task(
        self,
        title: str,
        description: Optional[str] = None,
        priority: TaskPriority = TaskPriority.MEDIUM,
        created_by: Optional[str] = None,
    ) -> TaskItem:
        """创建白板任务"""
        task = TaskItem(
            id=str(uuid.uuid4()),
            team_id=self.id,
            title=title,
            description=description,
            priority=priority,
            status=TaskStatus.PENDING,
            created_by=created_by,
        )
        
        self._tasks[task.id] = task
        
        # 存储到Redis列表
        await redis_client.lpush(f"team:{self.id}:tasks", task.model_dump_json())
        
        logger.info("Task created", team_id=self.id, task_id=task.id, title=title)
        return task
    
    async def claim_task(self, task_id: str, agent_id: str) -> bool:
        """认领任务"""
        task = self._tasks.get(task_id)
        if not task:
            return False
        
        if task.status != TaskStatus.PENDING:
            return False
        
        task.status = TaskStatus.CLAIMED
        task.assigned_to = agent_id
        task.updated_at = datetime.utcnow()
        
        # 更新成员状态
        member = self._members.get(agent_id)
        if member:
            member.status = MemberStatus.WORKING
            member.current_task = task_id
        
        await self._broadcast(
            TeamMessageType.EVENT,
            {"type": "task_claimed", "task_id": task_id, "agent_id": agent_id},
        )
        
        logger.info("Task claimed", task_id=task_id, agent_id=agent_id)
        return True
    
    async def complete_task(self, task_id: str, result: Optional[str] = None):
        """完成任务"""
        task = self._tasks.get(task_id)
        if not task:
            return
        
        task.status = TaskStatus.COMPLETED
        task.updated_at = datetime.utcnow()
        
        # 释放Agent
        if task.assigned_to:
            member = self._members.get(task.assigned_to)
            if member:
                member.status = MemberStatus.IDLE
                member.current_task = None
        
        await self._broadcast(
            TeamMessageType.EVENT,
            {"type": "task_completed", "task_id": task_id},
        )
    
    def get_pending_tasks(self) -> List[TaskItem]:
        """获取待认领任务"""
        return [t for t in self._tasks.values() if t.status == TaskStatus.PENDING]
    
    def get_team_status(self) -> Team:
        """获取团队状态"""
        return Team(
            id=self.id,
            config=self.config,
            status=self.status,
            members=list(self._members.values()),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
    
    # ===== 协作执行 =====
    
    async def assign_and_execute(
        self,
        goal: str,
    ) -> Dict[str, Any]:
        """分配并协作执行目标"""
        # 使用LLM分析任务并分配
        members_info = [
            {"agent_id": m.agent_id, "role": m.role, "capabilities": m.capabilities, "status": m.status}
            for m in self._members.values()
        ]
        
        prompt = f"""分析以下目标，并将其分配给合适的团队成员：

目标: {goal}

团队成员:
{chr(10).join([f"- {m['role']} (ID: {m['agent_id']}): {m['capabilities']}" for m in members_info])}

请返回JSON格式的任务分配方案:
{{"assignments": [{{"agent_id": "...", "task": "..."}}]}}
"""
        
        response = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        
        # 解析分配方案
        import json
        try:
            plan = json.loads(response)
            assignments = plan.get("assignments", [])
        except json.JSONDecodeError:
            assignments = []
        
        # 创建任务并分配
        results = {}
        for assignment in assignments:
            agent_id = assignment.get("agent_id")
            task_desc = assignment.get("task")
            
            if agent_id in self._agents:
                task = await self.create_task(
                    title=task_desc[:100],
                    description=task_desc,
                    created_by="coordinator",
                )
                await self.claim_task(task.id, agent_id)
                
                # 异步执行
                agent = self._agents[agent_id]
                result = await agent.execute_task(task_desc)
                await self.complete_task(task.id, result)
                
                results[agent_id] = result
        
        return {
            "goal": goal,
            "assignments": len(assignments),
            "results": results,
        }
    
    async def stop(self):
        """停止团队"""
        for agent in self._agents.values():
            await agent.stop()
        
        self.status = TeamStatus.ARCHIVED
        logger.info("Team stopped", team_id=self.id)


def create_team(
    config: TeamConfig,
    llm_service: LLMService,
    tool_registry: ToolRegistry,
    memory_manager: MemoryManager,
    prompt_engine: PromptEngine,
) -> AgentTeam:
    """创建团队实例"""
    return AgentTeam(
        config=config,
        llm_service=llm_service,
        tool_registry=tool_registry,
        memory_manager=memory_manager,
        prompt_engine=prompt_engine,
    )
