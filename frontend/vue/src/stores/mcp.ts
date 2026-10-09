/**
 * MCP 配置管理 Store
 */
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

const API_BASE = '/api/v1';

// 类型定义
export interface MCPOAuthConfig {
  enabled: boolean;
  scopes: string[];
}

export interface MCPServerConfig {
  name: string;
  type: 'stdio' | 'sse' | 'http' | 'ws' | 'sdk';
  enabled: boolean;
  // stdio
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  // sse / http / ws
  url?: string;
  headers?: Record<string, string>;
  // sdk
  module?: string;
  // OAuth
  oauth?: MCPOAuthConfig;
  // 超时覆盖
  call_timeout_seconds?: number;
  connect_timeout_seconds?: number;
  // 描述
  description?: string;
}

export interface MCPServerResponse extends MCPServerConfig {
  status?: string;
  tools_count: number;
  last_error?: string;
  oauth_required: boolean;
  source: string;
}

export interface MCPGlobalConfig {
  enabled: boolean;
  call_timeout_seconds: number;
  connect_timeout_seconds: number;
  max_consecutive_errors: number;
  reconnect_delay_seconds: number;
  reconnect_backoff_max_seconds: number;
  description?: string;
  [key: string]: any;
}

export interface MCPConfigResponse {
  global_config: MCPGlobalConfig;
  servers: MCPServerResponse[];
  total: number;
}

export const useMCPStore = defineStore('mcp', () => {
  // 状态
  const globalConfig = ref<MCPGlobalConfig | null>(null);
  const servers = ref<MCPServerResponse[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);

  // 计算属性
  const serverCount = computed(() => servers.value.length);
  const enabledCount = computed(() => servers.value.filter(s => s.enabled).length);
  const oauthCount = computed(() => servers.value.filter(s => s.oauth_required).length);

  // 获取完整 MCP 配置
  async function fetchConfig() {
    loading.value = true;
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/mcp`);
      if (!res.ok) throw new Error('获取 MCP 配置失败');
      const data: MCPConfigResponse = await res.json();
      globalConfig.value = data.global_config;
      servers.value = data.servers || [];
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to fetch MCP config:', e);
    } finally {
      loading.value = false;
    }
  }

  // 更新全局配置
  async function updateGlobalConfig(data: Partial<MCPGlobalConfig>) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/mcp`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新 MCP 全局配置失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update MCP global config:', e);
      return false;
    }
  }

  // 添加服务器
  async function addServer(data: MCPServerConfig) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/mcp/servers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '添加 MCP 服务器失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to add MCP server:', e);
      return false;
    }
  }

  // 更新服务器
  async function updateServer(name: string, data: MCPServerConfig) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/mcp/servers/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新 MCP 服务器失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update MCP server:', e);
      return false;
    }
  }

  // 删除服务器
  async function deleteServer(name: string) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/mcp/servers/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '删除 MCP 服务器失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to delete MCP server:', e);
      return false;
    }
  }

  // 触发热重载
  async function reload() {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/mcp/reload`, {
        method: 'POST',
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'MCP 重载失败');
      }
      await fetchConfig();
      const result = await res.json();
      return result;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to reload MCP:', e);
      return null;
    }
  }

  return {
    // 状态
    globalConfig,
    servers,
    loading,
    error,
    // 计算属性
    serverCount,
    enabledCount,
    oauthCount,
    // 方法
    fetchConfig,
    updateGlobalConfig,
    addServer,
    updateServer,
    deleteServer,
    reload,
  };
});
