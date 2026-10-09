/**
 * 消息列表渲染器
 * 渲染所有类型的消息气泡（文本、图片、音乐、视频、TTS、工具调用等）
 */
import { h, nextTick, type Ref } from 'vue';
import { marked } from 'marked';
import type { AudioPlayerState, PlaybackRateOption, StreamSettings } from '../types';
import { renderDAG } from './renderDAG';

export interface MessagesContext {
  // store
  messages: Ref<any[]>;
  isLoading: Ref<boolean>;
  currentDag: Ref<any>;
  // audio player
  audioPlayers: Ref<Record<string, AudioPlayerState>>;
  audioElements: Ref<Record<string, HTMLAudioElement>>;
  showVolumeSlider: Ref<Record<string, boolean>>;
  showAudioMenu: Ref<Record<string, boolean>>;
  showPlaybackRateMenu: Ref<Record<string, boolean>>;
  playbackRates: PlaybackRateOption[];
  formatTime: (s: number) => string;
  initAudioPlayer: (msgId: string, url: string, initialTime?: number, initialSettings?: StreamSettings, autoPlay?: boolean) => void;
  toggleAudioPlay: (msgId: string, url: string, onUserPause?: () => void, onUserResume?: () => void) => void;
  handleSeekInput: (msgId: string, value: number) => void;
  handleSeekChange: (msgId: string, value: number) => void;
  toggleMute: (msgId: string) => void;
  handleVolumeChange: (msgId: string, value: number) => void;
  showVolumeSliderFor: (msgId: string) => void;
  hideVolumeSliderFor: (msgId: string) => void;
  cancelVolumeHide: () => void;
  downloadAudio: (msgId: string, url: string, filename?: string) => void;
  setPlaybackRate: (msgId: string, rate: number) => void;
  toggleAudioMenu: (msgId: string) => void;
  // music stream
  streamPlaying: Ref<Record<string, boolean>>;
  streamProgressCache: Record<string, number>;
  streamSettingsCache: Record<string, StreamSettings>;
  toggleStreamPlay: (msgId: string, getChunks: () => string[], meta?: { jobId: string; songId: string }) => void;
  // tts stream
  ttsStreamPlaying: Ref<Record<string, boolean>>;
  ttsStreamProgressCache: Record<string, number>;
  ttsStreamSettingsCache: Record<string, StreamSettings>;
  ttsPlaybackDone: Record<string, boolean>;
  ttsSessionActive: Record<string, boolean>;
  ttsUserPaused: Ref<boolean>;
  toggleTTSStreamPlay: (msgId: string, getChunks: () => string[]) => void;
  isTTSTurnToPlay: (ttsMsgId: string, msgs: any[]) => boolean;
  // attachments
  handleImagePreview: (url: string) => void;
  handleFileDownload: (url: string, name: string) => void;
  formatFileSize: (bytes: number) => string;
}

export function renderMessages(ctx: MessagesContext) {
  return ctx.messages.value.map((msg: any, index: number) => {
    const isUser = msg.role === 'user';
    const isLastAssistant = !isUser && index === ctx.messages.value.length - 1;
    const showTypingAnimation = isLastAssistant && ctx.isLoading.value;
    const isSlashRequest = msg.message_type === 'slash_command_request';
    const isSlashResponse = msg.message_type === 'slash_command_response';
    const isImageGenerated = msg.message_type === 'image_generated';
    const isMusicGenerated = msg.message_type === 'music_generated';
    const isVideoGenerated = msg.message_type === 'video_generated';
    const isAudioGenerated = msg.message_type === 'audio_generated';
    const isPdfParsed = msg.message_type === 'pdf_parsed';

    return h('div', {
      key: msg.id || index,
      style: `display:flex;justify-content:${isUser ? 'flex-end' : 'flex-start'};`,
    }, [
      h('div', {
        style: `max-width:70%;padding:0.75rem 1rem;border-radius:0.75rem;${
          isUser
            ? 'background:var(--color-primary);color:white;'
            : 'background:var(--color-bg-hover);color:var(--color-text);'
        }`,
      }, [
        h('div', { style: 'font-size:0.75rem;opacity:0.7;margin-bottom:0.25rem;' },
          isSlashRequest ? '⚡ 命令' : isSlashResponse ? '⚡ 命令结果' : isImageGenerated ? '🎨 图片生成' : isMusicGenerated ? '🎵 音乐生成' : isVideoGenerated ? '🎬 视频生成' : isAudioGenerated ? '🔊 语音合成' : isPdfParsed ? '📄 PDF解析' : isUser ? '你' : 'Agent'
        ),
        // TTS 音频播放器（在文本内容之前渲染）
        ...(!isUser && !isImageGenerated && !isMusicGenerated && !isVideoGenerated && msg.metadata?.tts
          ? [renderTTSPlayer(ctx, msg)].filter(Boolean)
          : []),
        // 消息主体内容
        isImageGenerated
          ? renderImageMessage(ctx, msg)
          : isVideoGenerated
            ? renderVideoMessage(ctx, msg)
            : isMusicGenerated
              ? renderMusicMessage(ctx, msg, index)
              : isAudioGenerated
                ? renderAudioGeneratedMessage(ctx, msg, index)
                : isPdfParsed
                  ? renderPdfMessage(ctx, msg)
                  : msg.content
                  ? h('div', {
                      className: isUser ? '' : 'markdown-content',
                      innerHTML: isUser ? msg.content : marked.parse(msg.content) as string,
                      style: isUser ? '' : 'line-height:1.6;',
                    })
                  : showTypingAnimation
                    ? renderTypingAnimation()
                    : null,
        // 消息附件渲染
        ...(msg.metadata?.attachments ? renderAttachments(ctx, msg) : []),
        // 工具调用结果展示
        renderToolCalls(ctx, msg),
      ]),
    ]);
  });
}

// ===== 子渲染函数 =====

function renderTypingAnimation() {
  return h('div', { style: 'display:flex;gap:0.25rem;margin-top:0.25rem;' }, [
    h('span', { class: 'typing-dot', style: 'width:8px;height:8px;background:var(--color-text-disabled);border-radius:50%;animation:typing-bounce 1.4s infinite ease-in-out both;' }),
    h('span', { class: 'typing-dot', style: 'width:8px;height:8px;background:var(--color-text-disabled);border-radius:50%;animation:typing-bounce 1.4s infinite ease-in-out both;animation-delay:-0.32s;' }),
    h('span', { class: 'typing-dot', style: 'width:8px;height:8px;background:var(--color-text-disabled);border-radius:50%;animation:typing-bounce 1.4s infinite ease-in-out both;animation-delay:-0.16s;' }),
  ]);
}

function renderTTSPlayer(ctx: MessagesContext, msg: any) {
  const tts = msg.metadata.tts;
  const ttsMsgId = `tts_${msg.id}`;

  // 流式播放中
  if (tts.status === 'streaming') {
    const isPlaying = ctx.ttsStreamPlaying.value[ttsMsgId] || false;
    return h('div', {
      style: 'background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;padding:0.5rem 0.75rem;margin-bottom:0.5rem;max-width:360px;',
    }, [
      h('div', { style: 'display:flex;align-items:center;gap:0.5rem;' }, [
        h('button', {
          style: 'flex-shrink:0;width:1.75rem;height:1.75rem;border-radius:50%;border:1px solid var(--color-border);background:var(--color-bg-hover);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:0.8rem;',
          onClick: () => {
            ctx.toggleTTSStreamPlay(ttsMsgId, () => {
              const currentMsg = ctx.messages.value.find((m: any) => m.id === msg.id);
              return currentMsg?.metadata?.tts?.chunks || [];
            });
          },
        }, isPlaying ? '⏸' : '▶'),
        h('div', { style: 'flex:1;min-width:60px;height:4px;background:var(--color-bg-hover);border-radius:2px;overflow:hidden;position:relative;' }, [
          h('div', { style: `position:absolute;left:0;top:0;height:100%;width:${isPlaying ? '30%' : '10%'};background:var(--color-primary);border-radius:2px;opacity:0.7;${isPlaying ? 'animation:pulse 1.5s ease-in-out infinite;' : ''}` }),
        ]),
        h('span', { style: 'flex-shrink:0;font-size:0.7rem;opacity:0.7;' }, '🔊 合成中...'),
      ]),
    ]);
  }

  // 完成状态：标准播放器
  if (tts.status === 'complete' && tts.audio_url) {
    const audioUrl = tts.audio_url;
    const playerState = ctx.audioPlayers.value[ttsMsgId] || { playing: false, currentTime: 0, duration: 0, volume: 1.0, muted: false, playbackRate: 1.0 };
    // 懒初始化播放器（继承流式播放进度和设置）
    if (!ctx.audioElements.value[ttsMsgId]) {
      const inheritTime = ctx.ttsStreamProgressCache[ttsMsgId];
      const inheritSettings = ctx.ttsStreamSettingsCache[ttsMsgId];
      delete ctx.ttsStreamProgressCache[ttsMsgId];
      delete ctx.ttsStreamSettingsCache[ttsMsgId];
      const shouldAutoPlay = !!ctx.ttsSessionActive[ttsMsgId] && !ctx.ttsPlaybackDone[ttsMsgId] && !ctx.ttsUserPaused.value && ctx.isTTSTurnToPlay(ttsMsgId, ctx.messages.value);
      nextTick(() => ctx.initAudioPlayer(ttsMsgId, audioUrl, inheritTime, inheritSettings, shouldAutoPlay));
    }
    return renderFullAudioPlayer(ctx, ttsMsgId, audioUrl, playerState, {
      onUserPause: () => { ctx.ttsUserPaused.value = true; },
      onUserResume: () => { ctx.ttsUserPaused.value = false; },
    }, 'tts_audio.mp3');
  }

  // 错误状态
  if (tts.status === 'error') {
    return h('div', { style: 'font-size:0.75rem;color:var(--color-danger);margin-bottom:0.375rem;opacity:0.8;' }, `🔇 语音合成失败: ${tts.error || '未知错误'}`);
  }

  return null;
}

function renderImageMessage(ctx: MessagesContext, msg: any) {
  return h('div', { style: 'margin-top:0.25rem;' }, [
    msg.metadata?.status === 'failed'
      ? h('div', { style: 'color:#ef4444;font-size:0.8rem;' }, `❌ 生成失败: ${msg.metadata?.error || '未知错误'}`)
      : null,
    ...(msg.metadata?.images && msg.metadata.images.length > 0
      ? [h('div', { style: 'display:flex;flex-wrap:wrap;gap:0.5rem;margin-top:0.375rem;' },
          msg.metadata.images.map((img: any, imgIdx: number) => {
            const imgUrl = img.url || img.path || '';
            if (!imgUrl) return null;
            return h('img', {
              key: imgIdx,
              src: imgUrl,
              alt: `Generated image ${imgIdx + 1}`,
              style: 'max-width:320px;max-height:320px;border-radius:0.5rem;cursor:pointer;object-fit:contain;border:1px solid var(--color-border);',
              onClick: () => ctx.handleImagePreview(imgUrl),
            });
          }).filter(Boolean)
        )]
      : [msg.metadata?.status !== 'failed' ? h('div', { style: 'font-size:0.8rem;opacity:0.7;' }, '暂无图片数据') : null]),
    msg.metadata?.model
      ? h('div', { style: 'font-size:0.7rem;opacity:0.5;margin-top:0.25rem;' }, `模型: ${msg.metadata.model}`)
      : null,
  ]);
}

function renderVideoMessage(_ctx: MessagesContext, msg: any) {
  return h('div', { style: 'margin-top:0.25rem;' }, [
    msg.metadata?.status === 'failed'
      ? h('div', { style: 'color:#ef4444;font-size:0.8rem;' }, `❌ 生成失败: ${msg.metadata?.error || '未知错误'}`)
      : null,
    ...(msg.metadata?.videos && msg.metadata.videos.length > 0
      ? [h('div', { style: 'display:flex;flex-direction:column;gap:0.75rem;margin-top:0.375rem;' },
          msg.metadata.videos.map((video: any, vidIdx: number) => {
            const videoUrl = video.url || video.path || '';
            if (!videoUrl) return null;
            return h('div', { key: vidIdx, style: 'display:flex;flex-direction:column;gap:0.25rem;' }, [
              h('video', {
                src: videoUrl,
                controls: true,
                style: 'max-width:480px;max-height:320px;border-radius:0.5rem;background:#000;border:1px solid var(--color-border);',
              }),
              video.filename
                ? h('div', { style: 'font-size:0.7rem;opacity:0.6;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, `🎬 ${video.filename}`)
                : null,
            ]);
          }).filter(Boolean)
        )]
      : [msg.metadata?.status !== 'failed' ? h('div', { style: 'font-size:0.8rem;opacity:0.7;' }, '暂无视频数据') : null]),
    msg.metadata?.model
      ? h('div', { style: 'font-size:0.7rem;opacity:0.5;margin-top:0.25rem;' }, `模型: ${msg.metadata.model}`)
      : null,
  ]);
}

function renderPdfMessage(ctx: MessagesContext, msg: any) {
  return h('div', { style: 'margin-top:0.25rem;' }, [
    msg.metadata?.status === 'failed'
      ? h('div', { style: 'color:#ef4444;font-size:0.8rem;' }, `❌ 解析失败: ${msg.metadata?.error || '未知错误'}`)
      : null,
    ...(msg.metadata?.files && msg.metadata.files.length > 0
      ? [h('div', { style: 'display:flex;flex-direction:column;gap:0.5rem;margin-top:0.375rem;' },
          msg.metadata.files.map((file: any, fIdx: number) => {
            const fileUrl = file.url || file.path || '';
            if (!fileUrl) return null;
            const name = file.filename || '文件';
            return h('div', {
              key: fIdx,
              style: 'display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0.75rem;background:rgba(255,255,255,0.08);border:1px solid var(--color-border);border-radius:0.5rem;cursor:pointer;',
              onClick: () => ctx.handleFileDownload(fileUrl, name),
            }, [
              h('span', { style: 'font-size:1.25rem;' }, '📄'),
              h('div', { style: 'display:flex;flex-direction:column;min-width:0;' }, [
                h('span', { style: 'font-size:0.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, name),
                h('span', { style: 'font-size:0.7rem;opacity:0.7;' },
                  `${file.format ? file.format + ' · ' : ''}${ctx.formatFileSize(file.size || 0)}`),
              ]),
            ]);
          }).filter(Boolean)
        )]
      : [msg.metadata?.status !== 'failed' ? h('div', { style: 'font-size:0.8rem;opacity:0.7;' }, '暂无解析文件') : null]),
    msg.metadata?.provider
      ? h('div', { style: 'font-size:0.7rem;opacity:0.5;margin-top:0.25rem;' }, `提供商: ${msg.metadata.provider}`)
      : null,
  ]);
}

function renderMusicMessage(ctx: MessagesContext, msg: any, index: number) {
  return h('div', { style: 'margin-top:0.5rem;' }, [
    // 状态/错误提示
    msg.metadata?.status === 'failed'
      ? h('div', { style: 'color:#ef4444;font-size:0.8rem;margin-bottom:0.5rem;' }, `❌ 生成失败: ${msg.metadata?.error || '未知错误'}`)
      : null,
    // 流式播放中状态
    ...(msg.metadata?.streaming && msg.metadata?.songChunks
      ? Object.entries(msg.metadata.songChunks as Record<string, string[]>).map(([songId, chunks]) => {
          if (!chunks || chunks.length === 0) return null;
          const streamMsgId = `${msg.id || 'msg_' + index}_stream_${songId}`;
          const isPlaying = ctx.streamPlaying.value[streamMsgId] || false;
          const streamState = ctx.audioPlayers.value[streamMsgId] || { playing: false, currentTime: 0, duration: 0, volume: 1.0, muted: false, playbackRate: 1.0 };
          return h('div', {
            key: `stream_${songId}`,
            style: 'background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;padding:0.75rem;margin-top:0.5rem;',
          }, [
            h('div', { style: 'font-size:0.75rem;opacity:0.7;margin-bottom:0.375rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, `🎵 流式播放中...`),
            h('div', { style: 'display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;' }, [
              h('button', {
                style: 'flex-shrink:0;width:2rem;height:2rem;border-radius:50%;border:1px solid var(--color-border);background:var(--color-bg-hover);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:0.9rem;',
                onClick: () => {
                  ctx.toggleStreamPlay(streamMsgId, () => {
                    const currentMsg = ctx.messages.value.find((m: any) => m.id === msg.id);
                    return currentMsg?.metadata?.songChunks?.[songId] || [];
                  }, { jobId: msg.metadata?.job_id || '', songId });
                },
              }, isPlaying ? '⏸' : '▶'),
              h('div', { style: 'flex:1;min-width:80px;height:6px;background:var(--color-bg-hover);border-radius:3px;overflow:hidden;position:relative;' }, [
                h('div', { style: `position:absolute;left:0;top:0;height:100%;width:${isPlaying ? '30%' : '10%'};background:var(--color-primary);border-radius:3px;opacity:0.7;${isPlaying ? 'animation:pulse 1.5s ease-in-out infinite;' : ''}` }),
              ]),
              h('span', { style: 'flex-shrink:0;font-size:0.7rem;opacity:0.7;font-family:monospace;min-width:5.5rem;text-align:right;' },
                `${chunks.length} chunks · ${ctx.formatTime(streamState.currentTime)}`
              ),
              // 音量控制
              renderVolumeControl(ctx, streamMsgId, streamState),
              // 三点菜单（下载置灰）
              h('div', { style: 'position:relative;' }, [
                h('button', {
                  class: 'audio-menu-btn', 'data-msgid': streamMsgId,
                  style: 'flex-shrink:0;width:1.75rem;height:1.75rem;border-radius:50%;border:1px solid var(--color-border);background:var(--color-bg-hover);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:0.85rem;',
                  onClick: () => ctx.toggleAudioMenu(streamMsgId),
                }, '⋮'),
                ctx.showAudioMenu.value[streamMsgId]
                  ? h('div', {
                      class: 'audio-menu', 'data-msgid': streamMsgId,
                      style: 'position:absolute;bottom:calc(100% + 8px);right:0;padding:0.375rem;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:10;min-width:200px;',
                    }, [
                      h('button', {
                        style: 'width:100%;padding:0.5rem 0.75rem;border:none;background:none;cursor:not-allowed;font-size:0.8rem;color:var(--color-text);text-align:left;border-radius:0.25rem;display:flex;align-items:center;gap:0.5rem;white-space:nowrap;opacity:0.4;',
                        disabled: true,
                      }, '⬇ 下载（生成中...）'),
                    ])
                  : null,
              ]),
            ]),
          ]);
        }).filter(Boolean)
      : []),
    // 完成的音频播放器
    ...((msg.metadata?.audios || []).map((audio: any, audioIdx: number) => {
      const audioUrl = audio.url || '';
      if (!audioUrl) return null;
      const msgId = `${msg.id || 'msg_' + index}_audio_${audioIdx}`;
      const state = ctx.audioPlayers.value[msgId] || { playing: false, currentTime: 0, duration: 0, volume: 1.0, muted: false, playbackRate: 1.0 };
      if (!ctx.audioPlayers.value[msgId]) {
        let streamInheritTime: number | undefined;
        let streamInheritSettings: StreamSettings | undefined;
        const jobId = msg.metadata?.job_id || '';
        if (jobId) {
          const matchingKeys = Object.keys(ctx.streamProgressCache).filter(k => k.startsWith(jobId + '_'));
          if (matchingKeys.length > audioIdx) {
            const key = matchingKeys[audioIdx];
            streamInheritTime = ctx.streamProgressCache[key];
            streamInheritSettings = ctx.streamSettingsCache[key];
          }
          if (matchingKeys.length > 0 && audioIdx === 0) {
            matchingKeys.forEach(k => { delete ctx.streamProgressCache[k]; delete ctx.streamSettingsCache[k]; });
          }
        }
        ctx.initAudioPlayer(msgId, audioUrl, streamInheritTime, streamInheritSettings);
      }
      return h('div', {
        key: audioIdx,
        style: 'background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;padding:0.75rem;margin-top:0.5rem;',
      }, [
        audio.filename ? h('div', { style: 'font-size:0.75rem;opacity:0.7;margin-bottom:0.375rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, `🎵 ${audio.filename}`) : null,
        renderFullAudioPlayerInner(ctx, msgId, audioUrl, state, audio.filename || 'audio.mp3'),
        audio.lyrics
          ? h('details', { style: 'font-size:0.8rem;margin-top:0.5rem;' }, [
              h('summary', { style: 'cursor:pointer;opacity:0.8;' }, '📝 歌词'),
              h('pre', { style: 'margin-top:0.375rem;white-space:pre-wrap;font-size:0.75rem;opacity:0.8;line-height:1.5;font-family:inherit;' }, audio.lyrics),
            ])
          : null,
      ]);
    }).filter(Boolean)),
    // 统一歌词显示
    msg.metadata?.lyrics && !(msg.metadata?.audios || []).some((a: any) => a.lyrics)
      ? h('div', { style: 'margin-top:0.5rem;' }, [
          h('details', { style: 'font-size:0.8rem;' }, [
            h('summary', { style: 'cursor:pointer;opacity:0.8;' }, '📝 歌词'),
            h('pre', { style: 'margin-top:0.375rem;white-space:pre-wrap;font-size:0.75rem;opacity:0.8;line-height:1.5;font-family:inherit;' }, msg.metadata.lyrics),
          ]),
        ])
      : null,
    msg.metadata?.model
      ? h('div', { style: 'font-size:0.7rem;opacity:0.5;margin-top:0.25rem;' }, `模型: ${msg.metadata.model}`)
      : null,
  ]);
}

function renderAudioGeneratedMessage(ctx: MessagesContext, msg: any, index: number) {
  return h('div', { style: 'margin-top:0.5rem;' }, [
    !msg.metadata?.audio_url
      ? h('div', { style: 'color:#ef4444;font-size:0.8rem;' }, '❌ 语音合成失败或数据丢失')
      : (() => {
          const audioUrl = msg.metadata.audio_url;
          const msgId = `${msg.id || 'msg_' + index}_tts_audio`;
          const state = ctx.audioPlayers.value[msgId] || { playing: false, currentTime: 0, duration: 0, volume: 1.0, muted: false, playbackRate: 1.0 };
          if (!ctx.audioPlayers.value[msgId]) {
            ctx.initAudioPlayer(msgId, audioUrl);
          }
          return h('div', {
            style: 'background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;padding:0.75rem;max-width:360px;',
          }, [
            msg.content ? h('div', { style: 'font-size:0.8rem;opacity:0.8;margin-bottom:0.5rem;line-height:1.5;' }, msg.content) : null,
            renderFullAudioPlayerInner(ctx, msgId, audioUrl, state, 'tts_audio.mp3'),
          ]);
        })(),
  ]);
}

/** 渲染完整态音频播放器（含 TTS 暂停/恢复回调） */
function renderFullAudioPlayer(ctx: MessagesContext, msgId: string, audioUrl: string, playerState: AudioPlayerState, callbacks?: { onUserPause?: () => void; onUserResume?: () => void }, downloadFilename?: string) {
  const rateLabel = ctx.playbackRates.find(r => r.value === playerState.playbackRate)?.label || '正常';
  return h('div', {
    style: 'background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;padding:0.5rem 0.75rem;margin-bottom:0.5rem;max-width:360px;',
  }, [
    h('div', { style: 'display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;' }, [
      h('button', {
        style: 'flex-shrink:0;width:1.75rem;height:1.75rem;border-radius:50%;border:1px solid var(--color-border);background:var(--color-bg-hover);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:0.8rem;',
        onClick: () => ctx.toggleAudioPlay(msgId, audioUrl, callbacks?.onUserPause, callbacks?.onUserResume),
      }, playerState.playing ? '⏸' : '▶'),
      h('input', {
        type: 'range', min: 0, max: playerState.duration || 1, step: 0.1, value: playerState.currentTime,
        style: 'flex:1;min-width:60px;height:4px;accent-color:var(--color-primary);cursor:pointer;',
        onInput: (e: Event) => ctx.handleSeekInput(msgId, parseFloat((e.target as HTMLInputElement).value)),
        onChange: (e: Event) => ctx.handleSeekChange(msgId, parseFloat((e.target as HTMLInputElement).value)),
      }),
      h('span', { style: 'flex-shrink:0;font-size:0.7rem;opacity:0.7;font-family:monospace;' },
        `${ctx.formatTime(playerState.currentTime)}/${ctx.formatTime(playerState.duration)}`
      ),
      renderVolumeControl(ctx, msgId, playerState),
      renderAudioMenu(ctx, msgId, audioUrl, rateLabel, playerState, downloadFilename),
    ]),
  ]);
}

/** 渲染完整态音频播放器内部控件（不含外层容器，用于音乐/TTS消息内嵌） */
function renderFullAudioPlayerInner(ctx: MessagesContext, msgId: string, audioUrl: string, state: AudioPlayerState, downloadFilename?: string) {
  const rateLabel = ctx.playbackRates.find(r => r.value === state.playbackRate)?.label || '正常';
  return h('div', { style: 'display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;' }, [
    h('button', {
      style: 'flex-shrink:0;width:2rem;height:2rem;border-radius:50%;border:1px solid var(--color-border);background:var(--color-bg-hover);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:0.9rem;',
      onClick: () => ctx.toggleAudioPlay(msgId, audioUrl),
    }, state.playing ? '⏸' : '▶'),
    h('input', {
      type: 'range', min: 0, max: state.duration || 0, step: 0.1, value: state.currentTime,
      style: 'flex:1;height:6px;cursor:pointer;accent-color:var(--color-primary);min-width:80px;',
      onInput: (e: Event) => ctx.handleSeekInput(msgId, parseFloat((e.target as HTMLInputElement).value)),
      onChange: (e: Event) => ctx.handleSeekChange(msgId, parseFloat((e.target as HTMLInputElement).value)),
    }),
    h('span', { style: 'flex-shrink:0;font-size:0.7rem;opacity:0.7;font-family:monospace;min-width:5.5rem;text-align:right;' },
      `${ctx.formatTime(state.currentTime)} / ${ctx.formatTime(state.duration)}`
    ),
    renderVolumeControl(ctx, msgId, state),
    renderAudioMenu(ctx, msgId, audioUrl, rateLabel, state, downloadFilename),
  ]);
}

/** 渲染音量控制区域 */
function renderVolumeControl(ctx: MessagesContext, msgId: string, state: AudioPlayerState) {
  return h('div', {
    style: 'position:relative;display:flex;align-items:center;',
    onMouseenter: () => ctx.showVolumeSliderFor(msgId),
    onMouseleave: () => ctx.hideVolumeSliderFor(msgId),
  }, [
    h('button', {
      style: 'flex-shrink:0;width:1.75rem;height:1.75rem;border-radius:50%;border:1px solid var(--color-border);background:var(--color-bg-hover);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:0.85rem;',
      onClick: () => ctx.toggleMute(msgId),
    }, state.muted || state.volume === 0 ? '🔇' : state.volume < 0.5 ? '🔉' : '🔊'),
    ctx.showVolumeSlider.value[msgId]
      ? h('div', {
          style: 'position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);padding:0.5rem;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:10;',
          onMouseenter: ctx.cancelVolumeHide,
          onMouseleave: () => ctx.hideVolumeSliderFor(msgId),
        }, [
          h('input', {
            type: 'range', min: 0, max: 1, step: 0.01, value: state.muted ? 0 : state.volume,
            style: 'width:80px;height:4px;accent-color:var(--color-primary);cursor:pointer;',
            onInput: (e: Event) => ctx.handleVolumeChange(msgId, parseFloat((e.target as HTMLInputElement).value)),
          }),
        ])
      : null,
  ]);
}

/** 渲染三点菜单（下载 + 播放速度） */
function renderAudioMenu(ctx: MessagesContext, msgId: string, audioUrl: string, rateLabel: string, state: AudioPlayerState, downloadFilename?: string) {
  return h('div', { style: 'position:relative;' }, [
    h('button', {
      class: 'audio-menu-btn', 'data-msgid': msgId,
      style: 'flex-shrink:0;width:1.75rem;height:1.75rem;border-radius:50%;border:1px solid var(--color-border);background:var(--color-bg-hover);cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:0.85rem;',
      onClick: () => ctx.toggleAudioMenu(msgId),
    }, '⋮'),
    ctx.showAudioMenu.value[msgId]
      ? h('div', {
          class: 'audio-menu', 'data-msgid': msgId,
          style: 'position:absolute;bottom:calc(100% + 8px);right:0;padding:0.375rem;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:10;min-width:200px;',
        }, [
          h('button', {
            style: 'width:100%;padding:0.5rem 0.75rem;border:none;background:none;cursor:pointer;font-size:0.8rem;color:var(--color-text);text-align:left;border-radius:0.25rem;display:flex;align-items:center;gap:0.5rem;white-space:nowrap;',
            onMouseenter: (e: MouseEvent) => { (e.currentTarget as HTMLElement).style.background = 'var(--color-bg-hover)'; },
            onMouseleave: (e: MouseEvent) => { (e.currentTarget as HTMLElement).style.background = 'none'; },
            onClick: () => { ctx.downloadAudio(msgId, audioUrl, downloadFilename || 'audio.mp3'); ctx.showAudioMenu.value[msgId] = false; },
          }, '⬇ 下载'),
          h('div', { style: 'position:relative;' }, [
            h('button', {
              style: 'width:100%;padding:0.5rem 0.75rem;border:none;background:none;cursor:pointer;font-size:0.8rem;color:var(--color-text);text-align:left;border-radius:0.25rem;display:flex;align-items:center;gap:0.5rem;justify-content:space-between;white-space:nowrap;',
              onMouseenter: (e: MouseEvent) => { (e.currentTarget as HTMLElement).style.background = 'var(--color-bg-hover)'; },
              onMouseleave: (e: MouseEvent) => { (e.currentTarget as HTMLElement).style.background = 'none'; },
              onClick: () => { ctx.showPlaybackRateMenu.value[msgId] = !ctx.showPlaybackRateMenu.value[msgId]; },
            }, [
              h('span', null, '⏱ 播放速度'),
              h('span', { style: 'opacity:0.6;flex-shrink:0;' }, rateLabel),
            ]),
            ctx.showPlaybackRateMenu.value[msgId]
              ? h('div', {
                  style: 'position:absolute;right:calc(100% + 8px);bottom:0;padding:0.375rem;background:var(--color-bg-card);border:1px solid var(--color-border);border-radius:0.5rem;box-shadow:0 2px 8px rgba(0,0,0,0.15);z-index:10;min-width:90px;',
                }, [
                  ...ctx.playbackRates.map((rate) =>
                    h('button', {
                      key: rate.value,
                      style: `width:100%;padding:0.375rem 0.75rem;border:none;background:${rate.value === state.playbackRate ? 'var(--color-primary-light)' : 'none'};cursor:pointer;font-size:0.8rem;color:var(--color-text);text-align:left;border-radius:0.25rem;white-space:nowrap;`,
                      onMouseenter: (e: MouseEvent) => { if (rate.value !== state.playbackRate) (e.currentTarget as HTMLElement).style.background = 'var(--color-bg-hover)'; },
                      onMouseleave: (e: MouseEvent) => { if (rate.value !== state.playbackRate) (e.currentTarget as HTMLElement).style.background = 'none'; },
                      onClick: () => ctx.setPlaybackRate(msgId, rate.value),
                    }, rate.label)
                  ),
                ])
              : null,
          ]),
        ])
      : null,
  ]);
}

/** 渲染消息附件 */
function renderAttachments(ctx: MessagesContext, msg: any) {
  return msg.metadata.attachments.map((att: any) => {
    const fileUrl = att.url || '';
    if (att.is_image) {
      return h('div', { key: att.file_id || fileUrl, style: 'margin-top:0.5rem;' }, [
        h('img', {
          src: fileUrl,
          alt: att.name || '图片',
          style: 'max-width:300px;max-height:200px;border-radius:0.5rem;cursor:pointer;object-fit:cover;',
          onClick: () => ctx.handleImagePreview(fileUrl),
        }),
      ]);
    } else {
      return h('div', {
        key: att.file_id || fileUrl,
        style: 'margin-top:0.5rem;display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0.75rem;background:rgba(255,255,255,0.1);border-radius:0.5rem;cursor:pointer;',
        onClick: () => ctx.handleFileDownload(fileUrl, att.name || 'file'),
      }, [
        h('span', { style: 'font-size:1.25rem;' }, '📄'),
        h('div', { style: 'display:flex;flex-direction:column;min-width:0;' }, [
          h('span', { style: 'font-size:0.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' }, att.name || '文件'),
          h('span', { style: 'font-size:0.7rem;opacity:0.7;' }, ctx.formatFileSize(att.size || 0)),
        ]),
      ]);
    }
  });
}

/** 渲染工具调用 */
function renderToolCalls(ctx: MessagesContext, msg: any) {
  const hasToolResults = msg.tool_results && msg.tool_results.length > 0;
  const hasToolCalls = msg.tool_calls && msg.tool_calls.length > 0;
  if (!hasToolResults && msg.message_type !== 'tool_call' && !hasToolCalls) return null;

  return h('details', { style: 'margin-top:0.5rem;font-size:0.75rem;' }, [
    h('summary', { style: 'cursor:pointer;opacity:0.8;' },
      hasToolResults
        ? `🔧 工具调用 (${msg.tool_results.length})`
        : hasToolCalls
          ? `🔧 工具调用 (${msg.tool_calls.length})`
          : '🔧 工具调用'
    ),
    ...(hasToolResults
      ? msg.tool_results.map((tool: any, toolIndex: number) => {
          const matchedCall = msg.tool_calls?.find((tc: any) => tc.id === tool.tool_call_id);
          const argsJson = matchedCall?.function?.arguments;
          let argsDisplay: string | null = null;
          if (argsJson) {
            try {
              const parsed = JSON.parse(argsJson);
              argsDisplay = Object.keys(parsed).length > 0 ? JSON.stringify(parsed, null, 2) : null;
            } catch { argsDisplay = argsJson; }
          }

          const children = [
            h('div', { key: `header-${toolIndex}`, style: 'font-weight:600;margin-bottom:0.25rem;padding:0.25rem 0;' },
              `${tool.name} - ${tool.success ? '✅ 成功' : '❌ 失败'}`
            ),
          ];

          const detailChildren: any[] = [];
          if (argsDisplay) {
            detailChildren.push(
              h('div', { key: `args-label-${toolIndex}`, style: 'font-size:0.7rem;color:var(--color-text-secondary);margin-bottom:0.125rem;' }, '调用参数'),
              h('pre', { key: `args-${toolIndex}`, style: 'margin-top:0.125rem;padding:0.5rem;background:rgba(59,130,246,0.06);border-radius:0.25rem;overflow-x:auto;font-size:0.7rem;border-left:3px solid #93c5fd;' }, argsDisplay)
            );
          }

          const hasStructured = tool.structured !== undefined && tool.structured !== null;
          const resultText = hasStructured
            ? JSON.stringify(tool.structured, null, 2)
            : (typeof tool.result === 'string' ? tool.result : JSON.stringify(tool.result, null, 2));
          detailChildren.push(
            h('div', { key: `result-label-${toolIndex}`, style: 'font-size:0.7rem;color:var(--color-text-secondary);margin-top:0.375rem;margin-bottom:0.125rem;' }, hasStructured ? '返回结果（结构化）' : '返回结果'),
            h('pre', { style: 'margin-top:0.125rem;padding:0.5rem;background:rgba(0,0,0,0.05);border-radius:0.25rem;overflow-x:auto;font-size:0.7rem;border-left:3px solid var(--color-primary);' }, resultText)
          );

          if (tool.name === 'spawn_agent' && tool.dag) {
            const dagView = renderDAG(tool.dag);
            if (dagView) detailChildren.push(dagView);
          }

          if (detailChildren.length > 0) {
            children.push(
              h('details', { key: `details-${toolIndex}`, style: 'margin-top:0.375rem;font-size:0.75rem;' }, [
                h('summary', { style: 'cursor:pointer;opacity:0.7;font-size:0.7rem;' }, '查看详情'),
                ...detailChildren,
              ])
            );
          }

          return h('div', { key: toolIndex, style: 'margin-top:0.5rem;padding:0.5rem;background:rgba(0,0,0,0.02);border-radius:0.25rem;border-left:3px solid var(--color-primary);' }, children);
        })
      : hasToolCalls
        ? msg.tool_calls.map((tc: any, tcIndex: number) => {
            const toolName = tc.function?.name || 'unknown';
            const isSpawnTool = toolName === 'spawn_agent';
            let argsDisplay: string | null = null;
            if (tc.function?.arguments) {
              try {
                const parsed = JSON.parse(tc.function.arguments);
                argsDisplay = Object.keys(parsed).length > 0 ? JSON.stringify(parsed, null, 2) : null;
              } catch { argsDisplay = tc.function.arguments; }
            }
            const children = [
              h('div', { style: 'font-weight:600;margin-bottom:0.25rem;color:var(--color-text-secondary);' }, `${toolName} - ⏳ 执行中...`),
            ];
            const detailChildren: any[] = [];
            if (argsDisplay) {
              detailChildren.push(
                h('div', { style: 'font-size:0.7rem;color:var(--color-text-secondary);margin-bottom:0.125rem;' }, '调用参数'),
                h('pre', { style: 'margin-top:0.125rem;padding:0.5rem;background:rgba(59,130,246,0.06);border-radius:0.25rem;overflow-x:auto;font-size:0.7rem;border-left:3px solid #93c5fd;' }, argsDisplay)
              );
            }
            if (isSpawnTool && ctx.currentDag.value) {
              const dagView = renderDAG(ctx.currentDag.value);
              if (dagView) detailChildren.push(dagView);
            }
            if (detailChildren.length > 0) {
              children.push(
                h('details', { style: 'margin-top:0.375rem;font-size:0.75rem;' }, [
                  h('summary', { style: 'cursor:pointer;opacity:0.7;font-size:0.7rem;' }, '查看详情'),
                  ...detailChildren,
                ])
              );
            }
            return h('div', { key: tcIndex, style: 'margin-top:0.5rem;padding:0.5rem;background:rgba(0,0,0,0.05);border-radius:0.25rem;border-left:3px solid var(--color-text-disabled);' }, children);
          })
        : [h('div', { style: 'margin-top:0.5rem;opacity:0.7;' }, '执行中...')]
    ),
  ]);
}
