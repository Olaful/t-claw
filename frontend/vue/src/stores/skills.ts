/**
 * 技能管理 Store
 */
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

const API_BASE = '/api/v1';

// 类型定义
export type SkillType = 'builtin' | 'plugin' | 'workspace';

export interface SkillItem {
  name: string;
  description: string;
  type: SkillType;
  enabled: boolean;
  context: string;
  version: string;
  gate_reasons: string[];
}

export interface AgentSkillsConfig {
  disallowed: string[];
  allowed: string[];
}

export interface SkillsConfig {
  skills: SkillItem[];
  disallowed: string[];
  agents: Record<string, AgentSkillsConfig>;
  total: number;
  enabled_count: number;
  disabled_count: number;
}

export const useSkillsStore = defineStore('skills', () => {
  // 状态
  const skills = ref<SkillItem[]>([]);
  const disallowed = ref<string[]>([]);
  const agents = ref<Record<string, AgentSkillsConfig>>({});
  const loading = ref(false);
  const error = ref<string | null>(null);

  // 计算属性
  const total = computed(() => skills.value.length);
  const enabledCount = computed(() => skills.value.filter(s => s.enabled).length);
  const disabledCount = computed(() => skills.value.filter(s => !s.enabled).length);
  const builtinSkills = computed(() => skills.value.filter(s => s.type === 'builtin'));
  const pluginSkills = computed(() => skills.value.filter(s => s.type === 'plugin'));
  const workspaceSkills = computed(() => skills.value.filter(s => s.type === 'workspace'));

  // 获取完整技能配置
  async function fetchConfig() {
    loading.value = true;
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/skills`);
      if (!res.ok) throw new Error('获取技能配置失败');
      const data: SkillsConfig = await res.json();
      skills.value = data.skills || [];
      disallowed.value = data.disallowed || [];
      agents.value = data.agents || {};
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to fetch skills config:', e);
    } finally {
      loading.value = false;
    }
  }

  // 启用/禁用技能（全局）
  async function toggleSkill(name: string, enabled: boolean) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/skills/${encodeURIComponent(name)}/toggle`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '切换技能状态失败');
      }
      // 本地即时更新，避免整表重拉
      const item = skills.value.find(s => s.name === name);
      if (item) item.enabled = enabled;
      if (enabled) {
        disallowed.value = disallowed.value.filter(p => p !== name);
      } else if (!disallowed.value.includes(name)) {
        disallowed.value = [...disallowed.value, name];
      }
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to toggle skill:', e);
      return false;
    }
  }

  // 更新某个 agent 的技能配置
  async function updateAgentSkills(agentName: string, cfg: Partial<AgentSkillsConfig>) {
    error.value = null;
    try {
      const res = await fetch(`${API_BASE}/settings/skills/agent/${encodeURIComponent(agentName)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '更新 agent 技能配置失败');
      }
      await fetchConfig();
      return true;
    } catch (e: any) {
      error.value = e.message;
      console.error('Failed to update agent skills:', e);
      return false;
    }
  }

  return {
    // 状态
    skills,
    disallowed,
    agents,
    loading,
    error,
    // 计算属性
    total,
    enabledCount,
    disabledCount,
    builtinSkills,
    pluginSkills,
    workspaceSkills,
    // 方法
    fetchConfig,
    toggleSkill,
    updateAgentSkills,
  };
});
