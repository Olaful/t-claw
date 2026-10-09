"""事件总线 + 连接管理器测试"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.event_bus import EventBus, ServerEvent
from app.core.connection_manager import ConnectionManager


# ========== ServerEvent 测试 ==========

class TestServerEvent:
    """测试 ServerEvent 模型"""

    def test_auto_generate_id(self):
        """id 应自动生成，格式 evt_xxxxxxxx"""
        event = ServerEvent(type="test")
        assert event.id.startswith("evt_")
        assert len(event.id) == 12  # "evt_" + 8 chars

    def test_auto_generate_timestamp(self):
        """timestamp 应自动生成 ISO8601 格式"""
        event = ServerEvent(type="test")
        assert event.timestamp.endswith("Z")
        assert "T" in event.timestamp

    def test_unique_ids(self):
        """每次构造应生成不同的 id"""
        e1 = ServerEvent(type="test")
        e2 = ServerEvent(type="test")
        assert e1.id != e2.id

    def test_default_priority(self):
        """默认优先级为 normal"""
        event = ServerEvent(type="test")
        assert event.priority == "normal"

    def test_to_dict(self):
        """to_dict 应返回完整字典"""
        event = ServerEvent(
            type="task_status",
            source="agent_123",
            data={"status": "running"},
            priority="high",
        )
        d = event.to_dict()
        assert d["type"] == "task_status"
        assert d["source"] == "agent_123"
        assert d["data"] == {"status": "running"}
        assert d["priority"] == "high"
        assert "id" in d
        assert "timestamp" in d


# ========== EventBus 测试 ==========

class TestEventBus:
    """测试 EventBus"""

    @pytest.fixture
    def event_bus(self):
        bus = EventBus()
        return bus

    @pytest.mark.asyncio
    async def test_emit_event_returns_event(self, event_bus):
        """emit_event 应返回 ServerEvent 实例"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()
        mock_redis.client = AsyncMock()

        with patch.object(EventBus, 'redis', new_callable=lambda: property(lambda self: mock_redis)):
            event = await event_bus.emit_event(
                type="test_event",
                source="test_source",
                data={"key": "value"},
            )

        assert isinstance(event, ServerEvent)
        assert event.type == "test_event"
        assert event.source == "test_source"
        assert event.data == {"key": "value"}

    @pytest.mark.asyncio
    async def test_emit_event_publishes_to_redis(self, event_bus):
        """emit_event 应发布到 Redis Pub/Sub"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()
        mock_redis.client = AsyncMock()

        with patch.object(EventBus, 'redis', new_callable=lambda: property(lambda self: mock_redis)):
            await event_bus.emit_event(
                type="test",
                source="agent_1",
                dimensions=["agent:agent_1", "conversation:conv_1"],
            )

        # 应发布到各维度 channel + events:all
        assert mock_redis.publish.call_count == 3  # 2 dimensions + 1 all

    @pytest.mark.asyncio
    async def test_high_priority_event_persisted(self, event_bus):
        """高优先级事件应被持久化"""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()
        mock_redis.client = AsyncMock()
        mock_redis.client.rpush = AsyncMock()
        mock_redis.client.ltrim = AsyncMock()
        mock_redis.client.expire = AsyncMock()
        mock_redis.client.zadd = AsyncMock()
        mock_redis.client.hset = AsyncMock()

        with patch.object(EventBus, 'redis', new_callable=lambda: property(lambda self: mock_redis)):
            event = await event_bus.emit_event(
                type="alert",
                source="system",
                data={"message": "critical error"},
                priority="critical",
            )

        # 应调用持久化方法
        mock_redis.client.rpush.assert_called_once()
        mock_redis.client.zadd.assert_called_once()


# ========== ConnectionManager 测试 ==========

class TestConnectionManager:
    """测试 ConnectionManager"""

    @pytest.fixture
    def manager(self):
        return ConnectionManager(queue_maxsize=5)

    @pytest.mark.asyncio
    async def test_connect_disconnect(self, manager):
        """测试连接注册和清理"""
        ws = AsyncMock()
        await manager.connect(ws, "client_1")

        assert "client_1" in manager.connections
        assert "client_1" in manager.send_queues
        assert manager.active_count == 1

        await manager.disconnect("client_1")

        assert "client_1" not in manager.connections
        assert manager.active_count == 0

    @pytest.mark.asyncio
    async def test_subscribe_unsubscribe(self, manager):
        """测试维度订阅和退订"""
        ws = AsyncMock()
        await manager.connect(ws, "client_1")

        manager.subscribe("client_1", ["agent:a1", "conversation:c1"])
        assert "agent:a1" in manager.subscriptions["client_1"]
        assert "client_1" in manager.get_subscribed_clients("agent:a1")

        manager.unsubscribe("client_1", ["agent:a1"])
        assert "agent:a1" not in manager.subscriptions["client_1"]
        assert "client_1" not in manager.get_subscribed_clients("agent:a1")

        await manager.disconnect("client_1")

    @pytest.mark.asyncio
    async def test_backpressure_drops_oldest(self, manager):
        """队列满时应丢弃最旧事件"""
        ws = AsyncMock()
        # 暂停发送循环，让事件堆积在队列中
        send_task = manager._send_tasks.get("client_1")

        await manager.connect(ws, "client_1")

        # 填满队列（maxsize=5）
        for i in range(7):
            await manager.send_event("client_1", {"i": i})

        queue = manager.send_queues["client_1"]
        assert queue.qsize() == 5  # 不超过上限

        # 最旧的 2 个应被丢弃，最早的应该是 i=2
        first = queue.get_nowait()
        assert first["i"] == 2

        await manager.disconnect("client_1")

    @pytest.mark.asyncio
    async def test_broadcast_to_dimension(self, manager):
        """测试按维度广播"""
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        ws3 = AsyncMock()

        await manager.connect(ws1, "c1")
        await manager.connect(ws2, "c2")
        await manager.connect(ws3, "c3")

        manager.subscribe("c1", ["agent:a1"])
        manager.subscribe("c2", ["agent:a1"])
        manager.subscribe("c3", ["agent:a2"])  # 不同维度

        count = await manager.broadcast({"type": "test"}, dimension="agent:a1")
        assert count == 2  # c1 和 c2

        # 验证事件进入了正确的队列
        q1 = manager.send_queues["c1"]
        q2 = manager.send_queues["c2"]
        q3 = manager.send_queues["c3"]

        assert q1.qsize() == 1
        assert q2.qsize() == 1
        assert q3.qsize() == 0

        await manager.disconnect("c1")
        await manager.disconnect("c2")
        await manager.disconnect("c3")

    @pytest.mark.asyncio
    async def test_disconnect_cleans_subscriptions(self, manager):
        """断开连接应清理订阅和反向索引"""
        ws = AsyncMock()
        await manager.connect(ws, "c1")
        manager.subscribe("c1", ["agent:a1"])

        assert "c1" in manager.get_subscribed_clients("agent:a1")

        await manager.disconnect("c1")

        assert "c1" not in manager.get_subscribed_clients("agent:a1")
