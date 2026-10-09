"""事件总线 - 全局事件发射器 + 事件标准化"""
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()


class ServerEvent(BaseModel):
    """标准化服务端事件（id/timestamp 自动生成）"""
    id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:8]}")
    type: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    source: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)
    priority: str = "normal"  # low/normal/high/critical

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()


class EventBus:
    """
    全局事件总线
    
    用法:
        from app.core.event_bus import event_bus
        await event_bus.emit_event(type="task_status", source=agent_id, data={...})
    """

    def __init__(self):
        self._redis = None  # 延迟获取 Redis 实例

    @property
    def redis(self):
        if self._redis is None:
            from app.utils.redis import redis_client
            self._redis = redis_client
        return self._redis

    async def emit_event(
        self,
        type: str,
        source: str = "",
        data: Optional[Dict[str, Any]] = None,
        priority: str = "normal",
        dimensions: Optional[List[str]] = None,
    ) -> ServerEvent:
        """
        发射标准化事件
        
        Args:
            type: 事件类型 (task_status/agent_progress/alert 等)
            source: 事件来源 (agent_id/conversation_id/system)
            data: 事件数据
            priority: 优先级 (low/normal/high/critical)
            dimensions: 推送维度列表 (如 ["agent:xxx", "conversation:yyy"])
                        如果不传，默认使用 ["source:{source}"]
        
        Returns:
            生成的 ServerEvent 实例
        """
        # 1. 构造事件（id/timestamp 自动生成）
        event = ServerEvent(
            type=type,
            source=source,
            data=data or {},
            priority=priority,
        )
        event_dict = event.to_dict()
        event_json = json.dumps(event_dict, ensure_ascii=False)

        # 2. 确定推送维度
        if not dimensions:
            dimensions = [f"source:{source}"] if source else ["all"]

        # 3. 发布到 Redis Pub/Sub
        try:
            # 发布到各维度 channel
            for dim in dimensions:
                channel = f"events:{dim}"
                await self.redis.publish(channel, event_json)

            # 同时发布到全局广播 channel
            await self.redis.publish("events:all", event_json)

            logger.debug(
                "Event emitted",
                event_id=event.id,
                type=type,
                source=source,
                dimensions=dimensions,
            )
        except Exception as e:
            logger.warning(
                "Failed to publish event to Redis",
                event_id=event.id,
                error=str(e),
                exc_info=True,
            )

        # 4. 高优先级事件持久化（用于断线补发）
        if priority in ("high", "critical"):
            try:
                await self._persist_event(event_dict)
            except Exception as e:
                logger.warning(
                    "Failed to persist event",
                    event_id=event.id,
                    error=str(e),
                    exc_info=True,
                )

        return event

    async def _persist_event(self, event_dict: Dict[str, Any]) -> None:
        """持久化高优先级事件到 Redis，用于断线补发"""
        event_id = event_dict["id"]
        event_json = json.dumps(event_dict, ensure_ascii=False)

        # 写入 Redis List（保留最近 10000 条）
        await self.redis.client.rpush("events:store", event_json)
        await self.redis.client.ltrim("events:store", -10000, -1)
        # 设置 TTL 24h
        await self.redis.client.expire("events:store", 86400)

        # 写入 Sorted Set（按时间戳排序，用于按时间范围查询）
        ts = event_dict.get("timestamp", "")
        # 将 ISO 时间戳转为 float score
        try:
            from datetime import datetime as dt
            score = dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            score = 0.0
        await self.redis.client.zadd("events:timeline", {event_id: score})
        # 将事件 JSON 存为 hash，方便按 id 取回
        # ✅ event_dict 含嵌套 dict（data），Redis hash 值必须是标量，故整体存为单字段 JSON 字符串
        await self.redis.client.hset(f"events:data:{event_id}", "json", event_json)
        await self.redis.client.expire(f"events:data:{event_id}", 86400)
        await self.redis.client.expire("events:timeline", 86400)

    async def get_missed_events(
        self,
        since: Optional[str] = None,
        last_event_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取断线期间遗漏的事件
        
        Args:
            since: ISO 时间戳，获取该时间之后的事件
            last_event_id: 最后收到的事件 ID（从该事件之后开始）
        
        Returns:
            事件列表
        """
        try:
            if since:
                # 按时间范围从 Sorted Set 查询
                from datetime import datetime as dt
                score = dt.fromisoformat(since.replace("Z", "+00:00")).timestamp()
                event_ids = await self.redis.client.zrangebyscore(
                    "events:timeline", score, "+inf"
                )
            else:
                # 获取最近 100 条
                event_ids = await self.redis.client.zrevrange(
                    "events:timeline", 0, 99
                )
                event_ids = list(reversed(event_ids))

            # 如果提供了 last_event_id，跳过该 ID 及之前的
            if last_event_id and event_ids:
                try:
                    idx = event_ids.index(last_event_id)
                    event_ids = event_ids[idx + 1:]
                except ValueError:
                    pass  # last_event_id 不在列表中，返回全部

            # 批量获取事件数据（存储格式：hash 单字段 json，兼容旧的扁平字段格式）
            events = []
            for eid in event_ids:
                eid_str = eid.decode() if isinstance(eid, bytes) else eid
                event_data = await self.redis.client.hgetall(f"events:data:{eid_str}")
                if not event_data:
                    continue
                raw_json = event_data.get("json") or event_data.get(b"json")
                if raw_json:
                    try:
                        if isinstance(raw_json, bytes):
                            raw_json = raw_json.decode()
                        events.append(json.loads(raw_json))
                        continue
                    except Exception:
                        pass
                events.append(event_data)

            return events
        except Exception as e:
            logger.warning("Failed to get missed events", error=str(e), exc_info=True)
            return []

    # ========== 便捷方法 ==========

    async def emit_task_status(
        self,
        source: str,
        status: str,
        progress: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
        dimensions: Optional[List[str]] = None,
    ) -> ServerEvent:
        """发射任务状态事件"""
        data = {"status": status}
        if progress is not None:
            data["progress"] = progress
        if extra:
            data.update(extra)
        return await self.emit_event(
            type="task_status",
            source=source,
            data=data,
            priority="normal",
            dimensions=dimensions,
        )

    async def emit_agent_progress(
        self,
        source: str,
        message: str,
        phase: str = "processing",
        extra: Optional[Dict[str, Any]] = None,
        dimensions: Optional[List[str]] = None,
    ) -> ServerEvent:
        """发射 Agent 进度事件"""
        data = {"message": message, "phase": phase}
        if extra:
            data.update(extra)
        return await self.emit_event(
            type="agent_progress",
            source=source,
            data=data,
            priority="low",
            dimensions=dimensions,
        )

    async def emit_alert(
        self,
        source: str,
        message: str,
        level: str = "warning",
        extra: Optional[Dict[str, Any]] = None,
        dimensions: Optional[List[str]] = None,
    ) -> ServerEvent:
        """发射告警事件（自动 high 优先级，会持久化）"""
        data = {"message": message, "level": level}
        if extra:
            data.update(extra)
        return await self.emit_event(
            type="alert",
            source=source,
            data=data,
            priority="high",
            dimensions=dimensions,
        )

    async def emit_message(
        self,
        source: str,
        message_data: Dict[str, Any],
        dimensions: Optional[List[str]] = None,
    ) -> ServerEvent:
        """发射消息事件（用于实时推送聊天消息）"""
        return await self.emit_event(
            type="message",
            source=source,
            data=message_data,
            priority="normal",
            dimensions=dimensions,
        )

    async def emit_background_job_event(
        self,
        source: str,
        job_id: str,
        status: str,
        name: str = "",
        summary: str = "",
        dimensions: Optional[List[str]] = None,
    ) -> ServerEvent:
        """发射后台作业状态事件"""
        data = {"job_id": job_id, "status": status}
        if name:
            data["name"] = name
        if summary:
            data["summary"] = summary
        return await self.emit_event(
            type="background_job_update",
            source=source,
            data=data,
            priority="normal",
            dimensions=dimensions,
        )


# 全局单例
event_bus = EventBus()
