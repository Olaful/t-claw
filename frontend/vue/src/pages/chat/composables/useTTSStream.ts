/**
 * TTS 流式播放 + 顺序队列管理 composable
 * 管理 TTS 语音的流式播放、完成过渡、顺序播放队列
 */
import { ref, watch, nextTick, type Ref } from 'vue';
import type { AudioPlayerState, StreamSettings } from '../types';

export interface UseTTSStreamOptions {
  /** 音频播放器状态引用（来自 useAudioPlayer） */
  audioPlayers: Ref<Record<string, AudioPlayerState>>;
  audioElements: Ref<Record<string, HTMLAudioElement>>;
  /** 消息列表引用（来自 chatStore） */
  messages: Ref<any[]>;
  /** 初始化音频播放器的方法（来自 useAudioPlayer） */
  initAudioPlayer: (msgId: string, url: string, initialTime?: number, initialSettings?: StreamSettings, autoPlay?: boolean) => void;
}

export function useTTSStream(options: UseTTSStreamOptions) {
  const { audioPlayers, audioElements, messages, initAudioPlayer } = options;

  // ===== TTS 流式播放状态 =====
  const ttsStreamPlaying = ref<Record<string, boolean>>({}); // msgId -> isPlaying
  const ttsStreamAudioEls = ref<Record<string, HTMLAudioElement>>({});
  const ttsStreamMediaSources: Record<string, MediaSource> = {};
  const ttsStreamSourceBuffers: Record<string, SourceBuffer> = {};
  const ttsStreamPlayIndex = ref<Record<string, number>>({});
  const ttsStreamAutoStarted = ref<Record<string, boolean>>({});
  // TTS 流式→完成过渡：缓存播放进度和设置
  const ttsStreamProgressCache: Record<string, number> = {}; // ttsMsgId -> currentTime
  const ttsStreamSettingsCache: Record<string, StreamSettings> = {};
  // TTS 顺序播放队列
  const ttsPlaybackDone: Record<string, boolean> = {}; // ttsMsgId -> true 表示已播放完毕
  const ttsSessionActive: Record<string, boolean> = {}; // 本轮会话中经历过 streaming 阶段的 TTS 消息
  const ttsUserPaused = ref(false); // 用户手动暂停标志

  // ===== TTS 流式播放方法 (MSE) =====
  // 注意：流式播放器不负责队列推进。队列推进由完成态播放器的 ended 事件触发。
  // 流式播放器只负责播放 chunks，直到 stopTTSStreamPlayback 被调用（tts_stream_end 到达时）。

  const startTTSStreamPlayback = (msgId: string, getChunks: () => string[]) => {
    if (ttsStreamPlaying.value[msgId]) return;

    const mediaSource = new MediaSource();
    const audio = new Audio();
    audio.src = URL.createObjectURL(mediaSource);

    ttsStreamAudioEls.value[msgId] = audio;
    ttsStreamMediaSources[msgId] = mediaSource;
    ttsStreamPlaying.value[msgId] = true;
    ttsStreamPlayIndex.value[msgId] = 0;

    // 注册到统一状态系统
    audioElements.value[msgId] = audio;
    audioPlayers.value[msgId] = { playing: true, currentTime: 0, duration: 0, volume: 1.0, muted: false, playbackRate: 1.0 };
    audio.addEventListener('timeupdate', () => {
      if (audioPlayers.value[msgId]) {
        audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], currentTime: audio.currentTime };
      }
    });

    let isAppending = false;

    const appendNextChunk = () => {
      if (!ttsStreamPlaying.value[msgId]) return;
      const sb = ttsStreamSourceBuffers[msgId];
      if (!sb || sb.updating || isAppending) return;

      const chunks = getChunks();
      const currentIndex = ttsStreamPlayIndex.value[msgId] || 0;

      if (currentIndex >= chunks.length) {
        // 无新数据：继续轮询等待（不主动结束流，等待 tts_stream_end 信号）
        setTimeout(() => appendNextChunk(), 150);
        return;
      }

      isAppending = true;
      try {
        const binary = atob(chunks[currentIndex]);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        sb.appendBuffer(bytes.buffer);
        ttsStreamPlayIndex.value[msgId] = currentIndex + 1;
      } catch (e) {
        console.warn('[TTSStream] append chunk failed', e);
        isAppending = false;
        ttsStreamPlayIndex.value[msgId] = (ttsStreamPlayIndex.value[msgId] || 0) + 1;
        setTimeout(() => appendNextChunk(), 50);
      }
    };

    mediaSource.addEventListener('sourceopen', () => {
      try {
        const sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');
        sourceBuffer.mode = 'sequence';
        ttsStreamSourceBuffers[msgId] = sourceBuffer;

        sourceBuffer.addEventListener('updateend', () => {
          isAppending = false;
          appendNextChunk();
        });
        sourceBuffer.addEventListener('error', () => {
          isAppending = false;
          // 错误后恢复轮询，避免死锁
          setTimeout(() => appendNextChunk(), 150);
        });

        audio.play().catch(e => console.warn('[TTSStream] autoplay failed', e));
        appendNextChunk();
      } catch (e) {
        console.error('[TTSStream] MediaSource setup failed', e);
      }
    });
  };

  const stopTTSStreamPlayback = (msgId: string) => {
    const audio = ttsStreamAudioEls.value[msgId];
    if (audio) {
      // 缓存当前播放进度和设置（用于流式→完成过渡继承）
      if (audio.currentTime > 0) {
        ttsStreamProgressCache[msgId] = audio.currentTime;
      }
      ttsStreamSettingsCache[msgId] = { volume: audio.volume, muted: audio.muted, playbackRate: audio.playbackRate };
      audio.pause();
      audio.src = '';
      delete ttsStreamAudioEls.value[msgId];
    }
    const ms = ttsStreamMediaSources[msgId];
    if (ms && ms.readyState === 'open') {
      try { ms.endOfStream(); } catch (e) { /* ignore */ }
    }
    delete ttsStreamMediaSources[msgId];
    delete ttsStreamSourceBuffers[msgId];
    ttsStreamPlaying.value[msgId] = false;
    delete audioElements.value[msgId];
    delete audioPlayers.value[msgId];
  };

  const toggleTTSStreamPlay = (msgId: string, getChunks: () => string[]) => {
    if (ttsStreamPlaying.value[msgId]) {
      const audio = ttsStreamAudioEls.value[msgId];
      if (audio) audio.pause();
      ttsStreamPlaying.value[msgId] = false;
      if (audioPlayers.value[msgId]) {
        audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], playing: false };
      }
      ttsUserPaused.value = true; // 用户手动暂停 → 禁止后续自动播放
    } else {
      const audio = ttsStreamAudioEls.value[msgId];
      if (audio && audio.src) {
        audio.play().catch(e => console.warn('[TTSStream] resume failed', e));
        ttsStreamPlaying.value[msgId] = true;
        if (audioPlayers.value[msgId]) {
          audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], playing: true };
        }
      } else {
        startTTSStreamPlayback(msgId, getChunks);
      }
      ttsUserPaused.value = false; // 用户手动恢复 → 允许后续自动播放
    }
  };

  // ===== TTS 顺序播放队列管理 =====

  /** 检查某个 TTS 消息是否轮到播放（前面所有有 TTS 的消息均已播放完成） */
  const isTTSTurnToPlay = (ttsMsgId: string, msgs: any[]): boolean => {
    const rawId = ttsMsgId.replace('tts_', '');
    const targetIdx = msgs.findIndex((m: any) => m.id === rawId);
    if (targetIdx === -1) return false;
    for (let i = 0; i < targetIdx; i++) {
      const msg = msgs[i];
      if (!msg.metadata?.tts) continue;
      const prevTtsMsgId = `tts_${msg.id}`;
      // 非本轮会话的消息（历史/数据库加载）不阻塞队列
      if (!ttsSessionActive[prevTtsMsgId]) continue;
      // 错误状态视为已完成
      if (msg.metadata.tts.status === 'error') continue;
      // 已播放完成
      if (ttsPlaybackDone[prevTtsMsgId]) continue;
      // 前面的消息还没播完，不轮到
      return false;
    }
    return true;
  };

  /** 推进 TTS 顺序播放队列：找到下一条应播放的消息并触发播放 */
  const advanceTTSQueue = () => {
    const msgs = messages.value;
    for (const msg of msgs) {
      if (!msg.metadata?.tts) continue;
      const ttsMsgId = `tts_${msg.id}`;
      if (ttsPlaybackDone[ttsMsgId]) continue;
      const tts = msg.metadata.tts;

      // 错误状态：跳过并标记完成
      if (tts.status === 'error') {
        ttsPlaybackDone[ttsMsgId] = true;
        continue;
      }

      // 历史消息（非本轮会话，从数据库加载）：跳过
      if (!ttsSessionActive[ttsMsgId]) {
        continue;
      }

      // 流式中：如果还没自动启动且有数据，则启动流式播放（用户暂停时不自动启动）
      if (tts.status === 'streaming') {
        if (!ttsStreamAutoStarted.value[ttsMsgId] && !ttsUserPaused.value && tts.chunks && tts.chunks.length > 0) {
          ttsStreamAutoStarted.value[ttsMsgId] = true;
          nextTick(() => {
            if (!ttsStreamPlaying.value[ttsMsgId]) {
              startTTSStreamPlayback(ttsMsgId, () => {
                const currentMsg = messages.value.find((m: any) => m.id === msg.id);
                return currentMsg?.metadata?.tts?.chunks || [];
              });
            }
          });
        }
        return; // 等待这条消息播放完成
      }

      // 完成状态：自动播放（用户手动暂停时不自动播放）
      if (tts.status === 'complete' && tts.audio_url) {
        if (ttsUserPaused.value) return; // 用户已暂停，等待手动恢复
        const audio = audioElements.value[ttsMsgId];
        if (audio) {
          // 播放器已初始化，直接播放
          audio.play().catch(() => {});
          if (audioPlayers.value[ttsMsgId]) {
            audioPlayers.value[ttsMsgId] = { ...audioPlayers.value[ttsMsgId], playing: true };
          }
        } else {
          // 播放器未初始化，初始化并自动播放
          nextTick(() => initAudioPlayer(ttsMsgId, tts.audio_url, undefined, undefined, true));
        }
        return; // 等待播放完成
      }

      // 还没收到 TTS 数据（尚未开始），等待
      return;
    }
  };

  /** 标记 TTS 播放完成并推进队列（由 audioPlayer 的 ended 回调触发） */
  const markTTSPlaybackDone = (msgId: string) => {
    ttsPlaybackDone[msgId] = true;
    nextTick(() => advanceTTSQueue());
  };

  // ===== Watch：TTS 流式播放自动启动 + 完成过渡 + 顺序队列控制 =====

  watch(messages, (newMessages: any[]) => {
    newMessages.forEach((msg: any) => {
      if (!msg.metadata?.tts) return;
      const tts = msg.metadata.tts;
      const ttsMsgId = `tts_${msg.id}`;

      // 记录本轮会话中活跃的 TTS 消息（经历过 streaming 状态）
      if (tts.status === 'streaming') {
        ttsSessionActive[ttsMsgId] = true;
      }

      // 流式播放中：按消息顺序自动启动（仅当轮到该消息时才播放，且用户未手动暂停）
      if (tts.status === 'streaming' && tts.chunks && tts.chunks.length > 0) {
        if (!ttsStreamAutoStarted.value[ttsMsgId] && !ttsUserPaused.value && isTTSTurnToPlay(ttsMsgId, newMessages)) {
          ttsStreamAutoStarted.value[ttsMsgId] = true;
          nextTick(() => {
            if (!ttsStreamPlaying.value[ttsMsgId]) {
              startTTSStreamPlayback(ttsMsgId, () => {
                const currentMsg = messages.value.find((m: any) => m.id === msg.id);
                return currentMsg?.metadata?.tts?.chunks || [];
              });
            }
          });
        }
      }

      // 完成过渡：停止流式播放并清理资源
      if ((tts.status === 'complete' || tts.status === 'error') && ttsStreamAutoStarted.value[ttsMsgId]) {
        stopTTSStreamPlayback(ttsMsgId);
        delete ttsStreamAutoStarted.value[ttsMsgId];
      }

      // 错误状态：标记完成并推进队列
      if (tts.status === 'error' && !ttsPlaybackDone[ttsMsgId]) {
        ttsPlaybackDone[ttsMsgId] = true;
        nextTick(() => advanceTTSQueue());
      }
    });
  }, { deep: true });

  return {
    // 状态
    ttsStreamPlaying,
    ttsStreamAudioEls,
    ttsStreamPlayIndex,
    ttsStreamAutoStarted,
    ttsStreamProgressCache,
    ttsStreamSettingsCache,
    ttsPlaybackDone,
    ttsSessionActive,
    ttsUserPaused,
    // 方法
    startTTSStreamPlayback,
    stopTTSStreamPlayback,
    toggleTTSStreamPlay,
    isTTSTurnToPlay,
    advanceTTSQueue,
    markTTSPlaybackDone,
  };
}
