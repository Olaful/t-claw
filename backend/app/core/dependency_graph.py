"""DependencyGraph - 子agent依赖关系图（DAG）"""
from typing import Dict, List, Set, Optional, Any
from datetime import datetime
import structlog

logger = structlog.get_logger()


class DependencyGraph:
    """
    有向无环图（DAG）管理子agent之间的依赖关系。
    
    同时支持：
    - 执行顺序调度：拓扑排序、依赖检查、并发层级
    - 可视化状态追踪：节点元数据、执行状态、结果摘要
    """
    
    def __init__(
        self,
        dag_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ):
        # node_id -> 该节点依赖的 node_id 列表
        self._dependencies: Dict[str, List[str]] = {}
        # node_id -> 依赖该节点的其他节点列表（反向索引）
        self._dependents: Dict[str, List[str]] = {}
        # 已完成的节点
        self._completed: Set[str] = set()
        # 正在执行的节点
        self._running: Set[str] = set()
        
        # 可视化元数据
        self.dag_id = dag_id
        self.conversation_id = conversation_id
        self.tool_call_id = tool_call_id
        self._node_metadata: Dict[str, Dict[str, Any]] = {}  # node_id -> {name, description, role, ...}
        self._node_status: Dict[str, str] = {}  # node_id -> pending|running|completed|failed
        self._node_result: Dict[str, Optional[str]] = {}  # node_id -> result_summary
        self.created_at = datetime.utcnow().isoformat()
    
    def add_node(
        self,
        node_id: str,
        depends_on: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        添加节点及其依赖关系
        
        Args:
            node_id: 节点唯一标识（通常是 spawn_uuid）
            depends_on: 该节点依赖的其他节点 ID 列表
            metadata: 可视化元数据（name, description, role, level 等）
        """
        deps = depends_on or []
        self._dependencies[node_id] = deps
        self._node_status[node_id] = "pending"
        if metadata:
            self._node_metadata[node_id] = metadata
        
        # 确保依赖节点也在图中（即使没有显式添加）
        for dep_id in deps:
            if dep_id not in self._dependents:
                self._dependents[dep_id] = []
            if node_id not in self._dependents[dep_id]:
                self._dependents[dep_id].append(node_id)
            # 如果依赖节点尚未添加，为其创建空依赖列表
            if dep_id not in self._dependencies:
                self._dependencies[dep_id] = []
    
    def add_edge(self, from_id: str, to_id: str) -> None:
        """
        添加一条从 from_id 到 to_id 的有向边（依赖关系）。
        用于跨层级的父子连接（如父 agent 到其派生的子 agent）。
        
        Args:
            from_id: 边的起点（父节点）
            to_id: 边的终点（子节点）
        """
        # 确保两端节点都在图中
        if from_id not in self._dependencies:
            self._dependencies[from_id] = []
        if to_id not in self._dependencies:
            self._dependencies[to_id] = []
        
        # 添加依赖关系：to_id 依赖 from_id
        if from_id not in self._dependencies[to_id]:
            self._dependencies[to_id].append(from_id)
        
        # 更新反向索引
        if from_id not in self._dependents:
            self._dependents[from_id] = []
        if to_id not in self._dependents[from_id]:
            self._dependents[from_id].append(to_id)
    
    def validate(self) -> bool:
        """
        检测是否存在循环依赖
        
        Returns:
            True 如果图有效（无循环），False 如果存在循环依赖
        """
        visited = set()
        rec_stack = set()
        
        def _has_cycle(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            
            for dep_id in self._dependencies.get(node_id, []):
                if dep_id not in visited:
                    if _has_cycle(dep_id):
                        return True
                elif dep_id in rec_stack:
                    return True
            
            rec_stack.discard(node_id)
            return False
        
        for node_id in self._dependencies:
            if node_id not in visited:
                if _has_cycle(node_id):
                    logger.error("Circular dependency detected in graph")
                    return False
        
        return True
    
    def get_ready_nodes(self) -> List[str]:
        """
        获取所有依赖已满足且尚未执行的节点
        
        Returns:
            可以立即执行的节点 ID 列表
        """
        ready = []
        for node_id, deps in self._dependencies.items():
            if node_id in self._completed or node_id in self._running:
                continue
            # 检查所有依赖是否已完成
            if all(dep in self._completed for dep in deps):
                ready.append(node_id)
        return ready
    
    def mark_running(self, node_id: str) -> None:
        """标记节点为正在执行"""
        self._running.add(node_id)
        self._node_status[node_id] = "running"
    
    def mark_completed(self, node_id: str, success: bool = True, result_summary: Optional[str] = None) -> None:
        """标记节点为已完成"""
        self._running.discard(node_id)
        self._completed.add(node_id)
        self._node_status[node_id] = "completed" if success else "failed"
        if result_summary:
            self._node_result[node_id] = result_summary
    
    def is_completed(self, node_id: str) -> bool:
        """检查节点是否已完成"""
        return node_id in self._completed
    
    def is_all_completed(self) -> bool:
        """检查所有节点是否都已完成"""
        return all(
            node_id in self._completed
            for node_id in self._dependencies
        )
    
    def get_dependents(self, node_id: str) -> List[str]:
        """获取依赖指定节点的所有其他节点"""
        return self._dependents.get(node_id, [])
    
    def get_dependencies(self, node_id: str) -> List[str]:
        """获取指定节点的所有依赖"""
        return self._dependencies.get(node_id, [])
    
    def get_execution_layers(self) -> List[List[str]]:
        """
        拓扑排序，返回按执行层级分组的结果。
        同一层级的节点可以并发执行。
        
        Returns:
            层级列表，每个层级是一组可以并发执行的节点 ID
            例如: [["A", "B"], ["C"], ["D"]] 表示 A和B 先并发，然后 C，最后 D
        """
        if not self.validate():
            raise ValueError("Cannot sort: circular dependency detected")
        
        layers = []
        remaining = set(self._dependencies.keys())
        completed = set()
        
        while remaining:
            # 找出当前层：所有依赖都已完成的节点
            current_layer = []
            for node_id in remaining:
                deps = self._dependencies.get(node_id, [])
                if all(dep in completed for dep in deps):
                    current_layer.append(node_id)
            
            if not current_layer:
                # 不应该到这里（已通过 validate 检查）
                raise ValueError("Cannot resolve remaining dependencies")
            
            layers.append(current_layer)
            for node_id in current_layer:
                remaining.discard(node_id)
                completed.add(node_id)
        
        return layers
    
    # ========== 可视化状态 ==========
    
    def update_node_status(
        self,
        node_id: str,
        status: str,
        result_summary: Optional[str] = None,
    ) -> None:
        """
        更新节点状态（用于执行过程中的实时状态更新）
        
        Args:
            node_id: 节点ID
            status: pending | running | completed | failed
            result_summary: 结果摘要
        """
        self._node_status[node_id] = status
        if result_summary:
            self._node_result[node_id] = result_summary
    
    def to_dict(self) -> Dict[str, Any]:
        """
        生成前端可视化所需的 DAG 状态字典
        
        Returns:
            DAG 状态字典，包含 nodes、edges、status 等
        """
        nodes = []
        edges = []
        
        for node_id, deps in self._dependencies.items():
            meta = self._node_metadata.get(node_id, {})
            nodes.append({
                "id": node_id,
                "name": meta.get("name", node_id),
                "spawn_uuid": node_id,
                "status": self._node_status.get(node_id, "pending"),
                "depends_on": deps,
                "description": meta.get("description", ""),
                "role": meta.get("role", ""),
                "level": meta.get("level"),
                "parent_spawn_uuid": meta.get("parent_spawn_uuid"),
                "result_summary": self._node_result.get(node_id),
            })
            for dep_id in deps:
                edges.append({"from": dep_id, "to": node_id})
        
        # 整体状态：全部完成则为 completed，否则 running
        all_done = all(
            s in ("completed", "failed")
            for s in self._node_status.values()
        ) if self._node_status else False
        
        return {
            "dag_id": self.dag_id,
            "nodes": nodes,
            "edges": edges,
            "status": "completed" if all_done else "running",
            "conversation_id": self.conversation_id,
            "tool_call_id": self.tool_call_id,
            "created_at": self.created_at,
            "updated_at": datetime.utcnow().isoformat(),
        }
