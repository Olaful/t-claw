"""事件 WebSocket + REST 同步端点"""
import asyncio
import json
from typing import Optional

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse

from app.core.connection_manager import connection_manager
from app.core.event_bus import event_bus
from app.utils.redis import redis_client

logger = structlog.get_logger()

router = APIRouter()

# 全局单例 Redis Pub/Sub 监听任务（所有 WebSocket 连接共享）
_global_redis_listener: Optional[asyncio.Task] = None


async def _global_redis_listener_loop() -> None:
    """
    全局 Redis Pub/Sub 监听器（单例）
    
    只创建一个 pubsub 连接，订阅 events:* 所有 channel，
    收到消息后根据各 client 的订阅维度分发给对应的 WebSocket 连接。
    """
    try:
        logger.info("Global Redis listener starting")
        pubsub = redis_client.pubsub()
        await pubsub.psubscribe("events:*")
        logger.info("Global Redis listener subscribed to events:*")

        async for message in pubsub.listen():
            if message["type"] not in ("pmessage",):
                continue

            channel = message.get("channel", "")
            if isinstance(channel, bytes):
                channel = channel.decode("utf-8")

            # 解析维度（channel 格式: "events:{dimension}"）
            if channel.startswith("events:"):
                dimension = channel[len("events:"):]
            else:
                continue

            # 遍历所有已连接的 client，检查是否订阅了该维度
            try:
                event_data = json.loads(message["data"])
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Failed to parse Redis message", error=str(e))
                continue

            # 分发给订阅了该维度或 "all" 的所有 client
            for client_id, subs in list(connection_manager.subscriptions.items()):
                if dimension in subs or "all" in subs or dimension == client_id:
                    try:
                        await connection_manager.send_event(client_id, event_data)
                    except Exception as e:
                        logger.warning(
                            "Failed to send event to client",
                            client_id=client_id,
                            error=str(e),
                        )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Global Redis listener error", error=str(e), exc_info=True)


async def _ensure_global_listener() -> None:
    """确保全局 Redis 监听器已启动"""
    global _global_redis_listener
    if _global_redis_listener is None or _global_redis_listener.done():
        _global_redis_listener = asyncio.create_task(
            _global_redis_listener_loop(),
            name="global_redis_listener",
        )


@router.websocket("/ws/events/{client_id}")
async def websocket_events(websocket: WebSocket, client_id: str):
    """
    事件专用 WebSocket 端点
    
    客户端消息协议:
    - {"type": "subscribe", "dimensions": ["agent:xxx", "conversation:yyy"]}
    - {"type": "unsubscribe", "dimensions": ["agent:xxx"]}
    - {"type": "sync", "last_event_id": "evt_xxxx", "timestamp": "..."}
    - {"type": "ping"}                    # 心跳
    - {"type": "ping_test"}               # Pub/Sub 链路健康检查
    """
    await websocket.accept()
    logger.info("WebSocket accepted", client_id=client_id)
    await connection_manager.connect(websocket, client_id)
    logger.info("Connection registered in manager", client_id=client_id)

    # 确保全局 Redis 监听器已启动（所有连接共享同一个监听器）
    await _ensure_global_listener()

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "subscribe":
                dimensions = data.get("dimensions", [])
                if dimensions:
                    connection_manager.subscribe(client_id, dimensions)
                    await websocket.send_json({
                        "type": "subscribed",
                        "dimensions": dimensions,
                    })
                    logger.info("Subscribed to dimensions", client_id=client_id, dimensions=dimensions)

            elif msg_type == "unsubscribe":
                dimensions = data.get("dimensions", [])
                if dimensions:
                    connection_manager.unsubscribe(client_id, dimensions)
                    await websocket.send_json({
                        "type": "unsubscribed",
                        "dimensions": dimensions,
                    })

            elif msg_type == "sync":
                # 断线重连后请求补发遗漏事件
                last_event_id = data.get("last_event_id")
                timestamp = data.get("timestamp")
                missed_events = await event_bus.get_missed_events(
                    since=timestamp,
                    last_event_id=last_event_id,
                )
                await websocket.send_json({
                    "type": "sync_result",
                    "events": missed_events,
                    "count": len(missed_events),
                })

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif msg_type == "ping_test":
                # Pub/Sub 链路健康检查：通过 Redis 发布测试事件，验证完整链路
                # 使用 client_id 作为维度，监听器中 dimension == client_id 会自动匹配
                logger.info("Pub/Sub health check requested", client_id=client_id)
                await event_bus.emit_event(
                    type="pubsub_check",
                    source="system",
                    data={
                        "message": "Pub/Sub pipeline OK",
                        "client_id": client_id,
                    },
                    priority="low",
                    dimensions=[client_id],
                )

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected", client_id=client_id)
    except Exception as e:
        logger.error(
            "WebSocket error",
            client_id=client_id,
            error_type=type(e).__name__,
            error=str(e),
            exc_info=True,
        )
    finally:
        # 清理连接
        await connection_manager.disconnect(client_id)


@router.get("/sync")
async def sync_events(
    client_id: str = Query(..., description="客户端 ID"),
    since: Optional[str] = Query(None, description="ISO 时间戳，获取该时间之后的事件"),
    last_event_id: Optional[str] = Query(None, description="最后收到的事件 ID"),
):
    """
    REST 端点：获取断线期间的事件（WebSocket 不可用时的降级方案）
    """
    missed_events = await event_bus.get_missed_events(
        since=since,
        last_event_id=last_event_id,
    )
    return {
        "client_id": client_id,
        "events": missed_events,
        "count": len(missed_events),
    }
