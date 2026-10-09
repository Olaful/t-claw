/**
 * 模型配置管理 composable
 * 管理 LLM 模型列表加载、会话级模型配置、保存
 */
import { ref } from 'vue';
import type { LLMModel, SessionModelConfig } from '../types';

const API_BASE = 'http://localhost:8000/api/v1';

export interface UseModelConfigOptions {
  /** 获取当前 agentId 的函数 */
  getAgentId: () => string;
  /** chatStore 引用 */
  chatStore: any;
}

export function useModelConfig(options: UseModelConfigOptions) {
  const { getAgentId, chatStore } = options;

  // ===== 状态 =====
  const sessionModelConfig = ref<SessionModelConfig | null>(null);
  const showModelSelector = ref(false);
  const llmModels = ref<Array<LLMModel>>([]);
  const currentPrimaryModel = ref<string | null>(null);
  // 临时编辑状态（用于弹窗内的未提交修改）
  const tempModelConfig = ref<SessionModelConfig | null>(null);
  // Token 用量查看组件状态
  const showTokenUsage = ref(false);

  // ===== 方法 =====

  /** 加载 LLM 模型列表（从当前 agent 的 config.models 读取） */
  const loadLLMModels = async () => {
    try {
      const agentId = getAgentId();
      const res = await fetch(`${API_BASE}/agents/${agentId}`);
      if (!res.ok) {
        throw new Error(`Failed to fetch agent: ${res.status}`);
      }
      const data = await res.json();

      // 从 agent.config.models 获取可用模型列表
      const modelsFromConfig = data.config?.models || [];
      if (modelsFromConfig.length === 0) {
        console.warn('No models configured for this agent');
        llmModels.value = [];
        return;
      }

      // 从全局 /agents/llm/models 获取所有模型的详细信息
      const allModelsRes = await fetch(`${API_BASE}/agents/llm/models`);
      const allModelsData = allModelsRes.ok ? await allModelsRes.json() : { models: [] };
      const allModelsMap = new Map<string, LLMModel>();
      (allModelsData.models || []).forEach((m: any) => {
        allModelsMap.set(`${m.provider}/${m.id}`, m);
      });

      // 根据 agent.config.models 过滤并合并详细信息
      llmModels.value = modelsFromConfig.map((modelRef: string) => {
        const fullInfo = allModelsMap.get(modelRef);
        if (fullInfo) {
          return fullInfo;
        }
        // 如果找不到详细信息，构造默认对象
        const [provider, modelId] = modelRef.split('/');
        return {
          id: modelId,
          name: modelId,
          displayName: `${modelId} (${provider})`,
          provider: provider,
          reasoning: false,
          api: 'openai-completions',
          compat: { supportsReasoningEffort: false },
        };
      });

      // 保存 primary_model 用于"使用默认模型"按钮
      currentPrimaryModel.value = data.config?.primary_model || null;
    } catch (e) {
      console.error('Failed to load agent models:', e);
      llmModels.value = [];
    }
  };

  /** 加载会话级模型配置 */
  const loadSessionModelConfig = async (agentId: string, conversationId: string) => {
    try {
      const res = await fetch(`${API_BASE}/chat/conversations/${conversationId}/model?agent_id=${agentId}`);
      const data = await res.json();
      // 无论是否存在会话级模型配置都同步：新会话需重置为 null
      sessionModelConfig.value = data.session_model || null;
    } catch (e) {
      console.error('Failed to load session model config:', e);
      // 加载失败时也重置，避免显示上一个会话的模型
      sessionModelConfig.value = null;
    }
  };

  /** 保存会话级模型配置（返回是否保存成功） */
  const saveSessionModelConfig = async (config: SessionModelConfig): Promise<boolean> => {
    // 新会话尚未创建（无 activeConversationId）：暂存到 store
    if (!chatStore.activeConversationId) {
      sessionModelConfig.value = config;
      chatStore.setPendingSessionModel(config);
      return true;
    }

    try {
      const agentId = getAgentId();
      const res = await fetch(`${API_BASE}/chat/conversations/${chatStore.activeConversationId}/model?agent_id=${agentId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
      });

      if (!res.ok) {
        throw new Error('Failed to save session model config');
      }

      sessionModelConfig.value = config;
      return true;
    } catch (e) {
      console.error('Failed to save session model config:', e);
      return false;
    }
  };

  return {
    // 状态
    sessionModelConfig,
    showModelSelector,
    llmModels,
    currentPrimaryModel,
    tempModelConfig,
    showTokenUsage,
    // 方法
    loadLLMModels,
    loadSessionModelConfig,
    saveSessionModelConfig,
  };
}
