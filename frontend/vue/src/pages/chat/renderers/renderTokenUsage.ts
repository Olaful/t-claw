/**
 * Token 用量渲染器
 * 渲染 Token 使用量详情弹窗
 */
import { h, type Ref } from 'vue';
import type { LLMModel, SessionModelConfig } from '../types';

export interface TokenUsageContext {
  messages: Ref<any[]>;
  llmModels: Ref<Array<LLMModel>>;
  sessionModelConfig: Ref<SessionModelConfig | null>;
  showTokenUsage: Ref<boolean>;
  showModelSelector: Ref<boolean>;
}

export function renderTokenUsage(ctx: TokenUsageContext) {
  const { messages, llmModels, sessionModelConfig, showTokenUsage, showModelSelector } = ctx;

  // 从最后一条助手消息中提取 token 用量信息
  const lastAssistantMsg = messages.value.slice().reverse().find(msg => msg.role === 'assistant');
  const msgWithMetadata = lastAssistantMsg as any;
  const usageData = msgWithMetadata?.metadata?.usage;
  const costData = msgWithMetadata?.metadata?.cost;

  if (!usageData || !costData) return null;

  const totalTokens = usageData.total_tokens || 0;
  const contextWindow = llmModels.value.find(m => m.id === sessionModelConfig.value?.model_id)?.contextWindow || 1_000_000;
  const usagePercent = ((totalTokens / contextWindow) * 100).toFixed(1);

  // 格式化数字（82k, 1M 等）
  const formatNumber = (num: number) => {
    if (num >= 1_000_000) {
      return `${(num / 1_000_000).toFixed(num % 1_000_000 === 0 ? 0 : 1)}M`;
    }
    if (num >= 1000) {
      return `${(num / 1000).toFixed(num % 1000 === 0 ? 0 : 1)}k`;
    }
    return num.toString();
  };

  // 切换显示/隐藏
  const toggleUsage = () => {
    const willShow = !showTokenUsage.value;
    showTokenUsage.value = willShow;
    // 互斥组：展开 token 用量弹窗时关闭模型选择器
    if (willShow) showModelSelector.value = false;
  };

  return h('div', { class: 'token-usage-widget', style: 'position:relative;' }, [
    // 触发按钮
    h('button', {
      onClick: toggleUsage,
      style: 'display:flex;align-items:center;gap:0.5rem;padding:0.375rem 0.625rem;border:none;border-radius:0.375rem;background:transparent;cursor:pointer;font-size:0.8rem;color:var(--color-text-secondary);',
    }, [
      h('span', {}, `${formatNumber(totalTokens)} / ${formatNumber(contextWindow)} · ${usagePercent}%`),
      h('span', { style: 'font-size:0.625rem;color:var(--color-text-disabled);' }, showTokenUsage.value ? '▲' : '▼'),
    ]),

    // 弹窗内容
    showTokenUsage.value
      ? h('div', {
          style: 'position:absolute;bottom:100%;right:0;margin-bottom:0.5rem;width:400px;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;box-shadow:0 4px 6px rgba(0,0,0,0.1);z-index:1000;padding:1rem;',
        }, [
          // 标题
          h('div', { style: 'font-size:0.875rem;font-weight:600;color:var(--color-text);margin-bottom:0.75rem;' }, 'Token 用量详情'),

          // 最新运行令牌
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('div', { style: 'font-size:0.75rem;font-weight:600;color:var(--color-text-secondary);margin-bottom:0.5rem;' }, '最新运行令牌'),
            h('div', { style: 'display:grid;grid-template-columns:repeat(3,1fr);gap:0.5rem;' }, [
              h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;text-align:center;' }, [
                h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '输入'),
                h('div', { style: 'font-size:1.125rem;font-weight:600;color:var(--color-text);' }, formatNumber(usageData.prompt_tokens || 0)),
              ]),
              h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;text-align:center;' }, [
                h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '输出'),
                h('div', { style: 'font-size:1.125rem;font-weight:600;color:var(--color-text);' }, formatNumber(usageData.completion_tokens || 0)),
              ]),
              h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;text-align:center;' }, [
                h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '预估成本'),
                h('div', { style: 'font-size:1.125rem;font-weight:600;color:var(--color-text);' }, `$${costData.total_cost.toFixed(3)}`),
              ]),
            ]),
          ]),

          // 按类型划分的成本
          h('div', { style: 'margin-bottom:1rem;' }, [
            h('div', { style: 'font-size:0.75rem;font-weight:600;color:var(--color-text-secondary);margin-bottom:0.5rem;' }, '按类型划分的成本'),
            h('div', { style: 'display:grid;grid-template-columns:repeat(3,1fr);gap:0.5rem;' }, [
              h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;' }, [
                h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '输入'),
                h('div', { style: 'font-size:1rem;font-weight:600;color:var(--color-text);' }, `$${(costData.breakdown.input_cost || 0).toFixed(4)}`),
              ]),
              h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;' }, [
                h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '输出'),
                h('div', { style: 'font-size:1rem;font-weight:600;color:var(--color-text);' }, `$${(costData.breakdown.output_cost || 0).toFixed(4)}`),
              ]),
              ...(costData.breakdown.cache_write_cost ? [
                h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;' }, [
                  h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '缓存写入'),
                  h('div', { style: 'font-size:1rem;font-weight:600;color:var(--color-text);' }, `$${costData.breakdown.cache_write_cost.toFixed(4)}`),
                ]),
              ] : []),
              ...(costData.breakdown.implicit_cache_read_cost ? [
                h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;' }, [
                  h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '隐式缓存读取'),
                  h('div', { style: 'font-size:1rem;font-weight:600;color:var(--color-text);' }, `$${costData.breakdown.implicit_cache_read_cost.toFixed(4)}`),
                ]),
              ] : []),
              ...(costData.breakdown.explicit_cache_input_cost ? [
                h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;' }, [
                  h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '显式缓存创建'),
                  h('div', { style: 'font-size:1rem;font-weight:600;color:var(--color-text);' }, `$${costData.breakdown.explicit_cache_input_cost.toFixed(4)}`),
                ]),
              ] : []),
              ...(costData.breakdown.explicit_cache_read_cost ? [
                h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;' }, [
                  h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '显式缓存命中'),
                  h('div', { style: 'font-size:1rem;font-weight:600;color:var(--color-text);' }, `$${costData.breakdown.explicit_cache_read_cost.toFixed(4)}`),
                ]),
              ] : []),
            ]),
          ]),

          // 令牌详情栏（推理输出）
          (() => {
            const completionDetails = usageData.completion_tokens_details || {};
            const reasoningTokens = completionDetails.reasoning_tokens || 0;

            if (reasoningTokens === 0) return null;

            return h('div', { style: 'margin-bottom:1rem;' }, [
              h('div', { style: 'font-size:0.75rem;font-weight:600;color:var(--color-text-secondary);margin-bottom:0.5rem;' }, '令牌详情'),
              h('div', { style: 'padding:0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;display:flex;justify-content:space-between;align-items:center;' }, [
                h('div', {}, [
                  h('div', { style: 'font-size:0.75rem;color:var(--color-text-secondary);margin-bottom:0.25rem;' }, '推理输出'),
                  h('div', { style: 'font-size:1rem;font-weight:600;color:var(--color-text);' }, formatNumber(reasoningTokens)),
                ]),
                h('div', { style: 'font-size:0.75rem;color:var(--color-text-disabled);' }, '思维链 token'),
              ]),
            ]);
          })(),

          // 提供商和模型信息
          h('div', { style: 'font-size:0.75rem;color:var(--color-text-disabled);border-top:1px solid var(--color-border);padding-top:0.75rem;' }, [
            h('div', {}, `提供商: ${sessionModelConfig.value?.provider || 'deepseek'}`),
            h('div', {}, `模型: ${sessionModelConfig.value?.model_id || 'deepseek-v4-flash'}`),
            ...(usageData.llm_calls > 1 ? [h('div', {}, `LLM 调用次数: ${usageData.llm_calls}`)] : []),
          ]),
        ])
      : null,
  ]);
}
