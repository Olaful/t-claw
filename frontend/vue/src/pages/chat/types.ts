/**
 * Chat 页面共享类型定义
 */

// ===== 模型配置类型 =====

export type LLMModel = {
  id: string;
  name: string;
  displayName: string;
  provider: string;
  reasoning?: boolean;
  api?: string;
  compat?: { supportsReasoningEffort?: boolean };
  contextWindow?: number;
};

export type SessionModelConfig = {
  provider: string;
  model_id: string;
  reasoning_level?: string;
  speed_mode?: string;
};

// ===== 音频播放器类型 =====

export type AudioPlayerState = {
  playing: boolean;
  currentTime: number;
  duration: number;
  volume: number;
  muted: boolean;
  playbackRate: number;
};

export type PlaybackRateOption = {
  label: string;
  value: number;
};

export type StreamSettings = {
  volume: number;
  muted: boolean;
  playbackRate: number;
};

// ===== 斜杠命令类型 =====

export type ParamPromptData = {
  title: string;
  command: any;
  missingArgs: Array<{ name: string; config: any }>;
  existingArgs: Record<string, string>;
};

export type ParsedSlashInput = {
  title: string;
  command: string;
  args: string[];
  kwargs: Record<string, string>;
};


