"""工具安全校验 - 危险命令检测（按操作系统路由）"""
import json
import re
import sys
import platform
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


def detect_platform() -> str:
    """
    检测当前操作系统平台
    
    Returns:
        平台标识: "windows", "linux", "macos", 或 "unknown"
    """
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    elif system == "linux":
        return "linux"
    elif system == "darwin":
        return "macos"
    else:
        return "unknown"


@dataclass
class PlatformSecurityConfig:
    """单个平台的安全配置"""
    dangerous_commands: List[str] = field(default_factory=list)
    blocked_patterns: List[str] = field(default_factory=list)


@dataclass
class SecurityConfig:
    """安全配置（包含多平台）"""
    enabled: bool = True
    current_platform: str = "unknown"
    platform_configs: Dict[str, PlatformSecurityConfig] = field(default_factory=dict)
    
    @classmethod
    def load_from_file(cls, config_path: str) -> "SecurityConfig":
        """从配置文件加载安全设置"""
        path = Path(config_path)
        
        if not path.exists():
            logger.warning("Security config file not found", path=str(path))
            return cls()
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
            return cls._parse_config(config_data)
        except Exception as e:
            logger.error("Failed to load security config", path=str(path), error=str(e), exc_info=True)
            return cls()
    
    @classmethod
    def load_from_settings(cls) -> "SecurityConfig":
        """从统一 settings 加载安全设置"""
        try:
            from app.config import settings
            security_data = settings.security
            # 将 DotDict 转为普通 dict
            if hasattr(security_data, 'to_dict'):
                config_data = {"security": security_data.to_dict()}
            else:
                config_data = {"security": security_data}
            return cls._parse_config(config_data)
        except Exception as e:
            logger.warning("Failed to load security config from settings, falling back to file", error=str(e), exc_info=True)
            # 回退到文件加载
            project_root = Path(__file__).resolve().parent.parent.parent.parent
            config_path = str(project_root / ".happy-claw" / "settings.json")
            return cls.load_from_file(config_path)
    
    @classmethod
    def _parse_config(cls, config_data: dict) -> "SecurityConfig":
        """解析配置数据"""
        security_config = config_data.get("security", {})
        bash_config = security_config.get("bash_command", {})
        
        enabled = bash_config.get("enabled", True)
        platforms_data = bash_config.get("platforms", {})
        
        # 解析各平台配置
        platform_configs = {}
        for platform_name, platform_data in platforms_data.items():
            platform_configs[platform_name] = PlatformSecurityConfig(
                dangerous_commands=platform_data.get("dangerous_commands", []),
                blocked_patterns=platform_data.get("blocked_patterns", []),
            )
        
        # 兼容旧版配置格式（无 platforms 字段，直接有 dangerous_commands）
        if not platforms_data and "dangerous_commands" in bash_config:
            platform_configs["all"] = PlatformSecurityConfig(
                dangerous_commands=bash_config.get("dangerous_commands", []),
                blocked_patterns=bash_config.get("blocked_patterns", []),
            )
        
        current_platform = detect_platform()
        
        logger.info(
            "Security config loaded",
            current_platform=current_platform,
            available_platforms=list(platform_configs.keys()),
        )
        
        return cls(
            enabled=enabled,
            current_platform=current_platform,
            platform_configs=platform_configs,
        )
    
    def get_active_config(self) -> PlatformSecurityConfig:
        """
        获取当前平台的安全配置
        
        优先级：当前平台 > "all" > 空配置
        """
        # 1. 精确匹配当前平台
        if self.current_platform in self.platform_configs:
            return self.platform_configs[self.current_platform]
        
        # 2. macOS 回退到 linux（类 Unix 系统）
        if self.current_platform == "macos" and "linux" in self.platform_configs:
            return self.platform_configs["linux"]
        
        # 3. 使用 "all" 通用配置
        if "all" in self.platform_configs:
            return self.platform_configs["all"]
        
        # 4. 无配置
        return PlatformSecurityConfig()


class CommandSecurityChecker:
    """命令安全检查器（按操作系统路由）"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化安全检查器
        
        Args:
            config_path: 配置文件路径，默认从统一 settings 加载
        """
        if config_path is not None:
            self.config = SecurityConfig.load_from_file(config_path)
        else:
            self.config = SecurityConfig.load_from_settings()
        self._active_config = self.config.get_active_config()
        self._compiled_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in self._active_config.blocked_patterns
        ]
        
        logger.info(
            "CommandSecurityChecker initialized",
            current_platform=self.config.current_platform,
            dangerous_commands_count=len(self._active_config.dangerous_commands),
            blocked_patterns_count=len(self._active_config.blocked_patterns),
        )
    
    def check_command(self, command: str) -> Dict[str, Any]:
        """
        检查命令是否安全（仅使用当前平台的规则）
        
        Args:
            command: 要检查的命令
            
        Returns:
            检查结果：{"is_safe": bool, "reason": str, "matched_pattern": str, "match_type": str}
        """
        # 如果安全检查被禁用，直接返回安全
        if not self.config.enabled:
            return {
                "is_safe": True,
                "reason": "Security checks are disabled",
                "matched_pattern": None,
                "match_type": None,
            }
        
        command_lower = command.lower().strip()
        
        # 1. 检查精确匹配的危险命令
        for dangerous_cmd in self._active_config.dangerous_commands:
            if dangerous_cmd.lower() in command_lower:
                return {
                    "is_safe": False,
                    "reason": f"Dangerous command detected: '{dangerous_cmd}'",
                    "matched_pattern": dangerous_cmd,
                    "match_type": "exact",
                }
        
        # 2. 检查正则表达式模式
        for i, pattern in enumerate(self._compiled_patterns):
            if pattern.search(command):
                return {
                    "is_safe": False,
                    "reason": f"Blocked pattern matched: '{self._active_config.blocked_patterns[i]}'",
                    "matched_pattern": self._active_config.blocked_patterns[i],
                    "match_type": "regex",
                }
        
        # 3. 额外安全检查：检测 fork bomb（仅 Linux/macOS）
        if self.config.current_platform in ("linux", "macos", "unknown"):
            if ":(){ :|:& };" in command or ":(){:|:&};" in command.replace(" ", ""):
                return {
                    "is_safe": False,
                    "reason": "Fork bomb pattern detected",
                    "matched_pattern": ":(){ :|:& };",
                    "match_type": "special",
                }
        
        # 4. 检测重定向到设备文件（Linux 风格）
        if re.search(r'>\s*/dev/[a-z]+', command, re.IGNORECASE):
            return {
                "is_safe": False,
                "reason": "Redirecting to device file detected",
                "matched_pattern": "> /dev/...",
                "match_type": "special",
            }
        
        # 5. 检测重定向到 Windows 设备文件
        if self.config.current_platform == "windows":
            if re.search(r'>\s*\\\\\.\\(PhysicalDrive|[A-Z]:)', command, re.IGNORECASE):
                return {
                    "is_safe": False,
                    "reason": "Redirecting to Windows device file detected",
                    "matched_pattern": "> \\\\.\\Device",
                    "match_type": "special",
                }
        
        return {
            "is_safe": True,
            "reason": "Command passed security checks",
            "matched_pattern": None,
            "match_type": None,
        }
    
    def reload_config(self, config_path: Optional[str] = None) -> bool:
        """重新加载配置"""
        if config_path is None:
            project_root = Path(__file__).resolve().parent.parent.parent.parent
            config_path = str(project_root / ".happy-claw" / "settings.json")
        
        try:
            self.config = SecurityConfig.load_from_file(config_path)
            self._active_config = self.config.get_active_config()
            self._compiled_patterns = [
                re.compile(pattern, re.IGNORECASE)
                for pattern in self._active_config.blocked_patterns
            ]
            logger.info(
                "Security config reloaded",
                current_platform=self.config.current_platform,
                dangerous_commands_count=len(self._active_config.dangerous_commands),
            )
            return True
        except Exception as e:
            logger.error("Failed to reload security config", error=str(e), exc_info=True)
            return False


# 全局安全检查器实例
command_security_checker = CommandSecurityChecker()
