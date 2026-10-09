"""Skills加载系统 - 基于 SKILL.md 的分级加载与注册"""
import os
import re
import shutil
import platform
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


# ===== SKILL.md Frontmatter 解析 =====

def _parse_frontmatter(content: str) -> tuple:
    """
    解析 SKILL.md 文件中的 YAML Frontmatter。
    
    Returns:
        (metadata_dict, body_str) 元组
        - metadata_dict: Frontmatter 中的 YAML 数据（dict）
        - body_str: Frontmatter 之后的 Markdown 正文内容
    """
    # 匹配 ---\n...\n--- 格式的 Frontmatter
    pattern = r'^---\s*\n(.*?)\n---\s*\n?(.*)'
    match = re.match(pattern, content, re.DOTALL)
    
    if not match:
        return {}, content
    
    yaml_str = match.group(1)
    body = match.group(2)
    
    try:
        import yaml
        metadata = yaml.safe_load(yaml_str) or {}
    except ImportError:
        logger.warning("PyYAML not installed, falling back to simple parser")
        metadata = _simple_yaml_parse(yaml_str)
    except Exception as e:
        logger.error("Failed to parse SKILL.md frontmatter", error=str(e), exc_info=True)
        metadata = {}
    
    return metadata, body


def _simple_yaml_parse(yaml_str: str) -> dict:
    """简易 YAML 解析器（仅支持基本 key: value 格式，作为 PyYAML 的 fallback）"""
    result = {}
    for line in yaml_str.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip()
            # 尝试转换类型
            if value.lower() == 'true':
                result[key] = True
            elif value.lower() == 'false':
                result[key] = False
            elif value.isdigit():
                result[key] = int(value)
            else:
                result[key] = value
    return result


# ===== 环境门控检查 =====

def _get_current_os() -> str:
    """获取当前操作系统标识（与 SKILL.md 中 os 字段对应）"""
    system = platform.system().lower()
    mapping = {"windows": "win", "linux": "linux", "darwin": "mac"}
    return mapping.get(system, system)


def _check_config_exists(config_key: str) -> bool:
    """
    检查 settings.json 中是否存在指定配置项（支持点号路径）。
    例如: "security.bash_command" -> settings.security.bash_command
    """
    try:
        from app.config import settings
        parts = config_key.split(".")
        obj = settings
        for part in parts:
            if hasattr(obj, part):
                obj = getattr(obj, part)
            elif isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                return False
        return True
    except Exception:
        return False


def _check_requirements(happy_claw_meta: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    检查技能的所有依赖门控条件。
    
    Args:
        happy_claw_meta: metadata.happy-claw 下的配置字典
    
    Returns:
        (all_passed, failed_reasons) 元组
        - all_passed: 所有条件是否全部通过
        - failed_reasons: 未通过的原因列表
    """
    if not happy_claw_meta:
        return True, []
    
    failed = []
    requires = happy_claw_meta.get("requires", {})
    
    # 1. 检查 bins：系统 PATH 中是否存在指定命令
    bins = requires.get("bins", [])
    for bin_name in bins:
        if not shutil.which(bin_name):
            failed.append(f"binary '{bin_name}' not found in PATH")
    
    # 2. 检查 env：是否存在指定环境变量
    env_vars = requires.get("env", [])
    for env_name in env_vars:
        if not os.environ.get(env_name):
            failed.append(f"environment variable '{env_name}' not set")
    
    # 3. 检查 config：settings.json 中是否存在指定配置
    configs = requires.get("config", [])
    for config_key in configs:
        if not _check_config_exists(config_key):
            failed.append(f"config '{config_key}' not found in settings.json")
    
    # 4. 检查 os：当前平台是否在允许列表中
    allowed_os = happy_claw_meta.get("os", [])
    if allowed_os:
        current_os = _get_current_os()
        # 标准化为小写比较
        allowed_normalized = [o.lower() for o in allowed_os]
        if current_os not in allowed_normalized:
            failed.append(f"OS '{current_os}' not in allowed list {allowed_os}")
    
    return (len(failed) == 0), failed


# ===== Skill 数据模型 =====

@dataclass
class SkillEntry:
    """技能条目"""
    name: str                          # 技能名称（唯一标识）
    description: str                   # 技能描述
    version: str = "1.0.0"             # 版本
    enabled: bool = True               # 是否启用
    context: str = "inline"            # 上下文模式: "inline"(load_skill加载到当前上下文) 或 "fork"(spawn_agent作为子agent运行)
    parameters: Dict[str, Any] = field(default_factory=dict)  # 参数定义
    source_dir: str = ""               # 技能目录路径
    source_priority: int = 0           # 来源优先级（数字越大优先级越高）
    metadata: Dict[str, Any] = field(default_factory=dict)    # 完整 frontmatter 元数据
    body: str = ""                     # SKILL.md 正文内容（Frontmatter 之后）
    gate_reasons: List[str] = field(default_factory=list)     # 门控未通过的原因


# ===== 目录优先级定义 =====

# 优先级：数字越大越优先（高优先级同名技能覆盖低优先级）
def _get_skill_dirs():
    """返回技能目录列表：(目录绝对路径, 优先级)"""
    from app.config import _PROJECT_ROOT, _HAPPYCLAW_HOME
    return [
        (str(_PROJECT_ROOT / "backend" / "app" / "skills" / "builtin"), 1),  # 内置技能（最低优先级）
        (str(_HAPPYCLAW_HOME / "plugin-skills"), 2),                           # 插件技能（中等优先级）
        (str(_HAPPYCLAW_HOME / "workspace" / "skills"), 3),                    # 工作区技能（最高优先级）
    ]


# ===== 全局禁用与模式匹配 =====

def _match_skill_pattern(pattern: str, skill_name: str) -> bool:
    """判断技能名是否匹配给定模式。

    - `*` 匹配所有技能
    - 精确相等优先
    - 其余作为正则全匹配（非法正则时仅精确相等）
    """
    if pattern == "*":
        return True
    if pattern == skill_name:
        return True
    try:
        return bool(re.fullmatch(pattern, skill_name))
    except re.error:
        return False


def get_global_disallowed_skills() -> List[str]:
    """读取全局 skills.disallowed 配置列表（所有 agent 都不能使用）。"""
    try:
        from app.config import settings
        skills_cfg = settings._json_config.get("skills", {})
        if hasattr(skills_cfg, "get"):
            disallowed = skills_cfg.get("disallowed", [])
        else:
            disallowed = []
        return list(disallowed) if disallowed else []
    except Exception:
        return []


def is_skill_globally_disabled(skill_name: str, patterns: Optional[List[str]] = None) -> bool:
    """判断技能是否被全局 skills.disallowed 命中（禁用）。"""
    if patterns is None:
        patterns = get_global_disallowed_skills()
    for pattern in patterns:
        if _match_skill_pattern(pattern, skill_name):
            return True
    return False


def get_skill_type(source_priority: int) -> str:
    """根据来源优先级映射技能类型：1=内置, 2=插件, 3=工作区。"""
    return {1: "builtin", 2: "plugin", 3: "workspace"}.get(source_priority, "builtin")


# ===== SkillLoader =====

class SkillLoader:
    """
    基于 SKILL.md 的技能加载器。
    
    扫描三个目录（按优先级从低到高）：
    1. backend/app/skills/builtin  - 内置技能（优先级 1，最低）
    2. .happy-claw/plugin-skills   - 插件技能（优先级 2）
    3. .happy-claw/workspace/skills - 工作区技能（优先级 3，最高）
    
    同名技能高优先级覆盖低优先级。
    """
    
    def __init__(self):
        self._skills: Dict[str, SkillEntry] = {}
        self._loaded: bool = False
    
    def scan_and_load(self) -> List[SkillEntry]:
        """
        扫描所有技能目录并加载技能。
        按优先级从低到高排序（先加载低优先级，后加载高优先级以覆盖）。
        
        Returns:
            加载的技能列表
        """
        # 按优先级从低到高排序
        sorted_dirs = sorted(_get_skill_dirs(), key=lambda x: x[1])
        
        for dir_path_str, priority in sorted_dirs:
            dir_path = Path(dir_path_str)
            if not dir_path.is_dir():
                logger.debug("Skill directory not found, skipping", path=str(dir_path))
                continue
            
            self._scan_directory(dir_path, priority)
        
        self._loaded = True

        # 应用全局 skills.disallowed（所有 agent 都不能使用）
        self._apply_global_disallowed()

        enabled_count = sum(1 for s in self._skills.values() if s.enabled)
        logger.info(
            "Skills scan completed",
            total_skills=len(self._skills),
            enabled=enabled_count,
        )
        
        return list(self._skills.values())

    def _apply_global_disallowed(self):
        """将全局 skills.disallowed 命中的技能置为 enabled=False（覆盖所有消费路径）。"""
        patterns = get_global_disallowed_skills()
        if not patterns:
            return
        disabled = 0
        for entry in self._skills.values():
            if any(_match_skill_pattern(p, entry.name) for p in patterns):
                if entry.enabled:
                    entry.enabled = False
                    entry.gate_reasons.append("disabled by global skills.disallowed config")
                disabled += 1
        if disabled:
            logger.info("Applied global skills.disallowed", disabled=disabled, patterns=patterns)
    
    def _scan_directory(self, directory: Path, priority: int):
        """扫描单个目录下的所有子目录，寻找 SKILL.md 文件"""
        try:
            for item in directory.iterdir():
                if not item.is_dir():
                    continue
                
                skill_md = item / "SKILL.md"
                if not skill_md.is_file():
                    continue
                
                try:
                    entry = self._load_skill_md(skill_md, item, priority)
                    if entry:
                        # 检查是否覆盖已有技能
                        if entry.name in self._skills:
                            existing = self._skills[entry.name]
                            logger.info(
                                "Skill overridden by higher priority",
                                name=entry.name,
                                old_priority=existing.source_priority,
                                new_priority=entry.source_priority,
                                old_dir=str(existing.source_dir),
                                new_dir=str(entry.source_dir),
                            )
                        self._skills[entry.name] = entry
                        logger.debug(
                            "Skill loaded",
                            name=entry.name,
                            priority=priority,
                            source=str(item),
                        )
                except Exception as e:
                    logger.error(
                        "Failed to load skill",
                        skill_dir=str(item),
                        error=str(e),
                        exc_info=True,
                    )
        except OSError as e:
            logger.warning("Cannot read skill directory", directory=str(directory), error=str(e))
    
    def _load_skill_md(self, skill_md_path: Path, skill_dir: Path, priority: int) -> Optional[SkillEntry]:
        """
        加载并解析单个 SKILL.md 文件。
        
        Returns:
            SkillEntry 或 None（如果解析失败或技能被禁用）
        """
        content = skill_md_path.read_text(encoding="utf-8")
        metadata, body = _parse_frontmatter(content)
        
        # 提取必要字段
        name = metadata.get("name", "").strip()
        if not name:
            logger.warning("SKILL.md missing 'name' field", path=str(skill_md_path))
            return None
        
        description = metadata.get("description", "").strip()
        version = str(metadata.get("version", "1.0.0"))
        enabled = metadata.get("enabled", True)
        parameters = metadata.get("parameters", {})
        
        # 提取 happy-claw 门控元数据并进行环境检查
        happy_claw_meta = metadata.get("metadata", {}).get("happy-claw", {})
        gate_passed, gate_reasons = _check_requirements(happy_claw_meta)
        
        # enabled 由两个因素决定：frontmatter 中显式声明 + 门控检查
        base_enabled = bool(metadata.get("enabled", True))
        enabled = base_enabled and gate_passed
        
        if base_enabled and not gate_passed:
            logger.info(
                "Skill disabled by gating",
                name=name,
                reasons=gate_reasons,
            )
        
        # 提取 context 字段（默认 inline）
        context = happy_claw_meta.get("context", "inline")
        if context not in ("inline", "fork"):
            logger.warning("Invalid context value, defaulting to 'inline'", name=name, context=context)
            context = "inline"
        
        return SkillEntry(
            name=name,
            description=description,
            version=version,
            enabled=enabled,
            context=context,
            parameters=parameters if isinstance(parameters, dict) else {},
            source_dir=str(skill_dir),
            source_priority=priority,
            metadata=metadata,
            body=body.strip(),
            gate_reasons=gate_reasons,
        )
    
    # ===== 公共 API =====
    
    def get_skill(self, name: str) -> Optional[SkillEntry]:
        """获取技能条目"""
        if not self._loaded:
            self.scan_and_load()
        return self._skills.get(name)
    
    def get_skill_body(self, name: str, args: Optional[Dict[str, Any]] = None) -> str:
        """
        获取技能的主体内容（SKILL.md Frontmatter 之后的部分）。
        
        Args:
            name: 技能名称
            args: 可选参数，会注入到正文中供 LLM 参考
        
        Returns:
            技能主体 Markdown 内容（顶部包含技能目录路径，供解析相对资源路径）
        
        Raises:
            ValueError: 技能不存在或未启用
        """
        if not self._loaded:
            self.scan_and_load()
        
        entry = self._skills.get(name)
        if not entry:
            raise ValueError(f"Skill not found: '{name}'. Available skills: {', '.join(self.list_enabled_skill_names()) or 'none'}")
        
        if not entry.enabled:
            raise ValueError(f"Skill '{name}' is disabled")
        
        body = entry.body
        
        # 在正文顶部注入技能目录绝对路径，使 Agent 能解析 SKILL.md 中的相对资源路径
        dir_hint = (
            f"> **Skill Directory**: `{entry.source_dir}`\n"
            f"> All relative paths in this skill refer to this directory.\n\n"
        )
        body = dir_hint + body
        
        # 如果有 args，在正文后添加参数信息
        if args:
            import json
            args_section = f"\n\n## Provided Args\n\n```json\n{json.dumps(args, indent=2, ensure_ascii=False)}\n```\n"
            body = body + args_section
        
        return body
    
    def list_enabled_skill_names(self) -> List[str]:
        """列出所有已启用的技能名称"""
        if not self._loaded:
            self.scan_and_load()
        return [name for name, entry in self._skills.items() if entry.enabled]
    
    def list_skills(self) -> List[SkillEntry]:
        """列出所有技能（包括禁用的）"""
        if not self._loaded:
            self.scan_and_load()
        return list(self._skills.values())
    
    def build_skills_prompt_section(self, exclude_names: Optional[List[str]] = None) -> str:
        """
        构建注入 system_prompt 的技能列表段落。
        只包含已启用技能的 name 和 description，排除 exclude_names 中的技能。
        按 context 分组展示：inline 技能用 load_skill 加载，fork 技能用 spawn_agent 指派。
        
        Args:
            exclude_names: 需要排除的技能名称列表（可选）
        
        Returns:
            格式化的技能列表字符串，若无技能则返回空字符串
        """
        if not self._loaded:
            self.scan_and_load()
        
        # 通配符 "*" 表示排除所有技能（forked agent 等后台场景使用）
        if exclude_names and "*" in exclude_names:
            return ""
        
        exclude_set = set(exclude_names) if exclude_names else set()
        enabled_skills = [
            entry for entry in self._skills.values()
            if entry.enabled and not any(_match_skill_pattern(p, entry.name) for p in exclude_set)
        ]
        
        if not enabled_skills:
            return ""
        
        # 按 context 分组
        inline_skills = [s for s in enabled_skills if s.context == "inline"]
        fork_skills = [s for s in enabled_skills if s.context == "fork"]
        
        lines = ["\nYou have access to the following skills:"]
        
        if inline_skills:
            lines.append("\n## Inline Skills (use `load_skill` tool to load full instructions)")
            for skill in inline_skills:
                lines.append(f"- {skill.name}: {skill.description}")
        
        if fork_skills:
            lines.append("\n## Fork Skills (use `spawn_agent` tool with `agent_type: \"skill\"` to run as subagent)")
            for skill in fork_skills:
                lines.append(f"- {skill.name}: {skill.description}")
        
        return "\n".join(lines)
    
    def reload(self) -> List[SkillEntry]:
        """重新加载所有技能（热重载）"""
        self._skills.clear()
        self._loaded = False
        return self.scan_and_load()


# ===== 全局单例 =====

skill_loader = SkillLoader()


def compute_agent_deny_skills(agent_name: str) -> List[str]:
    """计算某主 agent 的技能拒绝列表（deny_skills）。

    读取 agent.entries.{agent_name}.skills.disallowed/allowed：
    - disallowed：直接加入拒绝列表（支持模式）；
    - allowed 非空：仅保留白名单内技能，其余已启用技能全部加入拒绝列表。

    全局 skills.disallowed 已在 loader 层置 enabled=False，无需在此重复。
    """
    if not agent_name:
        return []
    try:
        from app.config import settings
        agent_cfg = settings._json_config.get("agent", {})
        entries = agent_cfg.get("entries", {}) if hasattr(agent_cfg, "get") else {}
        entry = entries.get(agent_name, {}) if hasattr(entries, "get") else {}
        skills_cfg = entry.get("skills", {}) if hasattr(entry, "get") else {}
        disallowed = list(skills_cfg.get("disallowed", []) or []) if hasattr(skills_cfg, "get") else []
        allowed = list(skills_cfg.get("allowed", []) or []) if hasattr(skills_cfg, "get") else []
    except Exception as e:
        logger.warning("Failed to compute agent deny skills", agent_name=agent_name, error=str(e))
        return []

    deny: List[str] = list(disallowed)
    # allowed 非空且不含通配符：拒绝白名单之外的所有已启用技能
    if allowed and "*" not in allowed:
        allowed_set = set(allowed)
        for sk in skill_loader.list_skills():
            if sk.enabled and sk.name not in allowed_set and sk.name not in deny:
                deny.append(sk.name)
    return deny
