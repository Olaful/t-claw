"""结构化日志配置，支持彩色控制台输出和文件日志"""
import logging
import logging.handlers
import sys
import os
import re
import json
import tempfile
import traceback as tb_module
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from typing import Optional, List
import structlog
from structlog.types import EventDict, WrappedLogger
from contextvars import ContextVar
from app.config import settings


# ── Agent 上下文变量 ──────────────────────────────────────────
# 用于在并发协程环境中追踪当前执行的 agent
_agent_context: ContextVar[Optional[str]] = ContextVar('agent_context', default=None)


def set_agent_context(agent_name: str, agent_id: str, parent_agent_name: Optional[str] = None) -> None:
    """设置当前协程的 agent 上下文
    
    格式: parent_agent_name:agent_name(id后八位) 或 agent_name(id后八位)
    """
    short_id = agent_id[-8:] if agent_id else "unknown"
    if parent_agent_name:
        _agent_context.set(f"{parent_agent_name}:{agent_name}({short_id})")
    else:
        _agent_context.set(f"{agent_name}({short_id})")


def clear_agent_context() -> None:
    """清除当前协程的 agent 上下文"""
    _agent_context.set(None)


def get_agent_context() -> Optional[str]:
    """获取当前协程的 agent 上下文"""
    return _agent_context.get()


# ── ANSI 颜色码 ──────────────────────────────────────────────
_RESET = "\033[0m"
_BOLD = "\033[1m"

# 日志级别 → 颜色映射
_LEVEL_COLORS = {
    "DEBUG": "\033[36m",     # 青色
    "INFO": "\033[32m",      # 绿色
    "WARNING": "\033[33m",   # 黄色
    "ERROR": "\033[31m",     # 红色
    "CRITICAL": "\033[35m",  # 紫色
}

# 日志级别显示宽度对齐（固定7字符宽）
_LEVEL_PAD = {
    "DEBUG": "DEBUG  ",
    "INFO": "INFO   ",
    "WARNING": "WARNING",
    "ERROR": "ERROR  ",
    "CRITICAL": "CRITICL",
}

_TIMESTAMP_COLOR = "\033[90m"   # 灰色
_LOCATION_COLOR = "\033[94m"    # 蓝色
_KEY_COLOR = "\033[35m"         # 紫色
_VALUE_COLOR = "\033[39m"       # 默认前景色
_EVENT_COLOR = "\033[97m"       # 亮白色


def _supports_color() -> bool:
    """检测终端是否支持 ANSI 颜色"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:
            return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_USE_COLOR = _supports_color()


def _c(text: str, color: str) -> str:
    """给文本包裹颜色，不支持颜色时原样返回"""
    if not _USE_COLOR:
        return text
    return f"{color}{text}{_RESET}"


# ── 位置处理器 ───────────────────────────────────────────────
def _add_location(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """从调用栈中提取日志打印位置（跳过 structlog 内部帧）"""
    import inspect

    frame = inspect.currentframe()
    try:
        caller = frame
        while caller:
            caller = caller.f_back
            if caller is None:
                break
            module = caller.f_globals.get("__name__", "")
            if not module.startswith("structlog") and module != __name__:
                event_dict["location"] = (
                    f"{caller.f_globals.get('__name__', '?').split('.')[-1]}"
                    f":{caller.f_lineno}"
                )
                break
    finally:
        del frame
    return event_dict


# ── Agent 上下文处理器 ───────────────────────────────────────
def _add_agent_context(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """将当前协程的 agent 上下文注入到日志事件中"""
    agent_ctx = _agent_context.get()
    if agent_ctx:
        event_dict["agent_name"] = agent_ctx
    return event_dict


# ── 彩色控制台渲染器 ─────────────────────────────────────────
_AGENT_NAME_COLOR = "\033[33m"  # 黄色

def _colorful_console_renderer(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> str:
    """
    输出格式：
        2025-08-11 14:30:00 [INFO   ] single:42 - [AgentName(abc12345)] Agent started  agent_id=abc
    各部分带独立颜色，日志级别按级别着色。
    """
    # 时间戳
    timestamp = event_dict.pop("timestamp", None)
    ts_str = _c(str(timestamp), _TIMESTAMP_COLOR) if timestamp else ""

    # 日志级别（带颜色 + 固定宽度）
    level = event_dict.pop("level", "INFO").upper()
    level_color = _LEVEL_COLORS.get(level, "")
    level_text = _LEVEL_PAD.get(level, level.ljust(7))
    level_str = _c(f"[{level_text}]", level_color + _BOLD)

    # 位置
    location = event_dict.pop("location", None)
    loc_str = _c(location, _LOCATION_COLOR) if location else ""

    # Agent 上下文（如果存在）
    agent_name = event_dict.pop("agent_name", None)
    agent_str = _c(f"[{agent_name}]", _AGENT_NAME_COLOR) if agent_name else ""

    # 主事件消息
    event = event_dict.pop("event", "")
    event_str = _c(str(event), _EVENT_COLOR + _BOLD)

    # 剩余键值对（排除内部字段）
    kv_parts = []
    for key, value in event_dict.items():
        if key.startswith("_") or key in ("exc_info", "stack"):
            continue
        k = _c(f"{key}=", _KEY_COLOR)
        v = _c(str(value), _VALUE_COLOR)
        kv_parts.append(f"{k}{v}")
    kv_str = " ".join(kv_parts)

    # 拼装
    segments = [ts_str, level_str]
    if loc_str:
        segments.append(loc_str + " -")
    if agent_str:
        segments.append(agent_str)
    segments.append(event_str)
    if kv_str:
        segments.append(" " + kv_str)

    line = " ".join(segments)

    # 异常堆栈追加（不应用颜色，避免多行文本转义码显示问题）
    exc_info_val = event_dict.get("exc_info")
    if exc_info_val:
        # exc_info=True 时，从当前调用栈获取异常信息
        if exc_info_val is True:
            exc_info_val = sys.exc_info()
        if isinstance(exc_info_val, tuple) and len(exc_info_val) == 3:
            # sys.exc_info() 返回的 (type, value, traceback) 元组
            exc_type, exc_value, exc_tb = exc_info_val
            if exc_tb:
                tb_lines = tb_module.format_exception(exc_type, exc_value, exc_tb)
                line += "\n" + "".join(tb_lines).rstrip()
            elif exc_type:
                line += "\n" + f"{exc_type.__name__}: {exc_value}"
        elif isinstance(exc_info_val, BaseException):
            line += "\n" + f"{type(exc_info_val).__name__}: {exc_info_val}"

    return line


# ── 日志配置解析 ─────────────────────────────────────────────

def _get_logging_config() -> dict:
    """从 settings 中提取 logging 配置，带默认值"""
    return {
        "write_file": getattr(settings, "LOGGING_WRITE_FILE", False),
        "level": getattr(settings, "LOGGING_LEVEL", "info").lower(),
        "file": getattr(settings, "LOGGING_FILE", ""),
        "dir": getattr(settings, "LOGGING_DIR", ""),
        "max_file_bytes": getattr(settings, "LOGGING_MAX_FILE_BYTES", 104857600),
        "max_retention_days": getattr(settings, "LOGGING_MAX_RETENTION_DAYS", 7),
        "console_level": getattr(settings, "LOGGING_CONSOLE_LEVEL", "info").lower(),
        "console_style": getattr(settings, "LOGGING_CONSOLE_STYLE", "pretty"),
        "redact_sensitive": getattr(settings, "LOGGING_REDACT_SENSITIVE", "tools"),
        "redact_patterns": getattr(settings, "LOGGING_REDACT_PATTERNS", []),
    }


def _resolve_log_dir() -> Path:
    """解析日志目录，确保以 happy-claw 结尾
    
    优先级: dir 配置 > 默认路径 (%TEMP%/happy-claw)
    """
    cfg = _get_logging_config()
    dir_cfg = cfg.get("dir", "").strip()
    
    if dir_cfg:
        log_dir = Path(dir_cfg)
    else:
        # 默认路径: %TEMP%/happy-claw
        temp_dir = Path(tempfile.gettempdir())
        log_dir = temp_dir / "happy-claw"
    
    # 确保目录名以 happy-claw 结尾
    if log_dir.name != "happy-claw":
        log_dir = log_dir / "happy-claw"
    
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _resolve_log_file() -> Path:
    """解析日志文件路径
    
    优先级: file 配置 > dir 配置(按天滚动) > 默认路径
    """
    cfg = _get_logging_config()
    file_cfg = cfg.get("file", "").strip()
    
    if file_cfg:
        # 直接指定文件路径
        log_file = Path(file_cfg)
        # 确保目录以 happy-claw 结尾
        if log_file.parent.name != "happy-claw":
            log_file = log_file.parent / "happy-claw" / log_file.name
        log_file.parent.mkdir(parents=True, exist_ok=True)
        return log_file
    else:
        # 按天滚动命名
        log_dir = _resolve_log_dir()
        today = datetime.now().strftime("%Y-%m-%d")
        return log_dir / f"happy-claw-{today}.log"


def _cleanup_old_logs(retention_days: int) -> None:
    """清理超过保留天数的日志文件"""
    if retention_days <= 0:
        return
    
    log_dir = _resolve_log_dir()
    cutoff = datetime.now() - timedelta(days=retention_days)
    
    try:
        for log_file in log_dir.glob("happy-claw-*.log"):
            try:
                # 从文件名提取日期
                date_str = log_file.stem.replace("happy-claw-", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    log_file.unlink()
            except (ValueError, OSError):
                continue
    except Exception:
        pass  # 清理失败不影响主流程


def _parse_level(level_str: str) -> int:
    """将字符串级别转换为 logging 常量"""
    level_map = {
        "trace": logging.DEBUG,  # trace 映射到 DEBUG
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }
    return level_map.get(level_str.lower(), logging.INFO)


class JSONLFormatter(logging.Formatter):
    """JSONL 格式器：每行一个 JSON 对象"""
    
    def __init__(self, redact_patterns: List[str] = None):
        super().__init__()
        self.redact_patterns = [
            re.compile(p) for p in (redact_patterns or [])
        ]
    
    def _redact(self, text: str) -> str:
        """对敏感信息进行脱敏"""
        for pattern in self.redact_patterns:
            text = pattern.sub("***REDACTED***", text)
        return text
    
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": self._redact(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # 添加异常信息
        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            if exc_tb:
                log_obj["exception"] = "".join(tb_module.format_exception(exc_type, exc_value, exc_tb))
            elif exc_type:
                log_obj["exception"] = f"{exc_type.__name__}: {exc_value}"
        
        # 添加额外字段
        if hasattr(record, "agent_name"):
            log_obj["agent_name"] = record.agent_name
        
        return json.dumps(log_obj, ensure_ascii=False)


# ── 全局 QueueListener 引用 ──────────────────────────────────
_queue_listener: Optional[logging.handlers.QueueListener] = None


def _setup_file_handler(root_logger: logging.Logger, log_level: int) -> None:
    """配置文件日志 handler（使用 QueueHandler + QueueListener）"""
    global _queue_listener
    
    cfg = _get_logging_config()
    if not cfg.get("write_file", False):
        return
    
    log_file = _resolve_log_file()
    max_bytes = cfg.get("max_file_bytes", 104857600)
    retention_days = cfg.get("max_retention_days", 7)
    redact_patterns = cfg.get("redact_patterns", [])
    
    # 使用 RotatingFileHandler 进行大小轮转
    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file),
        maxBytes=max_bytes,
        backupCount=5,  # 保留 5 个历史归档
        encoding="utf-8",
    )
    file_handler.setFormatter(JSONLFormatter(redact_patterns))
    file_handler.setLevel(log_level)
    
    # 使用 QueueHandler + QueueListener 实现异步写入
    log_queue: Queue = Queue(-1)  # 无限大小队列
    queue_handler = logging.handlers.QueueHandler(log_queue)
    queue_handler.setLevel(log_level)
    
    # QueueListener 在独立线程中消费队列
    _queue_listener = logging.handlers.QueueListener(
        log_queue,
        file_handler,
        respect_handler_level=True,
    )
    _queue_listener.start()
    
    root_logger.addHandler(queue_handler)
    
    # 清理旧日志文件
    _cleanup_old_logs(retention_days)


def shutdown_file_logging() -> None:
    """关闭文件日志（应用退出时调用）"""
    global _queue_listener
    if _queue_listener:
        _queue_listener.stop()
        _queue_listener = None


# ── 主配置入口 ───────────────────────────────────────────────
def setup_logging():
    """配置结构化日志（支持控制台 + 文件）"""
    cfg = _get_logging_config()
    
    is_console = settings.LOG_FORMAT == "console"
    console_level = _parse_level(cfg.get("console_level", "info"))
    file_level = _parse_level(cfg.get("level", "info"))
    
    # 构建 structlog 处理器链（用于控制台）
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _add_location,
        _add_agent_context,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
    ]
    
    if is_console and cfg.get("console_style", "pretty") == "pretty":
        processors.append(_colorful_console_renderer)
    else:
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.processors.JSONRenderer())
    
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(console_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    
    # 配置标准 logging
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(min(console_level, file_level))  # 取最低级别
    
    # 控制台 handler
    if is_console and cfg.get("console_style", "pretty") == "pretty":
        _setup_colorful_console_handler(root, console_level)
    else:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        console_handler.setLevel(console_level)
        root.addHandler(console_handler)
    
    # 文件 handler（异步写入）
    _setup_file_handler(root, file_level)
    
    # 降低第三方库日志级别
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _setup_colorful_console_handler(root: logging.Logger, level: int) -> None:
    """配置彩色控制台 handler"""
    
    class ColorfulFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            lvl = record.levelname
            color = _LEVEL_COLORS.get(lvl, "")
            level_padded = _LEVEL_PAD.get(lvl, lvl.ljust(7))
            
            ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
            name = record.name.split(".")[-1]
            location = f"{name}:{record.lineno}"
            
            parts = [
                _c(ts, _TIMESTAMP_COLOR),
                _c(f"[{level_padded}]", color + _BOLD),
                _c(location, _LOCATION_COLOR) + " -",
                _c(record.getMessage(), _EVENT_COLOR),
            ]
            return " ".join(parts)
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorfulFormatter())
    handler.setLevel(level)
    root.addHandler(handler)


def _setup_colorful_stdlib_logging():
    """配置标准 logging 模块使用彩色输出（向后兼容）"""
    root = logging.getLogger()
    _setup_colorful_console_handler(root, _parse_level(_get_logging_config().get("console_level", "info")))
