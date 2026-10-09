"""全局配置管理 - 统一从 .happy-claw/settings.json 读取，支持点号属性访问"""
import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional
import structlog

logger = structlog.get_logger()

# backend/ 目录的绝对路径（config.py 位于 backend/app/ 下）
_BACKEND_DIR = Path(__file__).resolve().parent.parent
# 项目根目录
_PROJECT_ROOT = _BACKEND_DIR.parent


def _read_env_file_early() -> dict:
    """提前解析 .env 文件（仅用于模块导入阶段的路径解析）
    
    完整的环境变量合并在 Settings.__init__ 中处理，
    这里只读取 .env 文件中的键值对，供 _resolve_happyclaw_home 等早期逻辑使用。
    """
    env_file = _BACKEND_DIR / ".env"
    if not env_file.exists():
        return {}
    data = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _resolve_happyclaw_home() -> Path:
    """解析 .happy-claw 根目录路径
    
    优先级（高到低）:
    1. 系统环境变量 HAPPYCLAW_HOME（os.environ）
    2. backend/.env 文件中的 HAPPYCLAW_HOME
    3. 默认值 ~/.happy-claw
    
    确保路径始终以 .happy-claw 结尾，避免文件散落在不合适的目录下。
    """
    # 1. 优先从系统环境变量读取
    raw = os.environ.get("HAPPYCLAW_HOME", "").strip()
    # 2. 回退到 .env 文件
    if not raw:
        raw = _read_env_file_early().get("HAPPYCLAW_HOME", "").strip()
    # 3. 解析路径
    if raw:
        p = Path(raw)
        # 如果路径不以 .happy-claw 结尾，自动追加
        if p.name != ".happy-claw":
            p = p / ".happy-claw"
        return p
    return Path.home() / ".happy-claw"


# .happy-claw 根目录
_HAPPYCLAW_HOME = _resolve_happyclaw_home()


def _ensure_happyclaw_home() -> None:
    """初始化 HAPPYCLAW_HOME 目录
    
    - 目录不存在则创建
    - 若项目下存在默认 settings.json 且目标目录尚无该文件，则拷贝过去
    """
    try:
        _HAPPYCLAW_HOME.mkdir(parents=True, exist_ok=True)
        logger.info("HAPPYCLAW_HOME ensured", path=str(_HAPPYCLAW_HOME))
    except Exception as e:
        logger.warning("Failed to create HAPPYCLAW_HOME directory",
                       path=str(_HAPPYCLAW_HOME), error=str(e))
        return

    # 拷贝默认 settings.json（仅当目标目录不存在该文件时）
    target_settings = _HAPPYCLAW_HOME / "settings.json"
    if not target_settings.exists():
        # 优先从项目根目录拷贝
        bundled = _PROJECT_ROOT / ".happy-claw" / "settings.json"
        if bundled.exists():
            try:
                shutil.copy2(str(bundled), str(target_settings))
                logger.info("Copied default settings.json to HAPPYCLAW_HOME",
                            src=str(bundled), dst=str(target_settings))
            except Exception as e:
                logger.warning("Failed to copy default settings.json",
                               error=str(e))


class DotDict:
    """
    支持点号属性访问的字典包装器。
    
    用法:
        d = DotDict({"a": {"b": {"c": 1}}})
        d.a.b.c       # -> 1
        d["a"]["b"]["c"]  # -> 1
    """

    def __init__(self, data: dict = None):
        object.__setattr__(self, '_data', {})
        if data:
            for key, value in data.items():
                if isinstance(value, dict):
                    object.__getattribute__(self, '_data')[key] = DotDict(value)
                elif isinstance(value, list):
                    object.__getattribute__(self, '_data')[key] = [
                        DotDict(item) if isinstance(item, dict) else item
                        for item in value
                    ]
                else:
                    object.__getattribute__(self, '_data')[key] = value

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, '_data')[name]
        except KeyError:
            raise AttributeError(f"配置项 '{name}' 不存在")

    def __setattr__(self, name: str, value: Any):
        if isinstance(value, dict):
            value = DotDict(value)
        object.__getattribute__(self, '_data')[name] = value

    def __getitem__(self, key: str) -> Any:
        return object.__getattribute__(self, '_data')[key]

    def __setitem__(self, key: str, value: Any):
        if isinstance(value, dict):
            value = DotDict(value)
        object.__getattribute__(self, '_data')[key] = value

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, '_data')

    def __repr__(self):
        return f"DotDict({object.__getattribute__(self, '_data')})"

    def __iter__(self):
        return iter(object.__getattribute__(self, '_data'))

    def __len__(self):
        return len(object.__getattribute__(self, '_data'))

    def __bool__(self):
        return bool(object.__getattribute__(self, '_data'))

    def __eq__(self, other):
        if isinstance(other, DotDict):
            return object.__getattribute__(self, '_data') == object.__getattribute__(other, '_data')
        if isinstance(other, dict):
            return object.__getattribute__(self, '_data') == other
        return False

    def get(self, key: str, default: Any = None) -> Any:
        """带默认值的获取方法"""
        try:
            return object.__getattribute__(self, '_data')[key]
        except KeyError:
            return default

    def keys(self):
        return object.__getattribute__(self, '_data').keys()

    def values(self):
        return object.__getattribute__(self, '_data').values()

    def items(self):
        return object.__getattribute__(self, '_data').items()

    def to_dict(self) -> dict:
        """转换为普通字典"""
        result = {}
        for key, value in object.__getattribute__(self, '_data').items():
            if isinstance(value, DotDict):
                result[key] = value.to_dict()
            elif isinstance(value, list):
                result[key] = [
                    item.to_dict() if isinstance(item, DotDict) else item
                    for item in value
                ]
            else:
                result[key] = value
        return result

    def update(self, data: dict):
        """用字典数据更新（递归合并）"""
        for key, value in data.items():
            if isinstance(value, dict):
                existing = object.__getattribute__(self, '_data').get(key)
                if isinstance(existing, DotDict):
                    existing.update(value)
                    continue
            if isinstance(value, dict):
                value = DotDict(value)
            object.__getattribute__(self, '_data')[key] = value


def _flatten_dotdict(source: DotDict, prefix: str = "") -> dict:
    """将 DotDict 扁平化为 UPPER_CASE 键的字典"""
    result = {}
    for key, value in source.items():
        full_key = f"{prefix}_{key.upper()}" if prefix else key.upper()
        if isinstance(value, DotDict):
            result.update(_flatten_dotdict(value, full_key))
            # 同时将嵌套 DotDict 本身也存一份（支持 settings.AGENT_SUBAGENT_DEFAULT_CONFIG.llm_provider）
            result[full_key] = value
        else:
            result[full_key] = value
    return result


def _load_settings_json() -> dict:
    """加载 .happy-claw/settings.json"""
    # 确保目录存在并拷贝默认配置
    _ensure_happyclaw_home()

    settings_file = _HAPPYCLAW_HOME / "settings.json"
    if settings_file.exists():
        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.info("Loaded settings.json", path=str(settings_file))
            return data
        except Exception as e:
            logger.warning("Failed to load settings.json", error=str(e), exc_info=True)
    else:
        logger.warning("settings.json not found", path=str(settings_file))
    return {}


def _load_env_file() -> dict:
    """解析 .env 文件为字典"""
    env_data = {}
    env_file = _BACKEND_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                env_data[key] = value
    # 也合并当前环境变量（优先级更高）
    for key, value in os.environ.items():
        if key in env_data or key.isupper():
            env_data[key] = value
    return env_data


def _coerce_value(value: str) -> Any:
    """将字符串值转换为合适的类型"""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


# ---- 环境变量名 → settings.json 路径的映射 ----
# 格式: ENV_VAR_NAME -> ("json_path", "to", "key")
_ENV_TO_JSON_PATH = {
    # 如果需要将某个环境变量覆盖到 settings.json 的特定路径，在此添加
    # 例如: "DEFAULT_LLM_MODEL" -> ("agent", "default_llm_model")
}


class Settings:
    """
    统一配置对象。
    
    配置来源优先级（高到低）:
    1. 环境变量 / .env 文件
    2. .happy-claw/settings.json
    
    支持两种访问方式:
    - 扁平大写: settings.APP_NAME, settings.AGENT_SUBAGENT_ENABLED
    - 点号嵌套: settings.agent.subagent.enabled
    - 混用:     settings.AGENT_SUBAGENT_DEFAULT_CONFIG.llm_provider
    """

    def __init__(self):
        # 1. 加载 settings.json
        json_data = _load_settings_json()

        # 2. 加载 .env + 环境变量
        env_data = _load_env_file()

        # 3. 将 json_data 包装为 DotDict（支持点号访问）
        self._json_config = DotDict(json_data) if json_data else DotDict()

        # 4. 扁平化 json 配置为 UPPER_CASE 键
        self._flat = _flatten_dotdict(self._json_config) if json_data else {}

        # 5. 用环境变量覆盖（.env + os.environ）
        for key, value in env_data.items():
            if key.isupper():
                self._flat[key] = _coerce_value(value)

        # 6. 处理环境变量到 JSON 路径的覆盖
        for env_name, json_path in _ENV_TO_JSON_PATH.items():
            if env_name in env_data:
                # 更新 DotDict
                node = self._json_config
                for part in json_path[:-1]:
                    if part not in node:
                        node.__setattr__(part, DotDict())
                    node = getattr(node, part)
                node.__setattr__(json_path[-1], _coerce_value(env_data[env_name]))
                # 更新 flat
                flat_key = "_".join(p.upper() for p in json_path)
                self._flat[flat_key] = _coerce_value(env_data[env_name])

        # 7. 内置默认值（当 settings.json 和 .env 都没有提供时）
        self._apply_defaults()

    def _apply_defaults(self):
        """设置默认值（仅对不存在的键）"""
        defaults = {
            "APP_NAME": "AI Agent Framework",
            "APP_VERSION": "1.0.0",
            "DEBUG": False,
            "API_PREFIX": "/api/v1",
            "HOST": "0.0.0.0",
            "PORT": 8000,
            "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_framework",
            "DATABASE_POOL_SIZE": 20,
            "DATABASE_MAX_OVERFLOW": 10,
            "REDIS_URL": "redis://localhost:6379/0",
            "REDIS_POOL_SIZE": 10,
            "CELERY_BROKER_URL": "redis://localhost:6379/1",
            "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
            "DEFAULT_LLM_MODEL": "gpt-4-turbo-preview",
            "DEFAULT_TEMPERATURE": 0.7,
            "DEFAULT_MAX_TOKENS": 4096,
            "SECRET_KEY": "your-secret-key-change-in-production",
            "ALGORITHM": "HS256",
            "ACCESS_TOKEN_EXPIRE_MINUTES": 1440,
            "MAX_CONCURRENT_AGENTS": 100,
            "AGENT_TIMEOUT_SECONDS": 3600,
            "WORKSPACE_BASE_PATH": str(_HAPPYCLAW_HOME / "workspace" / "agents"),
            "WORKSPACE_ROOT": str(_HAPPYCLAW_HOME / "workspace"),
            "AGENT_DEFAULTS_SANDBOX_SCOPE": "session",
            "SHORT_TERM_MEMORY_SIZE": 50,
            "LONG_TERM_MEMORY_ENABLED": True,
            "VECTOR_DB_PATH": "./vector_db",
            "CONTEXT_WINDOW_SIZE": 128000,
            "COMPRESSION_THRESHOLD": 0.8,
            "COMPRESSION_STRATEGY": "summary",
            "COMPRESSION_BUDGET_MAX_CHARS": 200000,
            "COMPRESSION_BUDGET_PREVIEW_CHARS": 1000,
            "COMPRESSION_MICRO_COMPACT_TTL_SECONDS": 3600,
            "COMPRESSION_MICRO_COMPACT_KEEP_RECENT": 5,
            "COMPRESSION_MICRO_COMPACT_MULTIMODAL_TOKEN_THRESHOLD": 2000,
            "COMPRESSION_MICRO_COMPACT_WHITELIST": ["web_search", "read_file", "bash_command"],
            "COMPRESSION_AUTO_COMPACT_WARNING_THRESHOLD": 0.85,
            "COMPRESSION_AUTO_COMPACT_FORCE_THRESHOLD": 0.95,
            "COMPRESSION_AUTO_COMPACT_KEEP_RECENT": 5,
            "COMPRESSION_REACTIVE_COMPACT_KEEP_RECENT": 5,
            "COMPRESSION_REACTIVE_COMPACT_MAX_RETRIES": 3,
            "COMPRESSION_POST_RESTORE_MAX_FILES": 5,
            "COMPRESSION_POST_RESTORE_MAX_FILE_TOKENS": 5000,
            "COMPRESSION_POST_RESTORE_TOTAL_BUDGET_TOKENS": 50000,
            "SYSTEM_PROMPT_DYNAMIC_TTL": 1800,
            "TASKS_RETENTION_DAYS": 7,
            "TASKS_LOCK_MAX_RETRIES": 30,
            "TASKS_LOCK_BACKOFF_MIN_MS": 5,
            "TASKS_LOCK_BACKOFF_MAX_MS": 100,
            "TASKS_WATCHER_ENABLED": True,
            "TASKS_WATCHER_INTERVAL_SECONDS": 2,
            "MEMORY_ENABLED": True,
            "MEMORY_ROOT": str(_HAPPYCLAW_HOME / "memory"),
            "MEMORY_MODE": "standard",
            "MEMORY_LLM_PROVIDER": "",
            "MEMORY_LLM_MODEL": "",
            "MEMORY_PREFETCH_LLM_PROVIDER": "",
            "MEMORY_PREFETCH_LLM_MODEL": "",
            "MEMORY_SCAN_MAX_FILES": 200,
            "MEMORY_SELECT_MAX": 5,
            "MEMORY_PER_FILE_MAX_LINES": 200,
            "MEMORY_PER_FILE_MAX_BYTES": 4096,
            "MEMORY_SESSION_INJECT_BUDGET_BYTES": 61440,
            "MEMORY_INDEX_MAX_LINES": 200,
            "MEMORY_INDEX_MAX_BYTES": 25600,
            "MEMORY_FRESHNESS_WARN_DAYS": 1,
            "MEMORY_EXTRACT_MIN_INTERVAL_SECONDS": 300,
            "MEMORY_EXTRACT_MAX_ROUNDS": 5,
            "MEMORY_EXTRACT_MIN_CONTENT_CHARS": 200,
            "MEMORY_EXTRACT_MIN_USER_MESSAGES": 5,
            "MEMORY_CONSOLIDATE_MIN_INTERVAL_HOURS": 24,
            "MEMORY_CONSOLIDATE_MIN_NEW_CONVERSATIONS": 5,
            "MEMORY_CONSOLIDATE_LOCK_TTL_SECONDS": 3600,
            "MEMORY_LOCK_TTL_SECONDS": 600,
            "MEMORY_CONSOLIDATE_SCAN_THROTTLE_SECONDS": 600,
            "MEMORY_PREFETCH_COLLECT_TIMEOUT_SECONDS": 8,
            "BACKGROUND_JOBS_ENABLED": True,
            "BACKGROUND_JOBS_MAX_CONCURRENT": 5,
            "BACKGROUND_JOBS_CLEANUP_DAYS": 7,
            "BACKGROUND_JOBS_MCP_TOOL_TIMEOUT": 0,
            "MCP_TOOL_TO_BACKGROUND_TIMEOUT": 0,
            "MCP_ENABLED": True,
            "MCP_CALL_TIMEOUT_SECONDS": 30,
            "MCP_CONNECT_TIMEOUT_SECONDS": 10,
            "MCP_MAX_CONSECUTIVE_ERRORS": 3,
            "MCP_RECONNECT_DELAY_SECONDS": 5,
            "MCP_RECONNECT_BACKOFF_MAX_SECONDS": 60,
            "TOOL_SEARCH_ENABLED": False,
            "TOOL_SEARCH_DEFAULT_LIMIT": 5,
            "TOOL_SEARCH_MAX_LIMIT": 10,
            "MAX_SCHEDULE": 50,
            "SCHEDULER_ENABLED": True,
            "ENABLE_AGENT_TEAMS": True,
            "TEAMS_LEAD_IDLE_TIMEOUT_SECONDS": 60,
            "TEAMS_LEAD_IDLE_POLL_SECONDS": 5,
            "TEAMS_INBOX_POLL_SECONDS": 1,
            "TEAMS_PERMISSION_POLL_MS": 500,
            "TEAMS_PERMISSION_TIMEOUT_SECONDS": 300,
            "TEAMS_INBOX_LOCK_MAX_RETRIES": 10,
            "TEAMS_MAX_MEMBERS": 10,
            "TEAMS_SHUTDOWN_ACK_TIMEOUT_SECONDS": 120,
            "SCHEDULER_POLL_INTERVAL_SECONDS": 1,
            "SCHEDULED_TASKS_RETENTION_DAYS": 7,
            "SCHEDULE_JITTER_ENABLED": True,
            "SCHEDULE_JITTER_RECURRING_RATIO": 0.1,
            "SCHEDULE_JITTER_RECURRING_MAX_SECONDS": 900,
            "SCHEDULE_JITTER_ONESHOT_EDGE_SECONDS": 90,
            "MEMORY_SESSION_MEMORY_ENABLED": True,
            "MEMORY_SESSION_MEMORY_FIRST_UPDATE_TOKENS": 10000,
            "MEMORY_SESSION_MEMORY_INCREMENT_TOKENS": 5000,
            "MEMORY_SESSION_MEMORY_TOOL_CALLS_THRESHOLD": 3,
            "MEMORY_SESSION_MEMORY_NATURAL_PAUSE_TURNS": 2,
            "MEMORY_SESSION_MEMORY_SUMMARY_MIN_TOKENS": 10000,
            "MEMORY_SESSION_MEMORY_SUMMARY_MAX_TOKENS": 40000,
            "MEMORY_SESSION_MEMORY_SUMMARY_MIN_MESSAGES": 5,
            "LOG_LEVEL": "WARNING",
            "LOG_FORMAT": "console",
            "LOGGING_WRITE_FILE": False,
            "LOGGING_LEVEL": "info",
            "LOGGING_FILE": "",
            "LOGGING_DIR": "",
            "LOGGING_MAX_FILE_BYTES": 104857600,
            "LOGGING_MAX_RETENTION_DAYS": 7,
            "LOGGING_CONSOLE_LEVEL": "info",
            "LOGGING_CONSOLE_STYLE": "pretty",
            "LOGGING_REDACT_SENSITIVE": "tools",
            "LOGGING_REDACT_PATTERNS": [],
            "PROMETHEUS_ENABLED": True,
            "PROMETHEUS_PORT": 9090,
            "OPENAI_API_KEY": None,
            "OPENAI_BASE_URL": None,
            "ANTHROPIC_API_KEY": None,
        }
        for key, value in defaults.items():
            if key not in self._flat:
                self._flat[key] = value

        # settings.json 中的默认结构值
        json_defaults = {
            "agent": {
                "defaults": {
                    "sandbox": {
                        "scope": "session"
                    }
                },
                "subagent": {
                    "enabled": True,
                    "max_spawn_level": 2,
                    "max_concurrent": 5,
                    "disallowed_tools_per_level": {
                        "0": [],
                        "1": [],
                        "2": []
                    },
                    "allowed_tools_per_level": {
                        "0": ["web_search", "read_file", "write_file", "bash_command", "todo_write"],
                        "1": ["web_search", "read_file", "todo_write"],
                        "2": ["web_search", "read_file"]
                    },
                    "default_config": {
                        "llm_provider": "openai",
                        "llm_model": "gpt-4-turbo-preview",
                        "temperature": 0.7,
                        "max_tokens": 4096
                    }
                }
            }
        }
        # 递归合并默认值（不覆盖已有值）
        self._merge_defaults(json_defaults)

    def _merge_defaults(self, defaults: dict, target: DotDict = None):
        """递归合并默认值到 DotDict，不覆盖已有值"""
        if target is None:
            target = self._json_config
        for key, value in defaults.items():
            if isinstance(value, dict):
                if key not in target:
                    target[key] = DotDict(value)
                else:
                    existing = getattr(target, key)
                    if isinstance(existing, DotDict):
                        self._merge_defaults(value, existing)
                    else:
                        target[key] = DotDict(value)
                # 同步到 flat
                flat_key = key.upper()
                if flat_key not in self._flat or not isinstance(self._flat[flat_key], DotDict):
                    self._flat[flat_key] = target[key]
            else:
                if key not in target:
                    target[key] = value
                # 同步到 flat
                flat_key = key.upper()
                if flat_key not in self._flat:
                    self._flat[flat_key] = target[key]

    def __getattr__(self, name: str) -> Any:
        # 先查 flat 字典（大写键）
        _flat = object.__getattribute__(self, '__dict__').get('_flat', {})
        if name in _flat:
            return _flat[name]
        # 再查 json 配置（小写/原始键）
        _json = object.__getattribute__(self, '__dict__').get('_json_config')
        if _json and name in _json:
            return getattr(_json, name)
        raise AttributeError(f"配置项 '{name}' 不存在")

    def __setattr__(self, name: str, value: Any):
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        else:
            self._flat[name] = value

    def __repr__(self):
        return f"Settings(keys={list(self._flat.keys())})"

    def get(self, key: str, default: Any = None) -> Any:
        """带默认值的获取方法"""
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    def reload(self):
        """重新加载配置（用于热更新）"""
        self.__init__()

    def to_dict(self) -> dict:
        """返回原始 JSON 配置的纯 dict 副本（内存缓存，无需读文件）
        
        注意：返回的是标准 dict 类型，isinstance(x, dict) 为 True。
        所有通过 update_settings() 的写入都会自动刷新此缓存。
        """
        return self._json_config.to_dict()


# 全局配置实例
settings = Settings()
