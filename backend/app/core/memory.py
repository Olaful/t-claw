"""记忆管理器 - 短期/长期/工作记忆管理"""
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from collections import deque

import structlog
from app.models.schemas.task import MemoryEntry, MemoryType, MemoryQuery, MemoryStore
from app.config import settings

logger = structlog.get_logger()


class MemoryManager:
    """记忆管理器"""
    
    def __init__(self):
        # 短期记忆：每个Agent一个双端队列
        self._short_term: Dict[str, deque] = {}
        # 工作记忆：当前任务上下文
        self._working: Dict[str, Dict[str, Any]] = {}
        # 长期记忆存储（简化版，实际应使用向量数据库）
        self._long_term: Dict[str, List[MemoryEntry]] = {}
    
    # ===== 短期记忆 =====
    
    async def add_short_term(
        self,
        agent_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEntry:
        """添加短期记忆"""
        if agent_id not in self._short_term:
            self._short_term[agent_id] = deque(maxlen=settings.SHORT_TERM_MEMORY_SIZE)
        
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            memory_type=MemoryType.SHORT_TERM,
            content=content,
            metadata=metadata or {},
        )
        
        self._short_term[agent_id].append(entry)
        logger.debug("Short-term memory added", agent_id=agent_id, memory_id=entry.id)
        return entry
    
    async def get_short_term(
        self,
        agent_id: str,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """获取短期记忆"""
        if agent_id not in self._short_term:
            return []
        
        memories = list(self._short_term[agent_id])
        return memories[-limit:]
    
    async def clear_short_term(self, agent_id: str):
        """清空短期记忆"""
        if agent_id in self._short_term:
            self._short_term[agent_id].clear()
            logger.info("Short-term memory cleared", agent_id=agent_id)
    
    # ===== 工作记忆 =====
    
    async def set_working_memory(
        self,
        agent_id: str,
        key: str,
        value: Any,
    ):
        """设置工作记忆"""
        if agent_id not in self._working:
            self._working[agent_id] = {}
        
        self._working[agent_id][key] = value
        logger.debug("Working memory set", agent_id=agent_id, key=key)
    
    async def get_working_memory(
        self,
        agent_id: str,
        key: Optional[str] = None,
    ) -> Any:
        """获取工作记忆"""
        if agent_id not in self._working:
            return None if key else {}
        
        if key:
            return self._working[agent_id].get(key)
        return self._working[agent_id]
    
    async def clear_working_memory(self, agent_id: str):
        """清空工作记忆"""
        if agent_id in self._working:
            self._working[agent_id] = {}
            logger.info("Working memory cleared", agent_id=agent_id)
    
    # ===== 长期记忆 =====
    
    async def add_long_term(
        self,
        agent_id: str,
        content: str,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEntry:
        """添加长期记忆"""
        if not settings.LONG_TERM_MEMORY_ENABLED:
            logger.warning("Long-term memory is disabled")
            return None
        
        if agent_id not in self._long_term:
            self._long_term[agent_id] = []
        
        # 生成向量嵌入（简化版，实际应调用embedding API）
        embedding = await self._generate_embedding(content)
        
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            memory_type=MemoryType.LONG_TERM,
            content=content,
            embedding=embedding,
            importance=importance,
            metadata=metadata or {},
        )
        
        self._long_term[agent_id].append(entry)
        logger.debug("Long-term memory added", agent_id=agent_id, memory_id=entry.id)
        return entry
    
    async def search_long_term(
        self,
        agent_id: str,
        query: str,
        top_k: int = 10,
        min_similarity: float = 0.7,
    ) -> List[MemoryEntry]:
        """搜索长期记忆（基于相似度）"""
        if agent_id not in self._long_term:
            return []
        
        # 生成查询向量
        query_embedding = await self._generate_embedding(query)
        
        # 计算相似度并排序
        memories = self._long_term[agent_id]
        scored_memories = []
        
        for memory in memories:
            if memory.embedding:
                similarity = self._cosine_similarity(query_embedding, memory.embedding)
                if similarity >= min_similarity:
                    scored_memories.append((similarity, memory))
        
        # 按相似度排序
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        
        # 更新访问计数
        results = []
        for similarity, memory in scored_memories[:top_k]:
            memory.access_count += 1
            memory.accessed_at = datetime.utcnow()
            results.append(memory)
        
        return results
    
    async def get_all_long_term(self, agent_id: str) -> List[MemoryEntry]:
        """获取所有长期记忆"""
        return self._long_term.get(agent_id, [])
    
    async def delete_long_term(self, agent_id: str, memory_id: str) -> bool:
        """删除长期记忆"""
        if agent_id not in self._long_term:
            return False
        
        memories = self._long_term[agent_id]
        for i, memory in enumerate(memories):
            if memory.id == memory_id:
                memories.pop(i)
                logger.info("Long-term memory deleted", agent_id=agent_id, memory_id=memory_id)
                return True
        
        return False
    
    # ===== 综合查询 =====
    
    async def query_memories(
        self,
        query: MemoryQuery,
    ) -> List[MemoryEntry]:
        """综合查询记忆"""
        results = []
        
        # 查询短期记忆
        short_term = await self.get_short_term(query.agent_id, limit=query.top_k)
        results.extend(short_term)
        
        # 查询长期记忆
        if query.query and settings.LONG_TERM_MEMORY_ENABLED:
            long_term = await self.search_long_term(
                query.agent_id,
                query.query,
                top_k=query.top_k,
                min_similarity=query.min_similarity,
            )
            results.extend(long_term)
        
        return results
    
    async def get_memory_summary(self, agent_id: str) -> MemoryStore:
        """获取记忆摘要"""
        short_term = await self.get_short_term(agent_id)
        long_term = await self.get_all_long_term(agent_id)
        working = await self.get_working_memory(agent_id)
        
        all_entries = short_term + long_term
        
        return MemoryStore(
            agent_id=agent_id,
            entries=all_entries,
            total_count=len(all_entries),
        )
    
    # ===== 辅助方法 =====
    
    async def _generate_embedding(self, text: str) -> List[float]:
        """生成文本向量嵌入（简化版）"""
        # 实际实现应调用OpenAI或其他embedding API
        # 这里返回一个模拟的向量
        import hashlib
        hash_bytes = hashlib.sha256(text.encode()).digest()
        # 生成1536维向量（OpenAI embedding维度）
        embedding = [float(b) / 255.0 for b in hash_bytes] * 6  # 32 * 6 = 192，重复到1536
        return embedding[:1536]
    
    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        if len(vec1) != len(vec2):
            return 0.0
        
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = sum(a * a for a in vec1) ** 0.5
        norm2 = sum(b * b for b in vec2) ** 0.5
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return dot_product / (norm1 * norm2)


# 便捷函数
def create_memory_manager() -> MemoryManager:
    """创建记忆管理器实例"""
    return MemoryManager()
