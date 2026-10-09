"""连接管理器 - WebSocket 连接管理 + 订阅/退订 + 背压控制"""
import asyncio
import json
from typing import Any, Dict, Optional, Set

import structlog
from fastapi import WebSocket

logger = structlog.get_logger()

# 每个连接的发送队列上限（满了丢弃最旧事件）
DEFAULT_QUEUE_MAXSIZE = 1000


class ConnectionManager:
    """
    管理所有 WebSocket 事件连接
    
    功能:
    - 连接注册/清理
    - 维度订阅/退订
    - 背压控制（每连接发送队列上限，满了丢弃最旧）
    - 按维度广播
    """

    def __init__(self, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE):
        # client_id -> WebSocket
        self.connections: Dict[str, WebSocket] = {}
        # client_id -> 订阅的维度集合 (如 {"agent:xxx", "conversation:yyy"})
        self.subscriptions: Dict[str, Set[str]] = {}
        # client_id -> 发送队列（背压控制）
        self.send_queues: Dict[str, asyncio.Queue] = {}
        # client_id -> 发送循环任务
        self._send_tasks: Dict[str, asyncio.Task] = {}
        # 队列上限
        self.queue_maxsize = queue_maxsize
        # 维度 -> 订阅该维度的 client_id 集合（反向索引，加速广播）
        self._dimension_clients: Dict[str, Set[str]] = {}

    async def connect(self, websocket: WebSocket, client_id: str) -> None:
        """接受并注册新连接"""
        self.connections[client_id] = websocket
        self.subscriptions[client_id] = set()
        self.send_queues[client_id] = asyncio.Queue(maxsize=self.queue_maxsize)

        # 启动后台发送循环
        self._send_tasks[client_id] = asyncio.create_task(
            self._send_loop(client_id),
            name=f"ws_send_{client_id}",
        )

        logger.info(
            "WebSocket connected",
            client_id=client_id,
            total_connections=len(self.connections),
        )

    async def disconnect(self, client_id: str) -> None:
        """清理连接、队列、订阅"""
        # 取消发送任务
        task = self._send_tasks.pop(client_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # 清理订阅反向索引
        dims = self.subscriptions.pop(client_id, set())
        for dim in dims:
            clients = self._dimension_clients.get(dim, set())
            clients.discard(client_id)
            if not clients:
                self._dimension_clients.pop(dim, None)

        # 清理连接和队列
        self.connections.pop(client_id, None)
        self.send_queues.pop(client_id, None)

        logger.info(
            "WebSocket disconnected",
            client_id=client_id,
            total_connections=len(self.connections),
        )

    def subscribe(self, client_id: str, dimensions: list) -> None:
        """订阅维度 (如 ["agent:xxx", "conversation:yyy"])"""
        if client_id not in self.subscriptions:
            return

        for dim in dimensions:
            self.subscriptions[client_id].add(dim)
            # 更新反向索引
            if dim not in self._dimension_clients:
                self._dimension_clients[dim] = set()
            self._dimension_clients[dim].add(client_id)

        logger.debug(
            "Client subscribed",
            client_id=client_id,
            dimensions=dimensions,
            total=len(self.subscriptions[client_id]),
        )

    def unsubscribe(self, client_id: str, dimensions: list) -> None:
        """退订维度"""
        if client_id not in self.subscriptions:
            return

        for dim in dimensions:
            self.subscriptions[client_id].discard(dim)
            # 更新反向索引
            clients = self._dimension_clients.get(dim, set())
            clients.discard(client_id)
            if not clients:
                self._dimension_clients.pop(dim, None)

        logger.debug(
            "Client unsubscribed",
            client_id=client_id,
            dimensions=dimensions,
        )

    async def send_event(self, client_id: str, event_dict: Dict[str, Any]) -> None:
        """
        将事件放入指定连接的发送队列（背压控制）
        
        队列满时丢弃最旧事件，保证新事件能进入。
        """
        queue = self.send_queues.get(client_id)
        if queue is None:
            return

        if queue.full():
            # 背压：丢弃最旧事件
            try:
                queue.get_nowait()
                logger.debug(
                    "Backpressure: dropped oldest event",
                    client_id=client_id,
                )
            except asyncio.QueueEmpty:
                pass

        await queue.put(event_dict)

    async def broadcast(
        self,
        event_dict: Dict[str, Any],
        dimension: Optional[str] = None,
    ) -> int:
        """
        向订阅了某维度的所有连接广播事件
        
        Args:
            event_dict: 事件字典
            dimension: 维度名称，None 表示广播给所有连接
        
        Returns:
            实际发送的连接数
        """
        if dimension:
            target_clients = self._dimension_clients.get(dimension, set()).copy()
        else:
            target_clients = set(self.connections.keys())

        count = 0
        for client_id in target_clients:
            if client_id in self.send_queues:
                await self.send_event(client_id, event_dict)
                count += 1

        return count

    async def broadcast_to_dimensions(
        self,
        event_dict: Dict[str, Any],
        dimensions: list,
    ) -> int:
        """向多个维度广播"""
        sent_clients: Set[str] = set()
        for dim in dimensions:
            clients = self._dimension_clients.get(dim, set())
            for client_id in clients:
                if client_id not in sent_clients and client_id in self.send_queues:
                    await self.send_event(client_id, event_dict)
                    sent_clients.add(client_id)
        return len(sent_clients)

    async def _send_loop(self, client_id: str) -> None:
        """后台发送循环：从队列取事件并通过 WebSocket 发送"""
        websocket = self.connections.get(client_id)
        queue = self.send_queues.get(client_id)
        if not websocket or not queue:
            return

        try:
            while True:
                event_dict = await queue.get()
                try:
                    await websocket.send_json(event_dict)
                except Exception as e:
                    logger.warning(
                        "Failed to send event via WebSocket",
                        client_id=client_id,
                        error=str(e),
                    )
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                "Send loop error",
                client_id=client_id,
                error=str(e),
                exc_info=True,
            )

    def get_subscribed_clients(self, dimension: str) -> Set[str]:
        """获取订阅了某维度的所有 client_id"""
        return self._dimension_clients.get(dimension, set()).copy()

    @property
    def active_count(self) -> int:
        """当前活跃连接数"""
        return len(self.connections)


# 全局单例
connection_manager = ConnectionManager()
