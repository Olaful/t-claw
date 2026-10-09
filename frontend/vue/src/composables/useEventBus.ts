/**
 * 前端事件总线 - WebSocket 连接管理 + 事件分发 + 节流/合并 + 断线补发
 */
import { ref, onUnmounted } from 'vue';

const WS_BASE = 'ws://localhost:8000';

// 事件类型
interface ServerEvent {
  id: string;
  type: string;
  timestamp: string;
  source: string;
  data: Record<string, any>;
  priority: 'low' | 'normal' | 'high' | 'critical';
}

type EventCallback = (event: ServerEvent) => void;

// 全局单例状态（所有组件共享）
let ws: WebSocket | null = null;
let clientId: string = '';
let reconnectAttempts = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
let heartbeatTimeout: ReturnType<typeof setTimeout> | null = null;
let lastEventId: string = '';
let lastEventTimestamp: string = '';
let healthCheckTimer: ReturnType<typeof setTimeout> | null = null;

// 连接状态
const connectionStatus = ref<'connecting' | 'connected' | 'disconnected'>('disconnected');

// 事件监听器 Map: type -> Set<callback>
const listeners = new Map<string, Set<EventCallback>>();

// 去重：最近 1000 个 event.id
const recentEventIds = new Set<string>();
const MAX_RECENT_IDS = 1000;

// 节流控制：type -> { timer, latestEvent }
const throttleState = new Map<string, { timer: ReturnType<typeof setTimeout> | null; latestEvent: ServerEvent | null }>();
const THROTTLE_MS = 200; // 同类事件节流时间

// 单例标志
let initialized = false;

// ✅ 已订阅维度集合（WS 未就绪时暂存，连接后自动重发）
const subscribedDimensions = new Set<string>();

function generateClientId(): string {
  return `client_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function connect() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return; // 已连接或正在连接中
  }

  if (!clientId) {
    clientId = generateClientId();
  }

  connectionStatus.value = 'connecting';

  const wsUrl = `${WS_BASE}/api/v1/events/ws/events/${clientId}`;
  console.log('[EventBus] Connecting to:', wsUrl);
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log('[EventBus] Connected');
    connectionStatus.value = 'connected';
    reconnectAttempts = 0;
    startHeartbeat();

    // ✅ 连接成功后立即重发所有已记录的订阅维度（修复初始化竞态 + 断线重连丢失订阅）
    if (subscribedDimensions.size > 0) {
      const dims = Array.from(subscribedDimensions);
      ws!.send(JSON.stringify({ type: 'subscribe', dimensions: dims }));
      console.log('[EventBus] Re-subscribed dimensions on open:', dims);
    }

    // Pub/Sub 链路健康检查：发送测试消息，验证完整发布订阅链路
    send({ type: 'ping_test' });
    // 设置 5s 超时，未收到响应则警告
    healthCheckTimer = setTimeout(() => {
      console.warn(
        '[EventBus] Pub/Sub health check TIMEOUT (5s) - Redis Pub/Sub may be unavailable'
      );
    }, 5000);

    // 断线重连后请求补发遗漏事件
    if (lastEventId || lastEventTimestamp) {
      send({
        type: 'sync',
        last_event_id: lastEventId || undefined,
        timestamp: lastEventTimestamp || undefined,
      });
    }
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);

      // 处理控制消息
      if (data.type === 'pong' || data.type === 'subscribed' || data.type === 'unsubscribed') {
        if (data.type === 'pong') {
          onPong();
        }
        return;
      }

      // Pub/Sub 健康检查响应：验证完整链路（前端→后端→Redis→后端→前端）
      if (data.type === 'pubsub_check') {
        // 清除超时计时器
        if (healthCheckTimer) {
          clearTimeout(healthCheckTimer);
          healthCheckTimer = null;
        }
        console.log(
          '%c[EventBus] Pub/Sub health check PASSED',
          'color: #4CAF50; font-weight: bold;',
          { event_id: data.id, timestamp: data.timestamp, data: data.data }
        );
        return;
      }

      // 处理 sync_result（断线补发的事件）
      if (data.type === 'sync_result') {
        const events = data.events || [];
        for (const evt of events) {
          dispatchEvent(evt as ServerEvent);
        }
        return;
      }

      // 标准事件处理
      dispatchEvent(data as ServerEvent);
    } catch (e) {
      console.error('[EventBus] Failed to parse message:', e);
    }
  };

  ws.onclose = (event) => {
    console.log('[EventBus] Closed', { code: event.code, reason: event.reason, wasClean: event.wasClean });
    connectionStatus.value = 'disconnected';
    stopHeartbeat();
    scheduleReconnect();
  };

  ws.onerror = (e) => {
    console.error('[EventBus] WebSocket error:', e);
  };
}

function disconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (healthCheckTimer) {
    clearTimeout(healthCheckTimer);
    healthCheckTimer = null;
  }
  stopHeartbeat();
  if (ws) {
    ws.close();
    ws = null;
  }
  connectionStatus.value = 'disconnected';
  reconnectAttempts = 0;
}

function scheduleReconnect() {
  if (reconnectTimer) return;

  // 指数退避: 1s, 2s, 4s, 8s, max 30s
  const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
  reconnectAttempts++;

  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

// ========== 心跳 ==========

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    send({ type: 'ping' });
    // 10s 超时判定断线
    heartbeatTimeout = setTimeout(() => {
      console.warn('[EventBus] Heartbeat timeout, reconnecting...');
      ws?.close();
    }, 10000);
  }, 30000);
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  if (heartbeatTimeout) {
    clearTimeout(heartbeatTimeout);
    heartbeatTimeout = null;
  }
}

function onPong() {
  if (heartbeatTimeout) {
    clearTimeout(heartbeatTimeout);
    heartbeatTimeout = null;
  }
}

// ========== 事件分发 ==========

function dispatchEvent(event: ServerEvent) {
  if (!event.id) return;

  // 去重
  if (recentEventIds.has(event.id)) return;
  recentEventIds.add(event.id);
  if (recentEventIds.size > MAX_RECENT_IDS) {
    // 删除最早的（Set 按插入顺序）
    const first = recentEventIds.values().next().value;
    if (first) recentEventIds.delete(first);
  }

  // 更新最后收到的事件信息
  lastEventId = event.id;
  lastEventTimestamp = event.timestamp;

  // 节流处理：对 agent_progress 等高频事件做节流
  if (event.type === 'agent_progress') {
    throttleEvent(event);
    return;
  }

  // 直接分发给监听器
  emitToListeners(event);
}

function throttleEvent(event: ServerEvent) {
  let state = throttleState.get(event.type);
  if (!state) {
    state = { timer: null, latestEvent: null };
    throttleState.set(event.type, state);
  }

  state.latestEvent = event;

  if (!state.timer) {
    state.timer = setTimeout(() => {
      state!.timer = null;
      if (state!.latestEvent) {
        emitToListeners(state!.latestEvent);
        state!.latestEvent = null;
      }
    }, THROTTLE_MS);
  }
}

function emitToListeners(event: ServerEvent) {
  // 分发给特定类型的监听器
  const typeListeners = listeners.get(event.type);
  if (typeListeners) {
    for (const cb of typeListeners) {
      try {
        cb(event);
      } catch (e) {
        console.error('[EventBus] Listener error:', e);
      }
    }
  }

  // 分发给通配符 "*" 监听器（监听所有事件）
  const allListeners = listeners.get('*');
  if (allListeners) {
    for (const cb of allListeners) {
      try {
        cb(event);
      } catch (e) {
        console.error('[EventBus] Listener error:', e);
      }
    }
  }
}

// ========== 公共 API ==========

function send(data: Record<string, any>) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
  } else if (data.type === 'subscribe') {
    console.warn('[EventBus] Subscribe deferred (WS not open, will re-send on connect):', data.dimensions);
  }
}

function subscribe(dimensions: string[]) {
  // ✅ 始终记录维度，即使 WS 未就绪也能在连接后自动重发
  for (const dim of dimensions) {
    subscribedDimensions.add(dim);
  }
  send({ type: 'subscribe', dimensions });
}

function unsubscribe(dimensions: string[]) {
  for (const dim of dimensions) {
    subscribedDimensions.delete(dim);
  }
  send({ type: 'unsubscribe', dimensions });
}

function onEvent(type: string, callback: EventCallback): () => void {
  if (!listeners.has(type)) {
    listeners.set(type, new Set());
  }
  listeners.get(type)!.add(callback);

  // 返回取消订阅函数
  return () => {
    const set = listeners.get(type);
    if (set) {
      set.delete(callback);
      if (set.size === 0) listeners.delete(type);
    }
  };
}

// ========== Composable 导出 ==========

export function useEventBus() {
  // 自动连接（首次调用时）
  if (!initialized) {
    initialized = true;
    connect();
  }

  // 组件卸载时不清理全局状态（其他组件可能还在用）
  // 但提供手动清理方法

  return {
    connectionStatus,
    clientId,
    connect,
    disconnect,
    subscribe,
    unsubscribe,
    onEvent,
    send,
  };
}
