/**
 * Agent状态管理 - Pinia Store
 */
import { defineStore } from 'pinia';
import { ref, computed } from 'vue';

const API_BASE = 'http://localhost:8000/api/v1';

interface Agent {
  id: string;
  name: string;
  status: string;
  description?: string;
  created_at: string;
}

export const useAgentStore = defineStore('agent', () => {
  const agents = ref<Agent[]>([]);
  const currentAgent = ref<Agent | null>(null);
  const isConnected = ref(true);
  const totalTokenUsage = ref(0);

  const activeAgentCount = computed(() =>
    agents.value.filter(a => a.status === 'running').length
  );

  async function fetchAgents() {
    try {
      const res = await fetch(`${API_BASE}/agents/`);
      const data = await res.json();
      agents.value = data.agents || [];
    } catch (e) {
      console.error('Failed to fetch agents:', e);
      isConnected.value = false;
    }
  }

  async function createAgent(config: any) {
    try {
      const res = await fetch(`${API_BASE}/agents/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config }),
      });
      
      if (!res.ok) {
        // 获取错误信息
        const errorData = await res.json();
        throw new Error(errorData.detail || '创建失败');
      }
      
      const agent = await res.json();
      agents.value.push(agent);
      return agent;
    } catch (e: any) {
      console.error('Failed to create agent:', e);
      throw e; // 重新抛出错误，让调用者处理
    }
  }

  async function deleteAgent(agentId: string) {
    try {
      await fetch(`${API_BASE}/agents/${agentId}`, { method: 'DELETE' });
      agents.value = agents.value.filter(a => a.id !== agentId);
    } catch (e) {
      console.error('Failed to delete agent:', e);
    }
  }

  function setCurrentAgent(agent: Agent | null) {
    currentAgent.value = agent;
  }

  function addTokenUsage(count: number) {
    totalTokenUsage.value += count;
  }

  return {
    agents,
    currentAgent,
    isConnected,
    totalTokenUsage,
    activeAgentCount,
    fetchAgents,
    createAgent,
    deleteAgent,
    setCurrentAgent,
    addTokenUsage,
  };
});
