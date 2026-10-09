"""Redis客户端管理"""
import json
from typing import Any, Optional
import redis.asyncio as redis
from app.config import settings


class RedisClient:
    """异步Redis客户端封装"""
    
    def __init__(self):
        self._client: Optional[redis.Redis] = None
    
    async def initialize(self):
        """初始化Redis连接"""
        self._client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=settings.REDIS_POOL_SIZE,
        )
    
    async def close(self):
        """关闭Redis连接"""
        if self._client:
            await self._client.close()
    
    @property
    def client(self) -> redis.Redis:
        """获取Redis客户端"""
        if not self._client:
            raise RuntimeError("Redis client not initialized")
        return self._client
    
    # 基础操作
    async def get(self, key: str) -> Optional[str]:
        """获取字符串值"""
        return await self.client.get(key)
    
    async def set(self, key: str, value: str, ex: Optional[int] = None):
        """设置字符串值"""
        await self.client.set(key, value, ex=ex)
    
    async def delete(self, key: str):
        """删除键"""
        await self.client.delete(key)
    
    async def exists(self, key: str) -> bool:
        """检查键是否存在"""
        return bool(await self.client.exists(key))
    
    # JSON操作
    async def get_json(self, key: str) -> Optional[Any]:
        """获取JSON数据"""
        value = await self.get(key)
        return json.loads(value) if value else None
    
    async def set_json(self, key: str, data: Any, ex: Optional[int] = None):
        """设置JSON数据"""
        await self.set(key, json.dumps(data), ex=ex)
    
    # Hash操作
    async def hset(self, name: str, key: str, value: str):
        """设置hash字段"""
        await self.client.hset(name, key, value)
    
    async def hget(self, name: str, key: str) -> Optional[str]:
        """获取hash字段"""
        return await self.client.hget(name, key)
    
    async def hgetall(self, name: str) -> dict:
        """获取所有hash字段"""
        return await self.client.hgetall(name)
    
    # List操作
    async def lpush(self, name: str, *values):
        """列表左推"""
        await self.client.lpush(name, *values)
    
    async def rpop(self, name: str) -> Optional[str]:
        """列表右弹"""
        return await self.client.rpop(name)
    
    async def lrange(self, name: str, start: int, end: int) -> list:
        """获取列表范围"""
        return await self.client.lrange(name, start, end)
    
    # Pub/Sub
    async def publish(self, channel: str, message: str):
        """发布消息"""
        await self.client.publish(channel, message)
    
    def pubsub(self):
        """获取PubSub对象"""
        return self.client.pubsub()


# 全局Redis客户端实例
redis_client = RedisClient()
