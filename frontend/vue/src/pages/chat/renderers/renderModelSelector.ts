/**
 * 模型选择器渲染器
 * 渲染模型下拉选择面板（两栏布局：模型列表 + 详细配置）
 */
import { h, computed, type Ref } from 'vue';
import type { LLMModel, SessionModelConfig } from '../types';

export interface ModelSelectorContext {
  llmModels: Ref<Array<LLMModel>>;
  sessionModelConfig: Ref<SessionModelConfig | null>;
  tempModelConfig: Ref<SessionModelConfig | null>;
  showModelSelector: Ref<boolean>;
  currentPrimaryModel: Ref<string | null>;
  showTokenUsage: Ref<boolean>;
  saveSessionModelConfig: (config: SessionModelConfig) => Promise<boolean>;
}

export function renderModelSelector(ctx: ModelSelectorContext) {
  const { llmModels, sessionModelConfig, tempModelConfig, showModelSelector, currentPrimaryModel, showTokenUsage, saveSessionModelConfig } = ctx;

  // 获取当前选中的模型显示名称
  const getCurrentModelDisplay = () => {
    if (!sessionModelConfig.value) {
      let defaultModel: LLMModel | undefined;
      if (currentPrimaryModel.value) {
        const [provider, modelId] = currentPrimaryModel.value.split('/');
        defaultModel = llmModels.value.find(m => m.provider === provider && m.id === modelId);
      }
      if (!defaultModel) {
        defaultModel = llmModels.value.find(m => m.provider === 'deepseek') || llmModels.value[0];
      }
      return defaultModel ? `${defaultModel.displayName}` : '选择模型';
    }
    const model = llmModels.value.find(
      m => m.provider === sessionModelConfig.value!.provider && m.id === sessionModelConfig.value!.model_id
    );
    return model ? `${model.displayName}` : `${sessionModelConfig.value.model_id}`;
  };

  // 按provider分组模型
  const modelsByProvider = computed(() => {
    const grouped: Record<string, LLMModel[]> = {};
    llmModels.value.forEach(model => {
      if (!grouped[model.provider]) {
        grouped[model.provider] = [];
      }
      grouped[model.provider].push(model);
    });
    return grouped;
  });

  // 切换下拉菜单显示/隐藏
  const toggleDropdown = () => {
    if (!showModelSelector.value) {
      tempModelConfig.value = sessionModelConfig.value
        ? { ...sessionModelConfig.value }
        : (llmModels.value.length > 0
            ? { provider: llmModels.value[0].provider, model_id: llmModels.value[0].id, reasoning_level: 'low', speed_mode: 'default' }
            : null);
    }
    const willShow = !showModelSelector.value;
    showModelSelector.value = willShow;
    // 互斥组：展开模型选择器时关闭 token 用量弹窗
    if (willShow) showTokenUsage.value = false;
  };

  // 选择模型（仅更新临时状态）
  const selectModel = (model: LLMModel) => {
    if (!tempModelConfig.value) return;
    const reasoningEnabled = model.reasoning === true;
    tempModelConfig.value = {
      ...tempModelConfig.value,
      provider: model.provider,
      model_id: model.id,
      reasoning_level: reasoningEnabled ? (tempModelConfig.value.reasoning_level ?? 'low') : undefined,
    };
  };

  // 更新推理级别
  const updateReasoningLevel = (level: string) => {
    if (!tempModelConfig.value) return;
    tempModelConfig.value = { ...tempModelConfig.value, reasoning_level: level };
  };

  // 更新速度模式
  const updateSpeedMode = (mode: string) => {
    if (!tempModelConfig.value) return;
    tempModelConfig.value = { ...tempModelConfig.value, speed_mode: mode };
  };

  // 点击"使用默认模型"按钮
  const resetToDefault = () => {
    if (!currentPrimaryModel.value || llmModels.value.length === 0) return;
    const [provider, modelId] = currentPrimaryModel.value.split('/');
    const defaultModel = llmModels.value.find(m => m.provider === provider && m.id === modelId);
    if (!defaultModel) return;

    const reasoningEnabled = defaultModel.reasoning === true;
    tempModelConfig.value = {
      provider: defaultModel.provider,
      model_id: defaultModel.id,
      reasoning_level: reasoningEnabled ? 'low' : undefined,
      speed_mode: 'default',
    };
  };

  // 保存按钮
  const handleSave = async () => {
    if (!tempModelConfig.value) return;
    const ok = await saveSessionModelConfig({ ...tempModelConfig.value });
    if (ok) {
      showModelSelector.value = false;
    }
  };

  // 放弃按钮
  const handleCancel = () => {
    showModelSelector.value = false;
    tempModelConfig.value = null;
  };

  // 关闭下拉菜单（点击外部时）
  const closeDropdown = (e: MouseEvent) => {
    const target = e.target as HTMLElement;
    if (!target.closest('.model-selector-dropdown')) {
      showModelSelector.value = false;
      tempModelConfig.value = null;
    }
    if (!target.closest('.token-usage-widget')) {
      showTokenUsage.value = false;
    }
  };

  // 监听全局点击事件以关闭下拉菜单
  if (typeof window !== 'undefined') {
    window.addEventListener('click', closeDropdown);
  }

  return h('div', { class: 'model-selector-dropdown', style: 'position:relative;' }, [
    // 触发按钮
    h('button', {
      onClick: toggleDropdown,
      style: 'display:flex;align-items:center;gap:0.5rem;padding:0.375rem 0.625rem;border:none;border-radius:0.375rem;background:transparent;cursor:pointer;font-size:0.8rem;color:var(--color-text-secondary);',
    }, [
      h('span', {}, getCurrentModelDisplay()),
      h('span', { style: 'font-size:0.625rem;color:var(--color-text-disabled);' }, showModelSelector.value ? '▲' : '▼'),
    ]),

    // 下拉面板（两栏布局）
    showModelSelector.value
      ? h('div', {
          style: 'position:absolute;bottom:100%;right:0;margin-bottom:0.5rem;width:600px;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;box-shadow:0 4px 6px rgba(0,0,0,0.1);z-index:1000;display:flex;',
        }, [
          // 左栏：Provider + 模型列表
          h('div', { style: 'flex:1;border-right:1px solid var(--color-border);padding:0.5rem 0;' }, [
            ...Object.entries(modelsByProvider.value).map(([provider, models]) =>
              h('div', { key: provider, style: 'padding:0.25rem 0;' }, [
                h('div', {
                  style: 'padding:0.25rem 0.75rem;font-size:0.75rem;font-weight:600;color:var(--color-text-secondary);text-transform:capitalize;',
                }, provider),
                ...models.map((model) => {
                  const isSelected = tempModelConfig.value?.provider === model.provider &&
                                     tempModelConfig.value?.model_id === model.id;
                  return h('div', {
                    key: `${model.provider}/${model.id}`,
                    onClick: () => selectModel(model),
                    style: `padding:0.5rem 0.75rem;cursor:pointer;display:flex;justify-content:space-between;align-items:center;${
                      isSelected ? 'background:var(--color-bg-hover);' : 'hover:bg-gray-50;'
                    }`,
                  }, [
                    h('div', { style: 'flex:1;' }, [
                      h('div', { style: 'font-size:0.875rem;font-weight:500;' }, model.displayName),
                      h('div', { style: 'font-size:0.75rem;color:var(--color-text-disabled);' }, model.id),
                    ]),
                    isSelected ? h('span', { style: 'color:var(--color-success);' }, '✓') : null,
                  ]);
                }),
              ])
            ),
          ]),

          // 右栏：当前选中模型的详细信息 + 控制项
          h('div', { style: 'flex:1;padding:1rem;display:flex;flex-direction:column;gap:1rem;' }, [
            (() => {
              const currentModel = tempModelConfig.value
                ? llmModels.value.find(m => m.provider === tempModelConfig.value!.provider && m.id === tempModelConfig.value!.model_id)
                : (llmModels.value.find(m => m.provider === 'deepseek') || llmModels.value[0]);

              if (!currentModel) return null;

              const reasoningEnabled = currentModel.reasoning === true && currentModel.compat?.supportsReasoningEffort !== false;

              return [
                h('div', { style: 'font-size:1rem;font-weight:600;' }, currentModel.displayName),
                h('div', { style: 'font-size:0.875rem;color:var(--color-text-secondary);' }, currentModel.id),

                // Reasoning 滑块
                h('div', { style: `margin-top:1rem;${reasoningEnabled ? '' : 'opacity:0.5;'}` }, [
                  h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;' }, [
                    h('span', { style: 'font-size:0.875rem;font-weight:600;color:var(--color-text);' }, 'REASONING'),
                    h('span', { style: 'font-size:0.75rem;color:var(--color-text-secondary);' }, reasoningEnabled ? (tempModelConfig.value?.reasoning_level ?? 'low') : '不支持推理'),
                  ]),
                  h('input', {
                    type: 'range',
                    min: 0,
                    max: 3,
                    step: 1,
                    value: ['none', 'low', 'high', 'max'].indexOf(tempModelConfig.value?.reasoning_level ?? 'low'),
                    disabled: !reasoningEnabled,
                    onInput: (e: Event) => {
                      const idx = Number((e.target as HTMLInputElement).value);
                      updateReasoningLevel(['none', 'low', 'high', 'max'][idx]);
                    },
                    style: 'width:100%;height:4px;-webkit-appearance:none;background:var(--color-border);border-radius:2px;outline:none;',
                  }),
                  h('div', { style: 'display:flex;justify-content:space-between;font-size:0.75rem;color:var(--color-text-disabled);margin-top:0.25rem;' }, [
                    h('span', {}, 'None'),
                    h('span', {}, 'Low'),
                    h('span', {}, 'High'),
                    h('span', {}, 'Max'),
                  ]),
                ]),

                // Speed 模式选择
                h('div', { style: 'margin-top:1rem;' }, [
                  h('div', { style: 'font-size:0.875rem;font-weight:600;color:var(--color-text);margin-bottom:0.5rem;' }, 'SPEED'),
                  h('div', { style: 'display:grid;grid-template-columns:repeat(4,1fr);gap:0.5rem;' }, [
                    ['default', 'fast', 'auto'].map(mode => {
                      const isSelected = (tempModelConfig.value?.speed_mode || 'default') === mode;
                      return h('button', {
                        onClick: () => updateSpeedMode(mode),
                        style: `padding:0.5rem;border-radius:0.375rem;border:1px solid ${isSelected ? 'var(--color-primary)' : 'var(--color-border)'};background:${isSelected ? 'var(--color-info-light)' : 'var(--color-bg-card)'};color:${isSelected ? 'var(--color-info-text)' : 'var(--color-text)'};cursor:pointer;font-size:0.875rem;text-transform:capitalize;`,
                      }, mode.charAt(0).toUpperCase() + mode.slice(1));
                    }),
                  ]),
                ]),

                // 底部按钮
                h('div', { style: 'margin-top:auto;display:flex;gap:0.5rem;justify-content:flex-end;' }, [
                  h('button', {
                    onClick: resetToDefault,
                    style: 'padding:0.5rem 1rem;border:1px solid var(--color-border);border-radius:0.375rem;background:var(--color-bg-card);cursor:pointer;font-size:0.875rem;',
                  }, '使用默认模型'),
                  h('button', {
                    onClick: handleCancel,
                    style: 'padding:0.5rem 1rem;border:1px solid var(--color-border);border-radius:0.375rem;background:var(--color-bg-card);cursor:pointer;font-size:0.875rem;',
                  }, '放弃'),
                  h('button', {
                    onClick: handleSave,
                    style: 'padding:0.5rem 1rem;border:none;border-radius:0.375rem;background:var(--color-danger-dark);color:white;cursor:pointer;font-size:0.875rem;',
                  }, '保存'),
                ]),
              ];
            })(),
          ]),
        ])
      : null,
  ]);
}
