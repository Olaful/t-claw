"""工具权限控制 - 基于RBAC"""
from typing import Dict, List, Optional, Set
from enum import Enum

import structlog
from app.models.schemas.tool import ToolPermission, UserRole

logger = structlog.get_logger()


class PermissionLevel(str, Enum):
    """权限级别"""
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


# 权限级别层次
PERMISSION_HIERARCHY = {
    PermissionLevel.PUBLIC: 0,
    PermissionLevel.AUTHENTICATED: 1,
    PermissionLevel.ADMIN: 2,
    PermissionLevel.SUPER_ADMIN: 3,
}


class PermissionManager:
    """权限管理器"""
    
    def __init__(self):
        # 用户角色映射
        self._user_roles: Dict[str, Set[str]] = {}
        # 角色权限映射
        self._role_permissions: Dict[str, Set[PermissionLevel]] = {
            "anonymous": {PermissionLevel.PUBLIC},
            "user": {PermissionLevel.PUBLIC, PermissionLevel.AUTHENTICATED},
            "admin": {PermissionLevel.PUBLIC, PermissionLevel.AUTHENTICATED, PermissionLevel.ADMIN},
            "super_admin": {
                PermissionLevel.PUBLIC,
                PermissionLevel.AUTHENTICATED,
                PermissionLevel.ADMIN,
                PermissionLevel.SUPER_ADMIN,
            },
        }
        # 工具特定权限覆盖
        self._tool_permissions: Dict[str, ToolPermission] = {}
    
    def check_permission(
        self,
        user_id: str,
        tool_name: str,
        required_role: PermissionLevel,
    ) -> bool:
        """检查用户是否有权限使用工具"""
        # 获取用户角色
        user_roles = self._user_roles.get(user_id, {"anonymous"})
        
        # 检查是否有工具特定的权限覆盖
        tool_perm = self._tool_permissions.get(tool_name)
        if tool_perm:
            # 检查是否在允许列表中
            if user_id in tool_perm.allowed_users:
                return True
            # 检查角色
            for role in user_roles:
                if role in tool_perm.allowed_roles:
                    return True
        
        # 检查角色权限
        for role in user_roles:
            role_perms = self._role_permissions.get(role, set())
            if required_role in role_perms:
                return True
        
        logger.warning(
            "Permission denied",
            user_id=user_id,
            tool_name=tool_name,
            required_role=required_role,
        )
        return False
    
    def assign_role(self, user_id: str, role: str):
        """为用户分配角色"""
        if user_id not in self._user_roles:
            self._user_roles[user_id] = set()
        self._user_roles[user_id].add(role)
        logger.info("Role assigned", user_id=user_id, role=role)
    
    def remove_role(self, user_id: str, role: str):
        """移除用户角色"""
        if user_id in self._user_roles:
            self._user_roles[user_id].discard(role)
            logger.info("Role removed", user_id=user_id, role=role)
    
    def get_user_roles(self, user_id: str) -> Set[str]:
        """获取用户角色"""
        return self._user_roles.get(user_id, {"anonymous"})
    
    def set_tool_permission(self, tool_permission: ToolPermission):
        """设置工具特定权限"""
        self._tool_permissions[tool_permission.tool_id] = tool_permission
        logger.info(
            "Tool permission set",
            tool_id=tool_permission.tool_id,
            required_role=tool_permission.required_role,
        )
    
    def add_custom_role(
        self,
        role_name: str,
        permissions: List[PermissionLevel],
    ):
        """添加自定义角色"""
        self._role_permissions[role_name] = set(permissions)
        logger.info("Custom role created", role=role_name, permissions=permissions)
    
    def get_effective_permissions(self, user_id: str) -> Set[PermissionLevel]:
        """获取用户的有效权限"""
        user_roles = self._user_roles.get(user_id, {"anonymous"})
        permissions = set()
        
        for role in user_roles:
            role_perms = self._role_permissions.get(role, set())
            permissions.update(role_perms)
        
        return permissions
    
    def is_admin(self, user_id: str) -> bool:
        """检查用户是否是管理员"""
        roles = self.get_user_roles(user_id)
        return "admin" in roles or "super_admin" in roles
    
    def can_access_tool(
        self,
        user_id: str,
        tool_name: str,
        required_role: PermissionLevel,
    ) -> bool:
        """检查工具访问权限（综合检查）"""
        # 公开工具
        if required_role == PermissionLevel.PUBLIC:
            return True
        
        # 检查用户权限
        return self.check_permission(user_id, tool_name, required_role)


# 全局权限管理器
permission_manager = PermissionManager()


# 装饰器：权限检查
def require_permission(permission: PermissionLevel):
    """权限检查装饰器"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            # 从参数中获取user_id
            user_id = kwargs.get("user_id") or (args[0] if args else None)
            tool_name = func.__name__
            
            if not permission_manager.check_permission(user_id, tool_name, permission):
                raise PermissionError(
                    f"User {user_id} does not have permission to use {tool_name}"
                )
            
            return await func(*args, **kwargs)
        return wrapper
    return decorator
