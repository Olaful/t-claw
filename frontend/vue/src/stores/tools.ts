/**
 * 工具管理 Store
 */
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

const API_BASE = '/api/v1';

// 类型定义
export type ToolType = 'builtin' | 'mcp' | 'extension';

export interface ToolItem {
  name: string;
  description: string;
  type: ToolType;
  enabled: boolean;
  system_tool: boolean;
  timeout_seconds: number;
  mcp_server?: string | null;
}

export interface SubagentToolsConfig {
  disallowed_tools_per_level: Record<string, string[]>;
  allowed_tools_per_level: Record<string, string[]>;
}

export interface TeamToolsConfig {
  disallowed: string[];
  allowed: string[];
}

export interface ToolsConfig {
  tools: ToolItem[];
  disallowed: string[];
  timeout_seconds: Record<string, any>;
  rate_limiting: Record<string, any>;
  subagent: SubagentToolsConfig;
  team: TeamToolsConfig;
  total: number;
  enabled_count: number;
  disabled_count: number;
}

export const useToolsStore = defineStore('tools', () => {
  // 状态
  const tools = ref<ToolItem[]>([]);
  const disallowed = ref<string[]>([]);
  const timeoutConfig = ref<Record<string, any>>({});
  const rateLimit = ref<Record<string, any>>({});
  const subagentConfig = ref<SubagentToolsConfig>({
    disallowed_tools_per_level: {},
    allowed_tools_per_level: {},
  });
  const teamConfig = ref<TeamToolsConfig>({ disallowed: [], allowed: [] });
  const loading = ref(false);
  const error = ref<string | null>(null);

  // 计算属性
  const total = computed(() => tools.value.length);
  const enabledCount = computed(() => tools.value.filter(t => t.enabled).length);
  const disabledCount = computed(() => tools.value.filter(t => !t.enabled).length);
  const builtinTools = computed(() => tools.value.filter(t => t.type === 'builtin'));
  const mcpTools = computed(() => tools.value.filter(t => t.type === 'mcp'));
  const extensionTools = computed(() => tools.value.filter(t => t.type === 'extension'));

  // 获取完整工具配置
  async function fetchConfig() {
    loading.value = true;
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/tools`);
      if (!res.ok) throw new Error('获取工具配置失败');
      const data: ToolsConfig = await res.json();
      tools.value = data.tools || [];
      disallowed.value = data.disallowed || [];
      timeoutConfig.value = data.timeout_seconds || {};
      rateLimit.value = data.rate_limiting || {};
      subagentConfig.value = data.subagent || { disallowed_tools_per_level: {}, allowed_tools_per_level: {} };
      teamConfig.value = data.team || { disallowed: [], allowed: [] };
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to fetch tools config:', e);
    } finally {
      loading.value = false;
    }
  }

  // 启用/禁用工具（全局）
  async function toggleTool(name: string, enabled: boolean) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/tools/${encodeURIComponent(name)}/toggle`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '切换工具状态失败');
      }
      // 本地即时更新，避免整表重拉
      const item = tools.value.find(t => t.name === name);
      if (item) item.enabled = enabled;
      if (enabled) {
        disallowed.value = disallowed.value.filter(p => p !== name);
      } else if (!disallowed.value.includes(name)) {
        disallowed.value = [...disallowed.value, name];
      }
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to toggle tool:', e);
      return false;
    }
  }

  // 更新超时配置
  async function updateTimeout(cfg: { default?: number | null; overrides?: Record<string, number | null> }) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/tools/timeout`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新超时配置失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update timeout:', e);
      return false;
    }
  }

  // 更新限流配置
  async function updateRateLimitCfg(cfg: { enabled?: boolean; requests_per_minute?: number }) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/tools/rate-limit`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新限流配置失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update rate limit:', e);
      return false;
    }
  }

  // 更新 subagent 分层工具配置
  async function updateSubagent(cfg: Partial<SubagentToolsConfig>) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/tools/subagent`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新 subagent 工具配置失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update subagent tools:', e);
      return false;
    }
  }

  // 更新团队工具配置
  async function updateTeam(cfg: Partial<TeamToolsConfig>) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/tools/team`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新团队工具配置失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update team tools:', e);
      return false;
    }
  }

  return {
    // 状态
    tools,
    disallowed,
    timeoutConfig,
    rateLimit,
    subagentConfig,
    teamConfig,
    loading,
    error,
    // 计算属性
    total,
    enabledCount,
    disabledCount,
    builtinTools,
    mcpTools,
    extensionTools,
    // 方法
    fetchConfig,
    toggleTool,
    updateTimeout,
    updateRateLimit: updateRateLimitCfg,
    updateSubagent,
    updateTeam,
  };
});
