"""扩展工具加载器 - 从 .happy-claw/extensions/ 目录加载自定义扩展工具"""
import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Optional, List

import structlog

from app.tools.registry import tool_registry, ToolPermissionEnum

logger = structlog.get_logger()

# 内置工具名称集合（扩展工具不能覆盖）
_BUILTIN_TOOL_NAMES = {
    "web_search",
    "bash_command",
    "read_file",
    "write_file",
    "todo_write",
    "spawn_agent",
}

# 已加载的扩展工具信息
_loaded_extensions: Dict[str, Dict[str, Any]] = {}


def _get_extensions_dir() -> Path:
    """获取扩展目录路径"""
    from app.config import _HAPPYCLAW_HOME
    ext_dir = _HAPPYCLAW_HOME / "extensions"
    if not ext_dir.exists():
        ext_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Created extensions directory", path=str(ext_dir))
    return ext_dir


def _get_tools_allow_list() -> List[str]:
    """从 settings.json 获取 tools.allow 白名单"""
    try:
        from app.config import settings
        allow = settings._json_config.get("tools", {})
        if hasattr(allow, "get"):
            allow = allow.get("allow", [])
        else:
            allow = []
        return allow if isinstance(allow, list) else []
    except Exception:
        return []


def _load_tool_definition(ext_dir: Path) -> Optional[Dict[str, Any]]:
    """加载扩展工具的 tool.json 定义文件"""
    tool_json = ext_dir / "tool.json"
    if not tool_json.exists():
        logger.warning("Extension missing tool.json, skipped", extension=ext_dir.name)
        return None

    try:
        with open(tool_json, "r", encoding="utf-8") as f:
            definition = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to parse tool.json", extension=ext_dir.name, error=str(e), exc_info=True)
        return None

    # 验证必填字段
    required_fields = ["name", "description", "parameters", "entry_point"]
    for field in required_fields:
        if field not in definition:
            logger.error(
                "tool.json missing required field",
                extension=ext_dir.name,
                field=field,
            )
            return None

    return definition


def _install_requirements(ext_dir: Path) -> bool:
    """安装扩展工具的 requirements.txt 依赖"""
    req_file = ext_dir / "requirements.txt"
    if not req_file.exists():
        return True

    try:
        logger.info("Installing extension dependencies", extension=ext_dir.name)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error(
                "Failed to install extension dependencies",
                extension=ext_dir.name,
                stderr=result.stderr[:500],
            )
            return False
        logger.info("Extension dependencies installed", extension=ext_dir.name)
        return True
    except subprocess.TimeoutExpired:
        logger.error("Dependency installation timed out", extension=ext_dir.name)
        return False
    except Exception as e:
        logger.error("Dependency installation failed", extension=ext_dir.name, error=str(e), exc_info=True)
        return False


def _import_handler(ext_dir: Path, entry_point: str):
    """
    从扩展目录动态导入处理函数。

    entry_point 格式: "module:function" 或 "module.submodule:function"
    例如: "my_tool:run" 表示从 my_tool.py 导入 run 函数
    """
    if ":" not in entry_point:
        logger.error(
            "Invalid entry_point format (expected 'module:function')",
            extension=ext_dir.name,
            entry_point=entry_point,
        )
        return None

    module_name, func_name = entry_point.rsplit(":", 1)

    # 将扩展目录加入 sys.path（临时），以便导入模块
    ext_dir_str = str(ext_dir)
    added_to_path = False
    if ext_dir_str not in sys.path:
        sys.path.insert(0, ext_dir_str)
        added_to_path = True

    try:
        # 构建完整模块名（避免命名冲突，使用扩展目录名作为前缀）
        full_module_name = f"happy_claw_ext_{ext_dir.name}.{module_name}"

        # 先尝试从文件加载
        module_file = ext_dir / f"{module_name.replace('.', '/')}.py"
        if not module_file.exists():
            # 可能是包（目录形式）
            module_file = ext_dir / module_name.replace(".", "/") / "__init__.py"
            if not module_file.exists():
                logger.error(
                    "Entry point module file not found",
                    extension=ext_dir.name,
                    expected_path=str(module_file),
                )
                return None

        spec = importlib.util.spec_from_file_location(full_module_name, str(module_file))
        if spec is None:
            logger.error("Cannot create module spec", extension=ext_dir.name, module=module_name)
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[full_module_name] = module
        spec.loader.exec_module(module)

        # 获取处理函数
        handler = getattr(module, func_name, None)
        if handler is None:
            logger.error(
                "Handler function not found in module",
                extension=ext_dir.name,
                module=module_name,
                function=func_name,
            )
            return None

        return handler

    except Exception as e:
        logger.error(
            "Failed to import extension handler",
            extension=ext_dir.name,
            entry_point=entry_point,
            error=str(e),
        )
        return None
    finally:
        # 清理 sys.path
        if added_to_path and ext_dir_str in sys.path:
            sys.path.remove(ext_dir_str)


def _register_extension_tool(
    ext_dir: Path,
    definition: Dict[str, Any],
    handler,
) -> bool:
    """将扩展工具注册到 tool_registry"""
    tool_name = definition["name"]
    timeout = definition.get("timeout_seconds", 30)
    required_role = definition.get("required_role", "public")

    # 映射权限字符串到枚举
    role_map = {
        "public": ToolPermissionEnum.PUBLIC,
        "authenticated": ToolPermissionEnum.AUTHENTICATED,
        "admin": ToolPermissionEnum.ADMIN,
    }
    perm = role_map.get(required_role, ToolPermissionEnum.PUBLIC)

    try:
        tool_registry.register(
            name=tool_name,
            description=definition["description"],
            handler=handler,
            parameters=definition["parameters"],
            required_role=perm,
            timeout_seconds=timeout,
            metadata={
                "source": "extension",
                "extension_dir": ext_dir.name,
                "extension_path": str(ext_dir),
            },
        )
        _loaded_extensions[tool_name] = {
            "name": tool_name,
            "description": definition["description"],
            "extension_dir": ext_dir.name,
            "optional": definition.get("optional", False),
            "entry_point": definition.get("entry_point", ""),
        }
        return True
    except Exception as e:
        logger.error("Failed to register extension tool", extension=ext_dir.name, error=str(e), exc_info=True)
        return False


def load_extensions():
    """
    扫描 .happy-claw/extensions/ 目录，加载并注册所有扩展工具。

    加载规则：
    1. 每个子目录视为一个扩展工具
    2. 子目录内必须有 tool.json 定义文件
    3. optional=false 的工具自动注册
    4. optional=true 的工具需在 settings.json 的 tools.allow 白名单中才注册
    5. 与内置工具同名时跳过（内置工具优先）
    """
    ext_dir = _get_extensions_dir()
    if not ext_dir.exists():
        logger.debug("No extensions directory found", path=str(ext_dir))
        return

    allow_list = _get_tools_allow_list()
    subdirs = sorted([d for d in ext_dir.iterdir() if d.is_dir()])

    if not subdirs:
        logger.debug("No extensions found", path=str(ext_dir))
        return

    loaded_count = 0
    skipped_count = 0

    for sub in subdirs:
        tool_name_hint = sub.name

        # 1. 加载 tool.json
        definition = _load_tool_definition(sub)
        if definition is None:
            skipped_count += 1
            continue

        tool_name = definition["name"]
        is_optional = definition.get("optional", False)

        # 2. 检查与内置工具是否冲突
        if tool_name in _BUILTIN_TOOL_NAMES:
            logger.warning(
                "Extension conflicts with built-in tool, skipped (built-in takes priority)",
                extension=tool_name_hint,
                tool_name=tool_name,
            )
            skipped_count += 1
            continue

        # 3. 检查是否已注册（避免重复）
        if tool_name in _loaded_extensions:
            logger.warning("Extension already loaded, skipped", tool_name=tool_name)
            skipped_count += 1
            continue

        # 4. optional=true 的工具需要白名单
        if is_optional and tool_name not in allow_list:
            logger.info(
                "Optional extension not in tools.allow whitelist, skipped",
                extension=tool_name_hint,
                tool_name=tool_name,
                hint="Add it to settings.json -> tools.allow to enable",
            )
            skipped_count += 1
            continue

        # 5. 安装依赖
        if not _install_requirements(sub):
            logger.error("Extension dependency installation failed, skipped", extension=tool_name_hint)
            skipped_count += 1
            continue

        # 6. 导入处理函数
        entry_point = definition["entry_point"]
        handler = _import_handler(sub, entry_point)
        if handler is None:
            skipped_count += 1
            continue

        # 7. 注册
        if _register_extension_tool(sub, definition, handler):
            loaded_count += 1
            logger.info(
                "Extension tool loaded",
                tool_name=tool_name,
                extension_dir=tool_name_hint,
                optional=is_optional,
            )
        else:
            skipped_count += 1

    logger.info(
        "Extension loading complete",
        loaded=loaded_count,
        skipped=skipped_count,
        total=len(subdirs),
    )


def get_loaded_extensions() -> Dict[str, Dict[str, Any]]:
    """获取已加载的扩展工具信息"""
    return _loaded_extensions.copy()
