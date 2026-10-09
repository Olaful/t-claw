/**
 * 斜杠命令系统 composable
 * 管理斜杠命令的加载、解析、过滤、校验、执行
 */
import { ref, computed, type Ref } from 'vue';
import type { ParamPromptData, ParsedSlashInput } from '../types';

const API_BASE = 'http://localhost:8000/api/v1';

export interface UseSlashCommandsOptions {
  /** 获取当前 agentId 的函数 */
  getAgentId: () => string;
  /** 输入消息 ref */
  inputMessage: Ref<string>;
  /** 聚焦输入框 */
  focusInput: () => void;
  /** 滚动到底部 */
  scrollToBottom: () => void;
  /** chatStore 引用 */
  chatStore: any;
}

export function useSlashCommands(options: UseSlashCommandsOptions) {
  const { getAgentId, inputMessage, focusInput, scrollToBottom, chatStore } = options;

  // ===== 状态 =====
  const showSlashPopup = ref(false);
  const slashCommands = ref<Record<string, any[]>>({});
  const slashFilter = ref('');
  const slashSelectedIdx = ref(0);
  const showMoreCounts = ref<Record<string, number>>({}); // 每栏显示数量，默认30
  const showParamPrompt = ref(false);
  const paramPromptData = ref<ParamPromptData | null>(null);
  const paramInputValues = ref<Record<string, string>>({});
  const paramPromptError = ref('');

  // ===== 加载斜杠命令配置 =====

  const loadSlashCommands = async (agentId?: string) => {
    try {
      const url = agentId
        ? `${API_BASE}/slash-commands?agent_id=${agentId}`
        : `${API_BASE}/slash-commands`;
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        let commands = data.commands || {};
        const mediaModels = data.media_models;
        const hasPdf = !!data.has_pdf;

        // 根据 mediaModels 的子配置条件性显示媒体命令
        const hasImageModel = !!(mediaModels && (mediaModels as any).image);
        const hasMusicModel = !!(mediaModels && (mediaModels as any).music);
        const hasVideoModel = !!(mediaModels && (mediaModels as any).video);
        if (!hasImageModel || !hasMusicModel || !hasVideoModel || !hasPdf) {
          for (const [title, cmds] of Object.entries(commands)) {
            commands[title] = (cmds as any[]).filter((cmd: any) => {
              if (cmd.name === 'image_generate') return hasImageModel;
              if (cmd.name === 'music_generate') return hasMusicModel;
              if (cmd.name === 'video_generate') return hasVideoModel;
              if (cmd.name === 'pdf') return hasPdf;
              return true;
            });
          }
          // 清理空分类
          for (const title of Object.keys(commands)) {
            if (commands[title].length === 0) delete commands[title];
          }
        }

        slashCommands.value = commands;
      }
    } catch (e) {
      console.error('Failed to load slash commands:', e);
    }
  };

  // ===== 计算属性 =====

  const getFilteredCommands = computed(() => {
    const filter = slashFilter.value.toLowerCase();
    const result: Record<string, any[]> = {};
    for (const [title, commands] of Object.entries(slashCommands.value)) {
      const filtered = (commands as any[]).filter(cmd =>
        !filter || cmd.name.toLowerCase().includes(filter) || (cmd.description || '').toLowerCase().includes(filter)
      );
      if (filtered.length > 0) {
        result[title] = filtered;
      }
    }
    return result;
  });

  const getFlatCommandList = computed(() => {
    const list: Array<{ title: string; command: any }> = [];
    for (const [title, commands] of Object.entries(getFilteredCommands.value)) {
      const maxShow = showMoreCounts.value[title] || 30;
      (commands as any[]).slice(0, maxShow).forEach(cmd => {
        list.push({ title, command: cmd });
      });
    }
    return list;
  });

  // ===== 引号感知分词器 =====

  const tokenizeInput = (text: string): string[] => {
    const tokens: string[] = [];
    const isQuote = (c: string) => c === '"' || c === "'";
    let i = 0;
    while (i < text.length) {
      if (/\s/.test(text[i])) { i++; continue; }
      let token = '';
      if (isQuote(text[i])) {
        const q = text[i];
        i++;
        while (i < text.length && text[i] !== q) {
          token += text[i]; i++;
        }
        if (i < text.length) i++;
      } else {
        while (i < text.length && !/\s/.test(text[i])) {
          if (text[i] === '[') {
            let depth = 0;
            while (i < text.length) {
              const c = text[i];
              if (isQuote(c)) {
                const q = c;
                token += c; i++;
                while (i < text.length && text[i] !== q) {
                  token += text[i]; i++;
                }
                if (i < text.length) { token += text[i]; i++; }
              } else if (c === '[') {
                depth++; token += c; i++;
              } else if (c === ']') {
                depth--; token += c; i++;
                if (depth <= 0) break;
              } else {
                token += c; i++;
              }
            }
          } else if (isQuote(text[i])) {
            const q = text[i];
            i++;
            while (i < text.length && text[i] !== q) {
              token += text[i]; i++;
            }
            if (i < text.length) i++;
          } else {
            token += text[i]; i++;
          }
        }
      }
      if (token) tokens.push(token);
    }
    return tokens;
  };

  // ===== 数组值解析 =====

  const parseArrayValue = (raw: string): string[] | null => {
    if (raw === undefined || raw === null) return null;
    let value = String(raw).trim();
    if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
      value = value.slice(1, -1).trim();
    }
    if (!value.startsWith('[') || !value.endsWith(']')) return null;
    // 1. 标准 JSON.parse
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed.map(v => String(v));
    } catch (e) { /* fallback */ }
    // 2. 单引号 → 双引号
    try {
      const normalized = value.replace(/'/g, '"');
      const parsed = JSON.parse(normalized);
      if (Array.isArray(parsed)) return parsed.map(v => String(v));
    } catch (e) { /* fallback */ }
    // 3. 手动分割
    const inner = value.slice(1, -1).trim();
    if (inner === '') return [];
    return inner.split(',').map(item => {
      let s = item.trim();
      if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
        s = s.slice(1, -1);
      }
      return s;
    }).filter(s => s !== '');
  };

  // ===== 解析斜杠命令输入 =====

  const parseSlashInput = (input: string): ParsedSlashInput | null => {
    const text = input.startsWith('/') ? input.slice(1) : input;
    const parts = tokenizeInput(text);
    if (parts.length === 0) return null;

    const commandName = parts[0];
    const rawArgs = parts.slice(1);

    for (const [title, commands] of Object.entries(slashCommands.value)) {
      const cmd = (commands as any[]).find(c => c.name === commandName);
      if (cmd) {
        const argNames = new Set<string>();
        if (cmd.arguments && typeof cmd.arguments === 'object') {
          for (const name of Object.keys(cmd.arguments)) {
            argNames.add(name);
          }
        }

        const positionalArgs: string[] = [];
        const kwargs: Record<string, string> = {};
        let currentKey: string | null = null;
        let currentValues: string[] = [];

        for (const token of rawArgs) {
          const eqIdx = token.indexOf('=');
          if (eqIdx > 0) {
            const possibleKey = token.substring(0, eqIdx);
            if (argNames.has(possibleKey)) {
              if (currentKey) {
                kwargs[currentKey] = currentValues.join(' ');
              }
              currentKey = possibleKey;
              currentValues = [token.substring(eqIdx + 1)];
              continue;
            }
          }

          if (currentKey) {
            currentValues.push(token);
          } else {
            positionalArgs.push(token);
          }
        }
        if (currentKey) {
          kwargs[currentKey] = currentValues.join(' ');
        }

        return { title, command: commandName, args: positionalArgs, kwargs };
      }
    }
    return null;
  };

  // ===== 构建命令参数 =====

  const buildCommandArgs = (title: string, commandName: string, positionalArgs: string[], kwargs?: Record<string, string>, extraArgs?: Record<string, string>): Record<string, any> => {
    const cmd = (slashCommands.value[title] || []).find((c: any) => c.name === commandName);
    if (!cmd || !cmd.arguments || typeof cmd.arguments !== 'object') return {};

    const argDefs = cmd.arguments as Record<string, any>;
    const requiredArgs: string[] = [];
    const optionalArgs: string[] = [];
    for (const [argName, argDef] of Object.entries(argDefs)) {
      if (argDef.required) {
        requiredArgs.push(argName);
      } else {
        optionalArgs.push(argName);
      }
    }
    const orderedArgs = [...requiredArgs, ...optionalArgs];

    const result: Record<string, any> = {};
    const convertByType = (argName: string, rawValue: any): any => {
      const argType = (argDefs[argName] && argDefs[argName].type) || 'string';
      if (argType === 'array') {
        if (Array.isArray(rawValue)) return rawValue;
        const parsedArr = parseArrayValue(String(rawValue));
        if (parsedArr !== null) return parsedArr;
        return String(rawValue).split(',').map(s => s.trim()).filter(s => s !== '');
      }
      if (argType === 'number') {
        const n = Number(rawValue);
        return Number.isNaN(n) ? rawValue : n;
      }
      if (argType === 'boolean') {
        const s = String(rawValue).toLowerCase();
        return s === 'true' || s === '1' || s === 'yes';
      }
      return rawValue;
    };

    // 1. 位置参数按顺序赋值
    let posIdx = 0;
    for (const argName of requiredArgs) {
      if (posIdx < positionalArgs.length) {
        result[argName] = convertByType(argName, positionalArgs[posIdx++]);
      }
    }
    for (const argName of optionalArgs) {
      if (posIdx >= positionalArgs.length) break;
      if (kwargs && kwargs[argName] !== undefined) continue;
      result[argName] = convertByType(argName, positionalArgs[posIdx++]);
    }
    // 2. 关键字参数
    if (kwargs) {
      for (const [key, value] of Object.entries(kwargs)) {
        if (argDefs[key]) {
          result[key] = convertByType(key, value);
        }
      }
    }
    // 3. 填充默认值
    for (const argName of orderedArgs) {
      if (result[argName] === undefined && argDefs[argName].default !== undefined) {
        result[argName] = argDefs[argName].default;
      }
    }
    // 4. 合并额外参数
    if (extraArgs) {
      Object.assign(result, extraArgs);
    }
    return result;
  };

  // ===== 检查必选参数缺失 =====

  const getMissingRequiredArgs = (title: string, commandName: string, args: Record<string, any>): Array<{ name: string; config: any }> => {
    const cmd = (slashCommands.value[title] || []).find((c: any) => c.name === commandName);
    if (!cmd || !cmd.arguments || typeof cmd.arguments !== 'object') return [];

    const missing: Array<{ name: string; config: any }> = [];
    for (const [argName, argDef] of Object.entries(cmd.arguments as Record<string, any>)) {
      if (argDef.required && (args[argName] === undefined || args[argName] === '')) {
        missing.push({ name: argName, config: argDef });
      }
    }
    return missing;
  };

  // ===== 参数类型规范化 =====

  const normalizeCommandArgs = (title: string, commandName: string, args: Record<string, any>): Record<string, any> => {
    const cmd = (slashCommands.value[title] || []).find((c: any) => c.name === commandName);
    if (!cmd || !cmd.arguments || typeof cmd.arguments !== 'object') return args;
    const argDefs = cmd.arguments as Record<string, any>;
    const normalized: Record<string, any> = {};
    for (const [key, value] of Object.entries(args)) {
      const argType = (argDefs[key] && argDefs[key].type) || 'string';
      if (value === undefined || value === null || value === '') {
        normalized[key] = value;
        continue;
      }
      if (argType === 'array') {
        if (Array.isArray(value)) {
          normalized[key] = value;
        } else {
          const parsedArr = parseArrayValue(String(value));
          normalized[key] = parsedArr !== null ? parsedArr : String(value).split(',').map(s => s.trim()).filter(s => s !== '');
        }
      } else if (argType === 'number') {
        const n = Number(value);
        normalized[key] = Number.isNaN(n) ? value : n;
      } else if (argType === 'boolean') {
        if (typeof value === 'boolean') {
          normalized[key] = value;
        } else {
          const s = String(value).toLowerCase();
          normalized[key] = s === 'true' || s === '1' || s === 'yes';
        }
      } else {
        normalized[key] = value;
      }
    }
    return normalized;
  };

  // ===== 执行斜杠命令 =====

  const executeSlashCommand = async (title: string, commandName: string, args: Record<string, any>, rawInput: string) => {
    const cmd = (slashCommands.value[title] || []).find((c: any) => c.name === commandName);
    if (!cmd) return;

    // 纯前端命令
    if (cmd.only_frontend) {
      if (title === 'session' && commandName === 'new') {
        await chatStore.createConversation(getAgentId());
        focusInput();
      }
      return;
    }

    // 后端命令
    const agentId = getAgentId();
    const normalizedArgs = normalizeCommandArgs(title, commandName, args);
    await chatStore.executeSlashCommand(agentId, title, commandName, normalizedArgs, chatStore.activeConversationId, rawInput);
    scrollToBottom();
  };

  // ===== 处理斜杠命令发送 =====

  const handleSlashCommand = async (): Promise<boolean> => {
    const input = inputMessage.value.trim();
    if (!input.startsWith('/')) return false;

    const parsed = parseSlashInput(input);
    if (!parsed) return false;

    const { title, command, args: positionalArgs, kwargs } = parsed;
    const cmdArgs = buildCommandArgs(title, command, positionalArgs, kwargs);

    // 检查必选参数
    const missingArgs = getMissingRequiredArgs(title, command, cmdArgs);
    if (missingArgs.length > 0) {
      paramPromptData.value = { title, command: (slashCommands.value[title] || []).find((c: any) => c.name === command), missingArgs, existingArgs: cmdArgs };
      paramInputValues.value = {};
      paramPromptError.value = '';
      showParamPrompt.value = true;
      showSlashPopup.value = false;
      return true;
    }

    // 校验：有 options 的参数，输入值必须在候选列表中
    const cmd = (slashCommands.value[title] || []).find((c: any) => c.name === command);
    if (cmd && cmd.arguments && typeof cmd.arguments === 'object') {
      for (const [argName, argDef] of Object.entries(cmd.arguments as Record<string, any>)) {
        const def = argDef as any;
        if (!def.options || !Array.isArray(def.options) || def.options.length === 0) continue;

        let val = cmdArgs[argName];
        if (val === undefined || val === '') {
          val = def.default;
        }
        if (val === undefined || val === null || val === '') continue;

        const valuesToCheck = Array.isArray(val) ? val : [val];
        if (valuesToCheck.length === 0) continue;
        const hasInvalid = valuesToCheck.some((v: any) => !def.options.includes(v));
        if (hasInvalid) {
          const invalidArgs = [{ name: argName, config: def }];
          paramPromptData.value = { title, command: cmd, missingArgs: invalidArgs, existingArgs: cmdArgs };
          paramInputValues.value = { [argName]: Array.isArray(val) ? val.join(', ') : val };
          paramPromptError.value = `参数 ${argName} 的值必须是: ${def.options.join(', ')}`;
          showParamPrompt.value = true;
          showSlashPopup.value = false;
          return true;
        }
      }
    }

    // 参数齐全且合法，直接执行
    inputMessage.value = '';
    showSlashPopup.value = false;
    await executeSlashCommand(title, command, cmdArgs, input);
    return true;
  };

  // ===== 参数候选框确认 =====

  const handleParamPromptConfirm = async () => {
    if (!paramPromptData.value) return;
    const { title, command, missingArgs, existingArgs } = paramPromptData.value;

    // 校验：有 options 的参数，输入值必须在候选列表中
    for (const arg of missingArgs) {
      const val = (paramInputValues.value[arg.name] || '').trim();
      if (!val) {
        paramPromptError.value = `请填写必选参数: ${arg.name}`;
        return;
      }
      const opts = arg.config.options;
      if (opts && Array.isArray(opts) && opts.length > 0) {
        const valuesToCheck = arg.config.type === 'array'
          ? val.split(',').map((s: string) => s.trim()).filter((s: string) => s !== '')
          : [val];
        if (valuesToCheck.some((v: string) => !opts.includes(v))) {
          paramPromptError.value = `参数 ${arg.name} 的值必须是: ${opts.join(', ')}`;
          return;
        }
      }
    }
    paramPromptError.value = '';

    const finalArgs = { ...existingArgs, ...paramInputValues.value };
    const rawInput = `/${command.name} ${Object.values(finalArgs).join(' ')}`.trim();

    showParamPrompt.value = false;
    paramPromptData.value = null;
    inputMessage.value = '';
    await executeSlashCommand(title, command.name, finalArgs, rawInput);
  };

  return {
    // 状态
    showSlashPopup,
    slashCommands,
    slashFilter,
    slashSelectedIdx,
    showMoreCounts,
    showParamPrompt,
    paramPromptData,
    paramInputValues,
    paramPromptError,
    // 计算属性
    getFilteredCommands,
    getFlatCommandList,
    // 方法
    loadSlashCommands,
    handleSlashCommand,
    handleParamPromptConfirm,
    executeSlashCommand,
    parseSlashInput,
    buildCommandArgs,
    normalizeCommandArgs,
    getMissingRequiredArgs,
  };
}
