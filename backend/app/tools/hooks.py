"""工具Hooks - 前置/后置钩子"""
import uuid
from datetime import datetime
from typing import Callable, Dict, Any, List, Optional
from dataclasses import dataclass, field

import structlog
from app.models.schemas.tool import HookContext, HookResult

logger = structlog.get_logger()


@dataclass
class HookDefinition:
    """Hook定义"""
    id: str
    name: str
    hook_type: str  # "pre" or "post"
    handler: Callable
    priority: int = 0  # 执行优先级，数字越大越先执行
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


class HookManager:
    """Hook管理器"""
    
    def __init__(self):
        self._pre_hooks: Dict[str, List[HookDefinition]] = {}  # tool_name -> hooks
        self._post_hooks: Dict[str, List[HookDefinition]] = {}
        self._global_pre_hooks: List[HookDefinition] = []
        self._global_post_hooks: List[HookDefinition] = []
    
    def register_pre_hook(
        self,
        handler: Callable,
        tool_name: Optional[str] = None,
        name: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        """注册前置钩子"""
        hook_id = str(uuid.uuid4())
        hook_name = name or f"pre_hook_{hook_id[:8]}"
        
        hook = HookDefinition(
            id=hook_id,
            name=hook_name,
            hook_type="pre",
            handler=handler,
            priority=priority,
        )
        
        if tool_name:
            if tool_name not in self._pre_hooks:
                self._pre_hooks[tool_name] = []
            self._pre_hooks[tool_name].append(hook)
            # 按优先级排序
            self._pre_hooks[tool_name].sort(key=lambda h: h.priority, reverse=True)
        else:
            self._global_pre_hooks.append(hook)
            self._global_pre_hooks.sort(key=lambda h: h.priority, reverse=True)
        
        logger.info("Pre-hook registered", hook_name=hook_name, tool_name=tool_name)
        return hook_id
    
    def register_post_hook(
        self,
        handler: Callable,
        tool_name: Optional[str] = None,
        name: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        """注册后置钩子"""
        hook_id = str(uuid.uuid4())
        hook_name = name or f"post_hook_{hook_id[:8]}"
        
        hook = HookDefinition(
            id=hook_id,
            name=hook_name,
            hook_type="post",
            handler=handler,
            priority=priority,
        )
        
        if tool_name:
            if tool_name not in self._post_hooks:
                self._post_hooks[tool_name] = []
            self._post_hooks[tool_name].append(hook)
            self._post_hooks[tool_name].sort(key=lambda h: h.priority, reverse=True)
        else:
            self._global_post_hooks.append(hook)
            self._global_post_hooks.sort(key=lambda h: h.priority, reverse=True)
        
        logger.info("Post-hook registered", hook_name=hook_name, tool_name=tool_name)
        return hook_id
    
    async def execute_pre_hooks(
        self,
        tool_name: str,
        context: HookContext,
    ) -> HookResult:
        """执行前置钩子"""
        # 收集所有适用的钩子
        hooks = list(self._global_pre_hooks)
        if tool_name in self._pre_hooks:
            hooks.extend(self._pre_hooks[tool_name])
        
        # 按优先级排序
        hooks.sort(key=lambda h: h.priority, reverse=True)
        
        # 依次执行
        modified_args = context.arguments.copy()
        
        for hook in hooks:
            if not hook.enabled:
                continue
            
            try:
                import asyncio
                if asyncio.iscoroutinefunction(hook.handler):
                    result = await hook.handler(context, modified_args)
                else:
                    result = hook.handler(context, modified_args)
                
                # 处理结果
                if isinstance(result, HookResult):
                    if not result.should_continue:
                        logger.warning(
                            "Pre-hook stopped execution",
                            hook_name=hook.name,
                            tool_name=tool_name,
                        )
                        return result
                    
                    if result.modified_arguments:
                        modified_args = result.modified_arguments
                
                logger.debug("Pre-hook executed", hook_name=hook.name, tool_name=tool_name)
            
            except Exception as e:
                logger.error(
                    "Pre-hook failed",
                    hook_name=hook.name,
                    tool_name=tool_name,
                    error=str(e),
                )
                # 根据策略决定是否继续
                # 默认继续执行
        
        return HookResult(
            success=True,
            modified_arguments=modified_args,
            should_continue=True,
        )
    
    async def execute_post_hooks(
        self,
        tool_name: str,
        context: HookContext,
        result: Any,
    ) -> Any:
        """执行后置钩子"""
        # 收集所有适用的钩子
        hooks = list(self._global_post_hooks)
        if tool_name in self._post_hooks:
            hooks.extend(self._post_hooks[tool_name])
        
        # 按优先级排序
        hooks.sort(key=lambda h: h.priority, reverse=True)
        
        modified_result = result
        
        for hook in hooks:
            if not hook.enabled:
                continue
            
            try:
                import asyncio
                if asyncio.iscoroutinefunction(hook.handler):
                    hook_result = await hook.handler(context, modified_result)
                else:
                    hook_result = hook.handler(context, modified_result)
                
                # 如果钩子返回了修改后的结果
                if hook_result is not None:
                    modified_result = hook_result
                
                logger.debug("Post-hook executed", hook_name=hook.name, tool_name=tool_name)
            
            except Exception as e:
                logger.error(
                    "Post-hook failed",
                    hook_name=hook.name,
                    tool_name=tool_name,
                    error=str(e),
                )
        
        return modified_result
    
    def unregister_hook(self, hook_id: str) -> bool:
        """注销钩子"""
        # 检查所有钩子列表
        for hooks_dict in [self._pre_hooks, self._post_hooks]:
            for tool_name, hooks in hooks_dict.items():
                for i, hook in enumerate(hooks):
                    if hook.id == hook_id:
                        hooks.pop(i)
                        logger.info("Hook unregistered", hook_id=hook_id)
                        return True
        
        # 检查全局钩子
        for hooks_list in [self._global_pre_hooks, self._global_post_hooks]:
            for i, hook in enumerate(hooks_list):
                if hook.id == hook_id:
                    hooks_list.pop(i)
                    logger.info("Global hook unregistered", hook_id=hook_id)
                    return True
        
        return False
    
    def enable_hook(self, hook_id: str) -> bool:
        """启用钩子"""
        hook = self._find_hook(hook_id)
        if hook:
            hook.enabled = True
            return True
        return False
    
    def disable_hook(self, hook_id: str) -> bool:
        """禁用钩子"""
        hook = self._find_hook(hook_id)
        if hook:
            hook.enabled = False
            return True
        return False
    
    def _find_hook(self, hook_id: str) -> Optional[HookDefinition]:
        """查找钩子"""
        for hooks_dict in [self._pre_hooks, self._post_hooks]:
            for hooks in hooks_dict.values():
                for hook in hooks:
                    if hook.id == hook_id:
                        return hook
        
        for hook in self._global_pre_hooks + self._global_post_hooks:
            if hook.id == hook_id:
                return hook
        
        return None


# 全局Hook管理器
hook_manager = HookManager()


# ===== 内置Hooks =====

async def audit_log_pre_hook(context: HookContext, arguments: Dict[str, Any]) -> HookResult:
    """审计日志前置钩子"""
    logger.info(
        "Tool call audit",
        tool_name=context.tool_name,
        tool_call_id=context.tool_call_id,
        user_id=context.user_id,
        agent_id=context.agent_id,
        arguments=arguments,
    )
    return HookResult(success=True, should_continue=True)


async def audit_log_post_hook(context: HookContext, result: Any) -> Any:
    """审计日志后置钩子"""
    logger.info(
        "Tool result audit",
        tool_name=context.tool_name,
        tool_call_id=context.tool_call_id,
        result_summary=str(result)[:200],
    )
    return result


async def rate_limit_pre_hook(context: HookContext, arguments: Dict[str, Any]) -> HookResult:
    """速率限制前置钩子（简化版）"""
    # 实际实现应使用Redis进行速率限制
    return HookResult(success=True, should_continue=True)


async def dangerous_command_check_pre_hook(context: HookContext, arguments: Dict[str, Any]) -> HookResult:
    """
    危险命令检查前置钩子
    
    仅对 bash_command 工具生效，检查命令是否包含危险操作
    """
    if context.tool_name != "bash_command":
        return HookResult(success=True, should_continue=True)
    
    # 获取命令参数
    command = arguments.get("command", "")
    if not command:
        return HookResult(success=True, should_continue=True)
    
    # 导入安全检查器
    from app.tools.security import command_security_checker
    
    # 执行安全检查
    check_result = command_security_checker.check_command(command)
    
    if not check_result["is_safe"]:
        logger.warning(
            "Dangerous command blocked",
            tool_name=context.tool_name,
            tool_call_id=context.tool_call_id,
            command=command[:100],  # 只记录前100字符
            reason=check_result["reason"],
            matched_pattern=check_result["matched_pattern"],
            match_type=check_result["match_type"],
        )
        
        # 返回错误结果，阻止工具执行
        return HookResult(
            success=False,
            should_continue=False,
            error=f"[BLOCKED] Dangerous command detected\n\nReason: {check_result['reason']}\nMatched pattern: {check_result['matched_pattern']}",
            modified_arguments=None,
        )
    
    logger.debug(
        "Command passed security check",
        tool_name=context.tool_name,
        tool_call_id=context.tool_call_id,
        command=command[:50],
    )
    
    return HookResult(success=True, should_continue=True)


# 注册内置钩子
hook_manager.register_pre_hook(
    audit_log_pre_hook,
    name="audit_log_pre",
    priority=100,
)

# ✅ 注册危险命令检查钩子（高优先级，先于审计日志执行）
hook_manager.register_pre_hook(
    dangerous_command_check_pre_hook,
    tool_name="bash_command",  # 仅对 bash_command 工具生效
    name="dangerous_command_check",
    priority=200,  # 更高优先级，确保最先执行
)

hook_manager.register_post_hook(
    audit_log_post_hook,
    name="audit_log_post",
    priority=100,
)
