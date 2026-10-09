/**
 * 附件系统 composable
 * 管理文件选择、粘贴、预览、下载等附件操作
 */
import { ref, onMounted, onUnmounted, nextTick } from 'vue';

const API_BASE = 'http://localhost:8000/api/v1';

export interface UseAttachmentsOptions {
  /** 获取当前 agentId 的函数 */
  getAgentId: () => string;
  /** chatStore 引用 */
  chatStore: any;
}

export function useAttachments(options: UseAttachmentsOptions) {
  const { getAgentId, chatStore } = options;

  // ===== 状态 =====
  const showAttachMenu = ref(false); // "+"按钮弹出的附件类型选择框
  const previewImageUrl = ref<string | null>(null); // 图片预览弹框 URL
  const imagePreviewMode = ref<string>('pop-up'); // preview_mode 配置
  const fileInputRef = ref<HTMLInputElement | null>(null);
  const fileInputAccept = ref<string>('*'); // 当前文件选择的 accept 类型

  // ===== 方法 =====

  /** 打开文件选择器 */
  const triggerFileSelect = (accept: string) => {
    fileInputAccept.value = accept;
    showAttachMenu.value = false;
    nextTick(() => {
      if (fileInputRef.value) {
        fileInputRef.value.accept = accept;
        fileInputRef.value.click();
      }
    });
  };

  /** 文件选择回调 */
  const handleFileSelected = async (e: Event) => {
    const input = e.target as HTMLInputElement;
    if (!input.files || input.files.length === 0) return;
    for (const file of Array.from(input.files)) {
      await chatStore.uploadFile(file, getAgentId());
    }
    input.value = ''; // 重置，允许重复选择同一文件
  };

  /** 文件粘贴处理（图片 / PDF / 任意文件） */
  const handlePaste = async (e: ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    let hasFile = false;
    for (const item of Array.from(items)) {
      // ✅ 扩展为处理任意文件类型（粘贴 PDF 等非图片文件也能上传为附件）
      if (item.kind === 'file') {
        const file = item.getAsFile();
        if (file) {
          hasFile = true;
          await chatStore.uploadFile(file, getAgentId());
        }
      }
    }
    if (hasFile) e.preventDefault();
  };

  /** 图片点击预览 */
  const handleImagePreview = (url: string) => {
    if (imagePreviewMode.value === 'new_tab') {
      window.open(url, '_blank');
    } else {
      previewImageUrl.value = url;
    }
  };

  /** 文件点击下载 */
  const handleFileDownload = (url: string, name: string) => {
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.target = '_blank';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  /** 格式化文件大小 */
  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  /** 加载图片预览模式配置 */
  const loadImagePreviewMode = async () => {
    try {
      const settingsRes = await fetch(`${API_BASE}/settings`);
      if (settingsRes.ok) {
        const settingsData = await settingsRes.json();
        const mode = settingsData?.conversation?.image?.preview_mode;
        if (mode) imagePreviewMode.value = mode;
      }
    } catch (e) {
      console.warn('[Attachments] Failed to load preview_mode config:', e);
    }
  };

  /** 点击附件菜单外部区域时关闭菜单 */
  const closeAttachMenuOnOutsideClick = () => {
    if (showAttachMenu.value) showAttachMenu.value = false;
  };

  // ===== 生命周期 =====

  onMounted(() => {
    document.addEventListener('click', closeAttachMenuOnOutsideClick);
  });

  onUnmounted(() => {
    document.removeEventListener('click', closeAttachMenuOnOutsideClick);
  });

  return {
    // 状态
    showAttachMenu,
    previewImageUrl,
    imagePreviewMode,
    fileInputRef,
    fileInputAccept,
    // 方法
    triggerFileSelect,
    handleFileSelected,
    handlePaste,
    handleImagePreview,
    handleFileDownload,
    formatFileSize,
    loadImagePreviewMode,
  };
}
