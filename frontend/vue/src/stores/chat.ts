/**
 * Chat状态管理 - Pinia Store
 */
import { defineStore } from 'pinia';
import { ref } from 'vue';
import { useEventBus } from '../composables/useEventBus';

const API_BASE = 'http://localhost:8000/api/v1';

interface AttachmentInfo {
  file_id: string;
  url: string;
  name: string;
  size: number;
  mime_type: string;
  is_image: boolean;
  stored_filename: string;
  subdir: string;
  local_preview?: string; // 本地 blob URL（上传前预览）
}

interface Conversation {
  id: string;
  title: string;
  message_count: number;
}

interface ChatMessage {
  id: string;
  role: string;
  content: string;
  message_type: string;
  created_at: string;
  tool_calls?: Array<{ // assistant消息的工具调用列表
    id: string;
    type: string;
    function: {
      name: string;
      arguments: string;
    };
  }>;
  tool_results?: Array<{ // 工具调用结果列表（内嵌在assistant消息中）
    tool_call_id: string;
    name: string;
    success: boolean;
    result: any;
    error?: string;
    dag?: any; // DAG 可视化数据
    structured?: any; // MCP structuredContent（后端置于 metadata，仅前端友好展示）
  }>;
  metadata?: any; // ✅ 消息元数据（token 用量、费用等）
}

export const useChatStore = defineStore('chat', () => {
  const conversations = ref<Conversation[]>([]);
  // ✅ 从 localStorage 恢复上次激活的会话ID
  const savedConvId = localStorage.getItem('active_conversation_id');
  const activeConversationId = ref<string | null>(savedConvId || null);
  // ✅ 当前激活会话是否为「新建但未发送任何消息」的 NEW 会话（用于复用判断与激活后清理标记）
  const activeIsNewConversation = ref<boolean>(false);
  const messages = ref<ChatMessage[]>([]);
  const isLoading = ref(false);
  // ✅ 新会话（尚未创建）预选的会话级模型配置，发送首条消息时随请求提交
  const pendingSessionModel = ref<{provider: string; model_id: string; reasoning_level?: string; speed_mode?: string} | null>(null);
  // ✅ 待发送附件列表
  const pendingAttachments = ref<AttachmentInfo[]>([]);
  // ✅ 多会话并发：每会话独立的流控制
  const activeStreams = new Map<string, AbortController>(); // conversationId -> AbortController
  const scheduledInFlight = new Set<string>(); // 已触发自动发送的 scheduled 消息ID（防重复）
  let lastUsedAgentId = 'default-agent'; // 最近一次请求使用的 agentId（定时任务事件缺失 agent_id 时兜底）

  // ========== 事件总线集成 ==========
  const eventBus = useEventBus();

  // 实时 DAG 状态（通过事件总线接收）
  const currentDag = ref<Record<string, any> | null>(null);

  // ✅ 订阅全局事件维度，确保非当前会话的定时任务消息也能送达（前端按 conversation_id 过滤）
  eventBus.subscribe(['all']);

  // 监听实时消息事件
  eventBus.onEvent('message', (event) => {
    console.log('[Chat] Received message:', event.data);
    const msgData = event.data;
    if (!msgData || !msgData.role || msgData.content === undefined) return;

    const isScheduled = msgData.message_type === 'scheduled';
    const targetConvId: string | null = msgData.conversation_id || null;
    const isActiveConv = !targetConvId || targetConvId === activeConversationId.value;

    if (!isActiveConv) {
      // ✅ 非当前会话：定时任务消息仍需自动触发 Agent 回复（切回会话后可见完整对话）
      if (isScheduled) {
        const agentId = msgData.agent_id || lastUsedAgentId;
        autoSendScheduled(agentId, targetConvId!, msgData.id, msgData.content);
      }
      return;
    }

    // 当前会话：追加或更新消息
    const existingIdx = messages.value.findIndex(m => m.id === msgData.id);
    if (existingIdx !== -1) {
      const existing = messages.value[existingIdx];
      const merged = { ...existing, ...msgData };
      // 深合并 metadata：保留已有字段（如 tts/music 流式数据），避免被空对象覆盖
      if (existing.metadata && msgData.metadata !== undefined && msgData.metadata !== null) {
        merged.metadata = { ...existing.metadata, ...msgData.metadata };
      } else if (existing.metadata && (msgData.metadata === undefined || msgData.metadata === null || (typeof msgData.metadata === 'object' && Object.keys(msgData.metadata).length === 0))) {
        //  incoming metadata 为空/null/undefined 时保留已有 metadata
        merged.metadata = existing.metadata;
      }
      messages.value[existingIdx] = merged;
    } else {
      messages.value.push({
        id: msgData.id,
        role: msgData.role,
        content: msgData.content,
        message_type: msgData.message_type || 'text',
        created_at: msgData.created_at || new Date().toISOString(),
        tool_calls: msgData.tool_calls,
      });
    }

    // ✅ 定时任务消息：自动发起请求（相当于点击发送按钮）
    if (isScheduled) {
      const agentId = msgData.agent_id || lastUsedAgentId;
      autoSendScheduled(agentId, targetConvId || activeConversationId.value!, msgData.id, msgData.content);
    }
  });

  // 监听任务状态事件
  eventBus.onEvent('task_status', (event) => {
    const { status } = event.data;
    if (status === 'completed' || status === 'failed' || status === 'idle') {
      if (!activeStreams.has(activeConversationId.value || '')) {
        isLoading.value = false;
      }
    } else if (status === 'running' || status === 'processing') {
      isLoading.value = true;
    }
  });

  // 监听 Agent 进度事件（已被节流）
  eventBus.onEvent('agent_progress', (event) => {
    // 进度事件可用于更新 UI 状态（如进度条、状态文本等）
    console.log('[Chat] Agent progress:', event.data.message, event.data.phase);
  });

  // 监听 DAG 实时更新事件
  eventBus.onEvent('dag_update', (event) => {
    const dagData = event.data?.dag;
    console.log('[Chat] DAG update:', dagData);
    if (dagData) {
      currentDag.value = dagData;
    }
  });

  // ✅ 监听图片生成完成事件
  eventBus.onEvent('image_generated', (event) => {
    const data = event.data;
    console.log('[Chat] Image generated:', data);
    if (!data) return;

    const targetConvId = data.conversation_id;
    // 仅当前会话显示
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    // 构建图片消息
    const images = data.images || [];
    const msgId = data.message_id || `img_gen_${data.job_id || Date.now()}`;

    // 检查是否已存在（避免重复）
    const existingIdx = messages.value.findIndex(m => m.id === msgId);
    const newMsg: ChatMessage = {
      id: msgId,
      role: 'tool',
      content: data.status === 'completed'
        ? JSON.stringify({ success: true, images, model: data.model, details: data.details })
        : JSON.stringify({ success: false, error: data.error || '生成失败' }),
      message_type: 'image_generated',
      created_at: new Date().toISOString(),
      metadata: {
        images,
        tool_name: 'image_generate',
        job_id: data.job_id,
        status: data.status,
        model: data.model,
        prompt: data.prompt,
        details: data.details,
        response_meta: data.details?.response_meta,
        error: data.error,
      },
    };

    if (existingIdx !== -1) {
      messages.value[existingIdx] = newMsg;
    } else {
      messages.value.push(newMsg);
    }
  });

  // ✅ 监听音乐流式生成开始事件
  eventBus.onEvent('music_stream_start', (event) => {
    const data = event.data;
    console.log('[Chat] Music stream start:', data);
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const msgId = data.message_id || `music_stream_${data.job_id || Date.now()}`;
    const existingIdx = messages.value.findIndex(m => m.id === msgId);
    if (existingIdx !== -1) return; // 已存在，跳过

    const newMsg: ChatMessage = {
      id: msgId,
      role: 'tool',
      content: '',
      message_type: 'music_generated',
      created_at: new Date().toISOString(),
      metadata: {
        tool_name: 'music_generate',
        job_id: data.job_id,
        status: 'streaming',
        model: data.model,
        mode: data.mode,
        prompt: data.prompt,
        streaming: true,
        songChunks: {} as Record<string, string[]>,
        audios: [],
        lyrics: '',
      },
    };
    messages.value.push(newMsg);
  });

  // ✅ 监听音乐流式数据块事件
  eventBus.onEvent('music_stream_chunk', (event) => {
    const data = event.data;
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const jobId = data.job_id;
    // 找到对应的流式消息
    const msgIdx = messages.value.findIndex(
      m => m.message_type === 'music_generated' && m.metadata?.job_id === jobId && m.metadata?.streaming
    );
    if (msgIdx === -1) return;

    // 按 song_id 分组累积 chunk 数据
    const msg = messages.value[msgIdx];
    const songId = data.song_id || '0';
    if (!msg.metadata.songChunks) msg.metadata.songChunks = {};
    if (!msg.metadata.songChunks[songId]) msg.metadata.songChunks[songId] = [];
    msg.metadata.songChunks[songId].push(data.data);
    // 触发响应式更新
    messages.value[msgIdx] = { ...msg };
  });

  // ✅ 监听音乐生成完成事件
  eventBus.onEvent('music_generated', (event) => {
    const data = event.data;
    console.log('[Chat] Music generated:', data);
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const audios = data.audios || [];
    const msgId = data.message_id || `music_gen_${data.job_id || Date.now()}`;

    const existingIdx = messages.value.findIndex(m => m.id === msgId);
    const newMsg: ChatMessage = {
      id: msgId,
      role: 'tool',
      content: data.status === 'completed'
        ? JSON.stringify({ success: true, audios, model: data.model, lyrics: data.lyrics, details: data.details })
        : JSON.stringify({ success: false, error: data.error || '生成失败' }),
      message_type: 'music_generated',
      created_at: new Date().toISOString(),
      metadata: {
        audios,
        tool_name: 'music_generate',
        job_id: data.job_id,
        status: data.status,
        model: data.model,
        mode: data.mode,
        prompt: data.prompt,
        lyrics: data.lyrics || '',
        title: data.title || '',
        details: data.details,
        response_meta: data.details?.response_meta,
        error: data.error,
        streaming: false,
      },
    };

    if (existingIdx !== -1) {
      messages.value[existingIdx] = newMsg;
    } else {
      // 也查找流式消息并替换
      const streamIdx = messages.value.findIndex(
        m => m.message_type === 'music_generated' && m.metadata?.job_id === data.job_id && m.metadata?.streaming
      );
      if (streamIdx !== -1) {
        messages.value[streamIdx] = newMsg;
      } else {
        messages.value.push(newMsg);
      }
    }
  });

  // ✅ 监听视频生成完成事件
  eventBus.onEvent('video_generated', (event) => {
    const data = event.data;
    console.log('[Chat] Video generated:', data);
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const videos = data.videos || [];
    const msgId = data.message_id || `video_gen_${data.job_id || Date.now()}`;

    const existingIdx = messages.value.findIndex(m => m.id === msgId);
    const newMsg: ChatMessage = {
      id: msgId,
      role: 'tool',
      content: data.status === 'completed'
        ? JSON.stringify({ success: true, videos, model: data.model, details: data.details })
        : JSON.stringify({ success: false, error: data.error || '生成失败' }),
      message_type: 'video_generated',
      created_at: new Date().toISOString(),
      metadata: {
        videos,
        tool_name: 'video_generate',
        job_id: data.job_id,
        status: data.status,
        model: data.model,
        prompt: data.prompt,
        details: data.details,
        response_meta: data.details?.response_meta,
        error: data.error,
      },
    };

    if (existingIdx !== -1) {
      messages.value[existingIdx] = newMsg;
    } else {
      messages.value.push(newMsg);
    }
  });

  // ✅ 监听 PDF 解析完成事件
  eventBus.onEvent('pdf_parsed', (event) => {
    const data = event.data;
    console.log('[Chat] PDF parsed:', data);
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const files = data.files || [];
    const msgId = data.message_id || `pdf_${data.job_id || Date.now()}`;

    const existingIdx = messages.value.findIndex(m => m.id === msgId);
    const newMsg: ChatMessage = {
      id: msgId,
      role: 'tool',
      content: data.status === 'completed'
        ? JSON.stringify({ success: true, files, provider: data.provider, details: data.details })
        : JSON.stringify({ success: false, error: data.error || '解析失败' }),
      message_type: 'pdf_parsed',
      created_at: new Date().toISOString(),
      metadata: {
        files,
        tool_name: 'pdf_parse',
        job_id: data.job_id,
        status: data.status,
        provider: data.provider,
        details: data.details,
        error: data.error,
      },
    };

    if (existingIdx !== -1) {
      messages.value[existingIdx] = newMsg;
    } else {
      messages.value.push(newMsg);
    }
  });
  eventBus.onEvent('tts_stream_start', (event) => {
    const data = event.data;
    console.log('[Chat] TTS stream start:', data);
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const msgId = data.message_id;
    if (!msgId) return;

    // 找到对应的 assistant 消息，添加 tts metadata
    const msgIdx = messages.value.findIndex(m => m.id === msgId);
    if (msgIdx !== -1) {
      const msg = messages.value[msgIdx];
      msg.metadata = msg.metadata || {};
      msg.metadata.tts = {
        status: 'streaming',
        chunks: [] as string[],
        provider: data.provider,
        model: data.model,
        voice: data.voice,
        stream_mode: data.stream_mode,
      };
      messages.value[msgIdx] = { ...msg };
    }
  });

  // ✅ 监听 TTS 流式音频数据块事件
  eventBus.onEvent('tts_stream_chunk', (event) => {
    const data = event.data;
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const msgId = data.message_id;
    if (!msgId) return;

    const msgIdx = messages.value.findIndex(m => m.id === msgId);
    if (msgIdx === -1) return;

    const msg = messages.value[msgIdx];
    // 容错：如果 tts_stream_start 未到达或消息刚创建，自动初始化 tts metadata
    if (!msg.metadata) msg.metadata = {};
    if (!msg.metadata.tts) {
      msg.metadata.tts = {
        status: 'streaming',
        chunks: [] as string[],
        provider: data.provider || '',
        model: '',
        voice: '',
        stream_mode: 'two-way',
      };
    }

    // 追加 base64 音频数据
    msg.metadata.tts.chunks.push(data.audio_base64);
    messages.value[msgIdx] = { ...msg };
  });

  // ✅ 监听 TTS 流式合成完成事件
  eventBus.onEvent('tts_stream_end', (event) => {
    const data = event.data;
    console.log('[Chat] TTS stream end:', data);
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const msgId = data.message_id;
    if (!msgId) return;

    const msgIdx = messages.value.findIndex(m => m.id === msgId);
    if (msgIdx === -1) return;

    const msg = messages.value[msgIdx];
    msg.metadata = msg.metadata || {};

    if (msg.metadata.tts) {
      msg.metadata.tts.status = data.error ? 'error' : 'complete';
      msg.metadata.tts.audio_url = data.audio_url || '';
      msg.metadata.tts.duration_ms = data.duration_ms || 0;
      msg.metadata.tts.file_size = data.file_size || 0;
      msg.metadata.tts.error = data.error || '';
    } else {
      msg.metadata.tts = {
        status: data.error ? 'error' : 'complete',
        audio_url: data.audio_url || '',
        duration_ms: data.duration_ms || 0,
        provider: data.provider,
        error: data.error || '',
      };
    }
    messages.value[msgIdx] = { ...msg };
  });

  // ✅ 监听 TTS 非流式合成完成事件
  eventBus.onEvent('tts_generated', (event) => {
    const data = event.data;
    console.log('[Chat] TTS generated:', data);
    if (!data) return;

    const targetConvId = data.conversation_id;
    if (targetConvId && targetConvId !== activeConversationId.value) return;

    const msgId = data.message_id;
    if (!msgId) return;

    const msgIdx = messages.value.findIndex(m => m.id === msgId);
    if (msgIdx === -1) return;

    const msg = messages.value[msgIdx];
    msg.metadata = msg.metadata || {};
    msg.metadata.tts = {
      status: data.error ? 'error' : 'complete',
      audio_url: data.audio_url || '',
      duration_ms: data.duration_ms || 0,
      file_size: data.file_size || 0,
      provider: data.provider,
      text_preview: data.text_preview || '',
      error: data.error || '',
    };
    messages.value[msgIdx] = { ...msg };
  });

  async function loadConversations(agentId: string) {
    try {
      // ✅ exclude_id：保护当前激活的 NEW 会话不被后端清理逻辑删除
      const excludeId = activeConversationId.value || '';
      const res = await fetch(`${API_BASE}/chat/conversations?agent_id=${agentId}&exclude_id=${encodeURIComponent(excludeId)}`);
      const data = await res.json();
      conversations.value = data.conversations || [];
      
      // ✅ 如果有保存的会话ID且存在于列表中,则恢复它
      const savedConvId = localStorage.getItem('active_conversation_id');
      if (savedConvId && conversations.value.some(c => c.id === savedConvId)) {
        activeConversationId.value = savedConvId;
      } else if (conversations.value.length > 0 && !activeConversationId.value) {
        // 否则选择第一个会话
        activeConversationId.value = conversations.value[0].id;
      }
    } catch (e) {
      console.error('Failed to load conversations:', e);
    }
  }

  async function loadMessages(agentId: string, conversationId: string) {
    try {
      const res = await fetch(`${API_BASE}/chat/conversations/${conversationId}?agent_id=${agentId}`);
      const data = await res.json();
      
      // 第一步：收集所有 tool 消息到 Map 中，key 为 tool_call_id
      const toolResultsMap = new Map<string, any>();
      for (const msg of data.messages || []) {
        if (msg.role === 'tool' && msg.tool_call_id) {
          // 解析工具结果
          let toolResult: any = {};
          try {
            toolResult = JSON.parse(msg.content || '{}');
          } catch {
            toolResult = { raw_content: msg.content };
          }
          
          toolResultsMap.set(msg.tool_call_id, {
            tool_call_id: msg.tool_call_id,
            name: msg.name || 'unknown',
            success: toolResult.success !== false,
            result: toolResult,
            error: toolResult.error,
            dag: msg.metadata?.dag ?? toolResult.dag, // DAG 可视化数据（后端置于 metadata，兼容旧持久化数据）
            structured: msg.metadata?.structured_content, // MCP 结构化结果（仅前端展示）
          });
        }
      }
      
      // 第二步：遍历消息，将 tool 结果匹配到对应的 assistant 消息
      const processedMessages: ChatMessage[] = [];
      
      for (const msg of data.messages || []) {
        // ✅ 过滤掉 system 角色的消息（系统提示词不显示）
        if (msg.role === 'system') {
          continue;
        }
        
        if (msg.role === 'tool') {
          // ✅ image_generated / music_generated / video_generated / pdf_parsed 类型的 tool 消息需独立显示（不匹配到 assistant 消息）
          if (msg.message_type === 'image_generated' || msg.message_type === 'music_generated' || msg.message_type === 'video_generated' || msg.message_type === 'pdf_parsed') {
            processedMessages.push(msg);
            continue;
          }
          // 其他 tool 消息：跳过，已在上面收集到 Map 中
          continue;
        }
        
        // 非 tool 消息：直接添加
        processedMessages.push(msg);
        
        // ✅ 如果是 assistant 消息且有 tool_calls，尝试匹配 tool 结果（不限制 message_type）
        if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
          // 从 tool_calls 中提取所有 tool_call_id
          const toolCallIds = msg.tool_calls.map((tc: any) => tc.id).filter(Boolean);
          
          // 查找匹配的 tool 结果并添加到 tool_results
          const matchedToolResults = [];
          for (const toolCallId of toolCallIds) {
            if (toolResultsMap.has(toolCallId)) {
              matchedToolResults.push(toolResultsMap.get(toolCallId));
            }
          }
          
          if (matchedToolResults.length > 0) {
            msg.tool_results = matchedToolResults;
          }
        }
      }
      
      messages.value = processedMessages;
    } catch (e) {
      console.error('Failed to load messages:', e);
    }
  }

  async function createConversation(agentId: string) {
    // ✅ 若当前已持有一个未使用的 NEW 会话，直接复用，不重复请求后端
    if (activeIsNewConversation.value && activeConversationId.value && messages.value.length === 0) {
      return activeConversationId.value;
    }
    // ✅ 清空当前会话状态（新会话不加入列表，等待发送消息激活后再显示）
    messages.value = [];
    pendingSessionModel.value = null;
    clearAttachments();
    isLoading.value = false;
    lastUsedAgentId = agentId;
    try {
      const res = await fetch(`${API_BASE}/chat/conversations/new?agent_id=${encodeURIComponent(agentId)}`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      activeConversationId.value = data.conversation_id;
      localStorage.setItem('active_conversation_id', data.conversation_id);
      activeIsNewConversation.value = true;
      return data.conversation_id;
    } catch (e) {
      console.error('Failed to create new conversation:', e);
      activeConversationId.value = null;
      localStorage.removeItem('active_conversation_id');
      activeIsNewConversation.value = false;
      return null;
    }
  }

  /** ✅ 设置/清除新会话预选模型配置（无激活会话时暂存，发送首条消息时提交） */
  function setPendingSessionModel(cfg: {provider: string; model_id: string; reasoning_level?: string; speed_mode?: string} | null) {
    pendingSessionModel.value = cfg;
  }

  async function selectConversation(agentId: string, id: string) {
    activeConversationId.value = id;
    // ✅ 切换到已有会话，不再是 NEW 会话
    activeIsNewConversation.value = false;
    // ✅ 切换到已有会话，清除新会话预选模型暂存
    pendingSessionModel.value = null;
    // ✅ 保存到 localStorage
    localStorage.setItem('active_conversation_id', id);
    // 加载选中会话的消息（切换不打断任何进行中的流，后台流继续执行）
    await loadMessages(agentId, id);
    // ✅ isLoading 反映目标会话是否有进行中的流
    isLoading.value = activeStreams.has(id);
    currentDag.value = null;
  }

  /** 停止生成：仅中止当前会话的流 + 通知后端停止Agent */
  async function stopGenerating() {
    const convId = activeConversationId.value;

    // 1. 中止前端 SSE 流式请求（仅当前会话）
    if (convId && activeStreams.has(convId)) {
      activeStreams.get(convId)!.abort();
      activeStreams.delete(convId);
    }
    isLoading.value = false;
    
    // 2. 通知后端停止 Agent 生成（节省 LLM token）
    if (convId && lastUsedAgentId) {
      try {
        await fetch(`${API_BASE}/chat/cancel?agent_id=${lastUsedAgentId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: convId }),
        });
        console.log('Cancel request sent to backend for conversation:', convId);
      } catch (e) {
        console.error('Failed to send cancel request:', e);
      }
    } else {
      console.warn('Cannot send cancel request: missing conversation_id or agent_id', { convId });
    }
  }

  /**
   * 内部流式请求执行（支持多会话并发）
   *
   * @param agentId        Agent ID
   * @param conversationId 会话ID（新会话传 null，由后端创建并通过 init 事件返回）
   * @param message        用户消息文本
   * @param opts.pushUserMessage  是否本地追加用户消息（定时任务已由事件处理追加，传 false）
   * @param opts.consumeUserMessageId 已持久化的用户消息ID（后端跳过重复追加）
   * @param opts.onConversationCreated 新会话创建后的回调（用于刷新列表/订阅）
   */
  async function _runStream(
    agentId: string,
    conversationId: string | null,
    message: string,
    opts: {
      pushUserMessage?: boolean;
      consumeUserMessageId?: string;
      onConversationCreated?: (convId: string) => void;
    } = {},
  ) {
    const abortController = new AbortController();
    // ✅ 会话作用域：流期间的 UI 更新仅在该会话仍为激活会话时生效
    let streamConvId: string | null = conversationId;
    if (streamConvId) {
      activeStreams.set(streamConvId, abortController);
    }
    lastUsedAgentId = agentId;

    // ✅ 是否为「刚新建、尚未发送消息」的 NEW 会话（用于决定是否携带预选会话模型）
    // 注意：在发送请求前捕获，因为 init 事件处理会将 activeIsNewConversation 置 false
    const isNewConversation = !conversationId
      || (activeIsNewConversation.value && conversationId === activeConversationId.value);

    const isActiveStream = () => !!streamConvId && streamConvId === activeConversationId.value;

    if (isActiveStream()) {
      isLoading.value = true;
      currentDag.value = null; // 重置实时 DAG 状态
    }

    // ✅ 发送前立即快照并清空待发送附件：输入框缩略图即时消失，
    // 不依赖响应头到达时机（响应头慢时会出现"附件残留到回复后才消失"）
    // 请求失败时在 catch 中恢复（local_preview 未释放，可直接放回）
    const sentAttachments = pendingAttachments.value.slice();
    if (sentAttachments.length > 0) {
      pendingAttachments.value = [];
    }
    // ✅ 标记请求是否已成功发出（响应头到达）：流中途停止时附件已被后端消费，不应恢复
    let requestSent = false;

    try {
      // 添加用户消息（定时任务消息已由事件处理追加，跳过）
      if (opts.pushUserMessage !== false) {
        const userMsg: ChatMessage = {
          id: `user-${Date.now()}`,
          role: 'user',
          content: message,
          message_type: 'text',
          created_at: new Date().toISOString(),
        };
        // ✅ 本地消息附带附件 metadata（即时显示，url 为工作区文件路径，不受 blob 释放影响）
        if (sentAttachments.length > 0) {
          // 注意：url 上传时即为会话工作区最终路径，无需后端重写
          userMsg.metadata = {
            attachments: sentAttachments.map(a => ({
              file_id: a.file_id,
              name: a.name,
              url: a.url,
              size: a.size,
              mime_type: a.mime_type,
              is_image: a.is_image,
              stored_filename: a.stored_filename,
              subdir: a.subdir,
            })),
          };
        }
        messages.value.push(userMsg);
      }
      
      // ✅ 预添加空的 assistant 占位消息，使圆点动画能在等待回复时显示
      let currentMessageId: string | null = `assistant-placeholder-${Date.now()}`;
      let lastAssistantMessageIndex: number = -1;
      messages.value.push({
        id: currentMessageId,
        role: 'assistant',
        content: '',
        message_type: 'text',
        created_at: new Date().toISOString(),
      });
      if (isActiveStream()) {
        lastAssistantMessageIndex = messages.value.length - 1;
      }
      
      // ✅ 新建会话时 streamConvId 为 null，isActiveStream() 为 false，
      // 但用户正在等待回复，需立即设置 isLoading 以显示圆点动画
      if (!streamConvId) {
        isLoading.value = true;
      }
      
      // 使用流式API
      const response = await fetch(`${API_BASE}/chat/stream?agent_id=${agentId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message,
          // ✅ 如果已有会话ID则传递，否则后端自动创建
          conversation_id: conversationId || undefined,
          consume_user_message_id: opts.consumeUserMessageId || undefined,
          // ✅ 新会话（含预创建的 NEW 会话）：携带预选的会话级模型配置，后端激活/创建会话后立即应用
          session_model: (isNewConversation && pendingSessionModel.value) ? pendingSessionModel.value : undefined,
          // ✅ 附件信息（使用发送前快照）
          attachments: sentAttachments.length > 0
            ? sentAttachments.map(a => ({
                file_id: a.file_id,
                name: a.name,
                url: a.url,  // ✅ 携带 url 用于前端显示
                size: a.size,
                mime_type: a.mime_type,
                is_image: a.is_image,
                stored_filename: a.stored_filename,
                subdir: a.subdir,
              }))
            : undefined,
        }),
        signal: abortController.signal, // 绑定中止信号
      });
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      requestSent = true;
      // ✅ 响应已到达：释放附件的 blob 预览（发送前已清空列表，此处仅回收内存）
      for (const att of sentAttachments) {
        if (att.local_preview) URL.revokeObjectURL(att.local_preview);
      }
      
      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let buffer = ''; // ✅ 跨 chunk 缓冲区，处理数据截断和粘包
      
      // ✅ SSE 事件处理函数（主循环和流结束缓冲区复用）
      const processSSEEvent = (parsed: any) => {
        // 处理初始化消息（立即返回 conversation_id）
        if (parsed.type === 'init' && parsed.conversation_id) {
          const convId: string = String(parsed.conversation_id);
          const wasNewConversation = !streamConvId; // ✅ 新会话场景：streamConvId 之前为 null
          streamConvId = convId;
          activeStreams.set(convId, abortController);
          if (isActiveStream() || wasNewConversation) {
            activeConversationId.value = convId;
            isLoading.value = true;
            // ✅ 新建会话时，补上占位消息的 index（发送前 streamConvId 为 null，isActiveStream 为 false）
            if (lastAssistantMessageIndex === -1) {
              const idx = messages.value.findIndex(m => m.id === currentMessageId);
              if (idx !== -1) lastAssistantMessageIndex = idx;
            }
          }
          eventBus.subscribe([`conversation:${parsed.conversation_id}`, `agent:${agentId}`]);
          console.log('Subscribed to conversation:', `conversation:${parsed.conversation_id}`, `agent:${agentId}`);
          // ✅ 会话已激活（NEW->ACTIVE 在后端 init 之前完成），清除本地 NEW 标记并刷新会话列表
          activeIsNewConversation.value = false;
          loadConversations(agentId);
          opts.onConversationCreated?.(parsed.conversation_id);
          // ✅ 会话已创建，预选模型已由后端持久化，清除暂存
          pendingSessionModel.value = null;
          return;
        }
        
        // ✅ 非激活会话的后台流：仅维持请求，不更新 UI（切回时从 DB 重新加载）
        if (!isActiveStream()) {
          return;
        }

        // 保存后端返回的 conversation_id
        if (parsed.conversation_id) {
          activeConversationId.value = parsed.conversation_id;
        }
        
        // 检查是否有新的消息ID
        if (parsed.id && parsed.role) {
          if (parsed.role === 'tool') {
            // tool 消息：附加到最后一个 assistant 消息
            if (lastAssistantMessageIndex !== -1) {
              const assistantMsg = messages.value[lastAssistantMessageIndex];
              if (!assistantMsg.tool_results) {
                assistantMsg.tool_results = [];
              }
              let toolResult: any = {};
              try {
                toolResult = JSON.parse(parsed.content || '{}');
              } catch {
                toolResult = { raw_content: parsed.content };
              }
              assistantMsg.tool_results.push({
                tool_call_id: parsed.id,
                name: parsed.name || 'unknown',
                success: toolResult.success !== false,
                result: toolResult,
                error: toolResult.error,
                dag: parsed.metadata?.dag ?? toolResult.dag, // DAG 可视化数据（后端置于 metadata，兼容旧数据）
                structured: parsed.metadata?.structured_content, // MCP 结构化结果（仅前端展示）
              });
            }
          } else {
            // 非 tool 消息
            if (parsed.id !== currentMessageId) {
              // ✅ 如果当前是占位消息，替换它而不是新增
              if (currentMessageId && currentMessageId.startsWith('assistant-placeholder-')) {
                const placeholderIdx = messages.value.findIndex(m => m.id === currentMessageId);
                if (placeholderIdx !== -1) {
                  messages.value[placeholderIdx] = {
                    id: parsed.id,
                    role: parsed.role,
                    content: parsed.content || '',
                    message_type: parsed.message_type || 'text',
                    created_at: new Date().toISOString(),
                    tool_calls: parsed.tool_calls,
                    metadata: parsed.metadata, // ✅ 保留 token 用量/费用等元数据
                  };
                  currentMessageId = parsed.id;
                  if (parsed.role === 'assistant') {
                    lastAssistantMessageIndex = placeholderIdx;
                  }
                }
              } else {
                currentMessageId = parsed.id;
                const newMessage: ChatMessage = {
                  id: parsed.id,
                  role: parsed.role,
                  content: parsed.content || '',
                  message_type: parsed.message_type || 'text',
                  created_at: new Date().toISOString(),
                  tool_calls: parsed.tool_calls,
                  metadata: parsed.metadata, // ✅ 保留 token 用量/费用等元数据
                };
                messages.value.push(newMessage);
                if (parsed.role === 'assistant') {
                  lastAssistantMessageIndex = messages.value.length - 1;
                }
              }
            } else {
              // 更新当前消息
              const msgIndex = messages.value.findIndex(m => m.id === currentMessageId);
              if (msgIndex !== -1) {
                if (parsed.content) {
                  messages.value[msgIndex].content = parsed.content;
                }
                if (parsed.tool_calls) {
                  messages.value[msgIndex].tool_calls = parsed.tool_calls;
                }
                if (parsed.metadata && Object.keys(parsed.metadata).length > 0) {
                  // 深合并 metadata：保留已有字段（如 tts/music 流式数据）
                  const existingMeta = messages.value[msgIndex].metadata;
                  messages.value[msgIndex].metadata = existingMeta
                    ? { ...existingMeta, ...parsed.metadata }
                    : parsed.metadata;
                }
              }
            }
          }
        }
      };
      
      // ✅ 让出控制权给事件循环，使 Vue 能在每次内容更新后渲染（打字机效果）
      const yieldToRenderer = () => new Promise<void>(resolve => setTimeout(resolve, 0));
      
      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) {
            // ✅ 流结束后，处理缓冲区中剩余的数据
            if (buffer.trim()) {
              const remainingEvents = buffer.split('\n\n');
              for (const event of remainingEvents) {
                const dataLines = event.split('\n')
                  .filter(line => line.startsWith('data: '))
                  .map(line => line.slice(6));
                for (const data of dataLines) {
                  if (data === '[DONE]' || !data.trim()) continue;
                  try {
                    processSSEEvent(JSON.parse(data));
                    await yieldToRenderer(); // ✅ 每次更新后让出，触发 Vue 渲染
                  } catch {
                    // 流末尾的不完整数据，静默忽略
                  }
                }
              }
            }
            break;
          }
          
          // ✅ 将新数据追加到缓冲区
          buffer += decoder.decode(value, { stream: true });
          
          // ✅ 按双换行分割完整的 SSE 事件（SSE 协议以 \n\n 分隔事件）
          const events = buffer.split('\n\n');
          // 最后一个元素可能是不完整的事件，保留在缓冲区
          buffer = events.pop() || '';
          
          for (const event of events) {
            const dataLines = event.split('\n')
              .filter(line => line.startsWith('data: '))
              .map(line => line.slice(6));
            
            for (const data of dataLines) {
              if (data === '[DONE]') continue;
              try {
                processSSEEvent(JSON.parse(data));
                await yieldToRenderer(); // ✅ 每次更新后让出，触发 Vue 渲染（打字机效果）
              } catch (e) {
                console.error('Failed to parse SSE data:', e);
              }
            }
          }
        }
      }
      
      return true;
    } catch (e: any) {
      // ✅ 请求未成功发出即失败：恢复发送前清空的附件，用户可修正后重发（blob 预览未释放，仍可用）
      if (!requestSent && sentAttachments.length > 0) {
        pendingAttachments.value = sentAttachments;
      }
      if (e.name === 'AbortError') {
        console.log('Stream aborted by user');
      } else {
        console.error('Failed to send message:', e);
        // 移除失败的消息（仅激活会话且由本次调用追加时）
        // ✅ 新建会话场景：init 未到达时 isActiveStream 为 false，也需清理占位消息
        if (opts.pushUserMessage !== false && (isActiveStream() || !streamConvId)) {
          messages.value.pop();
          messages.value.pop();
        }
      }
      return false;
    } finally {
      if (streamConvId) {
        activeStreams.delete(streamConvId);
      }
      // ✅ 仅当结束的是当前激活会话的流时，才更新 isLoading
      // ✅ 新建会话场景：init 未到达时 streamConvId 为 null，也需重置 isLoading
      if (isActiveStream() || !streamConvId) {
        isLoading.value = false;
      }
    }
  }

  async function sendMessage(agentId: string, message: string) {
    // ✅ 不传 conversation_id（新消息场景复用当前会话），后端自动创建新会话或沿用
    await _runStream(agentId, activeConversationId.value, message);
    return { message: messages.value[messages.value.length - 1] };
  }

  // ========== 附件管理 ==========

  /**
   * ✅ 上传文件到后端，返回附件信息并追加到 pendingAttachments
   */
  async function uploadFile(file: File, agentId?: string): Promise<AttachmentInfo | null> {
    const aid = agentId || lastUsedAgentId;
    // ✅ 上传前确保已有会话（附件直接落到会话工作区）；若无则先创建 NEW 会话
    if (!activeConversationId.value) {
      await createConversation(aid);
    }
    const formData = new FormData();
    formData.append('file', file);
    formData.append('agent_id', aid);
    // ✅ 有会话ID时后端直接写入会话工作区目录
    if (activeConversationId.value) {
      formData.append('conversation_id', activeConversationId.value);
    }

    try {
      const resp = await fetch(`${API_BASE}/chat/upload`, {
        method: 'POST',
        body: formData,
      });
      if (!resp.ok) {
        throw new Error(`Upload failed: ${resp.status}`);
      }
      const data = await resp.json();
      const att: AttachmentInfo = {
        file_id: data.file_id,
        url: data.url,
        name: data.name,
        size: data.size,
        mime_type: data.mime_type,
        is_image: data.is_image,
        stored_filename: data.stored_filename,
        subdir: data.subdir,
        local_preview: data.is_image ? URL.createObjectURL(file) : undefined,
      };
      pendingAttachments.value.push(att);
      return att;
    } catch (e) {
      console.error('[Chat] File upload failed:', e);
      return null;
    }
  }

  /** ✅ 移除待发送附件（同时删除后端工作区文件） */
  function removeAttachment(fileId: string) {
    const idx = pendingAttachments.value.findIndex(a => a.file_id === fileId);
    if (idx !== -1) {
      const att = pendingAttachments.value[idx];
      if (att.local_preview) URL.revokeObjectURL(att.local_preview);
      pendingAttachments.value.splice(idx, 1);
      // ✅ 通知后端删除已上传的文件（携带 subdir 定位工作区/staging 路径）
      if (att.stored_filename) {
        const subdirQuery = att.subdir ? `?subdir=${encodeURIComponent(att.subdir)}` : '';
        fetch(`${API_BASE}/chat/upload/${encodeURIComponent(att.stored_filename)}${subdirQuery}`, { method: 'DELETE' }).catch(() => {});
      }
    }
  }

  /** ✅ 清空所有待发送附件（发送成功后调用，文件已由后端移走，无需删除） */
  function clearAttachments() {
    for (const att of pendingAttachments.value) {
      if (att.local_preview) URL.revokeObjectURL(att.local_preview);
    }
    pendingAttachments.value = [];
  }

  /**
   * ✅ 执行斜杠命令（后端命令）
   * 在消息列表追加 request/response 消息，并调用后端执行接口
   */
  async function executeSlashCommand(
    agentId: string,
    title: string,
    command: string,
    args: Record<string, any>,
    conversationId: string | null,
    rawInput: string,
  ) {
    // ✅ 构建附件负载（供后端 image_generate 填充 images 参数 + 回显）
    const attachmentsPayload = pendingAttachments.value.length > 0
      ? pendingAttachments.value.map(a => ({
          file_id: a.file_id,
          name: a.name,
          url: a.url,
          size: a.size,
          mime_type: a.mime_type,
          is_image: a.is_image,
          stored_filename: a.stored_filename,
          subdir: a.subdir,
        }))
      : undefined;

    // ✅ 发送前立即清空待发送附件（已快照到 attachmentsPayload），不等待后端响应
    // 失败时在 catch 中恢复；成功时 blob 预览由 clearAttachments 统一释放
    const sentAttachments = pendingAttachments.value.slice();
    if (sentAttachments.length > 0) {
      pendingAttachments.value = [];
    }

    // 追加用户命令消息到 UI
    const requestMsgId = `slash-req-${Date.now()}`;
    messages.value.push({
      id: requestMsgId,
      role: 'user',
      content: rawInput,
      message_type: 'slash_command_request',
      created_at: new Date().toISOString(),
      // ✅ 本地即时显示附件（后端返回的 request_message 会含持久化 metadata 覆盖）
      metadata: attachmentsPayload ? { attachments: attachmentsPayload } : undefined,
    });

    // 显示加载状态
    isLoading.value = true;

    try {
      const res = await fetch(`${API_BASE}/slash-commands/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title,
          command,
          args,
          conversation_id: conversationId || undefined,
          agent_id: agentId,
          raw_input: rawInput,
          attachments: attachmentsPayload,
        }),
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const data = await res.json();

      // 替换本地 request 消息为后端返回的真实消息（含真实 ID）
      if (data.request_message) {
        const idx = messages.value.findIndex(m => m.id === requestMsgId);
        if (idx !== -1) {
          messages.value[idx] = data.request_message;
        }
      }

      // 追加响应消息
      if (data.response_message) {
        messages.value.push(data.response_message);
      } else {
        // 如果后端没有返回结构化消息（无 conversation_id 场景），本地构造
        messages.value.push({
          id: `slash-res-${Date.now()}`,
          role: 'assistant',
          content: data.result || '命令执行完成',
          message_type: 'slash_command_response',
          created_at: new Date().toISOString(),
        });
      }

      // ✅ session.name 命令成功后，实时更新本地会话标题
      if (data.success && title === 'session' && command === 'name' && conversationId) {
        const newTitle = args.title;
        if (newTitle) {
          const conv = conversations.value.find(c => c.id === conversationId);
          if (conv) {
            conv.title = newTitle;
            // 强制触发数组引用变更，确保 Lit 组件重新渲染
            conversations.value = [...conversations.value];
          }
        }
      }

      // ✅ 命令成功后：若当前为 NEW 会话（已被后端激活为 ACTIVE），清除本地标记并刷新列表
      // 覆盖「新会话首个动作即斜杠命令（如 image_generate）」场景，使会话立即出现在侧边栏
      if (data.success) {
        // ✅ 命令成功：释放已清空附件的 blob 预览（仅回收内存）
        for (const att of sentAttachments) {
          if (att.local_preview) URL.revokeObjectURL(att.local_preview);
        }
        if (activeIsNewConversation.value) {
          activeIsNewConversation.value = false;
          await loadConversations(agentId);
        }
      }
    } catch (e: any) {
      console.error('Failed to execute slash command:', e);
      // ✅ 请求失败：恢复发送前清空的附件，用户可修正后重发
      if (sentAttachments.length > 0) {
        pendingAttachments.value = sentAttachments;
      }
      // 追加错误消息
      messages.value.push({
        id: `slash-err-${Date.now()}`,
        role: 'assistant',
        content: `命令执行失败: ${e.message || '未知错误'}`,
        message_type: 'slash_command_response',
        created_at: new Date().toISOString(),
      });
    } finally {
      isLoading.value = false;
    }
  }

  /**
   * ✅ 定时任务结果自动发送（相当于点击发送按钮）
   *
   * 消息已由事件处理追加到 UI（当前会话）或已持久化到后端（任意会话），
   * 此处携带 consume_user_message_id 避免后端重复追加用户消息。
   * 即使目标会话非当前会话，也会在后台执行请求，切回后可见完整对话。
   */
  async function autoSendScheduled(agentId: string, conversationId: string, messageId: string, content: string) {
    if (!conversationId || !messageId) return;
    // 防重复：同一条定时消息只自动发送一次
    if (scheduledInFlight.has(messageId)) return;
    // 该会话已有进行中的流时跳过（避免并发冲突）
    if (activeStreams.has(conversationId)) return;
    scheduledInFlight.add(messageId);
    try {
      console.log('[Chat] Auto-sending scheduled task result', { conversationId, messageId });
      await _runStream(agentId, conversationId, content, {
        pushUserMessage: false,
        consumeUserMessageId: messageId,
      });
    } finally {
      scheduledInFlight.delete(messageId);
    }
  }

  function connectWebSocket(agentId: string) {
    const wsUrl = `ws://localhost:8000/api/v1/chat/ws/${agentId}`;
    const socket = new WebSocket(wsUrl);
    
    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'message' && data.data) {
        messages.value.push(data.data);
      }
    };
    
    socket.onclose = () => {
      console.log('WebSocket closed');
    };
    
    return socket;
  }

  return {
    conversations,
    activeConversationId,
    activeIsNewConversation,
    messages,
    isLoading,
    currentDag,
    pendingSessionModel,
    pendingAttachments,
    setPendingSessionModel,
    loadConversations,
    loadMessages,
    createConversation,
    selectConversation,
    sendMessage,
    executeSlashCommand,
    stopGenerating,
    connectWebSocket,
    uploadFile,
    removeAttachment,
    clearAttachments,
  };
});
