/**
 * 系统设置页面 - LLM配置管理
 */
import { defineComponent, h, ref, onMounted } from 'vue';
import { useSettingsStore, LLMProvider, LLMModelConfig, ModelCost, PeakTime, TimeRange } from '../stores/settings';
import { useMCPStore, MCPServerConfig, MCPServerResponse } from '../stores/mcp';
import { useToolsStore, ToolItem } from '../stores/tools';
import { useSkillsStore, SkillItem } from '../stores/skills';

const API_BASE = '/api/v1';

// 通用样式常量
const S = {
  input: 'width:100%;padding:10px 12px;border:1px solid var(--color-border-dark);border-radius:6px;font-size:14px;box-sizing:border-box;',
  smallInput: 'width:100%;padding:8px 10px;border:1px solid var(--color-border-dark);border-radius:6px;font-size:13px;margin-top:4px;box-sizing:border-box;',
  label: 'display:block;font-size:14px;margin-bottom:6px;color:var(--color-text);font-weight:500;',
  smallLabel: 'font-size:12px;color:var(--color-text-secondary);',
  section: 'margin-bottom:16px;padding:16px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-bg-panel);',
  sectionTitle: 'display:block;font-size:14px;font-weight:600;margin-bottom:12px;color:var(--color-text);',
  checkbox: 'display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;color:var(--color-text);',
  checkboxInput: 'width:16px;height:16px;cursor:pointer;',
  btnPrimary: 'padding:10px 20px;background:var(--color-primary);color:white;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:500;',
  btnSecondary: 'padding:10px 20px;border:1px solid var(--color-border-dark);border-radius:6px;background:var(--color-bg-card);cursor:pointer;font-size:14px;color:var(--color-text);',
  btnSmall: 'padding:6px 12px;font-size:12px;border:1px solid var(--color-border-dark);border-radius:4px;background:var(--color-bg-card);cursor:pointer;color:var(--color-text);',
  btnDanger: 'padding:6px 12px;font-size:12px;border:1px solid var(--color-danger);border-radius:4px;background:var(--color-bg-card);color:var(--color-danger);cursor:pointer;',
  overlay: 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:1000;backdrop-filter:blur(2px);',
  modal: 'background:var(--color-bg-card);border-radius:12px;padding:24px;max-height:85vh;overflow-y:auto;box-shadow:0 20px 25px -5px rgba(0,0,0,0.1),0 10px 10px -5px rgba(0,0,0,0.04);',
  tag: 'display:inline-block;padding:2px 8px;font-size:11px;border-radius:4px;font-weight:500;',
};

const emptyCost = (): ModelCost => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, explicitCacheInput: 0, explicitCacheRead: 0 });

const emptyModel = (): LLMModelConfig => ({
  id: '', name: '', reasoning: false, autoCreateExplicitCache: false, input: ['text'], billingMode: null,
  cost: emptyCost(), costInputStep: null, contextWindow: 128000, maxTokens: 4096,
  compat: { supportsReasoningEffort: false, supportsUsageInStreaming: false, maxTokensField: 'max_tokens', nativeWebSearchTool: false },
  api: 'openai-completions',
});

export default defineComponent({
  name: 'Settings',
  setup() {
    const settingsStore = useSettingsStore();
    const mcpStore = useMCPStore();
    const toolsStore = useToolsStore();
    const skillsStore = useSkillsStore();
    const activeTab = ref('llm');
    
    // Provider表单
    const showProviderModal = ref(false);
    const editingProvider = ref<string | null>(null);
    const providerForm = ref({
      name: '', baseUrl: '', apiKey: '',
      billingMode: [] as string[],
      peakTime: null as PeakTime | null,
    });
    
    // Model表单
    const showModelModal = ref(false);
    const editingModelId = ref<string | null>(null);
    const modelProviderName = ref('');
    const modelForm = ref<LLMModelConfig>(emptyModel());
    const modelOtherJson = ref('');  // other字段的原始JSON文本（自由输入，保存时校验格式）
    
    const expandedProviders = ref<Set<string>>(new Set());

    // === MCP 表单 ===
    const showServerModal = ref(false);
    const editingServerName = ref<string | null>(null);
    const serverForm = ref<MCPServerConfig>({
      name: '', type: 'http', enabled: true,
    });

    // === 工具管理 ===
    const toolsSubTab = ref<'list' | 'settings'>('list');
    // 《设置》页临时表单（编辑后点保存才提交）
    const toolTimeoutForm = ref<{ default: number; overrides: Record<string, any> }>({ default: 30, overrides: {} });
    const toolRateLimitForm = ref<{ enabled: boolean; requests_per_minute: number }>({ enabled: false, requests_per_minute: 60 });
    const subagentForm = ref<{ disallowed: Record<string, string>; allowed: Record<string, string> }>({ disallowed: {}, allowed: {} });
    const teamForm = ref<{ disallowed: string; allowed: string }>({ disallowed: '', allowed: '' });

    // === 技能管理 ===
    const skillsSubTab = ref<'list' | 'settings'>('list');
    // 《设置》页各 agent 技能临时表单（key=agent名）
    const agentSkillsForm = ref<Record<string, { disallowed: string; allowed: string }>>({});

    // === 记忆配置 ===
    const memoryForm = ref({
      enabled: true,
      root: '.happy-claw/memory',
      mode: 'standard',
      llm: { provider: 'aliyun', model: 'qwen3.8-max' },
      scan_max_files: 200,
      select_max: 5,
      per_file_max_lines: 200,
      per_file_max_bytes: 4096,
      session_inject_budget_bytes: 61440,
      index_max_lines: 200,
      index_max_bytes: 25600,
      freshness_warn_days: 1,
      extract_min_interval_seconds: 300,
      extract_max_rounds: 5,
      extract_min_content_chars: 200,
      extract_min_user_messages: 5,
      consolidate_min_interval_hours: 24,
      consolidate_min_new_conversations: 5,
      consolidate_lock_ttl_seconds: 3600,
      lock_ttl_seconds: 600,
      consolidate_scan_throttle_seconds: 600,
      prefetch_collect_timeout_seconds: 8,
      session_memory: {
        enabled: true,
        first_update_tokens: 10000,
        increment_tokens: 5000,
        tool_calls_threshold: 3,
        natural_pause_turns: 2,
        summary_min_tokens: 10000,
        summary_max_tokens: 40000,
        summary_min_messages: 5,
      },
    });
    const memoryLoading = ref(false);

    onMounted(() => {
      settingsStore.fetchProviders();
      mcpStore.fetchConfig();
      toolsStore.fetchConfig();
      skillsStore.fetchConfig();
      fetchMemoryConfig();
    });

    // 获取记忆配置
    const fetchMemoryConfig = async () => {
      memoryLoading.value = true;
      try {
        const res = await fetch(`${API_BASE}/settings/memory`);
        if (res.ok) {
          const data = await res.json();
          memoryForm.value = {
            enabled: data.enabled ?? true,
            root: data.root ?? '.happy-claw/memory',
            mode: data.mode ?? 'standard',
            llm: data.llm ?? { provider: 'aliyun', model: 'qwen3.8-max' },
            scan_max_files: data.scan_max_files ?? 200,
            select_max: data.select_max ?? 5,
            per_file_max_lines: data.per_file_max_lines ?? 200,
            per_file_max_bytes: data.per_file_max_bytes ?? 4096,
            session_inject_budget_bytes: data.session_inject_budget_bytes ?? 61440,
            index_max_lines: data.index_max_lines ?? 200,
            index_max_bytes: data.index_max_bytes ?? 25600,
            freshness_warn_days: data.freshness_warn_days ?? 1,
            extract_min_interval_seconds: data.extract_min_interval_seconds ?? 300,
            extract_max_rounds: data.extract_max_rounds ?? 5,
            extract_min_content_chars: data.extract_min_content_chars ?? 200,
            extract_min_user_messages: data.extract_min_user_messages ?? 5,
            consolidate_min_interval_hours: data.consolidate_min_interval_hours ?? 24,
            consolidate_min_new_conversations: data.consolidate_min_new_conversations ?? 5,
            consolidate_lock_ttl_seconds: data.consolidate_lock_ttl_seconds ?? 3600,
            lock_ttl_seconds: data.lock_ttl_seconds ?? 600,
            consolidate_scan_throttle_seconds: data.consolidate_scan_throttle_seconds ?? 600,
            prefetch_collect_timeout_seconds: data.prefetch_collect_timeout_seconds ?? 8,
            session_memory: {
              enabled: data.session_memory?.enabled ?? true,
              first_update_tokens: data.session_memory?.first_update_tokens ?? 10000,
              increment_tokens: data.session_memory?.increment_tokens ?? 5000,
              tool_calls_threshold: data.session_memory?.tool_calls_threshold ?? 3,
              natural_pause_turns: data.session_memory?.natural_pause_turns ?? 2,
              summary_min_tokens: data.session_memory?.summary_min_tokens ?? 10000,
              summary_max_tokens: data.session_memory?.summary_max_tokens ?? 40000,
              summary_min_messages: data.session_memory?.summary_min_messages ?? 5,
            },
          };
        }
      } catch (e) {
        console.error('Failed to fetch memory config:', e);
      } finally {
        memoryLoading.value = false;
      }
    };

    // 保存记忆配置
    const saveMemoryConfig = async () => {
      try {
        const res = await fetch(`${API_BASE}/settings/memory`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(memoryForm.value),
        });
        if (res.ok) {
          alert('记忆配置已保存');
        } else {
          alert('保存失败');
        }
      } catch (e) {
        console.error('Failed to save memory config:', e);
        alert('保存失败');
      }
    };

    // === Provider操作 ===
    const openAddProvider = () => {
      editingProvider.value = null;
      providerForm.value = { name: '', baseUrl: '', apiKey: '', billingMode: [], peakTime: null };
      showProviderModal.value = true;
    };

    const openEditProvider = (provider: LLMProvider) => {
      editingProvider.value = provider.name;
      providerForm.value = {
        name: provider.name,
        baseUrl: provider.config.baseUrl,
        apiKey: provider.config.apiKey,
        billingMode: provider.config.billingMode ? [...provider.config.billingMode] : [],
        peakTime: provider.config.peakTime ? JSON.parse(JSON.stringify(provider.config.peakTime)) : null,
      };
      showProviderModal.value = true;
    };

    const saveProvider = async () => {
      if (!providerForm.value.name.trim() || !providerForm.value.baseUrl.trim()) {
        alert('请填写Provider名称和Base URL'); return;
      }
      const payload = {
        baseUrl: providerForm.value.baseUrl,
        apiKey: providerForm.value.apiKey,
        billingMode: providerForm.value.billingMode.length > 0 ? providerForm.value.billingMode : null,
        peakTime: providerForm.value.peakTime,
      };
      const success = editingProvider.value
        ? await settingsStore.updateProvider(editingProvider.value, payload)
        : await settingsStore.addProvider({ name: providerForm.value.name, ...payload });
      if (success) showProviderModal.value = false;
      else alert(settingsStore.error || '保存失败');
    };

    const deleteProvider = async (name: string) => {
      if (!confirm(`确定要删除Provider "${name}" 吗？`)) return;
      await settingsStore.deleteProvider(name);
    };

    // === Model操作 ===
    const openAddModel = (providerName: string) => {
      modelProviderName.value = providerName;
      editingModelId.value = null;
      modelForm.value = emptyModel();
      modelOtherJson.value = '';
      showModelModal.value = true;
    };

    const openEditModel = (providerName: string, model: LLMModelConfig) => {
      modelProviderName.value = providerName;
      editingModelId.value = model.id;
      modelForm.value = JSON.parse(JSON.stringify(model));
      modelOtherJson.value = model.other ? JSON.stringify(model.other, null, 2) : '';
      showModelModal.value = true;
    };

    // 美化 other 字段的 JSON：先校验格式，合法则格式化(2空格缩进)回填，非法则提示且不改动
    const formatOtherJson = () => {
      const text = modelOtherJson.value.trim();
      if (!text) { alert('other 字段为空，无需美化'); return; }
      try {
        const parsed = JSON.parse(text);
        modelOtherJson.value = JSON.stringify(parsed, null, 2);
      } catch (e: any) {
        alert('JSON 格式错误，无法美化：' + (e?.message || '无法解析'));
      }
    };

    const saveModel = async () => {
      if (!modelForm.value.id.trim() || !modelForm.value.name.trim()) {
        alert('请填写Model ID和名称'); return;
      }
      // 校验 other 自定义JSON：留空表示无；非空必须是合法JSON对象
      const otherText = modelOtherJson.value.trim();
      let otherVal: Record<string, any> | null = null;
      if (otherText) {
        try {
          const parsed = JSON.parse(otherText);
          if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
            alert('other 字段必须是合法的 JSON 对象（形如 {"key": "value"}）'); return;
          }
          otherVal = parsed as Record<string, any>;
        } catch (e: any) {
          alert('other 字段 JSON 格式错误：' + (e?.message || '无法解析')); return;
        }
      }
      const payload = { ...modelForm.value };
      payload.other = otherVal || (undefined as any);
      // 清理空值
      if (!payload.billingMode || payload.billingMode.length === 0) payload.billingMode = undefined as any;
      if (!payload.costInputStep || Object.keys(payload.costInputStep).length === 0) payload.costInputStep = undefined as any;
      const success = editingModelId.value
        ? await settingsStore.updateModel(modelProviderName.value, editingModelId.value, payload)
        : await settingsStore.addModel(modelProviderName.value, payload);
      if (success) showModelModal.value = false;
      else alert(settingsStore.error || '保存失败');
    };

    const deleteModel = async (providerName: string, modelId: string) => {
      if (!confirm(`确定要删除Model "${modelId}" 吗？`)) return;
      await settingsStore.deleteModel(providerName, modelId);
    };

    const toggleProvider = (name: string) => {
      if (expandedProviders.value.has(name)) expandedProviders.value.delete(name);
      else expandedProviders.value.add(name);
    };

    // === MCP 操作 ===
    const openAddServer = () => {
      editingServerName.value = null;
      serverForm.value = { name: '', type: 'http', enabled: true };
      showServerModal.value = true;
    };

    const openEditServer = (server: MCPServerResponse) => {
      editingServerName.value = server.name;
      serverForm.value = {
        name: server.name,
        type: server.type,
        enabled: server.enabled,
        command: server.command || '',
        args: server.args ? [...server.args] : [],
        env: server.headers ? { ...server.headers } : {},
        url: server.url || '',
        headers: server.headers ? { ...server.headers } : {},
        module: server.module || '',
        oauth: server.oauth ? { ...server.oauth } as any : undefined,
        description: server.description || '',
      };
      showServerModal.value = true;
    };

    const saveServer = async () => {
      const form = serverForm.value;
      if (!form.name.trim()) {
        alert('请填写服务器名称'); return;
      }
      // 按 transport 类型清理无效字段
      const payload: MCPServerConfig = { ...form };
      if (form.type === 'stdio') {
        if (!form.command) { alert('stdio 类型需要填写命令'); return; }
        delete (payload as any).url;
        delete (payload as any).module;
      } else if (form.type === 'sdk') {
        if (!form.module) { alert('sdk 类型需要填写模块路径'); return; }
        delete (payload as any).command;
        delete (payload as any).args;
        delete (payload as any).url;
      } else {
        if (!form.url) { alert(`${form.type} 类型需要填写 URL`); return; }
        delete (payload as any).command;
        delete (payload as any).args;
        delete (payload as any).module;
      }
      // 清理空字符串
      if (!payload.command) delete payload.command;
      if (!payload.url) delete payload.url;
      if (!payload.module) delete payload.module;
      if (!payload.description) delete payload.description;
      if (payload.args && payload.args.length === 0) delete payload.args;

      const success = editingServerName.value
        ? await mcpStore.updateServer(editingServerName.value, payload)
        : await mcpStore.addServer(payload);
      if (success) showServerModal.value = false;
      else alert(mcpStore.error || '保存失败');
    };

    const deleteServer = async (name: string) => {
      if (!confirm(`确定要删除 MCP 服务器 "${name}" 吗？`)) return;
      await mcpStore.deleteServer(name);
    };

    const reloadMCP = async () => {
      const result = await mcpStore.reload();
      if (result) {
        alert(`MCP 配置已重载\n新增: ${result.added || 0}\n移除: ${result.removed || 0}\n重连: ${result.reloaded || 0}`);
      }
    };

    // === MCP 状态标签 ===
    const statusTag = (status?: string) => {
      const map: Record<string, [string, string]> = {
        connected: ['var(--color-success-light)', 'var(--color-success-text)'],
        connecting: ['var(--color-warning-light)', 'var(--color-warning-text)'],
        disconnected: ['var(--color-bg-hover)', 'var(--color-text-secondary)'],
        error: ['var(--color-danger-light)', 'var(--color-danger-text)'],
        auth_required: ['#fce7f3', '#9d174d'],
      };
      const [bg, color] = map[status || ''] || ['var(--color-bg-hover)', 'var(--color-text-secondary)'];
      const label = status === 'connected' ? '已连接' : status === 'connecting' ? '连接中' : status === 'error' ? '错误' : status === 'auth_required' ? '需认证' : '未连接';
      return h('span', { style: `${S.tag}background:${bg};color:${color};` }, label);
    };

    // === 渲染辅助 ===
    const renderBillingTag = (modes?: string[] | null) => {
      if (!modes || modes.length === 0) return null;
      return h('span', { style: `${S.tag}background:var(--color-info-light);color:var(--color-info-text);margin-left:8px;` }, modes.join(', '));
    };

    const renderCostFields = (cost: ModelCost, onUpdate: (field: string, val: number) => void, showExplicit: boolean = true) => {
      const fields: [string, string][] = [
        ['input', '输入'], ['output', '输出'], ['cacheRead', '隐式缓存读取'], ['cacheWrite', '缓存写入'],
      ];
      if (showExplicit) {
        fields.push(['explicitCacheInput', '显式缓存创建'], ['explicitCacheRead', '显式缓存命中']);
      }
      return h('div', { style: 'display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;' },
        fields.map(([key, label]) => h('div', {}, [
          h('label', { style: S.smallLabel }, label),
          h('input', {
            type: 'number', step: '0.001', value: (cost as any)[key],
            style: S.smallInput,
            onInput: (e: Event) => onUpdate(key, parseFloat((e.target as HTMLInputElement).value) || 0),
          }),
        ]))
      );
    };

    // === Provider卡片 ===
    const renderProviderCard = (provider: LLMProvider) => {
      const isExpanded = expandedProviders.value.has(provider.name);
      const modelCount = provider.config.models?.length || 0;
      
      return h('div', { style: 'border:1px solid var(--color-border);border-radius:8px;margin-bottom:16px;overflow:hidden;background:var(--color-bg-card);box-shadow:0 1px 3px rgba(0,0,0,0.1);' }, [
        h('div', {
          style: 'display:flex;justify-content:space-between;align-items:center;padding:16px;background:var(--color-bg-panel);cursor:pointer;border-bottom:1px solid var(--color-border);',
          onClick: () => toggleProvider(provider.name),
        }, [
          h('div', {}, [
            h('div', { style: 'display:flex;align-items:center;' }, [
              h('span', { style: 'font-weight:600;font-size:16px;color:var(--color-text);' }, provider.name),
              renderBillingTag(provider.config.billingMode),
            ]),
            h('div', { style: 'font-size:12px;color:var(--color-text-secondary);margin-top:4px;' }, [
              h('span', {}, provider.config.baseUrl),
              h('span', { style: 'margin-left:16px;' }, `${modelCount} 个模型`),
            ]),
          ]),
          h('div', { style: 'display:flex;gap:8px;align-items:center;' }, [
            h('button', { style: S.btnSmall, onClick: (e: Event) => { e.stopPropagation(); openEditProvider(provider); } }, '编辑'),
            h('button', { style: S.btnDanger, onClick: (e: Event) => { e.stopPropagation(); deleteProvider(provider.name); } }, '删除'),
            h('span', { style: 'font-size:14px;color:var(--color-text-secondary);' }, isExpanded ? '▼' : '▶'),
          ]),
        ]),
        isExpanded ? h('div', { style: 'padding:16px;background:var(--color-bg-panel);' }, [
          h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;' }, [
            h('span', { style: 'font-weight:500;font-size:14px;color:var(--color-text);' }, '模型列表'),
            h('button', { style: 'padding:8px 16px;font-size:13px;background:var(--color-primary);color:white;border:none;border-radius:6px;cursor:pointer;font-weight:500;', onClick: () => openAddModel(provider.name) }, '+ 添加模型'),
          ]),
          ...(provider.config.models || []).map(model => renderModelRow(provider.name, model)),
          modelCount === 0 ? h('div', { style: 'text-align:center;padding:24px;color:var(--color-text-disabled);font-size:14px;background:var(--color-bg-card);border-radius:8px;border:1px dashed var(--color-border-dark);' }, '暂无模型') : null,
        ]) : null,
      ]);
    };

    // === Model行 ===
    const renderModelRow = (providerName: string, model: LLMModelConfig) => {
      return h('div', {
        key: model.id,
        style: 'display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border:1px solid var(--color-border);border-radius:8px;margin-bottom:8px;background:var(--color-bg-card);',
      }, [
        h('div', { style: 'flex:1;' }, [
          h('div', { style: 'display:flex;align-items:center;' }, [
            h('span', { style: 'font-weight:500;font-size:14px;color:var(--color-text);' }, model.name),
            renderBillingTag(model.billingMode),
            model.reasoning ? h('span', { style: `${S.tag}background:var(--color-warning-light);color:var(--color-warning-text);margin-left:8px;` }, '推理') : null,
          ]),
          h('div', { style: 'font-size:12px;color:var(--color-text-secondary);margin-top:4px;' }, [
            h('span', {}, `ID: ${model.id}`),
            h('span', { style: 'margin-left:16px;' }, `上下文: ${model.contextWindow.toLocaleString()}`),
            h('span', { style: 'margin-left:16px;' }, `最大输出: ${model.maxTokens.toLocaleString()}`),
          ]),
        ]),
        h('div', { style: 'display:flex;gap:8px;' }, [
          h('button', { style: S.btnSmall, onClick: () => openEditModel(providerName, model) }, '编辑'),
          h('button', { style: S.btnDanger, onClick: () => deleteModel(providerName, model.id) }, '删除'),
        ]),
      ]);
    };

    // === Provider弹窗 ===
    const renderProviderModal = () => {
      if (!showProviderModal.value) return null;
      const form = providerForm.value;
      
      const toggleBillingMode = (mode: string) => {
        const idx = form.billingMode.indexOf(mode);
        if (idx >= 0) form.billingMode.splice(idx, 1);
        else form.billingMode.push(mode);
      };

      // peakTime辅助
      const ensurePeakTime = () => {
        if (!form.peakTime) form.peakTime = { weekdays: [1,2,3,4,5], timeRange: [{ start: '09:00', end: '18:00' }] };
      };

      return h('div', { style: S.overlay }, [
        h('div', { style: `${S.modal}width:520px;` }, [
          h('h3', { style: 'font-size:18px;font-weight:600;margin-bottom:20px;color:var(--color-text);' },
            editingProvider.value ? '编辑Provider' : '添加Provider'),
          
          // 名称
          h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: S.label }, '名称 *'),
            h('input', { type: 'text', value: form.name, disabled: !!editingProvider.value, placeholder: '如: openai, deepseek', style: S.input, onInput: (e: Event) => { form.name = (e.target as HTMLInputElement).value; } }),
          ]),
          // Base URL
          h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: S.label }, 'Base URL *'),
            h('input', { type: 'text', value: form.baseUrl, placeholder: 'https://api.openai.com/v1', style: S.input, onInput: (e: Event) => { form.baseUrl = (e.target as HTMLInputElement).value; } }),
          ]),
          // API Key
          h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: S.label }, 'API Key'),
            h('input', { type: 'password', value: form.apiKey, placeholder: 'sk-...', style: S.input, onInput: (e: Event) => { form.apiKey = (e.target as HTMLInputElement).value; } }),
          ]),
          // 计费模式
          h('div', { style: S.section }, [
            h('label', { style: S.sectionTitle }, '计费模式'),
            h('div', { style: 'display:flex;gap:16px;' }, [
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: form.billingMode.includes('peakTime'), style: S.checkboxInput, onChange: () => toggleBillingMode('peakTime') }),
                'peakTime（高峰/空闲分段计费）',
              ]),
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: form.billingMode.includes('inputStep'), style: S.checkboxInput, onChange: () => toggleBillingMode('inputStep') }),
                'inputStep（按输入量阶梯计费）',
              ]),
            ]),
          ]),
          // peakTime配置
          form.billingMode.includes('peakTime') ? h('div', { style: S.section }, [
            h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;' }, [
              h('label', { style: S.sectionTitle.replace('margin-bottom:12px;', '') }, '高峰时段配置'),
            ]),
            // weekdays
            h('div', { style: 'margin-bottom:12px;' }, [
              h('label', { style: S.smallLabel }, '高峰工作日'),
              h('div', { style: 'display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;' },
                [1,2,3,4,5,6,7].map(d => {
                  const dayNames = ['一','二','三','四','五','六','日'];
                  const isActive = (form.peakTime?.weekdays || [1,2,3,4,5]).includes(d);
                  return h('button', {
                    style: `padding:4px 10px;font-size:12px;border-radius:4px;cursor:pointer;border:1px solid ${isActive ? 'var(--color-primary)' : 'var(--color-border-dark)'};background:${isActive ? 'var(--color-primary-lighter)' : 'var(--color-bg-card)'};color:${isActive ? 'var(--color-primary)' : 'var(--color-text-secondary)'};`,
                    onClick: () => {
                      ensurePeakTime();
                      const wd = form.peakTime!.weekdays;
                      const idx = wd.indexOf(d);
                      if (idx >= 0) wd.splice(idx, 1); else wd.push(d);
                    },
                  }, dayNames[d-1]);
                })
              ),
            ]),
            // timeRange
            h('div', {}, [
              h('div', { style: 'display:flex;justify-content:space-between;align-items:center;' }, [
                h('label', { style: S.smallLabel }, '时间段'),
                h('button', { style: 'padding:2px 8px;font-size:11px;border:1px solid var(--color-primary);border-radius:4px;background:var(--color-bg-card);color:var(--color-primary);cursor:pointer;', onClick: () => { ensurePeakTime(); form.peakTime!.timeRange.push({ start: '09:00', end: '18:00' }); } }, '+ 添加'),
              ]),
              ...(form.peakTime?.timeRange || []).map((tr, i) => h('div', { key: i, style: 'display:flex;gap:8px;align-items:center;margin-top:6px;' }, [
                h('input', { type: 'time', value: tr.start, style: 'padding:4px 8px;border:1px solid var(--color-border-dark);border-radius:4px;font-size:12px;', onInput: (e: Event) => { ensurePeakTime(); form.peakTime!.timeRange[i].start = (e.target as HTMLInputElement).value; } }),
                h('span', { style: 'font-size:12px;color:var(--color-text-secondary);' }, '至'),
                h('input', { type: 'time', value: tr.end, style: 'padding:4px 8px;border:1px solid var(--color-border-dark);border-radius:4px;font-size:12px;', onInput: (e: Event) => { ensurePeakTime(); form.peakTime!.timeRange[i].end = (e.target as HTMLInputElement).value; } }),
                h('button', { style: 'padding:2px 6px;font-size:11px;border:1px solid var(--color-danger);border-radius:4px;background:var(--color-bg-card);color:var(--color-danger);cursor:pointer;', onClick: () => { ensurePeakTime(); form.peakTime!.timeRange.splice(i, 1); } }, '×'),
              ])),
            ]),
          ]) : null,
          // 按钮
          h('div', { style: 'display:flex;justify-content:flex-end;gap:12px;margin-top:24px;padding-top:16px;border-top:1px solid var(--color-border);' }, [
            h('button', { style: S.btnSecondary, onClick: () => { showProviderModal.value = false; } }, '取消'),
            h('button', { style: S.btnPrimary, onClick: saveProvider }, '保存'),
          ]),
        ]),
      ]);
    };

    // === Model弹窗 ===
    const renderModelModal = () => {
      if (!showModelModal.value) return null;
      const form = modelForm.value;
      const modelBillingModes = form.billingMode || [];

      const toggleModelBilling = (mode: string) => {
        if (!form.billingMode) form.billingMode = [];
        const idx = form.billingMode.indexOf(mode);
        if (idx >= 0) form.billingMode.splice(idx, 1);
        else form.billingMode.push(mode);
      };

      // costInputStep辅助
      const getStepEntries = (): [string, ModelCost][] => {
        if (!form.costInputStep) return [];
        return Object.entries(form.costInputStep);
      };
      const addStep = () => {
        if (!form.costInputStep) form.costInputStep = {};
        // 生成不重复的新区间key
        const existing = Object.keys(form.costInputStep);
        let idx = existing.length + 1;
        let newKey = `(0, ${idx * 32}k]`;
        while (existing.includes(newKey)) {
          idx++;
          newKey = `(0, ${idx * 32}k]`;
        }
        form.costInputStep[newKey] = emptyCost();
      };
      const removeStep = (key: string) => {
        if (form.costInputStep) delete form.costInputStep[key];
      };
      const updateStepKey = (oldKey: string, newKey: string) => {
        if (!form.costInputStep) return;
        const entries = Object.entries(form.costInputStep);
        form.costInputStep = {};
        for (const [k, v] of entries) {
          form.costInputStep[k === oldKey ? newKey : k] = v;
        }
      };

      return h('div', { style: S.overlay }, [
        h('div', { style: `${S.modal}width:620px;` }, [
          h('h3', { style: 'font-size:18px;font-weight:600;margin-bottom:20px;color:var(--color-text);' },
            editingModelId.value ? '编辑模型' : '添加模型'),
          
          // ID + 名称
          h('div', { style: 'display:flex;gap:16px;margin-bottom:16px;' }, [
            h('div', { style: 'flex:1;' }, [
              h('label', { style: S.label }, '模型ID *'),
              h('input', { type: 'text', value: form.id, disabled: !!editingModelId.value, placeholder: '如: gpt-4-turbo', style: S.input, onInput: (e: Event) => { form.id = (e.target as HTMLInputElement).value; } }),
            ]),
            h('div', { style: 'flex:1;' }, [
              h('label', { style: S.label }, '显示名称 *'),
              h('input', { type: 'text', value: form.name, placeholder: '如: GPT-4 Turbo', style: S.input, onInput: (e: Event) => { form.name = (e.target as HTMLInputElement).value; } }),
            ]),
          ]),
          // 上下文窗口 + 最大Token
          h('div', { style: 'display:flex;gap:16px;margin-bottom:16px;' }, [
            h('div', { style: 'flex:1;' }, [
              h('label', { style: S.label }, '上下文窗口'),
              h('input', { type: 'number', value: form.contextWindow, style: S.input, onInput: (e: Event) => { form.contextWindow = parseInt((e.target as HTMLInputElement).value) || 128000; } }),
            ]),
            h('div', { style: 'flex:1;' }, [
              h('label', { style: S.label }, '最大输出Token'),
              h('input', { type: 'number', value: form.maxTokens, style: S.input, onInput: (e: Event) => { form.maxTokens = parseInt((e.target as HTMLInputElement).value) || 4096; } }),
            ]),
          ]),
          // 计费模式（模型级覆盖）
          h('div', { style: S.section }, [
            h('label', { style: S.sectionTitle }, '计费模式（模型级覆盖，留空则继承Provider配置）'),
            h('div', { style: 'display:flex;gap:16px;' }, [
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: modelBillingModes.includes('peakTime'), style: S.checkboxInput, onChange: () => toggleModelBilling('peakTime') }),
                'peakTime（高峰/空闲分段）',
              ]),
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: modelBillingModes.includes('inputStep'), style: S.checkboxInput, onChange: () => toggleModelBilling('inputStep') }),
                'inputStep（阶梯计费）',
              ]),
            ]),
          ]),
          // 默认成本配置
          h('div', { style: S.section }, [
            h('label', { style: S.sectionTitle }, '默认成本配置（每百万Token单价）'),
            renderCostFields(form.cost, (field, val) => { (form.cost as any)[field] = val; }),
          ]),
          // 阶梯计费配置
          modelBillingModes.includes('inputStep') ? h('div', { style: S.section }, [
            h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;' }, [
              h('label', { style: 'font-size:14px;font-weight:600;color:var(--color-text);' }, '阶梯计费配置'),
              h('button', { style: 'padding:4px 12px;font-size:12px;border:1px solid var(--color-primary);border-radius:4px;background:var(--color-bg-card);color:var(--color-primary);cursor:pointer;', onClick: addStep }, '+ 添加阶梯'),
            ]),
            h('p', { style: 'font-size:12px;color:var(--color-text-secondary);margin-bottom:12px;' }, '区间格式: (min, max] — 支持k(千)后缀，如 (0, 32k], (32k, 256k]'),
            ...getStepEntries().map(([key, cost], stepIdx) => h('div', { key: stepIdx, style: 'border:1px solid var(--color-border);border-radius:8px;padding:12px;margin-bottom:12px;background:var(--color-bg-card);' }, [
              h('div', { style: 'display:flex;gap:8px;align-items:center;margin-bottom:10px;' }, [
                h('label', { style: 'font-size:12px;color:var(--color-text-secondary);white-space:nowrap;' }, '区间:'),
                h('input', { type: 'text', value: key, style: 'flex:1;padding:6px 10px;border:1px solid var(--color-border-dark);border-radius:4px;font-size:13px;font-family:monospace;', onChange: (e: Event) => updateStepKey(key, (e.target as HTMLInputElement).value) }),
                h('button', { style: 'padding:4px 8px;font-size:11px;border:1px solid var(--color-danger);border-radius:4px;background:var(--color-bg-card);color:var(--color-danger);cursor:pointer;', onClick: () => removeStep(key) }, '删除'),
              ]),
              renderCostFields(cost, (field, val) => { (cost as any)[field] = val; }),
            ])),
            getStepEntries().length === 0 ? h('div', { style: 'text-align:center;padding:16px;color:var(--color-text-disabled);font-size:13px;border:1px dashed var(--color-border-dark);border-radius:6px;' }, '暂无阶梯配置，点击上方按钮添加') : null,
          ]) : null,
          // 兼容性选项
          h('div', { style: S.section }, [
            h('label', { style: S.sectionTitle }, '兼容性选项'),
            h('div', { style: 'display:flex;flex-direction:column;gap:10px;' }, [
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: form.reasoning, style: S.checkboxInput, onChange: (e: Event) => { form.reasoning = (e.target as HTMLInputElement).checked; } }),
                '推理模型（支持思考过程）',
              ]),
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: !!form.autoCreateExplicitCache, style: S.checkboxInput, onChange: (e: Event) => { form.autoCreateExplicitCache = (e.target as HTMLInputElement).checked; } }),
                '自动创建显式缓存（autoCreateExplicitCache）',
              ]),
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: form.compat.supportsReasoningEffort, style: S.checkboxInput, onChange: (e: Event) => { form.compat.supportsReasoningEffort = (e.target as HTMLInputElement).checked; } }),
                '支持推理强度控制',
              ]),
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: form.compat.supportsUsageInStreaming, style: S.checkboxInput, onChange: (e: Event) => { form.compat.supportsUsageInStreaming = (e.target as HTMLInputElement).checked; } }),
                '流式响应支持Usage统计',
              ]),
              h('label', { style: S.checkbox }, [
                h('input', { type: 'checkbox', checked: form.compat.nativeWebSearchTool, style: S.checkboxInput, onChange: (e: Event) => { form.compat.nativeWebSearchTool = (e.target as HTMLInputElement).checked; } }),
                '支持原生网络搜索工具',
              ]),
            ]),
          ]),
          // API类型
          h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: S.label }, 'API类型'),
            h('select', { value: form.api, style: `${S.input}background:var(--color-bg-card);cursor:pointer;`, onChange: (e: Event) => { form.api = (e.target as HTMLSelectElement).value; } }, [
              h('option', { value: 'openai-completions' }, 'OpenAI Completions'),
              h('option', { value: 'anthropic-messages' }, 'Anthropic Messages'),
            ]),
          ]),
          // other 自定义JSON（自由输入框，保存时校验格式）
          h('div', { style: 'margin-bottom:16px;' }, [
            h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;' }, [
              h('label', { style: `${S.label}margin-bottom:0;` }, 'other（自定义JSON，可选）'),
              h('button', {
                style: 'padding:4px 12px;font-size:12px;border:1px solid var(--color-primary);border-radius:4px;background:var(--color-bg-card);color:var(--color-primary);cursor:pointer;',
                onClick: formatOtherJson,
              }, 'JSON 美化'),
            ]),
            h('p', { style: 'font-size:12px;color:var(--color-text-secondary);margin:0 0 6px;line-height:1.5;' }, '自由填写扩展配置，保存时校验JSON格式是否正确；留空表示无。示例：{"api":{"normal":{"url":"/api/v1/song/generate"}}}'),
            h('textarea', {
              value: modelOtherJson.value,
              rows: 8,
              spellcheck: 'false',
              placeholder: '{\n  "api": {\n    "normal": { "url": "/api/v1/song/generate" }\n  }\n}',
              style: `${S.input}font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;line-height:1.5;resize:vertical;`,
              onInput: (e: Event) => { modelOtherJson.value = (e.target as HTMLTextAreaElement).value; },
            }),
          ]),
          // 按钮
          h('div', { style: 'display:flex;justify-content:flex-end;gap:12px;margin-top:24px;padding-top:16px;border-top:1px solid var(--color-border);' }, [
            h('button', { style: S.btnSecondary, onClick: () => { showModelModal.value = false; } }, '取消'),
            h('button', { style: S.btnPrimary, onClick: saveModel }, '保存'),
          ]),
        ]),
      ]);
    };

    // === LLM标签页 ===
    const renderLLMTab = () => {
      if (settingsStore.loading) return h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-secondary);font-size:14px;' }, '加载中...');
      return h('div', {}, [
        h('div', { style: 'position:sticky;top:0;z-index:10;background:var(--color-bg-card);padding:24px 0 16px;margin-bottom:16px;border-bottom:1px solid var(--color-border);display:flex;justify-content:space-between;align-items:center;' }, [
          h('div', {}, [
            h('h2', { style: 'font-size:20px;font-weight:600;color:var(--color-text);margin:0;' }, 'LLM Provider管理'),
            h('p', { style: 'font-size:13px;color:var(--color-text-secondary);margin-top:4px;' }, `共 ${settingsStore.providerCount} 个Provider，${settingsStore.totalModelCount} 个模型`),
          ]),
          h('button', { style: `${S.btnPrimary}box-shadow:0 1px 2px rgba(0,0,0,0.05);`, onClick: openAddProvider }, '+ 添加Provider'),
        ]),
        settingsStore.providers.length > 0
          ? settingsStore.providers.map(p => renderProviderCard(p))
          : h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-disabled);border:2px dashed var(--color-border);border-radius:12px;background:var(--color-bg-panel);' }, '暂无Provider配置'),
        renderProviderModal(),
        renderModelModal(),
      ]);
    };

    // === MCP 服务器卡片 ===
    const renderServerCard = (server: MCPServerResponse) => {
      const transportLabels: Record<string, string> = { stdio: 'Stdio', sse: 'SSE', http: 'HTTP', ws: 'WebSocket', sdk: 'SDK' };
      return h('div', {
        key: server.name,
        style: 'border:1px solid var(--color-border);border-radius:8px;margin-bottom:12px;overflow:hidden;background:var(--color-bg-card);box-shadow:0 1px 3px rgba(0,0,0,0.1);',
      }, [
        h('div', { style: 'display:flex;justify-content:space-between;align-items:center;padding:16px;' }, [
          h('div', { style: 'flex:1;' }, [
            h('div', { style: 'display:flex;align-items:center;gap:8px;margin-bottom:6px;' }, [
              h('span', { style: 'font-weight:600;font-size:15px;color:var(--color-text);' }, server.name),
              h('span', { style: `${S.tag}background:var(--color-info-light);color:var(--color-info-text);` }, transportLabels[server.type] || server.type),
              statusTag(server.status),
              !server.enabled ? h('span', { style: `${S.tag}background:var(--color-bg-hover);color:var(--color-text-disabled);` }, '已禁用') : null,
              server.oauth_required ? h('span', { style: `${S.tag}background:var(--color-warning-light);color:var(--color-warning-text);` }, 'OAuth') : null,
            ]),
            h('div', { style: 'font-size:12px;color:var(--color-text-secondary);display:flex;gap:16px;' }, [
              server.url ? h('span', {}, server.url) : null,
              server.command ? h('span', {}, `${server.command} ${(server.args || []).join(' ')}`) : null,
              h('span', {}, `工具: ${server.tools_count}`),
              server.source ? h('span', {}, `来源: ${server.source}`) : null,
            ]),
            server.description ? h('div', { style: 'font-size:12px;color:var(--color-text-disabled);margin-top:4px;' }, server.description) : null,
            server.last_error ? h('div', { style: 'font-size:11px;color:var(--color-danger);margin-top:4px;' }, `错误: ${server.last_error}`) : null,
          ]),
          h('div', { style: 'display:flex;gap:8px;align-items:center;' }, [
            h('button', { style: S.btnSmall, onClick: () => openEditServer(server) }, '编辑'),
            h('button', { style: S.btnDanger, onClick: () => deleteServer(server.name) }, '删除'),
          ]),
        ]),
      ]);
    };

    // === MCP 服务器弹窗 ===
    const renderServerModal = () => {
      if (!showServerModal.value) return null;
      const form = serverForm.value;
      const isEdit = !!editingServerName.value;

      return h('div', { style: S.overlay }, [
        h('div', { style: `${S.modal}width:560px;` }, [
          h('h3', { style: 'font-size:18px;font-weight:600;margin-bottom:20px;color:var(--color-text);' },
            isEdit ? '编辑 MCP 服务器' : '添加 MCP 服务器'),

          // 名称
          h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: S.label }, '服务器名称 *'),
            h('input', { type: 'text', value: form.name, disabled: isEdit, placeholder: '如: echo-demo, remote-search', style: S.input, onInput: (e: Event) => { form.name = (e.target as HTMLInputElement).value; } }),
          ]),
          // 传输类型
          h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: S.label }, '传输类型'),
            h('select', { value: form.type, style: `${S.input}background:var(--color-bg-card);cursor:pointer;`, onChange: (e: Event) => { form.type = (e.target as HTMLSelectElement).value as any; } }, [
              h('option', { value: 'http' }, 'Streamable HTTP'),
              h('option', { value: 'sse' }, 'SSE'),
              h('option', { value: 'stdio' }, 'Stdio (本地进程)'),
              h('option', { value: 'ws' }, 'WebSocket'),
              h('option', { value: 'sdk' }, 'SDK (进程内)'),
            ]),
          ]),
          // 启用开关
          h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: S.checkbox }, [
              h('input', { type: 'checkbox', checked: form.enabled, style: S.checkboxInput, onChange: (e: Event) => { form.enabled = (e.target as HTMLInputElement).checked; } }),
              '启用此服务器',
            ]),
          ]),
          // 描述
          h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: S.label }, '描述'),
            h('input', { type: 'text', value: form.description || '', placeholder: '服务器用途描述', style: S.input, onInput: (e: Event) => { form.description = (e.target as HTMLInputElement).value; } }),
          ]),

          // stdio 字段
          form.type === 'stdio' ? h('div', { style: S.section }, [
            h('label', { style: S.sectionTitle }, 'Stdio 配置'),
            h('div', { style: 'margin-bottom:12px;' }, [
              h('label', { style: S.smallLabel }, '命令路径 *'),
              h('input', { type: 'text', value: form.command || '', placeholder: '如: python, node', style: S.smallInput, onInput: (e: Event) => { form.command = (e.target as HTMLInputElement).value; } }),
            ]),
            h('div', {}, [
              h('label', { style: S.smallLabel }, '参数（每行一个）'),
              h('textarea', { value: (form.args || []).join('\n'), placeholder: 'backend/echo_server.py', style: `${S.smallInput}height:60px;resize:vertical;font-family:monospace;`, onInput: (e: Event) => { form.args = ((e.target as HTMLTextAreaElement).value || '').split('\n').filter(Boolean); } }),
            ]),
          ]) : null,

          // 远程 URL 字段 (http/sse/ws)
          ['http', 'sse', 'ws'].includes(form.type) ? h('div', { style: S.section }, [
            h('label', { style: S.sectionTitle }, '远程服务器配置'),
            h('div', { style: 'margin-bottom:12px;' }, [
              h('label', { style: S.smallLabel }, 'URL *'),
              h('input', { type: 'text', value: form.url || '', placeholder: 'https://example.com/mcp', style: S.smallInput, onInput: (e: Event) => { form.url = (e.target as HTMLInputElement).value; } }),
            ]),
            h('div', {}, [
              h('label', { style: S.smallLabel }, '请求头（JSON 格式）'),
              h('textarea', {
                value: JSON.stringify(form.headers || {}, null, 2),
                placeholder: '{"Authorization": "Bearer xxx"}',
                style: `${S.smallInput}height:60px;resize:vertical;font-family:monospace;`,
                onInput: (e: Event) => {
                  try { form.headers = JSON.parse((e.target as HTMLTextAreaElement).value || '{}'); } catch {}
                },
              }),
            ]),
          ]) : null,

          // SDK 字段
          form.type === 'sdk' ? h('div', { style: S.section }, [
            h('label', { style: S.sectionTitle }, 'SDK 配置'),
            h('div', {}, [
              h('label', { style: S.smallLabel }, '模块路径 * (格式: pkg.mod:factory)'),
              h('input', { type: 'text', value: form.module || '', placeholder: 'my_package.server:create_server', style: S.smallInput, onInput: (e: Event) => { form.module = (e.target as HTMLInputElement).value; } }),
            ]),
          ]) : null,

          // OAuth
          ['http', 'sse'].includes(form.type) ? h('div', { style: S.section }, [
            h('label', { style: S.sectionTitle }, 'OAuth 认证'),
            h('label', { style: S.checkbox }, [
              h('input', { type: 'checkbox', checked: form.oauth?.enabled || false, style: S.checkboxInput, onChange: (e: Event) => {
                const checked = (e.target as HTMLInputElement).checked;
                form.oauth = checked ? { enabled: true, scopes: ['mcp:tools'] } : undefined;
              } }),
              '启用 OAuth 认证',
            ]),
          ]) : null,

          // 按钮
          h('div', { style: 'display:flex;justify-content:flex-end;gap:12px;margin-top:24px;padding-top:16px;border-top:1px solid var(--color-border);' }, [
            h('button', { style: S.btnSecondary, onClick: () => { showServerModal.value = false; } }, '取消'),
            h('button', { style: S.btnPrimary, onClick: saveServer }, '保存'),
          ]),
        ]),
      ]);
    };

    // === MCP 标签页 ===
    const renderMCPTab = () => {
      if (mcpStore.loading) return h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-secondary);font-size:14px;' }, '加载中...');
      return h('div', {}, [
        // 固定头部：标题 + 操作按钮 + 统计卡片
        h('div', { style: 'position:sticky;top:0;z-index:10;background:var(--color-bg-card);padding:24px 0 16px;margin-bottom:16px;border-bottom:1px solid var(--color-border);' }, [
          h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;' }, [
            h('div', {}, [
              h('h2', { style: 'font-size:20px;font-weight:600;color:var(--color-text);margin:0;' }, 'MCP 服务器管理'),
              h('p', { style: 'font-size:13px;color:var(--color-text-secondary);margin-top:4px;' }, '配置和管理 Model Context Protocol 服务器连接'),
            ]),
            h('div', { style: 'display:flex;gap:8px;' }, [
              h('button', { style: S.btnSecondary, onClick: reloadMCP }, '重载配置'),
              h('button', { style: `${S.btnPrimary}box-shadow:0 1px 2px rgba(0,0,0,0.05);`, onClick: openAddServer }, '+ 添加服务器'),
            ]),
          ]),
          // 统计卡片
          h('div', { style: 'display:grid;grid-template-columns:repeat(3,1fr);gap:16px;' }, [
            h('div', { style: 'padding:16px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-bg-panel);text-align:center;' }, [
              h('div', { style: 'font-size:28px;font-weight:700;color:var(--color-primary);' }, String(mcpStore.serverCount)),
              h('div', { style: 'font-size:13px;color:var(--color-text-secondary);margin-top:4px;' }, '服务器总数'),
            ]),
            h('div', { style: 'padding:16px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-bg-panel);text-align:center;' }, [
              h('div', { style: 'font-size:28px;font-weight:700;color:var(--color-success);' }, String(mcpStore.enabledCount)),
              h('div', { style: 'font-size:13px;color:var(--color-text-secondary);margin-top:4px;' }, '已启用'),
            ]),
            h('div', { style: 'padding:16px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-bg-panel);text-align:center;' }, [
              h('div', { style: 'font-size:28px;font-weight:700;color:var(--color-warning);' }, String(mcpStore.oauthCount)),
              h('div', { style: 'font-size:13px;color:var(--color-text-secondary);margin-top:4px;' }, 'OAuth 认证'),
            ]),
          ]),
        ]),

        // 全局配置开关
        mcpStore.globalConfig ? h('div', { style: 'display:flex;align-items:center;gap:12px;padding:12px 16px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-bg-panel);margin-bottom:16px;' }, [
          h('label', { style: S.checkbox }, [
            h('input', { type: 'checkbox', checked: mcpStore.globalConfig.enabled, style: S.checkboxInput, onChange: async (e: Event) => {
              await mcpStore.updateGlobalConfig({ enabled: (e.target as HTMLInputElement).checked });
            } }),
            h('span', { style: 'font-weight:500;' }, 'MCP 全局启用'),
          ]),
          h('span', { style: 'font-size:12px;color:var(--color-text-secondary);margin-left:auto;' }, `调用超时: ${mcpStore.globalConfig.call_timeout_seconds}s`),
        ]) : null,

        // 服务器列表
        mcpStore.servers.length > 0
          ? mcpStore.servers.map(s => renderServerCard(s))
          : h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-disabled);border:2px dashed var(--color-border);border-radius:12px;background:var(--color-bg-panel);' }, '暂无 MCP 服务器配置'),

        renderServerModal(),
      ]);
    };

    // === 工具管理：表单同步与保存 ===
    const syncToolSettingsForms = () => {
      const tc = toolsStore.timeoutConfig || {};
      const overrides: Record<string, any> = {};
      Object.keys(tc).forEach(k => { if (k !== 'default') overrides[k] = tc[k]; });
      toolTimeoutForm.value = { default: (tc.default as number) ?? 30, overrides };

      const rl = toolsStore.rateLimit || {};
      toolRateLimitForm.value = { enabled: rl.enabled ?? false, requests_per_minute: rl.requests_per_minute ?? 60 };

      const sa = toolsStore.subagentConfig || { disallowed_tools_per_level: {}, allowed_tools_per_level: {} };
      const disObj: Record<string, string> = {};
      const allowObj: Record<string, string> = {};
      Object.keys(sa.disallowed_tools_per_level || {}).forEach(lvl => { disObj[lvl] = (sa.disallowed_tools_per_level[lvl] || []).join('\n'); });
      Object.keys(sa.allowed_tools_per_level || {}).forEach(lvl => { allowObj[lvl] = (sa.allowed_tools_per_level[lvl] || []).join('\n'); });
      subagentForm.value = { disallowed: disObj, allowed: allowObj };

      const tm = toolsStore.teamConfig || { disallowed: [], allowed: [] };
      teamForm.value = { disallowed: (tm.disallowed || []).join('\n'), allowed: (tm.allowed || []).join('\n') };
    };

    const saveTimeout = async () => {
      const overrides: Record<string, number | null> = {};
      toolsStore.tools.forEach(t => {
        const v = toolTimeoutForm.value.overrides[t.name];
        overrides[t.name] = (v === undefined || v === null || v === '') ? null : Number(v);
      });
      const def = Number(toolTimeoutForm.value.default);
      await toolsStore.updateTimeout({ default: isNaN(def) ? null : def, overrides });
    };

    const saveRateLimit = async () => {
      await toolsStore.updateRateLimit({
        enabled: toolRateLimitForm.value.enabled,
        requests_per_minute: Number(toolRateLimitForm.value.requests_per_minute),
      });
    };

    const saveSubagent = async () => {
      const parse = (obj: Record<string, string>) => {
        const out: Record<string, string[]> = {};
        Object.keys(obj).forEach(lvl => { out[lvl] = (obj[lvl] || '').split('\n').map(s => s.trim()).filter(Boolean); });
        return out;
      };
      await toolsStore.updateSubagent({
        disallowed_tools_per_level: parse(subagentForm.value.disallowed),
        allowed_tools_per_level: parse(subagentForm.value.allowed),
      });
    };

    const saveTeam = async () => {
      const parse = (s: string) => (s || '').split('\n').map(x => x.trim()).filter(Boolean);
      await toolsStore.updateTeam({ disallowed: parse(teamForm.value.disallowed), allowed: parse(teamForm.value.allowed) });
    };

    // === 工具管理：工具项与列表 ===
    const renderToolItem = (tool: ToolItem) => {
      const typeLabels: Record<string, { text: string; bg: string; color: string }> = {
        builtin: { text: '内置', bg: 'var(--color-info-light)', color: 'var(--color-info-text)' },
        mcp: { text: 'MCP', bg: '#ede9fe', color: '#6d28d9' },
        extension: { text: '插件', bg: 'var(--color-success-light)', color: 'var(--color-success-text)' },
      };
      const tl = typeLabels[tool.type] || typeLabels.builtin;
      return h('div', {
        key: tool.name,
        style: 'display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;border:1px solid var(--color-border);border-radius:8px;margin-bottom:8px;background:var(--color-bg-card);',
      }, [
        h('div', { style: 'flex:1;min-width:0;' }, [
          h('div', { style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;' }, [
            h('span', { style: 'font-weight:600;font-size:14px;color:var(--color-text);font-family:monospace;' }, tool.name),
            h('span', { style: `${S.tag}background:${tl.bg};color:${tl.color};` }, tl.text),
            tool.system_tool ? h('span', { style: `${S.tag}background:var(--color-bg-hover);color:var(--color-text-secondary);` }, '系统') : null,
            tool.mcp_server ? h('span', { style: `${S.tag}background:var(--color-bg-hover);color:var(--color-text-secondary);` }, tool.mcp_server) : null,
            h('span', { style: 'font-size:11px;color:var(--color-text-disabled);' }, `${tool.timeout_seconds}s`),
          ]),
          tool.description ? h('div', { style: 'font-size:12px;color:var(--color-text-secondary);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, tool.description) : null,
        ]),
        h('div', {
          style: `width:44px;height:24px;border-radius:12px;background:${tool.enabled ? 'var(--color-primary)' : 'var(--color-border-dark)'};position:relative;cursor:pointer;transition:background 0.2s;flex-shrink:0;`,
          title: tool.enabled ? '点击禁用' : '点击启用',
          onClick: () => toolsStore.toggleTool(tool.name, !tool.enabled),
        }, [
          h('div', { style: `width:20px;height:20px;border-radius:50%;background:#fff;position:absolute;top:2px;left:${tool.enabled ? '22px' : '2px'};transition:left 0.2s;box-shadow:0 1px 3px rgba(0,0,0,0.3);` }),
        ]),
      ]);
    };

    const renderToolGroup = (title: string, list: ToolItem[]) => {
      if (!list.length) return null;
      return h('div', { style: 'margin-bottom:20px;' }, [
        h('div', { style: 'font-size:13px;font-weight:600;color:var(--color-text);margin-bottom:8px;display:flex;align-items:center;gap:8px;' }, [
          title, h('span', { style: 'font-size:12px;color:var(--color-text-disabled);font-weight:400;' }, `(${list.length})`),
        ]),
        list.map(t => renderToolItem(t)),
      ]);
    };

    const renderToolList = () => {
      return h('div', {}, [
        h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;' }, [
          h('p', { style: 'font-size:13px;color:var(--color-text-secondary);margin:0;' }, `共 ${toolsStore.total} 个工具，启用 ${toolsStore.enabledCount}，禁用 ${toolsStore.disabledCount}。禁用为全局设置，所有 agent 都不能使用。`),
          h('button', { style: S.btnSecondary, onClick: () => toolsStore.fetchConfig() }, '刷新'),
        ]),
        renderToolGroup('内置工具', toolsStore.builtinTools),
        renderToolGroup('MCP 工具', toolsStore.mcpTools),
        renderToolGroup('插件工具', toolsStore.extensionTools),
        toolsStore.total === 0 ? h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-disabled);border:2px dashed var(--color-border);border-radius:12px;background:var(--color-bg-panel);' }, '暂无工具') : null,
      ]);
    };

    // === 工具管理：设置页 ===
    const renderToolSettings = () => {
      const levels = Array.from(new Set([
        ...Object.keys(subagentForm.value.disallowed),
        ...Object.keys(subagentForm.value.allowed),
        '0', '1', '2',
      ])).sort();
      return h('div', {}, [
        // 超时配置
        h('div', { style: S.section }, [
          h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;' }, [
            h('label', { style: `${S.sectionTitle}margin-bottom:0;` }, '超时配置（秒）'),
            h('button', { style: S.btnSmall, onClick: saveTimeout }, '保存'),
          ]),
          h('div', { style: 'display:flex;align-items:center;gap:8px;margin-bottom:12px;' }, [
            h('label', { style: 'font-size:13px;color:var(--color-text);width:80px;' }, '默认超时'),
            h('input', { type: 'number', value: toolTimeoutForm.value.default, style: 'width:120px;padding:6px 10px;border:1px solid var(--color-border-dark);border-radius:6px;font-size:13px;', onInput: (e: Event) => { toolTimeoutForm.value.default = Number((e.target as HTMLInputElement).value); } }),
          ]),
          h('div', { style: 'display:grid;grid-template-columns:repeat(2,1fr);gap:8px;' },
            toolsStore.tools.map(t => h('div', { key: t.name, style: 'display:flex;align-items:center;gap:8px;' }, [
              h('span', { style: 'font-size:12px;color:var(--color-text);font-family:monospace;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;', title: t.name }, t.name),
              h('input', { type: 'number', value: toolTimeoutForm.value.overrides[t.name] ?? '', placeholder: '默认', style: 'width:80px;padding:4px 8px;border:1px solid var(--color-border-dark);border-radius:4px;font-size:12px;', onInput: (e: Event) => { const v = (e.target as HTMLInputElement).value; toolTimeoutForm.value.overrides[t.name] = v === '' ? '' : Number(v); } }),
            ]))
          ),
        ]),
        // 限流配置
        h('div', { style: S.section }, [
          h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;' }, [
            h('label', { style: `${S.sectionTitle}margin-bottom:0;` }, '限流配置'),
            h('button', { style: S.btnSmall, onClick: saveRateLimit }, '保存'),
          ]),
          h('label', { style: S.checkbox }, [
            h('input', { type: 'checkbox', checked: toolRateLimitForm.value.enabled, style: S.checkboxInput, onChange: (e: Event) => { toolRateLimitForm.value.enabled = (e.target as HTMLInputElement).checked; } }),
            '启用限流',
          ]),
          h('div', { style: 'display:flex;align-items:center;gap:8px;margin-top:12px;' }, [
            h('label', { style: 'font-size:13px;color:var(--color-text);' }, '每分钟请求数'),
            h('input', { type: 'number', value: toolRateLimitForm.value.requests_per_minute, style: 'width:120px;padding:6px 10px;border:1px solid var(--color-border-dark);border-radius:6px;font-size:13px;', onInput: (e: Event) => { toolRateLimitForm.value.requests_per_minute = Number((e.target as HTMLInputElement).value); } }),
          ]),
        ]),
        // subagent 分层工具
        h('div', { style: S.section }, [
          h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;' }, [
            h('label', { style: `${S.sectionTitle}margin-bottom:0;` }, 'subagent 分层工具配置'),
            h('button', { style: S.btnSmall, onClick: saveSubagent }, '保存'),
          ]),
          h('p', { style: 'font-size:12px;color:var(--color-text-disabled);margin:0 0 12px;' }, '每行一个工具名或正则模式；黑名单优先于白名单，白名单为空表示不限制。'),
          levels.map(lvl => h('div', { key: lvl, style: 'margin-bottom:12px;padding:12px;border:1px solid var(--color-border);border-radius:6px;background:var(--color-bg-card);' }, [
            h('div', { style: 'font-size:13px;font-weight:600;color:var(--color-text);margin-bottom:8px;' }, `层级 ${lvl}`),
            h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
              h('div', {}, [
                h('label', { style: S.smallLabel }, '禁用 (disallowed)'),
                h('textarea', { value: subagentForm.value.disallowed[lvl] || '', style: `${S.smallInput}height:80px;resize:vertical;font-family:monospace;`, onInput: (e: Event) => { subagentForm.value.disallowed[lvl] = (e.target as HTMLTextAreaElement).value; } }),
              ]),
              h('div', {}, [
                h('label', { style: S.smallLabel }, '允许 (allowed)'),
                h('textarea', { value: subagentForm.value.allowed[lvl] || '', style: `${S.smallInput}height:80px;resize:vertical;font-family:monospace;`, onInput: (e: Event) => { subagentForm.value.allowed[lvl] = (e.target as HTMLTextAreaElement).value; } }),
              ]),
            ]),
          ])),
        ]),
        // 团队工具
        h('div', { style: S.section }, [
          h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;' }, [
            h('label', { style: `${S.sectionTitle}margin-bottom:0;` }, '团队工具配置'),
            h('button', { style: S.btnSmall, onClick: saveTeam }, '保存'),
          ]),
          h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
            h('div', {}, [
              h('label', { style: S.smallLabel }, '禁用 (disallowed)，每行一个'),
              h('textarea', { value: teamForm.value.disallowed, style: `${S.smallInput}height:80px;resize:vertical;font-family:monospace;`, onInput: (e: Event) => { teamForm.value.disallowed = (e.target as HTMLTextAreaElement).value; } }),
            ]),
            h('div', {}, [
              h('label', { style: S.smallLabel }, '允许 (allowed)，每行一个'),
              h('textarea', { value: teamForm.value.allowed, style: `${S.smallInput}height:80px;resize:vertical;font-family:monospace;`, onInput: (e: Event) => { teamForm.value.allowed = (e.target as HTMLTextAreaElement).value; } }),
            ]),
          ]),
        ]),
      ]);
    };

    // === 工具管理标签页 ===
    const renderToolsTab = () => {
      if (toolsStore.loading) return h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-secondary);font-size:14px;' }, '加载中...');
      return h('div', {}, [
        h('div', { style: 'position:sticky;top:0;z-index:10;background:var(--color-bg-card);padding:24px 0 12px;margin-bottom:16px;border-bottom:1px solid var(--color-border);' }, [
          h('h2', { style: 'font-size:20px;font-weight:600;color:var(--color-text);margin:0 0 12px;' }, '工具管理'),
          h('div', { style: 'display:flex;gap:8px;' },
            [['list', '工具'], ['settings', '设置']].map(([key, label]) =>
              h('div', {
                style: `padding:8px 20px;cursor:pointer;font-size:14px;border-bottom:2px solid ${toolsSubTab.value === key ? 'var(--color-primary)' : 'transparent'};color:${toolsSubTab.value === key ? 'var(--color-primary)' : 'var(--color-text-secondary)'};font-weight:${toolsSubTab.value === key ? '500' : '400'};`,
                onClick: () => { toolsSubTab.value = key as 'list' | 'settings'; if (key === 'settings') syncToolSettingsForms(); },
              }, label)
            )
          ),
        ]),
        toolsSubTab.value === 'list' ? renderToolList() : renderToolSettings(),
      ]);
    };

    // === 技能管理：表单同步与保存 ===
    const syncSkillSettingsForms = () => {
      const form: Record<string, { disallowed: string; allowed: string }> = {};
      Object.keys(skillsStore.agents || {}).forEach(name => {
        const cfg = skillsStore.agents[name] || { disallowed: [], allowed: [] };
        form[name] = {
          disallowed: (cfg.disallowed || []).join('\n'),
          allowed: (cfg.allowed || []).join('\n'),
        };
      });
      agentSkillsForm.value = form;
    };

    const saveAgentSkills = async (agentName: string) => {
      const parse = (s: string) => (s || '').split('\n').map(x => x.trim()).filter(Boolean);
      const f = agentSkillsForm.value[agentName] || { disallowed: '', allowed: '' };
      await skillsStore.updateAgentSkills(agentName, { disallowed: parse(f.disallowed), allowed: parse(f.allowed) });
    };

    // === 技能管理：技能项与列表 ===
    const renderSkillItem = (skill: SkillItem) => {
      const typeLabels: Record<string, { text: string; bg: string; color: string }> = {
        builtin: { text: '内置', bg: 'var(--color-info-light)', color: 'var(--color-info-text)' },
        plugin: { text: '插件', bg: 'var(--color-success-light)', color: 'var(--color-success-text)' },
        workspace: { text: '工作区', bg: 'var(--color-warning-light)', color: 'var(--color-warning-text)' },
      };
      const tl = typeLabels[skill.type] || typeLabels.builtin;
      return h('div', {
        key: skill.name,
        style: 'display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;border:1px solid var(--color-border);border-radius:8px;margin-bottom:8px;background:var(--color-bg-card);',
      }, [
        h('div', { style: 'flex:1;min-width:0;' }, [
          h('div', { style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;' }, [
            h('span', { style: 'font-weight:600;font-size:14px;color:var(--color-text);font-family:monospace;' }, skill.name),
            h('span', { style: `${S.tag}background:${tl.bg};color:${tl.color};` }, tl.text),
            h('span', { style: `${S.tag}background:var(--color-bg-hover);color:var(--color-text-secondary);` }, skill.context === 'fork' ? 'fork' : 'inline'),
            h('span', { style: 'font-size:11px;color:var(--color-text-disabled);' }, `v${skill.version}`),
          ]),
          skill.description ? h('div', { style: 'font-size:12px;color:var(--color-text-secondary);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, skill.description) : null,
          skill.gate_reasons && skill.gate_reasons.length ? h('div', { style: 'font-size:11px;color:var(--color-warning-text);margin-top:4px;' }, `⚠ ${skill.gate_reasons.join('; ')}`) : null,
        ]),
        h('div', {
          style: `width:44px;height:24px;border-radius:12px;background:${skill.enabled ? 'var(--color-primary)' : 'var(--color-border-dark)'};position:relative;cursor:pointer;transition:background 0.2s;flex-shrink:0;`,
          title: skill.enabled ? '点击禁用' : '点击启用',
          onClick: () => skillsStore.toggleSkill(skill.name, !skill.enabled),
        }, [
          h('div', { style: `width:20px;height:20px;border-radius:50%;background:#fff;position:absolute;top:2px;left:${skill.enabled ? '22px' : '2px'};transition:left 0.2s;box-shadow:0 1px 3px rgba(0,0,0,0.3);` }),
        ]),
      ]);
    };

    const renderSkillGroup = (title: string, list: SkillItem[]) => {
      if (!list.length) return null;
      return h('div', { style: 'margin-bottom:20px;' }, [
        h('div', { style: 'font-size:13px;font-weight:600;color:var(--color-text);margin-bottom:8px;display:flex;align-items:center;gap:8px;' }, [
          title, h('span', { style: 'font-size:12px;color:var(--color-text-disabled);font-weight:400;' }, `(${list.length})`),
        ]),
        list.map(s => renderSkillItem(s)),
      ]);
    };

    const renderSkillList = () => {
      return h('div', {}, [
        h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;' }, [
          h('p', { style: 'font-size:13px;color:var(--color-text-secondary);margin:0;' }, `共 ${skillsStore.total} 个技能，启用 ${skillsStore.enabledCount}，禁用 ${skillsStore.disabledCount}。禁用为全局设置，所有 agent 都不能使用。`),
          h('button', { style: S.btnSecondary, onClick: () => skillsStore.fetchConfig() }, '刷新'),
        ]),
        renderSkillGroup('内置技能', skillsStore.builtinSkills),
        renderSkillGroup('插件技能', skillsStore.pluginSkills),
        renderSkillGroup('工作区技能', skillsStore.workspaceSkills),
        skillsStore.total === 0 ? h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-disabled);border:2px dashed var(--color-border);border-radius:12px;background:var(--color-bg-panel);' }, '暂无技能') : null,
      ]);
    };

    // === 技能管理：设置页 ===
    const renderSkillSettings = () => {
      const agentNames = Object.keys(skillsStore.agents || {});
      return h('div', {}, [
        h('div', { style: S.section }, [
          h('label', { style: `${S.sectionTitle}` }, '各 Agent 技能配置'),
          h('p', { style: 'font-size:12px;color:var(--color-text-disabled);margin:0 0 12px;' }, '每行一个技能名或模式；禁用列表优先，允许列表为空表示不限制。更改对新创建的会话生效。'),
          agentNames.length === 0 ? h('div', { style: 'color:var(--color-text-disabled);font-size:13px;padding:16px;text-align:center;' }, '暂无 agent 配置') : null,
          agentNames.map(name => {
            const f = agentSkillsForm.value[name] || { disallowed: '', allowed: '' };
            return h('div', { key: name, style: 'margin-bottom:12px;padding:12px;border:1px solid var(--color-border);border-radius:6px;background:var(--color-bg-card);' }, [
              h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;' }, [
                h('div', { style: 'font-size:13px;font-weight:600;color:var(--color-text);' }, name),
                h('button', { style: S.btnSmall, onClick: () => saveAgentSkills(name) }, '保存'),
              ]),
              h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
                h('div', {}, [
                  h('label', { style: S.smallLabel }, '禁用 (disallowed)'),
                  h('textarea', { value: f.disallowed, style: `${S.smallInput}height:80px;resize:vertical;font-family:monospace;`, onInput: (e: Event) => { if (agentSkillsForm.value[name]) agentSkillsForm.value[name].disallowed = (e.target as HTMLTextAreaElement).value; } }),
                ]),
                h('div', {}, [
                  h('label', { style: S.smallLabel }, '允许 (allowed)'),
                  h('textarea', { value: f.allowed, style: `${S.smallInput}height:80px;resize:vertical;font-family:monospace;`, onInput: (e: Event) => { if (agentSkillsForm.value[name]) agentSkillsForm.value[name].allowed = (e.target as HTMLTextAreaElement).value; } }),
                ]),
              ]),
            ]);
          }),
        ]),
      ]);
    };

    // === 技能管理标签页 ===
    const renderSkillsTab = () => {
      if (skillsStore.loading) return h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-secondary);font-size:14px;' }, '加载中...');
      return h('div', {}, [
        h('div', { style: 'position:sticky;top:0;z-index:10;background:var(--color-bg-card);padding:24px 0 12px;margin-bottom:16px;border-bottom:1px solid var(--color-border);' }, [
          h('h2', { style: 'font-size:20px;font-weight:600;color:var(--color-text);margin:0 0 12px;' }, '技能管理'),
          h('div', { style: 'display:flex;gap:8px;' },
            [['list', '技能'], ['settings', '设置']].map(([key, label]) =>
              h('div', {
                style: `padding:8px 20px;cursor:pointer;font-size:14px;border-bottom:2px solid ${skillsSubTab.value === key ? 'var(--color-primary)' : 'transparent'};color:${skillsSubTab.value === key ? 'var(--color-primary)' : 'var(--color-text-secondary)'};font-weight:${skillsSubTab.value === key ? '500' : '400'};`,
                onClick: () => { skillsSubTab.value = key as 'list' | 'settings'; if (key === 'settings') syncSkillSettingsForms(); },
              }, label)
            )
          ),
        ]),
        skillsSubTab.value === 'list' ? renderSkillList() : renderSkillSettings(),
      ]);
    };

    // === 记忆配置标签页 ===
    const renderMemoryTab = () => {
      if (memoryLoading.value) return h('div', { style: 'text-align:center;padding:48px;color:var(--color-text-secondary);font-size:14px;' }, '加载中...');

      const renderField = (label: string, key: string, type: 'text' | 'number' | 'checkbox' = 'number', hint?: string) => {
        const value = (memoryForm.value as any)[key];
        const inputStyle = 'width:100%;padding:8px 10px;border:1px solid var(--color-border-dark);border-radius:6px;font-size:13px;box-sizing:border-box;';
        
        if (type === 'checkbox') {
          return h('div', { style: 'margin-bottom:16px;' }, [
            h('label', { style: 'display:flex;align-items:center;gap:8px;font-size:13px;color:var(--color-text);cursor:pointer;' }, [
              h('input', {
                type: 'checkbox',
                checked: value,
                style: 'width:16px;height:16px;cursor:pointer;',
                onChange: (e: Event) => { (memoryForm.value as any)[key] = (e.target as HTMLInputElement).checked; },
              }),
              label,
            ]),
            hint ? h('div', { style: 'font-size:11px;color:var(--color-text-disabled);margin-top:4px;margin-left:24px;' }, hint) : null,
          ]);
        }

        return h('div', { style: 'margin-bottom:16px;' }, [
          h('label', { style: 'display:block;font-size:13px;color:var(--color-text);margin-bottom:6px;font-weight:500;' }, label),
          h('input', {
            type,
            value,
            style: inputStyle,
            onInput: (e: Event) => {
              const v = (e.target as HTMLInputElement).value;
              (memoryForm.value as any)[key] = type === 'number' ? Number(v) : v;
            },
          }),
          hint ? h('div', { style: 'font-size:11px;color:var(--color-text-disabled);margin-top:4px;' }, hint) : null,
        ]);
      };

      const renderSection = (title: string, children: any[]) => {
        return h('div', { style: 'margin-bottom:20px;padding:16px;border:1px solid var(--color-border);border-radius:8px;background:var(--color-bg-panel);' }, [
          h('div', { style: 'font-size:14px;font-weight:600;color:var(--color-text);margin-bottom:12px;' }, title),
          ...children,
        ]);
      };

      return h('div', {}, [
        h('div', { style: 'position:sticky;top:0;z-index:10;background:var(--color-bg-card);padding:24px 0 16px;margin-bottom:16px;border-bottom:1px solid var(--color-border);display:flex;justify-content:space-between;align-items:center;' }, [
          h('h2', { style: 'font-size:20px;font-weight:600;color:var(--color-text);margin:0;' }, '记忆配置'),
          h('button', { style: 'padding:8px 16px;background:var(--color-primary);color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;', onClick: saveMemoryConfig }, '保存配置'),
        ]),

        // 基本设置
        renderSection('基本设置', [
          renderField('启用记忆系统', 'enabled', 'checkbox', '关闭后系统将不再自动提取和注入记忆'),
          renderField('记忆根目录', 'root', 'text', '相对于 .happy-claw 目录'),
          renderField('运行模式', 'mode', 'text', 'standard（个人）或 teammem（团队）'),
          h('div', { style: 'margin-top:12px;padding:12px;background:var(--color-bg-card);border-radius:6px;' }, [
            h('div', { style: 'font-size:13px;font-weight:500;color:var(--color-text);margin-bottom:8px;' }, 'LLM 配置'),
            h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
              h('div', {}, [
                h('label', { style: 'font-size:12px;color:var(--color-text-secondary);' }, '供应商'),
                h('input', {
                  type: 'text',
                  value: memoryForm.value.llm.provider,
                  style: 'width:100%;padding:6px 8px;border:1px solid var(--color-border-dark);border-radius:4px;font-size:12px;margin-top:4px;box-sizing:border-box;',
                  onInput: (e: Event) => { memoryForm.value.llm.provider = (e.target as HTMLInputElement).value; },
                }),
              ]),
              h('div', {}, [
                h('label', { style: 'font-size:12px;color:var(--color-text-secondary);' }, '模型'),
                h('input', {
                  type: 'text',
                  value: memoryForm.value.llm.model,
                  style: 'width:100%;padding:6px 8px;border:1px solid var(--color-border-dark);border-radius:4px;font-size:12px;margin-top:4px;box-sizing:border-box;',
                  onInput: (e: Event) => { memoryForm.value.llm.model = (e.target as HTMLInputElement).value; },
                }),
              ]),
            ]),
          ]),
        ]),

        // 扫描与索引
        renderSection('扫描与索引', [
          h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
            h('div', {}, [renderField('扫描最大文件数', 'scan_max_files', 'number')]),
            h('div', {}, [renderField('注入时最多选择', 'select_max', 'number')]),
            h('div', {}, [renderField('单文件最大行数', 'per_file_max_lines', 'number')]),
            h('div', {}, [renderField('单文件最大字节', 'per_file_max_bytes', 'number')]),
            h('div', {}, [renderField('索引最大行数', 'index_max_lines', 'number')]),
            h('div', {}, [renderField('索引最大字节', 'index_max_bytes', 'number')]),
          ]),
        ]),

        // 会话注入与新鲜度
        renderSection('会话注入与新鲜度', [
          h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
            h('div', {}, [renderField('会话注入预算(字节)', 'session_inject_budget_bytes', 'number', '注入到 system prompt 的最大字节数')]),
            h('div', {}, [renderField('新鲜度警告(天)', 'freshness_warn_days', 'number', '记忆超过此天数未更新时显示警告')]),
          ]),
        ]),

        // 记忆提取
        renderSection('记忆提取', [
          h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
            h('div', {}, [renderField('提取最小间隔(秒)', 'extract_min_interval_seconds', 'number')]),
            h('div', {}, [renderField('提取最大轮次', 'extract_max_rounds', 'number')]),
            h('div', {}, [renderField('提取最小内容(字符)', 'extract_min_content_chars', 'number')]),
            h('div', {}, [renderField('提取最小用户消息数', 'extract_min_user_messages', 'number')]),
          ]),
        ]),

        // 记忆整合
        renderSection('记忆整合', [
          h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
            h('div', {}, [renderField('整合最小间隔(小时)', 'consolidate_min_interval_hours', 'number')]),
            h('div', {}, [renderField('整合最小新会话数', 'consolidate_min_new_conversations', 'number')]),
            h('div', {}, [renderField('整合锁 TTL(秒)', 'consolidate_lock_ttl_seconds', 'number')]),
            h('div', {}, [renderField('整合扫描节流(秒)', 'consolidate_scan_throttle_seconds', 'number')]),
            h('div', {}, [renderField('写入锁 TTL(秒)', 'lock_ttl_seconds', 'number')]),
            h('div', {}, [renderField('预取收集超时(秒)', 'prefetch_collect_timeout_seconds', 'number')]),
          ]),
        ]),

        // 会话记忆
        renderSection('会话记忆', [
          renderField('启用会话记忆', 'session_memory.enabled', 'checkbox', '自动总结会话内容并保存为记忆'),
          h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:12px;' }, [
            h('div', {}, [renderField('首次更新 token', 'session_memory.first_update_tokens', 'number')]),
            h('div', {}, [renderField('增量更新 token', 'session_memory.increment_tokens', 'number')]),
            h('div', {}, [renderField('工具调用阈值', 'session_memory.tool_calls_threshold', 'number')]),
            h('div', {}, [renderField('自然暂停轮次', 'session_memory.natural_pause_turns', 'number')]),
            h('div', {}, [renderField('摘要最小 token', 'session_memory.summary_min_tokens', 'number')]),
            h('div', {}, [renderField('摘要最大 token', 'session_memory.summary_max_tokens', 'number')]),
            h('div', {}, [renderField('摘要最小消息数', 'session_memory.summary_min_messages', 'number')]),
          ]),
        ]),
      ]);
    };

    return () => h('div', { style: 'display:flex;flex-direction:column;height:100%;background:var(--color-bg-page);overflow:hidden;' }, [
      // 固定头部：页面标题
      h('div', { style: 'padding:20px 24px 16px;flex-shrink:0;' }, [
        h('h1', { style: 'font-size:24px;font-weight:700;margin:0;color:var(--color-text);' }, '系统设置'),
      ]),
      // 卡片区域：左侧Tab导航 + 右侧可滚动内容
      h('div', { style: 'flex:1;overflow:hidden;padding:0 24px 24px;' }, [
        h('div', { style: 'display:flex;gap:0;background:var(--color-bg-card);border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.1);height:100%;overflow:hidden;' }, [
          // 左侧Tab导航（固定）
          h('div', { style: 'width:180px;flex-shrink:0;padding:24px 0;border-right:1px solid var(--color-border);overflow-y:auto;' },
            [['general', '常规设置'], ['llm', 'LLM配置'], ['mcp', 'MCP服务'], ['tools', '工具管理'], ['skills', '技能管理'], ['memory', '记忆配置'], ['security', '安全设置']].map(([key, label]) =>
              h('div', {
                style: `padding:10px 24px;cursor:pointer;font-size:14px;transition:all 0.2s;${activeTab.value === key ? 'background:var(--color-primary-lighter);color:var(--color-primary);font-weight:500;border-right:2px solid var(--color-primary);' : 'color:var(--color-text-secondary);'}`,
                onClick: () => { activeTab.value = key; },
              }, label)
            )
          ),
          // 右侧内容区（可滚动）
          h('div', { style: 'flex:1;min-width:0;overflow-y:auto;padding:0 24px 24px;' }, [
            activeTab.value === 'llm' ? renderLLMTab() : null,
            activeTab.value === 'mcp' ? renderMCPTab() : null,
            activeTab.value === 'general' ? h('div', { style: 'color:var(--color-text-secondary);font-size:14px;padding-top:24px;' }, '常规设置（待实现）') : null,
            activeTab.value === 'tools' ? renderToolsTab() : null,
            activeTab.value === 'skills' ? renderSkillsTab() : null,
            activeTab.value === 'memory' ? renderMemoryTab() : null,
            activeTab.value === 'security' ? h('div', { style: 'color:var(--color-text-secondary);font-size:14px;padding-top:24px;' }, '安全设置（待实现）') : null,
          ]),
        ]),
      ]),
    ]);
  },
});
