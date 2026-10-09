/**
 * Agent管理页面
 */
import { defineComponent, h, ref, onMounted } from 'vue';
import { useAgentStore } from '../stores/agent';

const API_BASE = 'http://localhost:8000/api/v1';

export default defineComponent({
  name: 'Agents',
  setup() {
    const agentStore = useAgentStore();
    const showCreateModal = ref(false);
    const showEditModal = ref(false);
    const editingAgent = ref<any>(null);
    // ✅ LLM模型列表（从后端API动态加载）
    const llmModels = ref<Array<{id: string; name: string; displayName: string; provider: string}>>([]);
    
    // ✅ 加载LLM模型列表
    const loadLLMModels = async () => {
      try {
        const response = await fetch(`${API_BASE}/agents/llm/models`);
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        llmModels.value = data.models || [];
        console.log('Loaded LLM models:', llmModels.value.length);
      } catch (error) {
        console.error('Failed to load LLM models:', error);
        // 如果加载失败，使用默认模型列表作为后备
        llmModels.value = [
          { id: 'gpt-4-turbo-preview', name: 'GPT-4 Turbo', displayName: 'GPT-4 Turbo (openai)', provider: 'openai' },
          { id: 'gpt-3.5-turbo', name: 'GPT-3.5 Turbo', displayName: 'GPT-3.5 Turbo (openai)', provider: 'openai' },
          { id: 'claude-3-opus', name: 'Claude 3 Opus', displayName: 'Claude 3 Opus (anthropic)', provider: 'anthropic' },
          { id: 'deepseek-chat', name: 'DeepSeek Chat', displayName: 'DeepSeek Chat (deepseek)', provider: 'deepseek' },
        ];
      }
    };
    
    // ✅ 创建Agent表单数据
    const newAgentForm = ref({
      name: '',
      description: '',
      system_prompt: 'You are a helpful AI assistant.',
      models: [] as string[],      // 选中的模型列表（provider/model_id）
      primary_model: '',           // 默认主模型（provider/model_id）
      temperature: 0.7,
      max_tokens: 4096,
    });
    
    // ✅ 编辑Agent表单数据
    const editAgentForm = ref({
      id: '',
      name: '',
      description: '',
      system_prompt: '',
      models: [] as string[],      // 选中的模型列表（provider/model_id）
      primary_model: '',           // 默认主模型（provider/model_id）
      temperature: 0.7,
      max_tokens: 4096,
    });
    
    // ✅ 切换模型选中状态（多选），并维护默认模型
    const toggleModel = (form: { models: string[]; primary_model: string }, modelRef: string) => {
      const idx = form.models.indexOf(modelRef);
      if (idx >= 0) {
        form.models.splice(idx, 1);
        // 若取消的是默认模型，回退到剩余首个
        if (form.primary_model === modelRef) {
          form.primary_model = form.models[0] || '';
        }
      } else {
        form.models.push(modelRef);
        // 首个选中的模型自动设为默认
        if (!form.primary_model) {
          form.primary_model = modelRef;
        }
      }
    };
    
    // ✅ 渲染模型选择器（多选 + 默认模型），供创建/编辑模态框复用
    const renderModelSelector = (form: { models: string[]; primary_model: string }) => [
      // 多选模型
      h('div', { style: 'margin-bottom:1rem;' }, [
        h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, 'LLM模型（可多选）'),
        h('div', { style: 'max-height:160px;overflow-y:auto;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);padding:0.5rem;' },
          llmModels.value.length === 0
            ? [h('div', { style: 'color:var(--color-text-disabled);font-size:0.875rem;' }, '暂无可用模型')]
            : llmModels.value.map(model => {
                const modelRef = `${model.provider}/${model.id}`;
                return h('label', { key: modelRef, style: 'display:flex;align-items:center;gap:0.5rem;padding:0.25rem 0;cursor:pointer;' }, [
                  h('input', {
                    type: 'checkbox',
                    checked: form.models.includes(modelRef),
                    onChange: () => toggleModel(form, modelRef),
                  }),
                  h('span', { style: 'font-size:0.875rem;' }, model.displayName),
                ]);
              })
        ),
      ]),
      // 默认模型
      h('div', { style: 'margin-bottom:1rem;' }, [
        h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, '默认模型'),
        h('select', {
          value: form.primary_model,
          disabled: form.models.length === 0,
          style: 'width:100%;padding:0.5rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);',
          onChange: (e: Event) => { form.primary_model = (e.target as HTMLSelectElement).value; },
        }, form.models.map(ref => h('option', { value: ref }, ref))),
      ]),
    ];

    // ✅ 重置创建表单
    const resetCreateForm = () => {
      newAgentForm.value = {
        name: '',
        description: '',
        system_prompt: 'You are a helpful AI assistant.',
        models: [],
        primary_model: '',
        temperature: 0.7,
        max_tokens: 4096,
      };
    };
    
    // ✅ 重置编辑表单
    const resetEditForm = () => {
      editAgentForm.value = {
        id: '',
        name: '',
        description: '',
        system_prompt: '',
        models: [],
        primary_model: '',
        temperature: 0.7,
        max_tokens: 4096,
      };
    };
    
    // ✅ 处理创建Agent
    const handleCreateAgent = async () => {
      if (!newAgentForm.value.name.trim()) {
        alert('请输入Agent名称');
        return;
      }
      
      const config = {
        name: newAgentForm.value.name,
        description: newAgentForm.value.description,
        system_prompt: newAgentForm.value.system_prompt,
        llm_provider: 'openai',
        // 默认模型取 primary_model 的 model_id 部分（后端会根据 settings.json 重新解析覆盖）
        llm_model: newAgentForm.value.primary_model.split('/')[1] || newAgentForm.value.primary_model,
        models: newAgentForm.value.models,
        primary_model: newAgentForm.value.primary_model,
        temperature: newAgentForm.value.temperature,
        max_tokens: newAgentForm.value.max_tokens,
        tool_ids: [],
        skill_ids: [],
      };
      
      try {
        const result = await agentStore.createAgent(config);
        showCreateModal.value = false;
        resetCreateForm();
        alert(`Agent "${result.name}" 创建成功！`);
      } catch (error: any) {
        // 显示具体的错误信息
        alert(error.message || '创建Agent失败，请重试');
      }
    };
    
    // ✅ 打开编辑模态框（主动获取最新数据）
    const openEditModal = async (agent: any) => {
      // 先刷新 agent 列表，确保获取最新的模型配置
      await agentStore.fetchAgents();
      
      // 从刷新后的列表中找到对应的 agent
      const freshAgent = agentStore.agents.find(a => a.id === agent.id) || agent;
      
      editingAgent.value = freshAgent;
      const cfgModels: string[] = freshAgent.config?.models || [];
      const cfgPrimary: string = freshAgent.config?.primary_model || cfgModels[0] || '';
      editAgentForm.value = {
        id: freshAgent.id,
        name: freshAgent.name || '',
        description: freshAgent.description || '',
        system_prompt: freshAgent.config?.system_prompt || 'You are a helpful AI assistant.',
        models: [...cfgModels],
        primary_model: cfgPrimary,
        temperature: freshAgent.config?.temperature || 0.7,
        max_tokens: freshAgent.config?.max_tokens || 4096,
      };
      showEditModal.value = true;
    };
    
    // ✅ 处理更新Agent
    const handleUpdateAgent = async () => {
      if (!editAgentForm.value.name.trim()) {
        alert('请输入Agent名称');
        return;
      }
      
      try {
        const res = await fetch(`${API_BASE}/agents/${editAgentForm.value.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: editAgentForm.value.name,
            description: editAgentForm.value.description,
            system_prompt: editAgentForm.value.system_prompt,
            llm_model: editAgentForm.value.primary_model.split('/')[1] || editAgentForm.value.primary_model,
            models: editAgentForm.value.models,
            primary_model: editAgentForm.value.primary_model,
            temperature: editAgentForm.value.temperature,
            max_tokens: editAgentForm.value.max_tokens,
          }),
        });
        
        if (res.ok) {
          showEditModal.value = false;
          resetEditForm();
          editingAgent.value = null;
          await agentStore.fetchAgents(); // 刷新列表
          alert(`Agent "${editAgentForm.value.name}" 更新成功！`);
        } else {
          alert('更新Agent失败，请重试');
        }
      } catch (e) {
        console.error('Failed to update agent:', e);
        alert('更新Agent失败，请重试');
      }
    };
    
    // ✅ 处理删除Agent
    const handleDeleteAgent = async (agentId: string, agentName: string) => {
      if (!confirm(`确定要删除 Agent "${agentName}" 吗？此操作不可恢复。`)) {
        return;
      }
      
      try {
        const res = await fetch(`${API_BASE}/agents/${agentId}`, {
          method: 'DELETE',
        });
        
        if (res.ok) {
          await agentStore.fetchAgents(); // 刷新列表
          alert(`Agent "${agentName}" 已删除`);
        } else {
          alert('删除Agent失败，请重试');
        }
      } catch (e) {
        console.error('Failed to delete agent:', e);
        alert('删除Agent失败，请重试');
      }
    };
    
    // ✅ 处理禁用Agent
    const handleDisableAgent = async (agentId: string, agentName: string) => {
      if (!confirm(`确定要禁用 Agent "${agentName}" 吗？禁用的Agent将不会加载。`)) {
        return;
      }
      
      try {
        const res = await fetch(`${API_BASE}/agents/${agentId}/disable`, {
          method: 'POST',
        });
        
        if (res.ok) {
          await agentStore.fetchAgents(); // 刷新列表
          alert(`Agent "${agentName}" 已禁用`);
        } else {
          alert('禁用Agent失败，请重试');
        }
      } catch (e) {
        console.error('Failed to disable agent:', e);
        alert('禁用Agent失败，请重试');
      }
    };

    // ✅ 处理启用Agent
    const handleEnableAgent = async (agentId: string, agentName: string) => {
      try {
        const res = await fetch(`${API_BASE}/agents/${agentId}/enable`, {
          method: 'POST',
        });
        
        if (res.ok) {
          await agentStore.fetchAgents(); // 刷新列表
          alert(`Agent "${agentName}" 已启用`);
        } else {
          alert('启用Agent失败，请重试');
        }
      } catch (e) {
        console.error('Failed to enable agent:', e);
        alert('启用Agent失败，请重试');
      }
    };

    onMounted(async () => {
      await agentStore.fetchAgents();
      // ✅ 加载LLM模型列表
      await loadLLMModels();
    });

    // ✅ 根据 agent 的默认模型（primary_model，格式 provider/model_id）解析模型名称（非id）
    const getPrimaryModelName = (agent: any): string => {
      const ref = agent?.config?.primary_model;
      if (!ref) return '-';
      const found = llmModels.value.find(m => `${m.provider}/${m.id}` === ref);
      if (found) return found.name;
      // 回退：模型列表尚未加载或未匹配时，去掉 provider 前缀展示
      return ref.includes('/') ? ref.split('/')[1] : ref;
    };

    return () => h('div', { style: 'height:100%;overflow-y:auto;padding:1.5rem;' }, [
      h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:1.5rem;' }, [
        h('h1', { style: 'font-size:1.5rem;font-weight:700;' }, 'Agent管理'),
        h('af-button', { variant: 'primary', onClick: () => { showCreateModal.value = true; } }, '+ 创建Agent'),
      ]),
      h('af-card', {}, [
        h('table', { style: 'width:100%;border-collapse:collapse;' }, [
          // 表头
          h('thead', {}, [
            h('tr', { style: 'background:var(--color-bg-sidebar);border-bottom:2px solid var(--color-border);' }, [
              h('th', { style: 'padding:0.75rem;text-align:left;font-weight:600;color:var(--color-text);width:200px;' }, '名称'),
              h('th', { style: 'padding:0.75rem;text-align:left;font-weight:600;color:var(--color-text);width:80px;' }, '状态'),
              h('th', { style: 'padding:0.75rem;text-align:left;font-weight:600;color:var(--color-text);width:80px;' }, '启用'),
              h('th', { style: 'padding:0.75rem;text-align:left;font-weight:600;color:var(--color-text);' }, '描述'),
              h('th', { style: 'padding:0.75rem;text-align:left;font-weight:600;color:var(--color-text);width:180px;' }, '模型'),
              h('th', { style: 'padding:0.75rem;text-align:left;font-weight:600;color:var(--color-text);' }, '创建时间'),
              h('th', { style: 'padding:0.75rem;text-align:left;font-weight:600;color:var(--color-text);width:250px;' }, '操作'),
            ]),
          ]),
          // 表体
          h('tbody', {}, agentStore.agents.map((a: any) => {
            const isEnabled = a.is_enabled !== false; // 默认为true
            return h('tr', { 
              style: 'border-bottom:1px solid var(--color-border);', 
              key: a.id,
              opacity: isEnabled ? 1 : 0.6, // 禁用的agent显示为半透明
            }, [
              h('td', { style: 'padding:0.75rem;' }, a.name || a.id.slice(0, 8)),
              h('td', { style: 'padding:0.75rem;' }, a.status),
              h('td', { style: 'padding:0.75rem;' }, 
                isEnabled ? 
                  h('span', { style: 'color:var(--color-success);font-weight:500;' }, '✓') :
                  h('span', { style: 'color:var(--color-danger);font-weight:500;' }, '✗')
              ),
              h('td', { style: 'padding:0.75rem;' }, a.description || '-'),
              h('td', { style: 'padding:0.75rem;' }, getPrimaryModelName(a)),
              h('td', { style: 'padding:0.75rem;' }, a.created_at ? new Date(a.created_at).toLocaleDateString() : '-'),
              h('td', { style: 'padding:0.75rem;' }, [
                h('button', {
                  style: 'margin-right:0.5rem;padding:0.25rem 0.75rem;border:1px solid var(--color-primary);border-radius:4px;background:var(--color-bg-card);color:var(--color-primary);cursor:pointer;font-size:0.875rem;',
                  onClick: () => openEditModal(a),
                }, '编辑'),
                isEnabled ? 
                  h('button', {
                    style: 'margin-right:0.5rem;padding:0.25rem 0.75rem;border:1px solid var(--color-warning);border-radius:4px;background:var(--color-bg-card);color:var(--color-warning);cursor:pointer;font-size:0.875rem;',
                    onClick: () => handleDisableAgent(a.id, a.name || a.id.slice(0, 8)),
                  }, '禁用') :
                  h('button', {
                    style: 'margin-right:0.5rem;padding:0.25rem 0.75rem;border:1px solid var(--color-success);border-radius:4px;background:var(--color-bg-card);color:var(--color-success);cursor:pointer;font-size:0.875rem;',
                    onClick: () => handleEnableAgent(a.id, a.name || a.id.slice(0, 8)),
                  }, '启用'),
                h('button', {
                  style: 'padding:0.25rem 0.75rem;border:1px solid var(--color-danger);border-radius:4px;background:var(--color-bg-card);color:var(--color-danger);cursor:pointer;font-size:0.875rem;',
                  onClick: () => handleDeleteAgent(a.id, a.name || a.id.slice(0, 8)),
                }, '删除'),
              ]),
            ]);
          })),
        ]),
      ]),
      
      // ✅ 创建Agent模态框
      showCreateModal.value ? h('div', {
        style: 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:1000;',
        onClick: () => { showCreateModal.value = false; },
      }, [
        h('div', {
          style: 'background:var(--color-bg-card);border-radius:8px;padding:2rem;width:600px;max-width:90%;box-shadow:0 4px 6px rgba(0,0,0,0.1);max-height:90vh;overflow-y:auto;',
          onClick: (e: MouseEvent) => e.stopPropagation(),
        }, [
          h('h2', { style: 'font-size:1.25rem;font-weight:700;margin-bottom:1.5rem;' }, '创建新Agent'),
          
          // Agent名称
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, 'Agent名称 *'),
            h('input', {
              type: 'text',
              value: newAgentForm.value.name,
              placeholder: '例如：数据分析助手、代码审查员',
              style: 'width:100%;padding:0.5rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);',
              onInput: (e: Event) => { newAgentForm.value.name = (e.target as HTMLInputElement).value; },
            }),
          ]),
          
          // 描述
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, '描述'),
            h('textarea', {
              value: newAgentForm.value.description,
              placeholder: '简要描述这个Agent的用途',
              rows: 2,
              style: 'width:100%;padding:0.5rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);resize:vertical;',
              onInput: (e: Event) => { newAgentForm.value.description = (e.target as HTMLTextAreaElement).value; },
            }),
          ]),
          
          // 系统提示词
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, '系统提示词'),
            h('textarea', {
              value: newAgentForm.value.system_prompt,
              placeholder: '定义Agent的行为和角色，例如：你是一个专业的数据分析助手...',
              rows: 4,
              style: 'width:100%;padding:0.5rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);resize:vertical;font-family:monospace;',
              onInput: (e: Event) => { newAgentForm.value.system_prompt = (e.target as HTMLTextAreaElement).value; },
            }),
          ]),
          
          // LLM模型（多选 + 默认模型）
          ...renderModelSelector(newAgentForm.value),
          
          // 温度参数
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, `Temperature: ${newAgentForm.value.temperature}`),
            h('input', {
              type: 'range',
              min: 0,
              max: 2,
              step: 0.1,
              value: newAgentForm.value.temperature,
              style: 'width:100%;',
              onInput: (e: Event) => { newAgentForm.value.temperature = parseFloat((e.target as HTMLInputElement).value); },
            }),
          ]),
          
          // 按钮组
          h('div', { style: 'display:flex;gap:0.75rem;margin-top:1.5rem;' }, [
            h('button', {
              type: 'button',
              style: 'flex:1;padding:0.75rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);background:var(--color-bg-card);cursor:pointer;',
              onClick: () => { showCreateModal.value = false; resetCreateForm(); },
            }, '取消'),
            h('button', {
              type: 'button',
              style: 'flex:1;padding:0.75rem;border:none;border-radius:4px;background:var(--color-primary);color:white;cursor:pointer;font-weight:500;',
              onClick: handleCreateAgent,
            }, '创建Agent'),
          ]),
        ]),
      ]) : null,
      
      // ✅ 编辑Agent模态框
      showEditModal.value ? h('div', {
        style: 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:1000;',
        onClick: () => { showEditModal.value = false; },
      }, [
        h('div', {
          style: 'background:var(--color-bg-card);border-radius:8px;padding:2rem;width:600px;max-width:90%;box-shadow:0 4px 6px rgba(0,0,0,0.1);max-height:90vh;overflow-y:auto;',
          onClick: (e: MouseEvent) => e.stopPropagation(),
        }, [
          h('h2', { style: 'font-size:1.25rem;font-weight:700;margin-bottom:1.5rem;' }, '编辑Agent'),
          
          // Agent名称
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, 'Agent名称 *'),
            h('input', {
              type: 'text',
              value: editAgentForm.value.name,
              placeholder: '例如：数据分析助手、代码审查员',
              style: 'width:100%;padding:0.5rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);',
              onInput: (e: Event) => { editAgentForm.value.name = (e.target as HTMLInputElement).value; },
            }),
          ]),
          
          // 描述
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, '描述'),
            h('textarea', {
              value: editAgentForm.value.description,
              placeholder: '简要描述这个Agent的用途',
              rows: 2,
              style: 'width:100%;padding:0.5rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);resize:vertical;',
              onInput: (e: Event) => { editAgentForm.value.description = (e.target as HTMLTextAreaElement).value; },
            }),
          ]),
          
          // 系统提示词
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, '系统提示词'),
            h('textarea', {
              value: editAgentForm.value.system_prompt,
              placeholder: '定义Agent的行为和角色，例如：你是一个专业的数据分析助手...',
              rows: 4,
              style: 'width:100%;padding:0.5rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);resize:vertical;font-family:monospace;',
              onInput: (e: Event) => { editAgentForm.value.system_prompt = (e.target as HTMLTextAreaElement).value; },
            }),
          ]),
          
          // LLM模型（多选 + 默认模型）
          ...renderModelSelector(editAgentForm.value),
          
          // 温度参数
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('label', { style: 'display:block;margin-bottom:0.5rem;font-weight:500;' }, `Temperature: ${editAgentForm.value.temperature}`),
            h('input', {
              type: 'range',
              min: 0,
              max: 2,
              step: 0.1,
              value: editAgentForm.value.temperature,
              style: 'width:100%;',
              onInput: (e: Event) => { editAgentForm.value.temperature = parseFloat((e.target as HTMLInputElement).value); },
            }),
          ]),
          
          // 按钮组
          h('div', { style: 'display:flex;gap:0.75rem;margin-top:1.5rem;' }, [
            h('button', {
              type: 'button',
              style: 'flex:1;padding:0.75rem;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-input);color:var(--color-text);background:var(--color-bg-card);cursor:pointer;',
              onClick: () => { showEditModal.value = false; resetEditForm(); editingAgent.value = null; },
            }, '取消'),
            h('button', {
              type: 'button',
              style: 'flex:1;padding:0.75rem;border:none;border-radius:4px;background:var(--color-primary);color:white;cursor:pointer;font-weight:500;',
              onClick: handleUpdateAgent,
            }, '保存修改'),
          ]),
        ]),
      ]) : null,
    ]);
  },
});
