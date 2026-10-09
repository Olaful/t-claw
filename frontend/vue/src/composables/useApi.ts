/**
 * API调用组合式函数
 */
import { ref } from 'vue';

const API_BASE = 'http://localhost:8000/api/v1';

export function useApi() {
  const loading = ref(false);
  const error = ref<string | null>(null);

  async function request<T>(
    url: string,
    options?: RequestInit,
  ): Promise<T | null> {
    loading.value = true;
    error.value = null;
    
    try {
      const res = await fetch(`${API_BASE}${url}`, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }
      
      return await res.json() as T;
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Unknown error';
      return null;
    } finally {
      loading.value = false;
    }
  }

  return { loading, error, request };
}

export function useWebSocket(url: string) {
  const connected = ref(false);
  const messages = ref<any[]>([]);
  let ws: WebSocket | null = null;

  function connect() {
    ws = new WebSocket(url);
    
    ws.onopen = () => { connected.value = true; };
    ws.onclose = () => { connected.value = false; };
    ws.onmessage = (event) => {
      try {
        messages.value.push(JSON.parse(event.data));
      } catch {
        messages.value.push(event.data);
      }
    };
  }

  function send(data: any) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(typeof data === 'string' ? data : JSON.stringify(data));
    }
  }

  function disconnect() {
    ws?.close();
    ws = null;
  }

  return { connected, messages, connect, send, disconnect };
}
