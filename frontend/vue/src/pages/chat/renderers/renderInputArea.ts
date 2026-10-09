/**
 * 输入区域渲染器
 * 渲染输入框、附件预览、斜杠命令弹窗、底部工具栏、图片预览弹框
 */
import { h, type Ref } from 'vue';
import type { ParamPromptData } from '../types';
import { renderSlashPopup, renderParamPrompt, type SlashPopupContext } from './renderSlashPopup';
import { renderModelSelector, type ModelSelectorContext } from './renderModelSelector';
import { renderTokenUsage, type TokenUsageContext } from './renderTokenUsage';

export interface InputAreaContext {
  // 基础状态
  inputMessage: Ref<string>;
  messageInput: Ref<HTMLTextAreaElement | null>;
  fileInputRef: Ref<HTMLInputElement | null>;
  isLoading: Ref<boolean>;
  pendingAttachments: Ref<any[]>;
  // 附件
  showAttachMenu: Ref<boolean>;
  previewImageUrl: Ref<string | null>;
  triggerFileSelect: (accept: string) => void;
  handleFileSelected: (e: Event) => void;
  handlePaste: (e: ClipboardEvent) => void;
  formatFileSize: (bytes: number) => string;
  removeAttachment: (fileId: string) => void;
  // 斜杠命令弹窗上下文
  slashPopupCtx: SlashPopupContext;
  // 模型选择器上下文
  modelSelectorCtx: ModelSelectorContext;
  // Token 用量上下文
  tokenUsageCtx: TokenUsageContext;
  // 操作
  handleSend: () => void;
  handleStop: () => void;
  autoResizeTextarea: () => void;
  focusInput: () => void;
  getFlatCommandList: Ref<Array<{ title: string; command: any }>>;
  showSlashPopup: Ref<boolean>;
  slashSelectedIdx: Ref<number>;
  slashFilter: Ref<string>;
  showParamPrompt: Ref<boolean>;
  paramPromptData: Ref<ParamPromptData | null>;
  paramPromptError: Ref<string>;
}

export function renderInputArea(ctx: InputAreaContext) {
  const {
    inputMessage, messageInput, fileInputRef, isLoading, pendingAttachments,
    showAttachMenu, previewImageUrl, triggerFileSelect, handleFileSelected,
    handlePaste, formatFileSize, removeAttachment, slashPopupCtx,
    modelSelectorCtx, tokenUsageCtx, handleSend, handleStop,
    autoResizeTextarea, focusInput, getFlatCommandList, showSlashPopup,
    slashSelectedIdx, slashFilter, showParamPrompt, paramPromptData, paramPromptError,
  } = ctx;

  const isSendDisabled = () => {
    return (!inputMessage.value.trim() && pendingAttachments.value.length === 0) || isLoading.value;
  };

  return [
    // 输入区域容器
    h('div', { style: 'padding:1rem 1rem 0.5rem;border-top:1px solid var(--color-border);max-width:48rem;width:100%;margin-left:auto;margin-right:auto;position:relative;' }, [
      // 斜杠命令弹窗
      renderSlashPopup(slashPopupCtx),
      // 参数候选弹窗
      renderParamPrompt(slashPopupCtx),
      // 隐藏的文件输入元素
      h('input', {
        ref: fileInputRef,
        type: 'file',
        multiple: true,
        style: 'display:none;',
        onChange: handleFileSelected,
      }),
      // 统一边框容器
      h('div', { style: 'border:1px solid var(--color-border);border-radius:0.75rem;background:var(--color-bg-card);' }, [
        // 待发送附件缩略图区域
        ...(pendingAttachments.value.length > 0 ? [
          h('div', { style: 'display:flex;flex-wrap:wrap;gap:0.5rem;padding:0.5rem 0.75rem 0;' },
            pendingAttachments.value.map((att: any) =>
              h('div', {
                key: att.file_id,
                style: 'position:relative;display:inline-flex;align-items:center;justify-content:center;border-radius:0.5rem;overflow:hidden;background:var(--color-bg-hover);border:1px solid var(--color-border);',
              }, [
                att.is_image
                  ? h('img', {
                      src: att.local_preview || att.url,
                      alt: att.name,
                      style: 'max-height:80px;max-width:120px;object-fit:cover;display:block;',
                    })
                  : h('div', { style: 'display:flex;align-items:center;gap:0.375rem;padding:0.5rem 0.75rem;max-width:160px;' }, [
                      h('span', { style: 'font-size:1.25rem;flex-shrink:0;' }, '📄'),
                      h('div', { style: 'min-width:0;' }, [
                        h('div', { style: 'font-size:0.75rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, att.name),
                        h('div', { style: 'font-size:0.65rem;color:var(--color-text-secondary);' }, formatFileSize(att.size)),
                      ]),
                    ]),
                h('button', {
                  onClick: () => removeAttachment(att.file_id),
                  style: 'position:absolute;top:2px;right:2px;width:18px;height:18px;border-radius:50%;border:none;background:rgba(0,0,0,0.6);color:white;font-size:0.65rem;cursor:pointer;display:flex;align-items:center;justify-content:center;line-height:1;',
                  title: '移除',
                }, '×'),
              ])
            )
          ),
        ] : []),
        // 上栏："+按钮 + 输入框 + 发送按钮
        h('div', { style: 'display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0.5rem 0.5rem 0.5rem;border-radius:0.75rem 0.75rem 0 0;position:relative;' }, [
          // "+"附件按钮
          h('div', { style: 'position:relative;', onClick: (e: Event) => e.stopPropagation() }, [
            h('button', {
              onClick: () => { showAttachMenu.value = !showAttachMenu.value; },
              title: '添加附件',
              style: 'width:32px;height:32px;border-radius:50%;border:1px solid var(--color-border);background:var(--color-bg-card);color:var(--color-text);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1.2rem;flex-shrink:0;transition:background 0.2s;',
            }, '+'),
            ...(showAttachMenu.value ? [
              h('div', {
                style: 'position:absolute;bottom:calc(100% + 8px);left:0;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;box-shadow:0 4px 12px rgba(0,0,0,0.15);padding:0.25rem;z-index:100;min-width:120px;',
              }, [
                h('button', {
                  onClick: () => triggerFileSelect('image/*'),
                  style: 'display:flex;align-items:center;gap:0.5rem;width:100%;padding:0.5rem 0.75rem;border:none;background:none;cursor:pointer;font-size:0.85rem;color:var(--color-text);border-radius:0.375rem;text-align:left;',
                  onMouseenter: (e: Event) => { (e.target as HTMLElement).style.background = 'var(--color-bg-hover)'; },
                  onMouseleave: (e: Event) => { (e.target as HTMLElement).style.background = 'none'; },
                }, [h('span', {}, '🖼️'), h('span', {}, '照片')]),
                h('button', {
                  onClick: () => triggerFileSelect('*'),
                  style: 'display:flex;align-items:center;gap:0.5rem;width:100%;padding:0.5rem 0.75rem;border:none;background:none;cursor:pointer;font-size:0.85rem;color:var(--color-text);border-radius:0.375rem;text-align:left;',
                  onMouseenter: (e: Event) => { (e.target as HTMLElement).style.background = 'var(--color-bg-hover)'; },
                  onMouseleave: (e: Event) => { (e.target as HTMLElement).style.background = 'none'; },
                }, [h('span', {}, '📁'), h('span', {}, '文件')]),
              ]),
            ] : []),
          ]),
          // 输入框
          h('textarea', {
            ref: messageInput,
            value: inputMessage.value,
            placeholder: isLoading.value ? 'Agent 正在回复中...' : pendingAttachments.value.length > 0 ? '添加消息或继续粘贴图片...' : '输入消息... (Enter发送, Shift+Enter换行, /命令)',
            disabled: isLoading.value,
            style: 'flex:1;padding:0.5rem;border:none;resize:none;font-family:inherit;font-size:0.875rem;min-height:2.5rem;max-height:12rem;outline:none;background:transparent;overflow-y:auto;height:auto;',
            onPaste: handlePaste,
            onInput: (e: Event) => {
              const val = (e.target as HTMLTextAreaElement).value;
              inputMessage.value = val;
              autoResizeTextarea();
              // 检测斜杠命令触发
              if (val.startsWith('/') && !val.includes('\n')) {
                const afterSlash = val.slice(1);
                if (!afterSlash.includes(' ')) {
                  showSlashPopup.value = true;
                  slashFilter.value = afterSlash;
                  slashSelectedIdx.value = 0;
                  if (showParamPrompt.value) {
                    showParamPrompt.value = false;
                    paramPromptData.value = null;
                    paramPromptError.value = '';
                  }
                } else {
                  showSlashPopup.value = false;
                }
              } else {
                showSlashPopup.value = false;
              }
            },
            onKeydown: (e: KeyboardEvent) => {
              // 斜杠命令弹窗键盘导航
              if (showSlashPopup.value) {
                const flatList = getFlatCommandList.value;
                if (e.key === 'ArrowDown') { e.preventDefault(); slashSelectedIdx.value = Math.min(slashSelectedIdx.value + 1, flatList.length - 1); return; }
                if (e.key === 'ArrowUp') { e.preventDefault(); slashSelectedIdx.value = Math.max(slashSelectedIdx.value - 1, 0); return; }
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  if (flatList.length > 0 && slashSelectedIdx.value < flatList.length) {
                    const selected = flatList[slashSelectedIdx.value];
                    inputMessage.value = `/${selected.command.name} `;
                    showSlashPopup.value = false;
                    slashFilter.value = '';
                    focusInput();
                  }
                  return;
                }
                if (e.key === 'Escape') { e.preventDefault(); showSlashPopup.value = false; return; }
                if (e.key === 'Tab') {
                  e.preventDefault();
                  if (flatList.length > 0 && slashSelectedIdx.value < flatList.length) {
                    const selected = flatList[slashSelectedIdx.value];
                    inputMessage.value = `/${selected.command.name} `;
                    showSlashPopup.value = false;
                    slashFilter.value = '';
                    focusInput();
                  }
                  return;
                }
              }
              // 普通 Enter 发送
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                if (!isLoading.value) handleSend();
              }
              // Escape 关闭参数弹窗
              if (e.key === 'Escape' && showParamPrompt.value) {
                e.preventDefault();
                showParamPrompt.value = false;
                paramPromptData.value = null;
                focusInput();
              }
            },
          }),
          // 发送/停止按钮
          isLoading.value
            ? h('button', {
                onClick: handleStop,
                title: '停止生成',
                style: 'width:40px;height:40px;border-radius:50%;border:none;background:linear-gradient(135deg,var(--color-danger),var(--color-danger-dark));color:white;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1.1rem;flex-shrink:0;',
              }, '■')
            : h('button', {
                onClick: !isSendDisabled() ? handleSend : undefined,
                disabled: isSendDisabled(),
                title: '发送',
                style: `width:40px;height:40px;border-radius:50%;border:none;background:${isSendDisabled() ? 'var(--color-border-dark)' : 'linear-gradient(135deg,var(--color-primary),#8b5cf6)'};color:white;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1.1rem;flex-shrink:0;${isSendDisabled() ? '' : 'box-shadow:0 2px 4px rgba(59,130,246,0.3);'}transition:all 0.2s;`,
              }, '↑'),
        ]),
        // 分隔线
        h('div', { style: 'height:1px;background:var(--color-border);margin:0 0.75rem;' }),
        // 下栏：模型选择器 + Token 用量
        h('div', { style: 'display:flex;align-items:center;justify-content:flex-end;gap:0.5rem;padding:0.375rem 0.5rem;border-radius:0 0 0.75rem 0.75rem;' }, [
          renderModelSelector(modelSelectorCtx),
          renderTokenUsage(tokenUsageCtx),
        ]),
      ]),
    ]),
    // 图片预览弹框
    ...(previewImageUrl.value ? [
      h('div', {
        style: 'position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.85);z-index:9999;display:flex;align-items:center;justify-content:center;cursor:pointer;',
        onClick: () => { previewImageUrl.value = null; },
        onKeydown: (e: KeyboardEvent) => { if (e.key === 'Escape') previewImageUrl.value = null; },
      }, [
        h('img', {
          src: previewImageUrl.value,
          style: 'max-width:90vw;max-height:90vh;object-fit:contain;border-radius:0.5rem;',
          onClick: (e: Event) => e.stopPropagation(),
        }),
      ]),
    ] : []),
  ];
}
