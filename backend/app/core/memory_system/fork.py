"""Forked Agent 工厂 - 后台记忆任务的轻量子代理（工具仅限记忆目录的读写）"""
import os
import uuid
from typing import Any, Dict, Optional

import structlog
from app.agents.single import SingleAgent, create_single_agent
from app.config import settings
from app.core.memory import MemoryManager
from app.core.memory_system.store import memory_store
from app.core.prompt_engine import PromptEngine
from app.models.schemas.agent import AgentConfig, AgentType
from app.models.schemas.tool import ToolPermission as ToolPermissionEnum
from app.services.llm_service import LLMService, create_llm_service
from app.tools.registry import ToolRegistry

logger = structlog.get_logger()


def _provider_value(provider) -> str:
    """将 provider（枚举或字符串）归一化为字符串"""
    if provider is None:
        return ""
    return provider.value if hasattr(provider, "value") else str(provider)


def resolve_memory_llm(
    llm_service: LLMService,
    provider=None,
    model: Optional[str] = None,
    settings_prefix: str = "MEMORY_LLM",
) -> "tuple[LLMService, str, Optional[str]]":
    """
    解析记忆后台任务（提取/整理/会话记忆/选择）使用的 LLM 栈。

    优先级：settings 中对应 prefix 的配置 > 传入的 agent 配置 > 主 llm_service 默认。
    - 目标 provider 与主服务不一致时，创建独立 LLMService 实例
      （LLMService 对请求无共享状态，多实例/并发均安全）；
    - 仅 model 不同时，复用主服务实例（model 经 agent.config.llm_model 逐请求传递）。
    - api_key/base_url 由 LLMService 内部回退到 settings 全局配置。

    Args:
        llm_service: 主 Agent 的 LLM 服务（回退用）
        provider: 主 Agent 配置的 provider（枚举或字符串，可为 None）
        model: 主 Agent 配置的模型（可为 None）
        settings_prefix: settings 中读取 provider/model 的键前缀，
            默认 "MEMORY_LLM"（读 MEMORY_LLM_PROVIDER / MEMORY_LLM_MODEL），
            预提取可传 "MEMORY_PREFETCH_LLM" 使用独立配置。

    Returns:
        (llm_service, provider, model) —— model 始终有值（同 provider 时回退到服务实例默认模型）
    """
    cfg_provider = settings.get(f"{settings_prefix}_PROVIDER") or None
    cfg_model = settings.get(f"{settings_prefix}_MODEL") or None

    svc_provider = _provider_value(getattr(llm_service, "provider", None))
    base_provider = _provider_value(provider) or svc_provider
    target_provider = cfg_provider or base_provider
    target_model = cfg_model or model or None

    if target_provider and target_provider != svc_provider:
        # provider 不同：实例级 provider 不可切换，创建专用服务（每次创建开销极小，客户端懒初始化）
        svc = create_llm_service(provider=target_provider, model=target_model)
        logger.debug(
            "Memory task uses dedicated LLM service",
            provider=target_provider, model=target_model,
        )
        return svc, target_provider, target_model

    return llm_service, base_provider, target_model or getattr(llm_service, "model", None)


async def _restricted_read_file(file_path: str, max_lines: int = 1000) -> Dict[str, Any]:
    """受限 read_file：仅允许读取记忆目录内的文件"""
    if not memory_store.is_within_memory_root(file_path):
        return {
            "success": False,
            "error": f"PermissionError: read access denied, path is outside the memory directory: {file_path}",
        }
    from app.tools.builtin_tools import read_file
    return await read_file(file_path, max_lines=max_lines)


async def _restricted_write_file(file_path: str, content: str, append: bool = False) -> Dict[str, Any]:
    """受限 write_file：仅允许写入记忆目录内的文件"""
    if not memory_store.is_within_memory_root(file_path):
        return {
            "success": False,
            "error": f"PermissionError: write access denied, path is outside the memory directory: {file_path}",
        }
    from app.tools.builtin_tools import write_file
    return await write_file(file_path, content, append=append)


def _build_restricted_registry() -> ToolRegistry:
    """构建仅含记忆目录读写权限的独立工具注册表"""
    registry = ToolRegistry()
    registry.register(
        name="read_file",
        description="Read a memory file. ONLY files inside the memory directory are accessible.",
        handler=_restricted_read_file,
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the memory file",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to read",
                    "minimum": 1,
                    "maximum": 10000,
                    "default": 1000,
                },
            },
            "required": ["file_path"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=5,
    )
    registry.register(
        name="write_file",
        description="Write content to a memory file. ONLY files inside the memory directory are accessible.",
        handler=_restricted_write_file,
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the memory file",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
                "append": {
                    "type": "boolean",
                    "description": "Whether to append instead of overwrite",
                    "default": False,
                },
            },
            "required": ["file_path", "content"],
        },
        required_role=ToolPermissionEnum.PUBLIC,
        timeout_seconds=5,
    )
    return registry


def create_forked_agent(
    name: str,
    system_prompt: str,
    llm_service,
    max_iterations: int = 5,
    parent_agent_id: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> SingleAgent:
    """
    创建 forked agent：

    - 轻量 SingleAgent（is_subagent=True，不持久化会话）
    - 独立 ToolRegistry，仅注册受限 read_file / write_file / bash_command（路径限定记忆目录）
    - 不注入 skills、不注入 memory 教学
    - 预分配 forked 工作区路径写入 metadata（.happy-claw/workspace/forked-agents/{forked_id}），
      由 run_forked_task 运行前创建、退出后删除
    """
    from app.core.workspace_isolation import workspace_isolation

    default_config = settings.AGENT_SUBAGENT_DEFAULT_CONFIG
    forked_id = f"fork-{uuid.uuid4().hex[:8]}"
    # 预分配 forked 工作区路径（仅计算，目录在 run_forked_task 中创建），
    # BaseAgent.ensure_workspace 优先使用注入的 workspace_path，
    # 避免 forked agent 懒创建 session/agent 工作区导致无人清理的泄漏
    workspace_path = workspace_isolation.forked_workspace_path(forked_id)
    config = AgentConfig(
        id=None,
        name=name,
        description=f"Forked background agent: {name}",
        system_prompt=system_prompt,
        agent_type=AgentType.SUB,
        llm_provider=llm_provider or default_config.get("llm_provider", "openai"),
        llm_model=llm_model or default_config.get("llm_model", "gpt-4-turbo-preview"),
        temperature=0.3,
        max_tokens=default_config.get("max_tokens", 4096),
        tool_ids=["read_file", "write_file", "bash_command"],
        skill_ids=[],
        memory_config=None,
        metadata={"is_subagent": True, "forked": True, "workspace_path": workspace_path},
        is_subagent=True,
        level=1,
        parent_agent_id=parent_agent_id,
        parent_agent_name="memory_system",
        parent_conversation_id=None,
        spawn_uuid=forked_id,
    )

    agent = create_single_agent(
        config=config,
        llm_service=llm_service,
        tool_registry=_build_restricted_registry(),
        memory_manager=MemoryManager(),
        prompt_engine=PromptEngine(),
    )
    agent._max_iterations = max_iterations
    agent.deny_skills = ["*"]  # 不注入任何技能
    return agent


async def run_forked_task(agent: SingleAgent, task: str, lock_holder: Optional[str] = None) -> str:
    """
    运行 forked agent 任务，返回最终文本结果。

    运行前创建 forked 工作区（metadata["workspace_path"]），退出后删除；
    进程异常退出等未清理场景由应用启动时扫描 forked-agents 目录兜底清空。

    Args:
        lock_holder: 若指定，在任务执行期间设置记忆写入锁持有者上下文，
            使受限 write_file 能识别当前任务身份（任务级锁由调用方获取/释放）。
    """
    from app.core.memory_system.store import current_memory_lock_holder
    from app.core.workspace_isolation import workspace_isolation

    # 提前创建 forked 工作区（与子agent同样：创建后注入 metadata，退出时清理）
    metadata = getattr(agent.config, "metadata", None) or {}
    ws_path = metadata.get("workspace_path") if isinstance(metadata, dict) else None
    if ws_path:
        forked_id = getattr(agent.config, "spawn_uuid", None) or os.path.basename(ws_path)
        try:
            await workspace_isolation.create_forked_workspace(forked_id)
        except Exception as e:
            logger.warning("Forked workspace create failed", path=ws_path, error=str(e))

    token = current_memory_lock_holder.set(lock_holder) if lock_holder else None
    try:
        final_result = ""
        async for msg in agent.process_message(task):
            if msg.role.value == "assistant" and msg.message_type.value == "text":
                final_result = msg.content
        return final_result
    finally:
        if token is not None:
            current_memory_lock_holder.reset(token)
        # 退出时清理 forked 工作区（失败不阻断，启动扫描兜底）
        if ws_path:
            try:
                await workspace_isolation.remove_forked_workspace(ws_path)
            except Exception as e:
                logger.warning("Forked workspace cleanup failed", path=ws_path, error=str(e))
