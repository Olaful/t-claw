/**
 * 斜杠命令弹窗 + 参数候选弹窗渲染器
 */
import { h, type Ref, type ComputedRef } from 'vue';
import type { ParamPromptData } from '../types';

export interface SlashPopupContext {
  showSlashPopup: Ref<boolean>;
  getFilteredCommands: ComputedRef<Record<string, any[]>>;
  getFlatCommandList: ComputedRef<Array<{ title: string; command: any }>>;
  showMoreCounts: Ref<Record<string, number>>;
  slashSelectedIdx: Ref<number>;
  inputMessage: Ref<string>;
  slashFilter: Ref<string>;
  focusInput: () => void;
  // 参数候选弹窗
  showParamPrompt: Ref<boolean>;
  paramPromptData: Ref<ParamPromptData | null>;
  paramInputValues: Ref<Record<string, string>>;
  paramPromptError: Ref<string>;
  handleParamPromptConfirm: () => Promise<void>;
}

/** 渲染斜杠命令弹窗 */
export function renderSlashPopup(ctx: SlashPopupContext) {
  const { showSlashPopup, getFilteredCommands, getFlatCommandList, showMoreCounts, slashSelectedIdx, inputMessage, slashFilter, focusInput } = ctx;

  if (!showSlashPopup.value) return null;
  const filtered = getFilteredCommands.value;
  const flatList = getFlatCommandList.value;

  return h('div', {
    style: 'position:absolute;bottom:100%;left:0;right:0;margin-bottom:0.5rem;max-height:400px;overflow-y:auto;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.75rem;box-shadow:0 -4px 12px rgba(0,0,0,0.15);z-index:1000;padding:0.5rem 0;',
  }, [
    ...Object.entries(filtered).map(([title, commands]: [string, any]) => {
      const maxShow = showMoreCounts.value[title] || 30;
      const visibleCmds = (commands as any[]).slice(0, maxShow);
      const remaining = (commands as any[]).length - maxShow;

      return h('div', { key: title, style: 'margin-bottom:0.25rem;' }, [
        // 分栏标题
        h('div', {
          style: 'padding:0.375rem 0.75rem;font-size:0.7rem;font-weight:600;color:var(--color-text-secondary);text-transform:uppercase;letter-spacing:0.05em;',
        }, title),
        // 命令列表
        ...visibleCmds.map((cmd: any) => {
          const flatIdx = flatList.findIndex(item => item.title === title && item.command.name === cmd.name);
          const isSelected = flatIdx === slashSelectedIdx.value;

          // 构建参数显示
          const argDefs = cmd.arguments && typeof cmd.arguments === 'object' ? cmd.arguments : {};
          const requiredArgs = Object.entries(argDefs).filter(([, def]: [string, any]) => def.required).map(([name]) => name);
          const optionalArgs = Object.entries(argDefs).filter(([, def]: [string, any]) => !def.required).map(([name]) => name);
          const argsDisplay = [...requiredArgs.map(a => `[${a}]`), ...optionalArgs.map(a => `(${a})`)].join(' ');

          const icon = cmd.icon || '⚡';

          return h('div', {
            key: `${title}.${cmd.name}`,
            onClick: () => {
              inputMessage.value = `/${cmd.name} `;
              showSlashPopup.value = false;
              slashFilter.value = '';
              focusInput();
            },
            style: `padding:0.5rem 0.75rem;cursor:pointer;display:flex;align-items:center;gap:0.5rem;font-size:0.85rem;${
              isSelected ? 'background:var(--color-bg-hover);' : ''
            }`,
            onMouseenter: () => { slashSelectedIdx.value = flatIdx; },
          }, [
            h('span', { style: 'flex-shrink:0;' }, icon),
            h('span', { style: 'font-weight:500;color:var(--color-primary);' }, `/${cmd.name}`),
            argsDisplay ? h('span', { style: 'color:var(--color-text-disabled);font-size:0.75rem;font-family:monospace;' }, argsDisplay) : null,
            h('span', { style: 'margin-left:auto;color:var(--color-text-secondary);font-size:0.75rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px;' }, cmd.description || ''),
          ]);
        }),
        // "显示更多" 按钮
        remaining > 0
          ? h('div', {
              onClick: () => { showMoreCounts.value = { ...showMoreCounts.value, [title]: maxShow + 30 }; },
              style: 'padding:0.375rem 0.75rem;font-size:0.75rem;color:var(--color-primary);cursor:pointer;text-align:center;',
            }, `显示 ${remaining} 条更多命令`)
          : null,
      ]);
    }),
    // 无匹配结果
    Object.keys(filtered).length === 0
      ? h('div', { style: 'padding:1rem;text-align:center;color:var(--color-text-secondary);font-size:0.85rem;' }, '无匹配命令')
      : null,
  ]);
}

/** 渲染参数候选弹窗 */
export function renderParamPrompt(ctx: SlashPopupContext) {
  const { showParamPrompt, paramPromptData, paramInputValues, paramPromptError, handleParamPromptConfirm, focusInput } = ctx;

  if (!showParamPrompt.value || !paramPromptData.value) return null;
  const { command, missingArgs } = paramPromptData.value;

  return h('div', {
    style: 'position:absolute;bottom:100%;left:0;right:0;margin-bottom:0.5rem;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.75rem;box-shadow:0 -4px 12px rgba(0,0,0,0.15);z-index:1001;padding:1rem;',
  }, [
    h('div', { style: 'font-size:0.85rem;font-weight:600;margin-bottom:0.75rem;color:var(--color-text);' },
      `请提供 /${command.name} 的必选参数：`),
    ...missingArgs.map((arg: { name: string; config: any }) => {
      const hasOptions = arg.config.options && Array.isArray(arg.config.options) && arg.config.options.length > 0;
      return h('div', { key: arg.name, style: 'margin-bottom:0.75rem;' }, [
        h('div', { style: 'font-size:0.8rem;font-weight:500;margin-bottom:0.375rem;color:var(--color-text-secondary);' },
          `${arg.name} - ${arg.config.description || ''}`),
        hasOptions
          ? h('div', { style: 'display:flex;flex-wrap:wrap;gap:0.5rem;' },
              (arg.config.options as string[]).map(opt =>
                h('button', {
                  key: opt,
                  onClick: () => { paramInputValues.value = { ...paramInputValues.value, [arg.name]: opt }; },
                  style: `padding:0.375rem 0.75rem;border-radius:0.375rem;border:1px solid ${
                    paramInputValues.value[arg.name] === opt ? 'var(--color-primary)' : 'var(--color-border)'
                  };background:${
                    paramInputValues.value[arg.name] === opt ? 'var(--color-info-light)' : 'var(--color-bg-card)'
                  };color:${
                    paramInputValues.value[arg.name] === opt ? 'var(--color-primary)' : 'var(--color-text)'
                  };cursor:pointer;font-size:0.8rem;`,
                }, opt)
              )
            )
          : h('input', {
              type: 'text',
              value: paramInputValues.value[arg.name] || '',
              placeholder: `请输入 ${arg.name}...`,
              onInput: (e: Event) => {
                paramInputValues.value = { ...paramInputValues.value, [arg.name]: (e.target as HTMLInputElement).value };
              },
              style: 'width:100%;padding:0.5rem 0.75rem;border:1px solid var(--color-border);border-radius:0.375rem;font-size:0.85rem;background:var(--color-bg-input);color:var(--color-text);outline:none;',
            }),
      ]);
    }),
    // 校验错误提示
    paramPromptError.value
      ? h('div', { style: 'font-size:0.78rem;color:var(--color-danger);margin-top:0.5rem;' }, paramPromptError.value)
      : null,
    // 确认/取消按钮
    h('div', { style: 'display:flex;justify-content:flex-end;gap:0.5rem;margin-top:0.75rem;' }, [
      h('button', {
        onClick: () => { showParamPrompt.value = false; paramPromptData.value = null; paramPromptError.value = ''; focusInput(); },
        style: 'padding:0.5rem 1rem;border:1px solid var(--color-border);border-radius:0.375rem;background:var(--color-bg-card);cursor:pointer;font-size:0.8rem;color:var(--color-text);',
      }, '取消'),
      h('button', {
        onClick: handleParamPromptConfirm,
        style: 'padding:0.5rem 1rem;border:none;border-radius:0.375rem;background:var(--color-primary);color:white;cursor:pointer;font-size:0.8rem;',
      }, '确认执行'),
    ]),
  ]);
}
