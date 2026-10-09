/**
 * 音乐 MSE 流式播放 composable
 * 管理音乐流式播放的启动、停止、过渡检测
 */
import { ref, watch, nextTick, type Ref } from 'vue';
import type { AudioPlayerState, StreamSettings } from '../types';

export interface UseMusicStreamOptions {
  /** 音频播放器状态引用（来自 useAudioPlayer） */
  audioPlayers: Ref<Record<string, AudioPlayerState>>;
  audioElements: Ref<Record<string, HTMLAudioElement>>;
  /** 消息列表引用（来自 chatStore） */
  messages: Ref<any[]>;
}

export function useMusicStream(options: UseMusicStreamOptions) {
  const { audioPlayers, audioElements, messages } = options;

  // ===== 流式播放状态 =====
  const streamAudioEls = ref<Record<string, HTMLAudioElement>>({});
  const streamMediaSources: Record<string, MediaSource> = {};
  const streamSourceBuffers: Record<string, SourceBuffer> = {};
  const streamAppendFns: Record<string, () => void> = {};
  const streamPlaying = ref<Record<string, boolean>>({});
  // 流式播放进度缓存（用于流式→完成过渡时继承播放位置）
  const streamProgressCache: Record<string, number> = {}; // key: jobId_songIdx -> currentTime
  // 流式播放音量/速度缓存（用于流式→完成过渡时继承播放设置）
  const streamSettingsCache: Record<string, StreamSettings> = {};
  const streamJobMap: Record<string, { jobId: string; songId: string }> = {}; // streamMsgId -> {jobId, songId}

  // 流式播放状态跟踪
  const streamAutoStarted = ref<Record<string, boolean>>({});
  const streamPlayIndex = ref<Record<string, number>>({});

  // ===== 流式播放方法 =====

  const startStreamPlayback = (msgId: string, getChunks: () => string[], meta?: { jobId: string; songId: string }) => {
    if (streamPlaying.value[msgId]) return;

    const mediaSource = new MediaSource();
    const audio = new Audio();
    audio.src = URL.createObjectURL(mediaSource);

    streamAudioEls.value[msgId] = audio;
    streamMediaSources[msgId] = mediaSource;
    streamPlaying.value[msgId] = true;
    streamPlayIndex.value[msgId] = 0;

    // 注册到统一状态系统（复用音量控制等）
    audioElements.value[msgId] = audio;
    audioPlayers.value[msgId] = { playing: true, currentTime: 0, duration: 0, volume: 1.0, muted: false, playbackRate: 1.0 };
    audio.addEventListener('timeupdate', () => {
      if (audioPlayers.value[msgId]) {
        audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], currentTime: audio.currentTime };
      }
    });

    // 记录 job 关联（用于流式→完成过渡）
    if (meta) {
      streamJobMap[msgId] = meta;
    }

    let isAppending = false;

    const appendNextChunk = () => {
      if (!streamPlaying.value[msgId]) return;
      const sb = streamSourceBuffers[msgId];
      if (!sb || sb.updating || isAppending) return;

      const chunks = getChunks();
      const currentIndex = streamPlayIndex.value[msgId] || 0;

      if (currentIndex >= chunks.length) {
        // 没有新数据，150ms 后重试
        setTimeout(() => appendNextChunk(), 150);
        return;
      }

      isAppending = true;
      try {
        const binary = atob(chunks[currentIndex]);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        sb.appendBuffer(bytes.buffer);
        streamPlayIndex.value[msgId] = currentIndex + 1;
      } catch (e) {
        console.warn('[MusicStream] append chunk failed', e);
        isAppending = false;
        streamPlayIndex.value[msgId] = (streamPlayIndex.value[msgId] || 0) + 1;
        setTimeout(() => appendNextChunk(), 50);
      }
    };

    streamAppendFns[msgId] = appendNextChunk;

    mediaSource.addEventListener('sourceopen', () => {
      try {
        const sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');
        sourceBuffer.mode = 'sequence';
        streamSourceBuffers[msgId] = sourceBuffer;

        sourceBuffer.addEventListener('updateend', () => {
          isAppending = false;
          appendNextChunk();
        });

        sourceBuffer.addEventListener('error', () => {
          isAppending = false;
        });

        audio.play().catch(e => console.warn('[MusicStream] autoplay failed', e));
        appendNextChunk();
      } catch (e) {
        console.error('[MusicStream] MediaSource setup failed', e);
      }
    });
  };

  const stopStreamPlayback = (msgId: string, cacheProgress = true) => {
    const audio = streamAudioEls.value[msgId];
    if (audio) {
      // 缓存当前播放进度与音量/倍速设置（用于流式→完成过渡）
      const meta = streamJobMap[msgId];
      if (meta) {
        const cacheKey = `${meta.jobId}_${meta.songId}`;
        if (cacheProgress && audio.currentTime > 0) {
          streamProgressCache[cacheKey] = audio.currentTime;
        }
        streamSettingsCache[cacheKey] = { volume: audio.volume, muted: audio.muted, playbackRate: audio.playbackRate };
      }
      audio.pause();
      audio.src = '';
      delete streamAudioEls.value[msgId];
    }
    const ms = streamMediaSources[msgId];
    if (ms && ms.readyState === 'open') {
      try { ms.endOfStream(); } catch (e) { /* ignore */ }
    }
    delete streamMediaSources[msgId];
    delete streamSourceBuffers[msgId];
    delete streamAppendFns[msgId];
    delete streamJobMap[msgId];
    streamPlaying.value[msgId] = false;
    // 清理统一状态
    delete audioElements.value[msgId];
    delete audioPlayers.value[msgId];
  };

  const toggleStreamPlay = (msgId: string, getChunks: () => string[], meta?: { jobId: string; songId: string }) => {
    if (streamPlaying.value[msgId]) {
      // 暂停
      const audio = streamAudioEls.value[msgId];
      if (audio) audio.pause();
      streamPlaying.value[msgId] = false;
      if (audioPlayers.value[msgId]) {
        audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], playing: false };
      }
    } else {
      // 恢复或启动
      const audio = streamAudioEls.value[msgId];
      if (audio && audio.src) {
        audio.play().catch(e => console.warn('[MusicStream] resume failed', e));
        streamPlaying.value[msgId] = true;
        if (audioPlayers.value[msgId]) {
          audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], playing: true };
        }
        // 恢复 append 循环
        const fn = streamAppendFns[msgId];
        if (fn) fn();
      } else {
        startStreamPlayback(msgId, getChunks, meta);
      }
    }
  };

  // ===== Watch：自动启动流式播放 + 检测流式→完成过渡 =====

  watch(messages, (newMessages: any[]) => {
    // === 过渡检测：检查已启动的流式播放是否对应的消息已不再 streaming ===
    Object.keys(streamAutoStarted.value).forEach((streamMsgId) => {
      if (!streamAutoStarted.value[streamMsgId]) return;
      // 检查是否仍有活动的流式播放
      if (!streamPlaying.value[streamMsgId] && !streamAudioEls.value[streamMsgId]) return;
      // 检查对应消息是否仍在 streaming
      const meta = streamJobMap[streamMsgId];
      if (!meta) return;
      const stillStreaming = newMessages.some(
        (m: any) => m.message_type === 'music_generated' && m.metadata?.job_id === meta.jobId && m.metadata?.streaming
      );
      if (!stillStreaming) {
        // 流式消息已被替换为完成消息，停止流式播放并缓存进度
        stopStreamPlayback(streamMsgId, true);
        delete streamAutoStarted.value[streamMsgId];
      }
    });

    // === 自动启动流式播放（同时只允许一个自动播放） ===
    const hasActiveStream = Object.values(streamPlaying.value).some(v => v);
    newMessages.forEach((msg: any) => {
      if (msg.message_type === 'music_generated' && msg.metadata?.streaming && msg.metadata?.songChunks) {
        Object.entries(msg.metadata.songChunks as Record<string, string[]>).forEach(([songId, chunks]) => {
          if (chunks && chunks.length > 0) {
            const streamMsgId = `${msg.id || 'msg'}_stream_${songId}`;
            if (!streamAutoStarted.value[streamMsgId] && chunks.length >= 1) {
              streamAutoStarted.value[streamMsgId] = true;
              // 只有当前没有任何流式播放时，才自动启动
              if (!hasActiveStream) {
                nextTick(() => {
                  if (!streamPlaying.value[streamMsgId]) {
                    startStreamPlayback(streamMsgId, () => {
                      const currentMsg = messages.value.find((m: any) => m.id === msg.id);
                      return currentMsg?.metadata?.songChunks?.[songId] || [];
                    }, { jobId: msg.metadata?.job_id || '', songId });
                  }
                });
              }
            }
          }
        });
      }
    });
  }, { deep: true });

  return {
    // 状态
    streamAudioEls,
    streamPlaying,
    streamAutoStarted,
    streamPlayIndex,
    streamProgressCache,
    streamSettingsCache,
    streamJobMap,
    // 方法
    startStreamPlayback,
    stopStreamPlayback,
    toggleStreamPlay,
  };
}
