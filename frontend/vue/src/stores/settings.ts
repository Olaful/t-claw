/**
 * LLM配置管理Store
 */
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

const API_BASE = '/api/v1';

// 类型定义
export interface ModelCost {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  explicitCacheInput: number;
  explicitCacheRead: number;
}

export interface ModelCompat {
  supportsReasoningEffort: boolean;
  supportsUsageInStreaming: boolean;
  maxTokensField: string;
  nativeWebSearchTool: boolean;
}

export interface TimeRange {
  start: string;
  end: string;
}

export interface PeakTime {
  weekdays: number[];
  timeRange: TimeRange[];
}

export interface LLMModelConfig {
  id: string;
  name: string;
  reasoning: boolean;
  autoCreateExplicitCache?: boolean;
  input: string[];
  billingMode?: string[] | null;
  cost: ModelCost;
  costInputStep?: Record<string, ModelCost> | null;
  contextWindow: number;
  maxTokens: number;
  compat: ModelCompat;
  api: string;
  other?: Record<string, any> | null;
}

export interface LLMProviderConfig {
  baseUrl: string;
  apiKey: string;
  billingMode?: string[] | null;
  peakTime?: PeakTime | null;
  models: LLMModelConfig[];
}

export interface LLMProvider {
  name: string;
  config: LLMProviderConfig;
}

export const useSettingsStore = defineStore('settings', () => {
  // 状态
  const providers = ref<LLMProvider[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);

  // 计算属性
  const providerCount = computed(() => providers.value.length);
  const totalModelCount = computed(() => 
    providers.value.reduce((sum, p) => sum + (p.config.models?.length || 0), 0)
  );

  // 获取所有Provider
  async function fetchProviders() {
    loading.value = true;
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/llm/providers`);
      if (!res.ok) throw new Error('获取Provider列表失败');
      const data = await res.json();
      providers.value = data.providers || [];
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to fetch providers:', e);
    } finally {
      loading.value = false;
    }
  }

  // 添加Provider
  async function addProvider(data: { name: string; baseUrl: string; apiKey?: string; billingMode?: string[] | null; peakTime?: PeakTime | null }) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/llm/providers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '添加Provider失败');
      }
      await fetchProviders();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to add provider:', e);
      return false;
    }
  }

  // 更新Provider
  async function updateProvider(name: string, data: { baseUrl?: string; apiKey?: string; billingMode?: string[] | null; peakTime?: PeakTime | null }) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/llm/providers/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新Provider失败');
      }
      await fetchProviders();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update provider:', e);
      return false;
    }
  }

  // 删除Provider
  async function deleteProvider(name: string) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/llm/providers/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '删除Provider失败');
      }
      await fetchProviders();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to delete provider:', e);
      return false;
    }
  }

  // 添加Model
  async function addModel(providerName: string, model: LLMModelConfig) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/llm/providers/${encodeURIComponent(providerName)}/models`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '添加Model失败');
      }
      await fetchProviders();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to add model:', e);
      return false;
    }
  }

  // 更新Model
  async function updateModel(providerName: string, modelId: string, model: LLMModelConfig) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/llm/providers/${encodeURIComponent(providerName)}/models/${encodeURIComponent(modelId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新Model失败');
      }
      await fetchProviders();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update model:', e);
      return false;
    }
  }

  // 删除Model
  async function deleteModel(providerName: string, modelId: string) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/llm/providers/${encodeURIComponent(providerName)}/models/${encodeURIComponent(modelId)}`, {
        method: 'DELETE',
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '删除Model失败');
      }
      await fetchProviders();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to delete model:', e);
      return false;
    }
  }

  return {
    // 状态
    providers,
    loading,
    error,
    // 计算属性
    providerCount,
    totalModelCount,
    // 方法
    fetchProviders,
    addProvider,
    updateProvider,
    deleteProvider,
    addModel,
    updateModel,
    deleteModel,
  };
});
