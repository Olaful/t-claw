"""工作区管理 - Agent/Team工作区隔离"""
import os
import uuid
import shutil
from datetime import datetime
from typing import Optional, Dict, Any, List

import structlog
from app.config import settings
from app.models.schemas.team import WorkspaceConfig, FileAccessLog

logger = structlog.get_logger()


class WorkspaceManager:
    """工作区管理器"""
    
    def __init__(self, base_path: Optional[str] = None):
        self.base_path = base_path or settings.WORKSPACE_BASE_PATH
        self._workspaces: Dict[str, WorkspaceConfig] = {}
        self._access_logs: List[FileAccessLog] = []
        
        # 确保基础目录存在
        os.makedirs(self.base_path, exist_ok=True)
    
    def create_workspace(
        self,
        agent_id: str,
        environment: Optional[Dict[str, str]] = None,
        resource_limits: Optional[Dict[str, Any]] = None,
    ) -> WorkspaceConfig:
        """创建Agent工作区"""
        workspace_path = os.path.join(self.base_path, agent_id)
        os.makedirs(workspace_path, exist_ok=True)
        
        # 创建子目录
        for subdir in ["input", "output", "temp", "data"]:
            os.makedirs(os.path.join(workspace_path, subdir), exist_ok=True)
        
        config = WorkspaceConfig(
            agent_id=agent_id,
            base_path=workspace_path,
            allowed_paths=[workspace_path],
            environment=environment or {},
            resource_limits=resource_limits or {},
        )
        
        self._workspaces[agent_id] = config
        logger.info("Workspace created", agent_id=agent_id, path=workspace_path)
        return config
    
    def get_workspace(self, agent_id: str) -> Optional[WorkspaceConfig]:
        """获取工作区配置"""
        return self._workspaces.get(agent_id)
    
    def delete_workspace(self, agent_id: str) -> bool:
        """删除工作区"""
        config = self._workspaces.get(agent_id)
        if not config:
            return False
        
        try:
            if os.path.exists(config.base_path):
                shutil.rmtree(config.base_path)
            
            del self._workspaces[agent_id]
            logger.info("Workspace deleted", agent_id=agent_id)
            return True
        except Exception as e:
            logger.error("Failed to delete workspace", agent_id=agent_id, error=str(e), exc_info=True)
            return False
    
    def check_access(self, agent_id: str, file_path: str) -> bool:
        """检查文件访问权限"""
        config = self._workspaces.get(agent_id)
        if not config:
            return False
        
        # 检查路径是否在工作区内
        abs_path = os.path.abspath(file_path)
        for allowed in config.allowed_paths:
            if abs_path.startswith(os.path.abspath(allowed)):
                return True
        
        return False
    
    async def read_file(
        self,
        agent_id: str,
        file_path: str,
    ) -> Optional[str]:
        """读取文件"""
        if not self.check_access(agent_id, file_path):
            self._log_access(agent_id, file_path, "read", success=False)
            raise PermissionError(f"Access denied: {file_path}")
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            self._log_access(agent_id, file_path, "read", success=True)
            return content
        except Exception as e:
            self._log_access(agent_id, file_path, "read", success=False)
            raise
    
    async def write_file(
        self,
        agent_id: str,
        file_path: str,
        content: str,
    ) -> int:
        """写入文件"""
        if not self.check_access(agent_id, file_path):
            self._log_access(agent_id, file_path, "write", success=False)
            raise PermissionError(f"Access denied: {file_path}")
        
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            with open(file_path, "w", encoding="utf-8") as f:
                bytes_written = f.write(content)
            
            self._log_access(agent_id, file_path, "write", success=True)
            return len(content)
        except Exception as e:
            self._log_access(agent_id, file_path, "write", success=False)
            raise
    
    async def list_files(
        self,
        agent_id: str,
        directory: Optional[str] = None,
        recursive: bool = False,
    ) -> List[str]:
        """列出文件"""
        config = self._workspaces.get(agent_id)
        if not config:
            raise ValueError(f"Workspace not found: {agent_id}")
        
        target_dir = directory or config.base_path
        
        if not self.check_access(agent_id, target_dir):
            raise PermissionError(f"Access denied: {target_dir}")
        
        files = []
        if recursive:
            for root, dirs, filenames in os.walk(target_dir):
                for filename in filenames:
                    filepath = os.path.join(root, filename)
                    files.append(os.path.relpath(filepath, config.base_path))
        else:
            for item in os.listdir(target_dir):
                files.append(item)
        
        return files
    
    async def delete_file(self, agent_id: str, file_path: str) -> bool:
        """删除文件"""
        if not self.check_access(agent_id, file_path):
            self._log_access(agent_id, file_path, "delete", success=False)
            raise PermissionError(f"Access denied: {file_path}")
        
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
            
            self._log_access(agent_id, file_path, "delete", success=True)
            return True
        except Exception as e:
            self._log_access(agent_id, file_path, "delete", success=False)
            raise
    
    async def get_workspace_size(self, agent_id: str) -> int:
        """获取工作区大小（字节）"""
        config = self._workspaces.get(agent_id)
        if not config or not os.path.exists(config.base_path):
            return 0
        
        total_size = 0
        for root, dirs, files in os.walk(config.base_path):
            for f in files:
                fp = os.path.join(root, f)
                total_size += os.path.getsize(fp)
        
        return total_size
    
    def _log_access(
        self,
        agent_id: str,
        file_path: str,
        action: str,
        success: bool = True,
    ):
        """记录访问日志"""
        log = FileAccessLog(
            agent_id=agent_id,
            file_path=file_path,
            action=action,
            success=success,
        )
        self._access_logs.append(log)
    
    def get_access_logs(
        self,
        agent_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[FileAccessLog]:
        """获取访问日志"""
        logs = self._access_logs
        if agent_id:
            logs = [l for l in logs if l.agent_id == agent_id]
        return logs[-limit:]
    
    def list_workspaces(self) -> List[WorkspaceConfig]:
        """列出所有工作区"""
        return list(self._workspaces.values())


# 全局工作区管理器
workspace_manager = WorkspaceManager()
