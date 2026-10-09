/**
 * 音频播放器核心 composable
 * 管理音频播放状态、音量控制、菜单、进度条等
 */
import { ref, onMounted, onUnmounted } from 'vue';
import type { AudioPlayerState, PlaybackRateOption, StreamSettings } from '../types';

export interface UseAudioPlayerOptions {
  /** TTS 播放完成时的回调（用于推进队列） */
  onTTSEnded?: (msgId: string) => void;
}

export function useAudioPlayer(options: UseAudioPlayerOptions = {}) {
  // ===== 核心状态 =====
  const audioPlayers = ref<Record<string, AudioPlayerState>>({});
  const audioElements = ref<Record<string, HTMLAudioElement>>({});

  // ===== 音量控制与菜单状态 =====
  const showVolumeSlider = ref<Record<string, boolean>>({});
  const showAudioMenu = ref<Record<string, boolean>>({});
  const showPlaybackRateMenu = ref<Record<string, boolean>>({});
  const volumeMenuTimeout: { current: number | null } = { current: null };

  // ===== 播放速度选项 =====
  const playbackRates: PlaybackRateOption[] = [
    { label: '0.25x', value: 0.25 },
    { label: '0.5x', value: 0.5 },
    { label: '0.75x', value: 0.75 },
    { label: '正常', value: 1.0 },
    { label: '1.25x', value: 1.25 },
    { label: '1.5x', value: 1.5 },
    { label: '1.75x', value: 1.75 },
    { label: '2x', value: 2.0 },
  ];

  // ===== 拖拽状态（非响应式，避免拖拽期间 timeupdate 触发 Vue 重渲染干扰原生 range input） =====
  const draggingMsgId: { current: string | null } = { current: null };

  // ===== 辅助函数 =====

  const formatTime = (seconds: number): string => {
    if (!seconds || isNaN(seconds) || !isFinite(seconds)) return '0:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  // ===== 播放器初始化 =====

  const initAudioPlayer = (msgId: string, url: string, initialTime?: number, initialSettings?: StreamSettings, autoPlay?: boolean) => {
    if (audioElements.value[msgId]) return;
    const audio = new Audio();
    audio.preload = 'metadata';
    audio.src = url;
    // 继承流式播放的音量/静音/倍速设置
    const initVolume = initialSettings?.volume ?? 1.0;
    const initMuted = initialSettings?.muted ?? false;
    const initRate = initialSettings?.playbackRate ?? 1.0;
    audio.volume = initVolume;
    audio.muted = initMuted;
    audio.playbackRate = initRate;
    audioPlayers.value[msgId] = { playing: false, currentTime: 0, duration: 0, volume: initVolume, muted: initMuted, playbackRate: initRate };

    const updateDuration = () => {
      if (audioPlayers.value[msgId] && audio.duration && isFinite(audio.duration)) {
        audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], duration: audio.duration };
      }
    };
    audio.addEventListener('loadedmetadata', () => {
      updateDuration();
      // 流式播放过渡：seek 到流式播放位置，仅当 autoPlay 时继续播放
      if (initialTime && initialTime > 0 && audio.duration && isFinite(audio.duration)) {
        const seekTime = Math.min(initialTime, audio.duration - 0.5);
        if (seekTime > 0) {
          audio.playbackRate = initRate; // 确保倍速在播放前生效
          audio.currentTime = seekTime;
          if (autoPlay) {
            audio.play().catch(() => {});
            if (audioPlayers.value[msgId]) {
              audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], playing: true, currentTime: seekTime };
            }
          } else {
            // 用户已暂停：仅 seek 到位置，不自动播放
            if (audioPlayers.value[msgId]) {
              audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], currentTime: seekTime };
            }
          }
        } else if (autoPlay) {
          // 音频太短无法 seek，从头播放
          audio.play().catch(() => {});
          if (audioPlayers.value[msgId]) {
            audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], playing: true };
          }
        }
      } else if (autoPlay) {
        // 顺序播放队列触发：从头自动播放
        audio.play().catch(() => {});
        if (audioPlayers.value[msgId]) {
          audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], playing: true };
        }
      }
    });
    audio.addEventListener('durationchange', updateDuration);
    audio.addEventListener('timeupdate', () => {
      if (audioPlayers.value[msgId] && draggingMsgId.current !== msgId) {
        audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], currentTime: audio.currentTime };
      }
    });
    audio.addEventListener('ended', () => {
      if (audioPlayers.value[msgId]) {
        audioPlayers.value[msgId] = { ...audioPlayers.value[msgId], playing: false, currentTime: 0 };
      }
      // TTS 顺序播放：标记完成并推进队列
      if (msgId.startsWith('tts_') && options.onTTSEnded) {
        options.onTTSEnded(msgId);
      }
    });
    audioElements.value[msgId] = audio;
    audio.load(); // 显式触发元数据加载
  };

  // ===== 播放/暂停 =====

  const toggleAudioPlay = (msgId: string, url: string, onUserPause?: () => void, onUserResume?: () => void) => {
    initAudioPlayer(msgId, url);
    const audio = audioElements.value[msgId];
    const state = audioPlayers.value[msgId];
    if (!audio || !state) return;

    if (state.playing) {
      audio.pause();
      audioPlayers.value[msgId] = { ...state, playing: false };
      if (onUserPause) onUserPause();
    } else {
      audio.play();
      audioPlayers.value[msgId] = { ...state, playing: true };
      if (onUserResume) onUserResume();
    }
  };

  // ===== 进度条 seek =====

  const handleSeekInput = (msgId: string, value: number) => {
    const audio = audioElements.value[msgId];
    if (!audio) return;
    draggingMsgId.current = msgId;
    try { audio.currentTime = value; } catch (_) { /* ignore */ }
  };

  const handleSeekChange = (msgId: string, value: number) => {
    const audio = audioElements.value[msgId];
    if (!audio) return;
    try { audio.currentTime = value; } catch (_) { /* ignore */ }
    draggingMsgId.current = null;
    // 提交到响应式状态
    const curState = audioPlayers.value[msgId];
    if (curState) {
      audioPlayers.value[msgId] = { ...curState, currentTime: value };
    }
  };

  // ===== 音量控制 =====

  const toggleMute = (msgId: string) => {
    const audio = audioElements.value[msgId];
    if (!audio) return;
    audio.muted = !audio.muted;
    const curState = audioPlayers.value[msgId];
    if (curState) {
      audioPlayers.value[msgId] = { ...curState, muted: audio.muted };
    }
  };

  const handleVolumeChange = (msgId: string, value: number) => {
    const audio = audioElements.value[msgId];
    if (!audio) return;
    audio.volume = value;
    audio.muted = value === 0;
    const curState = audioPlayers.value[msgId];
    if (curState) {
      audioPlayers.value[msgId] = { ...curState, volume: value, muted: audio.muted };
    }
  };

  // 音量滑块显示控制（hover 时显示，鼠标离开后延迟关闭）
  const showVolumeSliderFor = (msgId: string) => {
    if (volumeMenuTimeout.current) clearTimeout(volumeMenuTimeout.current);
    volumeMenuTimeout.current = null;
    showVolumeSlider.value[msgId] = true;
  };

  const hideVolumeSliderFor = (msgId: string) => {
    volumeMenuTimeout.current = window.setTimeout(() => {
      showVolumeSlider.value[msgId] = false;
      volumeMenuTimeout.current = null;
    }, 300);
  };

  // 鼠标进入音量区域时清除关闭定时器
  const cancelVolumeHide = () => {
    if (volumeMenuTimeout.current) {
      clearTimeout(volumeMenuTimeout.current);
      volumeMenuTimeout.current = null;
    }
  };

  // ===== 下载功能 =====

  const downloadAudio = (_msgId: string, url: string, filename?: string) => {
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'audio.mp3';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  // ===== 播放速度控制 =====

  const setPlaybackRate = (msgId: string, rate: number) => {
    const audio = audioElements.value[msgId];
    if (!audio) return;
    audio.playbackRate = rate;
    const curState = audioPlayers.value[msgId];
    if (curState) {
      audioPlayers.value[msgId] = { ...curState, playbackRate: rate };
    }
    showPlaybackRateMenu.value[msgId] = false;
    showAudioMenu.value[msgId] = false;
  };

  // ===== 菜单控制 =====

  const toggleAudioMenu = (msgId: string) => {
    showAudioMenu.value[msgId] = !showAudioMenu.value[msgId];
    if (!showAudioMenu.value[msgId]) {
      showPlaybackRateMenu.value[msgId] = false;
    }
  };

  // 点击外部关闭菜单（document 级别监听）
  const closeMenuOnOutsideClick = (e: MouseEvent) => {
    Object.keys(showAudioMenu.value).forEach((key) => {
      if (showAudioMenu.value[key]) {
        const target = e.target as HTMLElement;
        const menuEl = document.querySelector(`.audio-menu[data-msgid="${key}"]`);
        const btnEl = document.querySelector(`.audio-menu-btn[data-msgid="${key}"]`);
        if (menuEl && !menuEl.contains(target) && btnEl && !btnEl.contains(target)) {
          showAudioMenu.value[key] = false;
          showPlaybackRateMenu.value[key] = false;
        }
      }
    });
  };

  // ===== 生命周期 =====

  onMounted(() => {
    document.addEventListener('click', closeMenuOnOutsideClick);
  });

  onUnmounted(() => {
    document.removeEventListener('click', closeMenuOnOutsideClick);
  });

  return {
    // 状态
    audioPlayers,
    audioElements,
    showVolumeSlider,
    showAudioMenu,
    showPlaybackRateMenu,
    playbackRates,
    // 方法
    formatTime,
    initAudioPlayer,
    toggleAudioPlay,
    handleSeekInput,
    handleSeekChange,
    toggleMute,
    handleVolumeChange,
    showVolumeSliderFor,
    hideVolumeSliderFor,
    cancelVolumeHide,
    downloadAudio,
    setPlaybackRate,
    toggleAudioMenu,
  };
}
