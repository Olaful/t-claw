"""SubAgent Manager - 管理子代理生命周期"""
import asyncio
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable, Set, Tuple
import structlog

from app.core.a2a_protocol import (
    A2AMessageType, SpawnRequest, SpawnResponse, TaskResult, A2AMessage,
    DependencyResult,
    generate_subagent_id, parse_subagent_id, extract_level_from_spawn_uuid
)
from app.core.dependency_graph import DependencyGraph
from app.models.schemas.agent import AgentConfig, AgentStatus, AgentType
from app.agents.single import SingleAgent, create_single_agent
from app.services.llm_service import LLMService
from app.tools.registry import ToolRegistry, create_filtered_tool_registry
from app.core.memory import MemoryManager
from app.core.prompt_engine import PromptEngine
from app.config import settings

logger = structlog.get_logger()

# 跨层级共享 DAG 的上下文变量
# 当子agent内部再调用 spawn_agent 时，自动将新节点添加到父级 DAG 中
_active_dag: ContextVar[Optional[DependencyGraph]] = ContextVar('_active_dag', default=None)

# 跨层级共享根会话ID：确保所有层级的 DAG 事件都发布到前端订阅的原始会话维度
_root_conversation_id: ContextVar[Optional[str]] = ContextVar('_root_conversation_id', default=None)


class SubAgentManager:
    """子代理管理器"""
    
    def __init__(self):
        # 存储所有子代理实例
        self._subagents: Dict[str, SingleAgent] = {}
        # 存储父子关系映射（父ID -> [子ID列表]）
        self._parent_children_map: Dict[str, List[str]] = {}
        # 存储生成请求到子ID的映射
        self._spawn_requests: Dict[str, str] = {}
        # 最大层级限制
        self.max_level = settings.AGENT_SUBAGENT_MAX_SPAWN_LEVEL
        # 最大并发数
        self._max_concurrent = getattr(settings, 'AGENT_SUBAGENT_MAX_CONCURRENT', 5)
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        
        # 结果回调: key = (parent_agent_id, spawn_uuid)
        self._result_callbacks: Dict[str, Callable] = {}
        # 依赖关系图（每次 batch 执行时创建）
        self._dependency_graph: Optional[DependencyGraph] = None
        # 已完成的子agent结果: spawn_uuid -> DependencyResult
        self._completed_results: Dict[str, DependencyResult] = {}
        # spawn_uuid -> subagent_name 映射
        self._spawn_name_map: Dict[str, str] = {}
        # spawn_uuid -> (workspace_path, branch, repo) 临时工作区记录（退出时删除）
        self._subagent_workspaces: Dict[str, Tuple[str, str, str]] = {}
    
    async def spawn_subagent(
        self,
        request: SpawnRequest,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        memory_manager: MemoryManager,
        prompt_engine: PromptEngine,
    ) -> SpawnResponse:
        """
        生成子代理
        
        Args:
            request: 生成请求
            llm_service: LLM服务
            tool_registry: 工具注册表
            memory_manager: 记忆管理器
            prompt_engine: 提示词引擎
            
        Returns:
            生成响应
        """
        try:
            # 1. 检查层级是否超限
            level = extract_level_from_spawn_uuid(request.spawn_uuid)
            if level > self.max_level:
                return SpawnResponse(
                    success=False,
                    error_message=f"Spawn level {level} exceeds maximum allowed level {self.max_level}",
                    spawn_uuid=request.spawn_uuid,
                )
            
            # 2. 构建子agent配置（层级0时尝试复用已有agent配置，skill类型跳过）
            existing_config = None
            if level == 0 and request.agent_type != "skill":
                existing_config = self._find_existing_agent_config(request.subagent_name)
                if existing_config:
                    logger.info(
                        "Reusing existing agent config for level-0 subagent",
                        name=request.subagent_name,
                        existing_agent_id=existing_config.id,
                    )
            
            subagent_config = self._build_subagent_config(request, level, existing_config)
            
            # 2.1 创建子agent临时工作区（按 scope 配置位于相应级别工作区下）
            await self._prepare_subagent_workspace(request, subagent_config)
            
            # 3. 生成子agent ID
            subagent_id = generate_subagent_id(request.spawn_uuid)
            
            # 4. 创建子agent实例（根据层级过滤工具）
            filtered_registry = create_filtered_tool_registry(subagent_level=level)
            subagent = await self._create_subagent_instance(
                subagent_id,
                subagent_config,
                llm_service,
                filtered_registry,
                memory_manager,
                prompt_engine,
            )
            
            # 5. 设置 skill 类型的 deny_skills 和 agent_type
            if request.agent_type == "skill":
                subagent.deny_skills = [request.subagent_name]
                subagent.agent_type = "skill"
            
            # 6. 记录父子关系
            if request.parent_agent_id not in self._parent_children_map:
                self._parent_children_map[request.parent_agent_id] = []
            self._parent_children_map[request.parent_agent_id].append(subagent_id)
            
            # 7. 存储子agent
            self._subagents[subagent_id] = subagent
            self._spawn_requests[request.spawn_uuid] = subagent_id
            self._spawn_name_map[request.spawn_uuid] = request.subagent_name
            
            logger.info(
                "Subagent spawned successfully",
                subagent_id=subagent_id,
                parent_id=request.parent_agent_id,
                level=level,
                name=request.subagent_name,
            )
            
            # 发射子agent创建事件
            try:
                from app.core.event_bus import event_bus
                await event_bus.emit_event(
                    type="subagent_spawned",
                    source=request.parent_agent_id,
                    data={
                        "subagent_id": subagent_id,
                        "subagent_name": request.subagent_name,
                        "level": level,
                        "task": request.task_description[:200],
                    },
                    dimensions=[f"agent:{request.parent_agent_id}"],
                )
            except Exception as e:
                logger.warning("Failed to emit subagent spawn event", error=str(e))
            
            return SpawnResponse(
                success=True,
                subagent_id=subagent_id,
                spawn_uuid=request.spawn_uuid,
            )
            
        except Exception as e:
            logger.error("Failed to spawn subagent", error=str(e), request=request)
            return SpawnResponse(
                success=False,
                error_message=str(e),
                spawn_uuid=request.spawn_uuid,
            )
    
    def _find_existing_agent_config(self, agent_name: str) -> Optional[AgentConfig]:
        """
        根据名称查找已有的agent配置（非子agent）
        
        优先从 task_service 的运行时实例中查找，其次从数据库中查找。
        
        Args:
            agent_name: agent名称
            
        Returns:
            找到的 AgentConfig，未找到返回 None
        """
        # 1. 从 task_service 运行时实例中查找
        try:
            from app.services.task_service import task_service
            for agent in task_service._agents.values():
                if agent.config.name == agent_name and not agent.is_subagent:
                    logger.info("Found existing agent in task_service", name=agent_name, agent_id=agent.id)
                    return agent.config
        except Exception as e:
            logger.warning("Failed to search task_service for existing agent", error=str(e), exc_info=True)
        
        # 2. 从数据库中查找
        try:
            import asyncio
            from app.services.persistence import persistence
            # 尝试在已有事件循环中运行，否则创建新的
            try:
                loop = asyncio.get_running_loop()
                # 已在事件循环中，无法直接 await，跳过数据库查找
                return None
            except RuntimeError:
                pass
            
            configs = asyncio.get_event_loop().run_until_complete(
                persistence.load_all_agents()
            )
            for config in configs:
                if config.name == agent_name and not config.is_subagent:
                    logger.info("Found existing agent in database", name=agent_name, agent_id=config.id)
                    return config
        except Exception as e:
            logger.warning("Failed to search database for existing agent", error=str(e), exc_info=True)
        
        return None
    
    def _build_subagent_config(
        self,
        request: SpawnRequest,
        level: int,
        existing_config: Optional[AgentConfig] = None,
    ) -> AgentConfig:
        """
        构建子agent配置
        
        Args:
            request: 生成请求
            level: 层级
            existing_config: 已有的agent配置（层级0时可复用）
        """
        # 从配置文件获取该层级的允许工具
        allowed_tools = settings.AGENT_SUBAGENT_ALLOWED_TOOLS_PER_LEVEL.get(str(level), [])
        
        # 合并用户指定的工具和配置允许的
        final_allowed_tools = list(set(request.allowed_tools) & set(allowed_tools))
        
        if existing_config:
            # 复用已有agent配置，但覆盖子agent专用字段
            config_data = {
                "id": None,  # 将由 generate_subagent_id 生成
                "name": f"{existing_config.name} (Level {level})",
                "description": existing_config.description or f"Subagent for: {request.task_description[:100]}...",
                # 使用已有agent的系统提示词，追加当前任务描述
                "system_prompt": f"{existing_config.system_prompt}\n\nYour current task: {request.task_description}",
                "agent_type": AgentType.SUB,
                "llm_provider": existing_config.llm_provider,
                "llm_model": existing_config.llm_model,
                "temperature": existing_config.temperature,
                "max_tokens": existing_config.max_tokens,
                # 工具取交集：已有agent的工具 ∩ 该层级允许的工具
                "tool_ids": final_allowed_tools if final_allowed_tools else (
                    list(set(existing_config.tool_ids) & set(allowed_tools))
                ),
                "skill_ids": existing_config.skill_ids,
                "memory_config": existing_config.memory_config,
                "metadata": {
                    "is_subagent": True,
                    "level": level,
                    "parent_agent_id": request.parent_agent_id,
                    "parent_conversation_id": request.parent_conversation_id,
                    "spawn_uuid": request.spawn_uuid,
                    "reused_from_agent_id": existing_config.id,
                },
                # Subagent 专用字段
                "is_subagent": True,
                "level": level,
                "parent_agent_id": request.parent_agent_id,
                "parent_agent_name": request.parent_agent_name,
                "parent_conversation_id": request.parent_conversation_id,
                "spawn_uuid": request.spawn_uuid,
            }
        else:
            # 使用默认配置并应用覆盖
            default_config = settings.AGENT_SUBAGENT_DEFAULT_CONFIG
            
            config_data = {
                "id": None,  # 将由generate_subagent_id生成
                "name": f"{request.subagent_name} (Level {level})",
                "description": f"Subagent for: {request.task_description[:100]}...",
                "system_prompt": f"You are {request.subagent_name}. Your role: {request.subagent_role}\n\nYour current task: {request.task_description}",
                "agent_type": AgentType.SUB,
                "llm_provider": default_config.get("llm_provider", "openai"),
                "llm_model": default_config.get("llm_model", "gpt-4-turbo-preview"),
                "temperature": default_config.get("temperature", 0.7),
                "max_tokens": default_config.get("max_tokens", 4096),
                "tool_ids": final_allowed_tools,
                "skill_ids": [],
                "memory_config": None,
                "metadata": {
                    "is_subagent": True,
                    "level": level,
                    "parent_agent_id": request.parent_agent_id,
                    "parent_conversation_id": request.parent_conversation_id,
                    "spawn_uuid": request.spawn_uuid,
                },
                # Subagent 专用字段
                "is_subagent": True,
                "level": level,
                "parent_agent_id": request.parent_agent_id,
                "parent_agent_name": request.parent_agent_name,
                "parent_conversation_id": request.parent_conversation_id,
                "spawn_uuid": request.spawn_uuid,
            }
        
        # 应用用户覆盖
        if request.config_overrides:
            config_data.update(request.config_overrides)
        
        return AgentConfig(**config_data)
    
    async def _create_subagent_instance(
        self,
        subagent_id: str,
        config: AgentConfig,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        memory_manager: MemoryManager,
        prompt_engine: PromptEngine,
    ) -> SingleAgent:
        """创建子agent实例"""
        # 将 subagent_id 赋值给 config.id，用于日志显示
        # 子agent不入库，所以不受 varchar(36) 限制
        config.id = subagent_id
        
        # 创建单agent实例
        subagent = create_single_agent(
            config=config,
            llm_service=llm_service,
            tool_registry=tool_registry,
            memory_manager=memory_manager,
            prompt_engine=prompt_engine,
        )
        
        # 启动子agent
        await subagent.start()
        
        return subagent
    
    async def _prepare_subagent_workspace(
        self,
        request: SpawnRequest,
        config: AgentConfig,
    ) -> Optional[Tuple[str, str, str]]:
        """
        为子agent创建临时工作区（git worktree），并注入 config.metadata["workspace_path"]。
        
        按 agent.defaults.sandbox.scope 配置:
        - session: 位于父会话工作区下 {agent}/{session}/{sub_name}
        - agent:   位于主agent工作区下 {agent}/{sub_name}
        - shared:  位于全局共享工作区下 {sub_name}
        """
        try:
            from app.core.workspace_isolation import workspace_isolation
            scope = workspace_isolation.get_scope()
            if scope == "shared":
                path, branch, repo = await workspace_isolation.create_subagent_workspace(
                    None, None, request.subagent_name
                )
            elif scope == "agent":
                path, branch, repo = await workspace_isolation.create_subagent_workspace(
                    request.parent_agent_name, None, request.subagent_name
                )
            else:
                path, branch, repo = await workspace_isolation.create_subagent_workspace(
                    request.parent_agent_name,
                    request.parent_conversation_id,
                    request.subagent_name,
                )
            if config.metadata is None:
                config.metadata = {}
            config.metadata["workspace_path"] = path
            self._subagent_workspaces[request.spawn_uuid] = (path, branch, repo)
            logger.info(
                "Subagent workspace prepared",
                spawn_uuid=request.spawn_uuid,
                path=path,
                scope=scope,
            )
            return path, branch, repo
        except Exception as e:
            logger.warning(
                "Failed to prepare subagent workspace",
                spawn_uuid=request.spawn_uuid,
                error=str(e),
                exc_info=True,
            )
            return None
    
    async def _cleanup_workspace(self, spawn_uuid: str) -> None:
        """删除指定 spawn_uuid 的临时工作区（worktree + 分支），幂等"""
        ws_info = self._subagent_workspaces.pop(spawn_uuid, None)
        if not ws_info:
            return
        path, branch, repo = ws_info
        try:
            from app.core.workspace_isolation import workspace_isolation
            await workspace_isolation.remove_workspace(path, branch=branch, repo=repo)
        except Exception as e:
            logger.warning("Failed to remove subagent workspace", path=path, error=str(e))
    
    async def _cleanup_workspace_by_subagent_id(self, subagent_id: str) -> None:
        """根据子agent ID 反查 spawn_uuid 并清理工作区"""
        spawn_uuid = None
        for su, sid in self._spawn_requests.items():
            if sid == subagent_id:
                spawn_uuid = su
                break
        if spawn_uuid:
            await self._cleanup_workspace(spawn_uuid)
    
    async def stop_subagent(self, subagent_id: str):
        """停止单个子agent"""
        if subagent_id in self._subagents:
            subagent = self._subagents[subagent_id]
            await subagent.stop()
            del self._subagents[subagent_id]
            # ✅ 子agent退出时删除其临时工作区与分支
            await self._cleanup_workspace_by_subagent_id(subagent_id)
            logger.info("Subagent stopped", subagent_id=subagent_id)
    
    async def cascade_stop(self, agent_id: str):
        """
        级联停止：停止指定agent及其所有子agent
        
        Args:
            agent_id: 要停止的agent ID（可以是父agent或子agent）
        """
        logger.info("Starting cascade stop", agent_id=agent_id)
        
        # 收集需要停止的所有agent ID
        to_stop = set()
        
        # 如果是指定的agent本身，先加入
        if agent_id in self._subagents:
            to_stop.add(agent_id)
        
        # 递归查找所有子agent
        def find_children(parent_id: str):
            children = self._parent_children_map.get(parent_id, [])
            for child_id in children:
                to_stop.add(child_id)
                # 递归查找孙agent
                find_children(child_id)
        
        find_children(agent_id)
        
        # 同时检查该agent是否是某个父agent的子agent，向上查找
        def find_ancestors(child_id: str):
            for parent_id, children in self._parent_children_map.items():
                if child_id in children:
                    to_stop.add(parent_id)
                    find_ancestors(parent_id)
                    break
        
        find_ancestors(agent_id)
        
        # 执行停止
        stopped_count = 0
        for subagent_id in to_stop:
            try:
                await self.stop_subagent(subagent_id)
                stopped_count += 1
            except Exception as e:
                logger.error("Failed to stop subagent during cascade", 
                           subagent_id=subagent_id, error=str(e))
        
        logger.info("Cascade stop completed", 
                   total_to_stop=len(to_stop), 
                   stopped_count=stopped_count)
    
    def get_subagent(self, subagent_id: str) -> Optional[SingleAgent]:
        """获取子agent实例"""
        return self._subagents.get(subagent_id)
    
    def get_children(self, parent_id: str) -> List[str]:
        """获取指定agent的所有直接子agent"""
        return self._parent_children_map.get(parent_id, [])
    
    def get_all_descendants(self, ancestor_id: str) -> List[str]:
        """获取指定agent的所有后代子agent"""
        descendants = []
        
        def collect_descendants(parent_id: str):
            children = self._parent_children_map.get(parent_id, [])
            for child_id in children:
                descendants.append(child_id)
                collect_descendants(child_id)
        
        collect_descendants(ancestor_id)
        return descendants
    
    def cleanup(self):
        """清理所有资源"""
        for subagent_id in list(self._subagents.keys()):
            try:
                self._subagents[subagent_id].cleanup()
            except Exception as e:
                logger.error("Error cleaning up subagent", subagent_id=subagent_id, error=str(e), exc_info=True)
        
        # 同步清理所有残留的临时工作区（关闭时无事件循环可用）
        for spawn_uuid, (path, branch, repo) in list(self._subagent_workspaces.items()):
            try:
                from app.core.workspace_isolation import workspace_isolation
                workspace_isolation.remove_workspace_sync(path, branch=branch, repo=repo)
            except Exception as e:
                logger.warning("Failed to remove subagent workspace on cleanup", path=path, error=str(e))
        self._subagent_workspaces.clear()
        
        self._subagents.clear()
        self._parent_children_map.clear()
        self._spawn_requests.clear()
        self._result_callbacks.clear()
        self._completed_results.clear()
        self._spawn_name_map.clear()
        self._dependency_graph = None
        logger.info("SubAgentManager cleaned up")
    
    async def _cleanup_spawn_data(self, spawn_uuids: List[str]) -> None:
        """
        清理指定 spawn_uuid 关联的所有临时数据，防止长期运行内存泄漏。
        
        Args:
            spawn_uuids: 需要清理的 spawn_uuid 列表
        """
        subagent_ids_to_remove = []
        
        for spawn_uuid in spawn_uuids:
            # ✅ 删除临时工作区（worktree + 分支），无论成功/失败/异常都清理
            await self._cleanup_workspace(spawn_uuid)
            
            # 收集需要清理的子agent ID
            subagent_id = self._spawn_requests.pop(spawn_uuid, None)
            if subagent_id:
                subagent_ids_to_remove.append(subagent_id)
            
            # 清理已完成的结果
            self._completed_results.pop(spawn_uuid, None)
            
            # 清理名称映射
            self._spawn_name_map.pop(spawn_uuid, None)
            
            # 清理回调（key 格式: parent_agent_id:spawn_uuid）
            keys_to_remove = [k for k in self._result_callbacks if k.endswith(f":{spawn_uuid}")]
            for key in keys_to_remove:
                del self._result_callbacks[key]
        
        # 清理子agent实例和父子关系
        for subagent_id in subagent_ids_to_remove:
            self._subagents.pop(subagent_id, None)
            # 从父子关系映射中移除
            for parent_id in list(self._parent_children_map.keys()):
                children = self._parent_children_map.get(parent_id, [])
                if subagent_id in children:
                    children.remove(subagent_id)
                    if not children:
                        del self._parent_children_map[parent_id]
                    break
        
        logger.debug("Spawn data cleaned up", spawn_count=len(spawn_uuids))
    
    # ========== 结果回调机制 ==========
    
    def register_result_callback(
        self,
        parent_agent_id: str,
        spawn_uuid: str,
        callback: Callable,
    ) -> None:
        """
        注册子agent结果回调
        
        Args:
            parent_agent_id: 父agent ID
            spawn_uuid: 子agent的 spawn_uuid
            callback: 异步回调函数，接收 DependencyResult 参数
        """
        key = f"{parent_agent_id}:{spawn_uuid}"
        self._result_callbacks[key] = callback
        logger.debug("Result callback registered", key=key)
    
    async def notify_parent(self, task_result: TaskResult) -> None:
        """
        通知父agent子agent的执行结果
        
        Args:
            task_result: 子agent的任务执行结果
        """
        spawn_uuid = task_result.spawn_uuid
        subagent_id = task_result.subagent_id
        
        # 构建 DependencyResult
        subagent_name = self._spawn_name_map.get(spawn_uuid, "unknown")
        dep_result = DependencyResult(
            spawn_uuid=spawn_uuid,
            subagent_id=subagent_id,
            subagent_name=subagent_name,
            success=task_result.success,
            result=task_result.result,
            error_message=task_result.error_message,
        )
        
        # 记录已完成的结果
        self._completed_results[spawn_uuid] = dep_result
        
        # 查找父agent并调用回调
        parsed = parse_subagent_id(subagent_id)
        subagent_instance = self._subagents.get(subagent_id)
        parent_agent_id = subagent_instance.config.parent_agent_id if subagent_instance else "unknown"
        key = f"{parent_agent_id}:{spawn_uuid}"
        
        callback = self._result_callbacks.get(key)
        if callback:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(dep_result)
                else:
                    callback(dep_result)
                logger.info("Parent notified via callback", key=key)
            except Exception as e:
                logger.error("Failed to notify parent", key=key, error=str(e), exc_info=True)
        else:
            logger.warning("No callback found for parent notification", key=key)
    
    async def execute_subagent_task(
        self,
        subagent_id: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> TaskResult:
        """
        执行子agent任务并自动通知父agent
        
        Args:
            subagent_id: 子agent ID
            task: 任务描述
            context: 额外上下文
            
        Returns:
            TaskResult
        """
        subagent = self._subagents.get(subagent_id)
        if not subagent:
            raise ValueError(f"Subagent not found: {subagent_id}")
        
        # 查找 spawn_uuid
        spawn_uuid = None
        for su, sid in self._spawn_requests.items():
            if sid == subagent_id:
                spawn_uuid = su
                break
        if not spawn_uuid:
            spawn_uuid = "unknown"
        
        start_time = time.time()
        tool_calls_count = 0
        
        try:
            # 如果有依赖结果，注入到任务上下文中
            dep_context = context or {}

            # 检查该子agent是否有依赖
            if self._dependency_graph:
                deps = self._dependency_graph.get_dependencies(spawn_uuid)
                if deps:
                    dep_results = []
                    for dep_uuid in deps:
                        if dep_uuid in self._completed_results:
                            dr = self._completed_results[dep_uuid]
                            dep_results.append({
                                "subagent_name": dr.subagent_name,
                                "success": dr.success,
                                "result": dr.result,
                            })
                    dep_context["dependency_results"] = dep_results
            
            # 执行任务
            result = await subagent.execute_task(task, context=dep_context)
            execution_time = time.time() - start_time
            
            task_result = TaskResult(
                subagent_id=subagent_id,
                spawn_uuid=spawn_uuid,
                success=True,
                result=result,
                execution_time_seconds=execution_time,
                tool_calls_count=tool_calls_count,
            )
        except Exception as e:
            execution_time = time.time() - start_time
            task_result = TaskResult(
                subagent_id=subagent_id,
                spawn_uuid=spawn_uuid,
                success=False,
                error_message=str(e),
                execution_time_seconds=execution_time,
                tool_calls_count=tool_calls_count,
            )
        
        # 自动通知父agent
        await self.notify_parent(task_result)
        
        # 标记依赖图节点完成
        if self._dependency_graph:
            summary = (task_result.result[:100] + "...") if task_result.result and len(task_result.result) > 100 else task_result.result
            self._dependency_graph.mark_completed(
                spawn_uuid,
                success=task_result.success,
                result_summary=summary,
            )
        
        # 发射子agent完成事件
        try:
            from app.core.event_bus import event_bus
            # 获取父 agent id
            subagent_instance = self._subagents.get(subagent_id)
            parent_id = subagent_instance.config.parent_agent_id if subagent_instance else "unknown"
            await event_bus.emit_event(
                type="subagent_completed",
                source=parent_id,
                data={
                    "subagent_id": subagent_id,
                    "subagent_name": self._spawn_name_map.get(spawn_uuid, "unknown"),
                    "success": task_result.success,
                    "execution_time": task_result.execution_time_seconds,
                    "error": task_result.error_message,
                },
                priority="normal" if task_result.success else "high",
                dimensions=[f"agent:{parent_id}"],
            )
        except Exception as e:
            logger.warning("Failed to emit subagent complete event", error=str(e))
        
        return task_result
    
    # ========== 依赖解析 ==========
    
    def resolve_dependencies(self, spawn_uuid: str) -> Optional[List[DependencyResult]]:
        """
        检查指定子agent的依赖是否全部完成
        
        Args:
            spawn_uuid: 子agent的 spawn_uuid
            
        Returns:
            如果全部依赖完成，返回 DependencyResult 列表；否则返回 None
        """
        if not self._dependency_graph:
            return []
        
        deps = self._dependency_graph.get_dependencies(spawn_uuid)
        if not deps:
            return []
        
        results = []
        for dep_uuid in deps:
            if dep_uuid not in self._completed_results:
                return None  # 还有依赖未完成
            results.append(self._completed_results[dep_uuid])
        
        return results
    
    # ========== 并发调度 ==========
    
    async def _emit_dag_event(
        self,
        graph: DependencyGraph,
        action: str,
        node_ids: Optional[List[str]] = None,
        conversation_id: Optional[str] = None,
    ):
        """通过事件总线实时推送 DAG 状态变更到前端"""
        try:
            from app.core.event_bus import event_bus
            dimensions = []
            if conversation_id:
                dimensions.append(f"conversation:{conversation_id}")
            await event_bus.emit_event(
                type="dag_update",
                source=graph.dag_id or "unknown",
                data={
                    "action": action,
                    "node_ids": node_ids or [],
                    "dag": graph.to_dict(),
                },
                priority="normal",
                dimensions=dimensions,
            )
        except Exception as e:
            logger.warning("Failed to emit DAG event", action=action, error=str(e))
    
    async def execute_batch(
        self,
        requests: List[SpawnRequest],
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        memory_manager: MemoryManager,
        prompt_engine: PromptEngine,
        dag_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        existing_dag: Optional[DependencyGraph] = None,
    ) -> Tuple[List[TaskResult], DependencyGraph]:
        """
        批量执行子agent，支持依赖关系和并发执行。
        
        当 existing_dag 不为 None 时，新节点添加到已有 DAG 中，
        实现跨层级的完整 DAG 可视化。
        
        Args:
            requests: SpawnRequest 列表
            llm_service: LLM服务
            tool_registry: 工具注册表
            memory_manager: 记忆管理器
            prompt_engine: 提示词引擎
            dag_id: DAG可视化ID（仅新建 DAG 时使用）
            conversation_id: 关联的会话ID
            existing_dag: 已有的 DAG（嵌套调用时传入，共享同一个 DAG）
            
        Returns:
            (TaskResult 列表, DependencyGraph 用于可视化)
        """
        batch_spawn_uuids = [req.spawn_uuid for req in requests]
        
        # 管理根会话ID：顶层调用时设置，嵌套调用时复用已有的
        prev_root_conv_id = _root_conversation_id.get()
        if conversation_id and not prev_root_conv_id:
            _root_conversation_id.set(conversation_id)
        # 所有层级统一使用根会话ID发布 DAG 事件
        effective_conversation_id = _root_conversation_id.get() or conversation_id
        
        # 1. 构建或复用依赖图（包含可视化元数据）
        if existing_dag:
            # 嵌套调用：将新节点添加到已有 DAG 中
            graph = existing_dag
        else:
            # 顶层调用：创建新的 DAG
            graph = DependencyGraph(
                dag_id=dag_id,
                conversation_id=conversation_id,
            )
        
        request_map: Dict[str, SpawnRequest] = {}
        
        # 确定父节点的 spawn_uuid（用于跨层级边连接）
        parent_spawn_uuid = None
        if existing_dag and requests:
            # 从第一个 request 的 parent_agent_id 提取父节点的 spawn_uuid
            # 子agent的 ID 格式为 subagent:<spawn_uuid>
            pid = requests[0].parent_agent_id
            if pid.startswith("subagent:"):
                parent_spawn_uuid = pid[len("subagent:"):]
        
        for req in requests:
            graph.add_node(
                req.spawn_uuid,
                req.depends_on,
                metadata={
                    "name": req.subagent_name + f"({req.agent_type})",
                    "description": req.task_description[:200],
                    "role": req.subagent_role,
                    "level": req.level,
                    "parent_spawn_uuid": parent_spawn_uuid,
                },
            )
            # 嵌套调用时，添加从父节点到当前子节点的跨层级边
            if existing_dag and parent_spawn_uuid:
                graph.add_edge(parent_spawn_uuid, req.spawn_uuid)
            request_map[req.spawn_uuid] = req
        
        # 推送「节点已创建」的 DAG 事件
        await self._emit_dag_event(graph, "nodes_created", batch_spawn_uuids, effective_conversation_id)
        
        # 2. 验证依赖图
        if not graph.validate():
            raise ValueError("Circular dependency detected in batch spawn requests")
        
        # 管理 _active_dag 上下文变量（支持嵌套 DAG 共享）
        prev_dag = _active_dag.set(graph)
        
        try:
            # 3. 保存依赖图供 execute_subagent_task 使用
            self._dependency_graph = graph
            
            # 4. 先创建所有子agent实例
            for req in requests:
                resp = await self.spawn_subagent(
                    req, llm_service, tool_registry, memory_manager, prompt_engine
                )
                if not resp.success:
                    logger.error("Failed to spawn subagent in batch", spawn_uuid=req.spawn_uuid, error=resp.error_message)
                    graph.update_node_status(req.spawn_uuid, "failed", "Spawn failed")
            
            # 5. 获取执行层级（仅包含当前 batch 的节点，避免重新执行已完成的上层节点）
            all_layers = graph.get_execution_layers()
            batch_set = set(batch_spawn_uuids)
            layers = [[n for n in layer if n in batch_set] for layer in all_layers]
            layers = [l for l in layers if l]  # 移除空层级
            all_results: List[TaskResult] = []
            
            # 6. 按层级并发执行
            for layer in layers:
                async def _run_with_semaphore(spawn_uuid: str) -> TaskResult:
                    async with self._semaphore:
                        subagent_id = self._spawn_requests.get(spawn_uuid)
                        if not subagent_id:
                            return TaskResult(
                                subagent_id="unknown",
                                spawn_uuid=spawn_uuid,
                                success=False,
                                error_message="Subagent not found after spawn",
                                execution_time_seconds=0,
                                tool_calls_count=0,
                            )
                        graph.mark_running(spawn_uuid)
                        await self._emit_dag_event(graph, "node_status_changed", [spawn_uuid], effective_conversation_id)
                        req = request_map[spawn_uuid]
                        return await self.execute_subagent_task(
                            subagent_id, req.task_description
                        )
                
                # 同一层级并发执行
                layer_results = await asyncio.gather(
                    *[_run_with_semaphore(su) for su in layer],
                    return_exceptions=True,
                )
                
                for r in layer_results:
                    if isinstance(r, Exception):
                        logger.error("Batch task failed", error=str(r))
                        all_results.append(TaskResult(
                            subagent_id="unknown",
                            spawn_uuid="unknown",
                            success=False,
                            error_message=str(r),
                            execution_time_seconds=0,
                            tool_calls_count=0,
                        ))
                    else:
                        # 更新节点状态
                        if r.spawn_uuid:
                            summary = (r.result[:100] + "...") if r.result and len(r.result) > 100 else r.result
                            graph.mark_completed(r.spawn_uuid, success=r.success, result_summary=summary)
                            await self._emit_dag_event(graph, "node_status_changed", [r.spawn_uuid], effective_conversation_id)
                        all_results.append(r)
            
            return all_results, graph
        finally:
            # 7. 恢复上层 DAG 上下文（嵌套调用时让上层继续使用原来的 DAG）
            _active_dag.reset(prev_dag)
            # 恢复根会话ID
            _root_conversation_id.set(prev_root_conv_id)
            # 8. 统一清理本次 batch 的所有临时数据（无论成功/失败/异常）
            self._dependency_graph = None
            await self._cleanup_spawn_data(batch_spawn_uuids)


# 全局单例
subagent_manager = SubAgentManager()
