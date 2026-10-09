/**
 * 对话界面 - Chat
 * 主组件：组装 composables + renderers
 */
import { defineComponent, h, ref, computed, onMounted, nextTick, watch } from 'vue';
import { useRoute } from 'vue-router';
import { useChatStore } from '../../stores/chat';

// Composables
import { useAudioPlayer } from './composables/useAudioPlayer';
import { useMusicStream } from './composables/useMusicStream';
import { useTTSStream } from './composables/useTTSStream';
import { useSlashCommands } from './composables/useSlashCommands';
import { useAttachments } from './composables/useAttachments';
import { useModelConfig } from './composables/useModelConfig';

// Renderers
import { renderMessages } from './renderers/renderMessages';
import { renderInputArea } from './renderers/renderInputArea';
import type { SlashPopupContext } from './renderers/renderSlashPopup';
import type { ModelSelectorContext } from './renderers/renderModelSelector';
import type { TokenUsageContext } from './renderers/renderTokenUsage';

// 配置 marked（全局只需一次）
import { marked } from 'marked';
marked.setOptions({ breaks: true, gfm: true });

export default defineComponent({
  name: 'Chat',
  props: { agentId: { type: String, default: '' } },
  setup(props) {
    const route = useRoute();
    const chatStore = useChatStore();

    // ===== 组件级状态 =====
    const inputMessage = ref('');
    const messagesContainer = ref<HTMLElement | null>(null);
    const messageInput = ref<HTMLTextAreaElement | null>(null);

    // ===== 辅助函数 =====
    const getAgentId = () => {
      if (props.agentId) return props.agentId;
      const routeAgentId = route.params.agentId;
      return routeAgentId && typeof routeAgentId === 'string' ? routeAgentId : 'default-agent';
    };

    const focusInput = () => {
      nextTick(() => {
        if (messageInput.value) {
          messageInput.value.focus();
        }
      });
    };

    const autoResizeTextarea = () => {
      const el = messageInput.value;
      if (!el) return;
      el.style.height = 'auto';
      el.style.height = `${el.scrollHeight}px`;
    };

    const scrollToBottom = () => {
      nextTick(() => {
        if (messagesContainer.value) {
          messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight;
        }
      });
    };

    // ===== 消息列表 computed（用于传递给 composables） =====
    const messages = computed(() => chatStore.messages);

    // ===== 初始化 Composables =====

    // TTS stream 需要先创建（因为 audioPlayer 的 onTTSEnded 回调依赖它）
    // 使用延迟绑定模式解决循环依赖
    let ttsStreamInstance: ReturnType<typeof useTTSStream> | null = null;

    const audioPlayer = useAudioPlayer({
      onTTSEnded: (msgId: string) => {
        if (ttsStreamInstance) {
          ttsStreamInstance.markTTSPlaybackDone(msgId);
        }
      },
    });

    const musicStream = useMusicStream({
      audioPlayers: audioPlayer.audioPlayers,
      audioElements: audioPlayer.audioElements,
      messages,
    });

    const ttsStream = useTTSStream({
      audioPlayers: audioPlayer.audioPlayers,
      audioElements: audioPlayer.audioElements,
      messages,
      initAudioPlayer: audioPlayer.initAudioPlayer,
    });
    ttsStreamInstance = ttsStream;

    const slashCommands = useSlashCommands({
      getAgentId,
      inputMessage,
      focusInput,
      scrollToBottom,
      chatStore,
    });

    const attachments = useAttachments({
      getAgentId,
      chatStore,
    });

    const modelConfig = useModelConfig({
      getAgentId,
      chatStore,
    });

    // ===== 消息发送 =====

    const handleSend = async () => {
      if (!inputMessage.value.trim() && chatStore.pendingAttachments.length === 0) return;

      // 发送时关闭参数候选框
      if (slashCommands.showParamPrompt.value) {
        slashCommands.showParamPrompt.value = false;
        slashCommands.paramPromptData.value = null;
      }

      // 有文本时优先检查是否为斜杠命令
      if (inputMessage.value.trim()) {
        const handled = await slashCommands.handleSlashCommand();
        if (handled) return;
      }

      const msg = inputMessage.value;
      inputMessage.value = '';

      const agentId = getAgentId();
      await chatStore.sendMessage(agentId, msg);
      scrollToBottom();
    };

    const handleStop = () => {
      chatStore.stopGenerating();
    };

    // ===== Watchers =====

    // 监听 inputMessage 变化（程序化赋值时同步调整输入框高度）
    watch(inputMessage, () => {
      nextTick(autoResizeTextarea);
    });

    // 监听消息数量变化，自动滚动到底部
    watch(
      () => chatStore.messages.length,
      () => {
        if (chatStore.messages.length > 0) scrollToBottom();
      },
      { flush: 'post' }
    );

    // 监听最后一条消息内容变化（流式更新），自动滚动到底部
    watch(
      () => {
        const msgs = chatStore.messages;
        return msgs.length > 0 ? msgs[msgs.length - 1].content : '';
      },
      () => { scrollToBottom(); },
      { flush: 'post' }
    );

    // 监听会话切换
    watch(
      () => chatStore.activeConversationId,
      async (newConvId, oldConvId) => {
        focusInput();
        if (newConvId !== oldConvId) {
          ttsStream.ttsUserPaused.value = false;
        }
        if (newConvId && newConvId !== oldConvId) {
          const agentId = getAgentId();
          await modelConfig.loadSessionModelConfig(agentId, newConvId);
        }
      },
      { flush: 'post' }
    );

    // ===== 生命周期 =====

    onMounted(async () => {
      const agentId = getAgentId();
      if (chatStore.activeConversationId) {
        await chatStore.loadMessages(agentId, chatStore.activeConversationId);
        scrollToBottom();
        await modelConfig.loadSessionModelConfig(agentId, chatStore.activeConversationId);
      } else {
        await chatStore.createConversation(agentId);
      }
      focusInput();
      await modelConfig.loadLLMModels();
      await slashCommands.loadSlashCommands(agentId);
      await attachments.loadImagePreviewMode();
    });

    // ===== 构建 Renderer 上下文 =====

    const slashPopupCtx: SlashPopupContext = {
      showSlashPopup: slashCommands.showSlashPopup,
      getFilteredCommands: slashCommands.getFilteredCommands,
      getFlatCommandList: slashCommands.getFlatCommandList,
      showMoreCounts: slashCommands.showMoreCounts,
      slashSelectedIdx: slashCommands.slashSelectedIdx,
      inputMessage,
      slashFilter: slashCommands.slashFilter,
      focusInput,
      showParamPrompt: slashCommands.showParamPrompt,
      paramPromptData: slashCommands.paramPromptData,
      paramInputValues: slashCommands.paramInputValues,
      paramPromptError: slashCommands.paramPromptError,
      handleParamPromptConfirm: slashCommands.handleParamPromptConfirm,
    };

    const modelSelectorCtx: ModelSelectorContext = {
      llmModels: modelConfig.llmModels,
      sessionModelConfig: modelConfig.sessionModelConfig,
      tempModelConfig: modelConfig.tempModelConfig,
      showModelSelector: modelConfig.showModelSelector,
      currentPrimaryModel: modelConfig.currentPrimaryModel,
      showTokenUsage: modelConfig.showTokenUsage,
      saveSessionModelConfig: modelConfig.saveSessionModelConfig,
    };

    const tokenUsageCtx: TokenUsageContext = {
      messages: computed(() => chatStore.messages) as any,
      llmModels: modelConfig.llmModels,
      sessionModelConfig: modelConfig.sessionModelConfig,
      showTokenUsage: modelConfig.showTokenUsage,
      showModelSelector: modelConfig.showModelSelector,
    };

    // ===== 主渲染函数 =====

    return () => h('div', { style: 'display:flex;flex-direction:column;height:100%;' }, [
      // 消息列表
      h('div', {
        ref: messagesContainer,
        style: 'flex:1;overflow-y:auto;padding:1rem;display:flex;flex-direction:column;gap:1rem;',
      }, renderMessages({
        messages: computed(() => chatStore.messages) as any,
        isLoading: computed(() => chatStore.isLoading) as any,
        currentDag: computed(() => chatStore.currentDag) as any,
        // audio player
        audioPlayers: audioPlayer.audioPlayers,
        audioElements: audioPlayer.audioElements,
        showVolumeSlider: audioPlayer.showVolumeSlider,
        showAudioMenu: audioPlayer.showAudioMenu,
        showPlaybackRateMenu: audioPlayer.showPlaybackRateMenu,
        playbackRates: audioPlayer.playbackRates,
        formatTime: audioPlayer.formatTime,
        initAudioPlayer: audioPlayer.initAudioPlayer,
        toggleAudioPlay: audioPlayer.toggleAudioPlay,
        handleSeekInput: audioPlayer.handleSeekInput,
        handleSeekChange: audioPlayer.handleSeekChange,
        toggleMute: audioPlayer.toggleMute,
        handleVolumeChange: audioPlayer.handleVolumeChange,
        showVolumeSliderFor: audioPlayer.showVolumeSliderFor,
        hideVolumeSliderFor: audioPlayer.hideVolumeSliderFor,
        cancelVolumeHide: audioPlayer.cancelVolumeHide,
        downloadAudio: audioPlayer.downloadAudio,
        setPlaybackRate: audioPlayer.setPlaybackRate,
        toggleAudioMenu: audioPlayer.toggleAudioMenu,
        // music stream
        streamPlaying: musicStream.streamPlaying,
        streamProgressCache: musicStream.streamProgressCache,
        streamSettingsCache: musicStream.streamSettingsCache,
        toggleStreamPlay: musicStream.toggleStreamPlay,
        // tts stream
        ttsStreamPlaying: ttsStream.ttsStreamPlaying,
        ttsStreamProgressCache: ttsStream.ttsStreamProgressCache,
        ttsStreamSettingsCache: ttsStream.ttsStreamSettingsCache,
        ttsPlaybackDone: ttsStream.ttsPlaybackDone,
        ttsSessionActive: ttsStream.ttsSessionActive,
        ttsUserPaused: ttsStream.ttsUserPaused,
        toggleTTSStreamPlay: ttsStream.toggleTTSStreamPlay,
        isTTSTurnToPlay: ttsStream.isTTSTurnToPlay,
        // attachments
        handleImagePreview: attachments.handleImagePreview,
        handleFileDownload: attachments.handleFileDownload,
        formatFileSize: attachments.formatFileSize,
      })),
      // 输入区域
      ...renderInputArea({
        inputMessage,
        messageInput,
        fileInputRef: attachments.fileInputRef,
        isLoading: computed(() => chatStore.isLoading) as any,
        pendingAttachments: computed(() => chatStore.pendingAttachments) as any,
        showAttachMenu: attachments.showAttachMenu,
        previewImageUrl: attachments.previewImageUrl,
        triggerFileSelect: attachments.triggerFileSelect,
        handleFileSelected: attachments.handleFileSelected,
        handlePaste: attachments.handlePaste,
        formatFileSize: attachments.formatFileSize,
        removeAttachment: (fileId: string) => chatStore.removeAttachment(fileId),
        slashPopupCtx,
        modelSelectorCtx,
        tokenUsageCtx,
        handleSend,
        handleStop,
        autoResizeTextarea,
        focusInput,
        getFlatCommandList: slashCommands.getFlatCommandList as any,
        showSlashPopup: slashCommands.showSlashPopup,
        slashSelectedIdx: slashCommands.slashSelectedIdx,
        slashFilter: slashCommands.slashFilter,
        showParamPrompt: slashCommands.showParamPrompt,
        paramPromptData: slashCommands.paramPromptData as any,
        paramPromptError: slashCommands.paramPromptError,
      }),
    ]);
  },
});
