"""持久化层 - 数据库CRUD操作"""
import os
import shutil
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

import structlog
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.database import AsyncSessionLocal
from app.models.orm.models import (
    AgentORM, ConversationORM, MessageORM, TeamORM, TeamMemberORM, TeamTaskORM,
    AgentStatusEnum, ConversationStatusEnum, TaskStatusEnum,
)
from app.models.schemas.agent import AgentConfig, AgentStatus, AgentType, LLMProvider
from app.models.schemas.chat import (
    Conversation, ConversationStatus, Message, MessageRole, MessageType, ToolCallStatus,
)
from app.models.schemas.team import TeamConfig, TeamStatus, TaskPriority, TaskStatus

logger = structlog.get_logger()


class PersistenceLayer:
    """持久化层 - 管理数据库操作"""
    
    # ===== Agent操作 =====
    
    async def save_agent(self, config: AgentConfig, status: AgentStatus) -> None:
        """保存Agent到数据库"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                agent_orm = AgentORM(
                    id=config.id,
                    name=config.name,
                    description=config.description,
                    system_prompt=config.system_prompt,
                    agent_type=config.agent_type.value,
                    llm_provider=config.llm_provider.value,
                    llm_model=config.llm_model,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                    status=AgentStatusEnum(status.value),
                    tool_ids=config.tool_ids,
                    skill_ids=config.skill_ids,
                    memory_config=config.memory_config.dict() if config.memory_config else None,
                    metadata_=config.metadata,
                    # Subagent 字段
                    is_subagent=config.is_subagent,
                    level=config.level,
                    parent_agent_id=config.parent_agent_id,
                    parent_conversation_id=config.parent_conversation_id,
                    spawn_uuid=config.spawn_uuid,
                    # 启用/禁用状态（默认启用）
                    is_enabled=True,
                )
                # 检查是否已存在
                result = await session.execute(
                    select(AgentORM).where(AgentORM.id == config.id)
                )
                existing = result.scalar_one_or_none()
                if existing:
                    # 更新
                    existing.name = agent_orm.name
                    existing.description = agent_orm.description
                    existing.system_prompt = agent_orm.system_prompt
                    existing.llm_model = agent_orm.llm_model
                    existing.llm_provider = agent_orm.llm_provider
                    existing.temperature = agent_orm.temperature
                    existing.max_tokens = agent_orm.max_tokens
                    existing.status = agent_orm.status
                    existing.updated_at = datetime.utcnow()
                else:
                    # 插入
                    session.add(agent_orm)
                logger.debug("Agent saved", agent_id=config.id)
    
    async def load_agent(self, agent_id: str) -> Optional[AgentConfig]:
        """从数据库加载Agent"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(AgentORM).where(AgentORM.id == agent_id)
            )
            agent_orm = result.scalar_one_or_none()
            if not agent_orm:
                return None
            
            return self._agent_orm_to_config(agent_orm)
    
    async def load_all_agents(self, include_disabled: bool = False) -> List[AgentConfig]:
        """加载所有Agent
        
        Args:
            include_disabled: 是否包含禁用的agent，默认False只返回启用的agent
        """
        async with AsyncSessionLocal() as session:
            if include_disabled:
                result = await session.execute(select(AgentORM))
            else:
                # 只加载启用的agent
                result = await session.execute(
                    select(AgentORM).where(AgentORM.is_enabled == True)
                )
            agents_orm = result.scalars().all()
            return [self._agent_orm_to_config(a) for a in agents_orm]
    
    async def delete_agent(self, agent_id: str) -> None:
        """从数据库删除Agent（先删除关联的会话和消息）"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # 1. 先删除该agent所有会话的消息
                conv_result = await session.execute(
                    select(ConversationORM.id).where(ConversationORM.agent_id == agent_id)
                )
                conv_ids = [row[0] for row in conv_result.fetchall()]
                if conv_ids:
                    await session.execute(
                        delete(MessageORM).where(MessageORM.conversation_id.in_(conv_ids))
                    )
                # 2. 删除会话
                await session.execute(
                    delete(ConversationORM).where(ConversationORM.agent_id == agent_id)
                )
                # 3. 删除Agent
                await session.execute(
                    delete(AgentORM).where(AgentORM.id == agent_id)
                )
                logger.debug("Agent deleted", agent_id=agent_id)
    
    async def enable_agent(self, agent_id: str) -> bool:
        """启用Agent
        
        Returns:
            True if agent was found and enabled, False otherwise
        """
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    select(AgentORM).where(AgentORM.id == agent_id)
                )
                agent_orm = result.scalar_one_or_none()
                if not agent_orm:
                    return False
                agent_orm.is_enabled = True
                agent_orm.updated_at = datetime.utcnow()
                logger.info("Agent enabled", agent_id=agent_id)
                return True
    
    async def disable_agent(self, agent_id: str) -> bool:
        """禁用Agent
        
        Returns:
            True if agent was found and disabled, False otherwise
        """
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    select(AgentORM).where(AgentORM.id == agent_id)
                )
                agent_orm = result.scalar_one_or_none()
                if not agent_orm:
                    return False
                agent_orm.is_enabled = False
                agent_orm.updated_at = datetime.utcnow()
                logger.info("Agent disabled", agent_id=agent_id)
                return True
    
    def _agent_orm_to_config(self, orm: AgentORM) -> AgentConfig:
        """ORM模型转AgentConfig"""
        from app.models.schemas.agent import MemoryConfig
        
        memory_config = None
        if orm.memory_config:
            try:
                memory_config = MemoryConfig(**orm.memory_config)
            except Exception as e:
                print(f"[DEBUG] Failed to parse memory_config for agent {orm.id}: {str(e)}")
                memory_config = None
        
        # 将 is_enabled 存储到 metadata 中以便前端获取
        metadata = dict(orm.metadata_ or {})
        metadata['is_enabled'] = orm.is_enabled
        # 存储DB时间戳，供API返回准确的创建/更新时间
        if orm.created_at:
            metadata['db_created_at'] = orm.created_at.isoformat()
        if orm.updated_at:
            metadata['db_updated_at'] = orm.updated_at.isoformat()
        
        return AgentConfig(
            id=orm.id,
            name=orm.name,
            description=orm.description,
            system_prompt=orm.system_prompt,
            agent_type=AgentType(orm.agent_type),
            llm_provider=LLMProvider(orm.llm_provider),
            llm_model=orm.llm_model,
            temperature=orm.temperature,
            context_window=orm.context_window,
            max_tokens=orm.max_tokens,
            tool_ids=orm.tool_ids or [],
            skill_ids=orm.skill_ids or [],
            memory_config=memory_config,
            metadata=metadata,
            # Subagent 字段
            is_subagent=orm.is_subagent,
            level=orm.level,
            parent_agent_id=orm.parent_agent_id,
            parent_conversation_id=orm.parent_conversation_id,
            spawn_uuid=orm.spawn_uuid,
        )
    
    # ===== 会话操作 =====
    
    async def save_conversation(self, conversation: Conversation) -> None:
        """保存会话到数据库"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # 检查是否已存在
                result = await session.execute(
                    select(ConversationORM).where(ConversationORM.id == conversation.id)
                )
                existing = result.scalar_one_or_none()
                
                if existing:
                    # 更新
                    existing.title = conversation.title
                    existing.status = ConversationStatusEnum(conversation.status.value)
                    existing.context_tokens = conversation.context_tokens
                    existing.metadata_ = conversation.metadata
                    existing.updated_at = datetime.utcnow()
                else:
                    # 插入
                    conv_orm = ConversationORM(
                        id=conversation.id,
                        agent_id=conversation.agent_id,
                        user_id=conversation.user_id,
                        title=conversation.title,
                        status=ConversationStatusEnum(conversation.status.value),
                        context_tokens=conversation.context_tokens,
                        metadata_=conversation.metadata,
                    )
                    session.add(conv_orm)
                
                logger.debug("Conversation saved", conversation_id=conversation.id)
    
    async def load_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """从数据库加载会话"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationORM).where(ConversationORM.id == conversation_id)
            )
            conv_orm = result.scalar_one_or_none()
            if not conv_orm:
                return None
            
            # 加载消息
            msg_result = await session.execute(
                select(MessageORM)
                .where(MessageORM.conversation_id == conversation_id)
                .order_by(MessageORM.created_at)
            )
            messages_orm = msg_result.scalars().all()
            
            return self._conversation_orm_to_model(conv_orm, messages_orm)
    
    async def load_conversations_for_agent(self, agent_id: str) -> List[Conversation]:
        """加载Agent的所有会话"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationORM)
                .where(ConversationORM.agent_id == agent_id)
                .order_by(ConversationORM.updated_at.desc())
            )
            convs_orm = result.scalars().all()
            
            conversations = []
            for conv_orm in convs_orm:
                # 加载每个会话的消息
                msg_result = await session.execute(
                    select(MessageORM)
                    .where(MessageORM.conversation_id == conv_orm.id)
                    .order_by(MessageORM.created_at)
                )
                messages_orm = msg_result.scalars().all()
                conversations.append(self._conversation_orm_to_model(conv_orm, messages_orm))
            
            return conversations
    
    async def rename_conversation(self, conversation_id: str, title: str) -> None:
        """重命名会话"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    select(ConversationORM).where(ConversationORM.id == conversation_id)
                )
                conv_orm = result.scalar_one_or_none()
                if conv_orm:
                    conv_orm.title = title
                    conv_orm.updated_at = datetime.utcnow()
                    logger.debug("Conversation renamed", conversation_id=conversation_id, new_title=title)
    
    async def update_conversation_session_model(self, conversation_id: str, session_model: Dict[str, Any]) -> None:
        """更新会话级模型配置"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    select(ConversationORM).where(ConversationORM.id == conversation_id)
                )
                conv_orm = result.scalar_one_or_none()
                if conv_orm:
                    conv_orm.session_model = session_model
                    conv_orm.updated_at = datetime.utcnow()
                    logger.debug(
                        "Conversation session model updated",
                        conversation_id=conversation_id,
                        provider=session_model.get('provider'),
                        model_id=session_model.get('model_id'),
                    )
    
    async def find_new_conversation(self, agent_id: str) -> Optional[Conversation]:
        """查找指定 agent 最近一条 status==NEW 的会话（供新建时复用）"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationORM)
                .where(
                    ConversationORM.agent_id == agent_id,
                    ConversationORM.status == ConversationStatusEnum.NEW,
                )
                .order_by(ConversationORM.updated_at.desc())
            )
            conv_orm = result.scalars().first()
            if not conv_orm:
                return None
            return self._conversation_orm_to_model(conv_orm, [])

    async def find_new_conversations(self, agent_id: str) -> List[Conversation]:
        """返回指定 agent 所有 status==NEW 的会话（供清理）"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationORM)
                .where(
                    ConversationORM.agent_id == agent_id,
                    ConversationORM.status == ConversationStatusEnum.NEW,
                )
                .order_by(ConversationORM.updated_at.desc())
            )
            convs_orm = result.scalars().all()
            return [self._conversation_orm_to_model(c, []) for c in convs_orm]

    async def activate_conversation(self, conversation_id: str, title: Optional[str] = None) -> None:
        """将会话激活为 ACTIVE（title 非空时一并更新标题）"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    select(ConversationORM).where(ConversationORM.id == conversation_id)
                )
                conv_orm = result.scalar_one_or_none()
                if conv_orm:
                    conv_orm.status = ConversationStatusEnum.ACTIVE
                    if title:
                        conv_orm.title = title
                    conv_orm.updated_at = datetime.utcnow()
                    logger.debug("Conversation activated", conversation_id=conversation_id, title=title)

    async def delete_conversation(self, conversation_id: str) -> None:
        """从数据库删除会话"""
        # 先清理压缩标记关联的磁盘文件
        await self._cleanup_compressed_outputs(conversation_id)
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # 先删除消息
                await session.execute(
                    delete(MessageORM).where(MessageORM.conversation_id == conversation_id)
                )
                # 再删除会话
                await session.execute(
                    delete(ConversationORM).where(ConversationORM.id == conversation_id)
                )
                logger.debug("Conversation deleted", conversation_id=conversation_id)
    
    async def _cleanup_compressed_outputs(self, conversation_id: str) -> None:
        """清理会话关联的压缩输出目录"""
        from app.config import settings
        workspace_dir = Path(getattr(settings, 'WORKSPACE_BASE_PATH', './workspaces'))
        conv_dir = workspace_dir / conversation_id
        try:
            if conv_dir.exists():
                shutil.rmtree(conv_dir)
                logger.debug("Deleted conversation directory", path=str(conv_dir))
        except Exception as e:
            logger.warning("Failed to delete conversation directory", path=str(conv_dir), error=str(e))
    
    def _reposition_marker_messages(self, messages: List[Message]) -> List[Message]:
        """重新定位压缩标记消息到正确位置。
        
        从数据库加载的消息按 created_at 排序，标记消息会在末尾。
        根据 metadata 中的 compacted_last_idx 将标记消息插入到正确位置。
        """
        marker_types = {
            MessageType.COMPRESS_BUDGET_BOUNDARY,
            MessageType.COMPRESS_SNIP_COMPACT_BOUNDARY,
            MessageType.COMPRESS_MICRO_COMPACT_BOUNDARY,
            MessageType.COMPRESS_AUTO_COMPACT_BOUNDARY,
            MessageType.ACTIVE_COMPACT_BOUNDARY,
        }
        
        # 分离普通消息和标记消息
        regular_messages = [m for m in messages if m.message_type not in marker_types]
        marker_messages = [m for m in messages if m.message_type in marker_types]
        
        if not marker_messages:
            return messages
        
        # 按 compacted_last_idx 升序排序标记消息
        marker_messages.sort(key=lambda m: (m.metadata or {}).get("compacted_last_idx", 0))
        
        # 按顺序插入标记消息到正确位置
        for marker in marker_messages:
            last_idx = (marker.metadata or {}).get("compacted_last_idx", 0)
            if not last_idx:
                last_idx = len(regular_messages)
            # 插入位置为 last_idx + 1（在对应消息之后）
            insert_pos = min(last_idx + 1, len(regular_messages))
            regular_messages.insert(insert_pos, marker)
        
        return regular_messages
    
    def _conversation_orm_to_model(
        self,
        conv_orm: ConversationORM,
        messages_orm: List[MessageORM],
    ) -> Conversation:
        """ORM模型转Conversation"""
        messages = [
            Message(
                id=m.id,
                role=MessageRole(m.role),
                content=m.content,
                message_type=MessageType(m.message_type),
                reasoning_content=m.reasoning_content,
                tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id,
                name=m.name,
                metadata=m.metadata_ or {},
                created_at=m.created_at,
                token_count=m.token_count,
                tool_call_status=ToolCallStatus(m.tool_call_status) if m.tool_call_status else None,
            )
            for m in messages_orm
        ]
        
        return Conversation(
            id=conv_orm.id,
            agent_id=conv_orm.agent_id,
            user_id=conv_orm.user_id,
            title=conv_orm.title,
            status=ConversationStatus(conv_orm.status.value),
            messages=messages,
            context_tokens=conv_orm.context_tokens,
            created_at=conv_orm.created_at,
            updated_at=conv_orm.updated_at,
            metadata=conv_orm.metadata_ or {},
            session_model=conv_orm.session_model,
        )
    
    # ===== 消息操作 =====
    
    async def save_message(self, conversation_id: str, message: Message) -> None:
        """保存消息到数据库"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                msg_orm = MessageORM(
                    id=message.id,
                    conversation_id=conversation_id,
                    role=message.role.value,
                    content=message.content,
                    message_type=message.message_type.value,
                    reasoning_content=message.reasoning_content,
                    tool_calls=message.tool_calls,
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                    metadata_=message.metadata,
                    token_count=message.token_count,
                    tool_call_status=message.tool_call_status.value if message.tool_call_status else None,
                )
                session.add(msg_orm)
                logger.debug("Message saved", message_id=message.id)
    
    async def update_message_metadata(self, message_id: str, metadata: dict) -> None:
        """更新消息的 metadata 字段（用于持久化 remote_file_id 等临时缓存）"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    select(MessageORM).where(MessageORM.id == message_id)
                )
                msg_orm = result.scalar_one_or_none()
                if msg_orm:
                    msg_orm.metadata_ = metadata
                    logger.debug("Message metadata updated", message_id=message_id)
    
    # ===== 团队操作 =====
    
    async def save_team(self, team_id: str, config: TeamConfig, status: TeamStatus) -> None:
        """保存团队到数据库"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                result = await session.execute(
                    select(TeamORM).where(TeamORM.id == team_id)
                )
                existing = result.scalar_one_or_none()
                
                if existing:
                    existing.name = config.name
                    existing.description = config.description
                    existing.max_members = config.max_members
                    existing.communication_protocol = config.communication_protocol
                    existing.workspace_isolation = config.workspace_isolation
                    existing.metadata_ = config.metadata
                    existing.status = status.value
                    existing.updated_at = datetime.utcnow()
                else:
                    team_orm = TeamORM(
                        id=team_id,
                        name=config.name,
                        description=config.description,
                        max_members=config.max_members,
                        status=status.value,
                        communication_protocol=config.communication_protocol,
                        workspace_isolation=config.workspace_isolation,
                        metadata_=config.metadata,
                    )
                    session.add(team_orm)
                
                logger.debug("Team saved", team_id=team_id)
    
    async def load_team(self, team_id: str) -> Optional[Dict[str, Any]]:
        """从数据库加载团队"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(TeamORM).where(TeamORM.id == team_id)
            )
            team_orm = result.scalar_one_or_none()
            if not team_orm:
                return None
            
            # 加载成员
            members_result = await session.execute(
                select(TeamMemberORM).where(TeamMemberORM.team_id == team_id)
            )
            members_orm = members_result.scalars().all()
            
            # 加载任务
            tasks_result = await session.execute(
                select(TeamTaskORM).where(TeamTaskORM.team_id == team_id)
            )
            tasks_orm = tasks_result.scalars().all()
            
            return {
                "team": team_orm,
                "members": members_orm,
                "tasks": tasks_orm,
            }
    
    async def load_all_teams(self) -> List[Dict[str, Any]]:
        """加载所有团队"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(TeamORM))
            teams_orm = result.scalars().all()
            
            teams_data = []
            for team_orm in teams_orm:
                members_result = await session.execute(
                    select(TeamMemberORM).where(TeamMemberORM.team_id == team_orm.id)
                )
                members_orm = members_result.scalars().all()
                
                tasks_result = await session.execute(
                    select(TeamTaskORM).where(TeamTaskORM.team_id == team_orm.id)
                )
                tasks_orm = tasks_result.scalars().all()
                
                teams_data.append({
                    "team": team_orm,
                    "members": members_orm,
                    "tasks": tasks_orm,
                })
            
            return teams_data
    
    async def delete_team(self, team_id: str) -> None:
        """从数据库删除团队"""
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # 删除成员
                await session.execute(
                    delete(TeamMemberORM).where(TeamMemberORM.team_id == team_id)
                )
                # 删除任务
                await session.execute(
                    delete(TeamTaskORM).where(TeamTaskORM.team_id == team_id)
                )
                # 删除团队
                await session.execute(
                    delete(TeamORM).where(TeamORM.id == team_id)
                )
                logger.debug("Team deleted", team_id=team_id)


# 全局持久化层实例
persistence = PersistenceLayer()
