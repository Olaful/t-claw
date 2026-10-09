"""任务规划器 - 支持ReAct、Plan-and-Solve、Tree-of-Thoughts等策略"""
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from enum import Enum

import structlog
from app.models.schemas.task import Plan, SubTask, PlanStatus, SubTaskStatus
from app.services.llm_service import LLMService

logger = structlog.get_logger()


class PlanningStrategy(str, Enum):
    """规划策略"""
    REACT = "react"
    PLAN_AND_SOLVE = "plan_and_solve"
    TREE_OF_THOUGHTS = "tree_of_thoughts"


class Planner:
    """任务规划器"""
    
    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service
    
    async def create_plan(
        self,
        goal: str,
        strategy: PlanningStrategy = PlanningStrategy.REACT,
        context: Optional[Dict[str, Any]] = None,
    ) -> Plan:
        """创建任务计划"""
        logger.info("Creating plan", goal=goal, strategy=strategy)
        
        if strategy == PlanningStrategy.REACT:
            return await self._react_planning(goal, context)
        elif strategy == PlanningStrategy.PLAN_AND_SOLVE:
            return await self._plan_and_solve(goal, context)
        elif strategy == PlanningStrategy.TREE_OF_THOUGHTS:
            return await self._tree_of_thoughts(goal, context)
        else:
            raise ValueError(f"Unknown strategy: {strategy}")
    
    async def _react_planning(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Plan:
        """ReAct策略：思考-行动-观察循环"""
        prompt = f"""你是一个任务规划专家。请将以下目标分解为可执行的子任务。

目标: {goal}

请以JSON格式返回子任务列表，每个子任务包含:
- id: 子任务ID
- title: 标题
- description: 详细描述
- dependencies: 依赖的子任务ID列表

返回格式:
{{"subtasks": [{{"id": "1", "title": "...", "description": "...", "dependencies": []}}]}}
"""
        
        response = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        
        # 解析响应（简化处理）
        subtasks = self._parse_subtasks(response, goal)
        
        plan = Plan(
            id=str(uuid.uuid4()),
            goal=goal,
            strategy=PlanningStrategy.REACT,
            subtasks=subtasks,
            status=PlanStatus.PENDING,
        )
        
        logger.info("Plan created", plan_id=plan.id, subtask_count=len(subtasks))
        return plan
    
    async def _plan_and_solve(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Plan:
        """Plan-and-Solve策略：先完整规划，再逐步执行"""
        prompt = f"""请为以下目标制定详细的执行计划：

目标: {goal}

要求：
1. 将目标分解为有序的子任务
2. 明确每个子任务的输入和输出
3. 标识任务之间的依赖关系

请以JSON格式返回计划。
"""
        
        response = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        
        subtasks = self._parse_subtasks(response, goal)
        
        plan = Plan(
            id=str(uuid.uuid4()),
            goal=goal,
            strategy=PlanningStrategy.PLAN_AND_SOLVE,
            subtasks=subtasks,
            status=PlanStatus.PENDING,
        )
        
        return plan
    
    async def _tree_of_thoughts(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Plan:
        """Tree-of-Thoughts策略：探索多种解决路径"""
        prompt = f"""请使用发散思维，为目标提供多种解决方案路径：

目标: {goal}

请生成3种不同的解决方案，每种方案包含一系列步骤。
选择最优的方案并以JSON格式返回。
"""
        
        response = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,  # 更高的温度促进创造性
        )
        
        subtasks = self._parse_subtasks(response, goal)
        
        plan = Plan(
            id=str(uuid.uuid4()),
            goal=goal,
            strategy=PlanningStrategy.TREE_OF_THOUGHTS,
            subtasks=subtasks,
            status=PlanStatus.PENDING,
        )
        
        return plan
    
    def _parse_subtasks(self, response: str, goal: str) -> List[SubTask]:
        """解析LLM响应生成子任务"""
        # 简化的解析逻辑，实际应使用JSON解析
        import json
        try:
            data = json.loads(response)
            subtasks_data = data.get("subtasks", [])
        except json.JSONDecodeError:
            # 如果解析失败，创建默认子任务
            subtasks_data = [
                {
                    "id": "1",
                    "title": "分析目标",
                    "description": f"分析并理解目标: {goal}",
                    "dependencies": [],
                },
                {
                    "id": "2",
                    "title": "执行任务",
                    "description": "根据分析结果执行具体任务",
                    "dependencies": ["1"],
                },
            ]
        
        plan_id = str(uuid.uuid4())
        subtasks = []
        for st in subtasks_data:
            subtask = SubTask(
                id=st.get("id", str(uuid.uuid4())),
                plan_id=plan_id,
                title=st.get("title", "未命名任务"),
                description=st.get("description", ""),
                dependencies=st.get("dependencies", []),
                status=SubTaskStatus.PENDING,
            )
            subtasks.append(subtask)
        
        return subtasks
    
    async def refine_plan(
        self,
        plan: Plan,
        feedback: str,
    ) -> Plan:
        """根据反馈优化计划"""
        prompt = f"""当前计划:
目标: {plan.goal}
子任务: {len(plan.subtasks)}个

反馈: {feedback}

请根据反馈调整计划，返回更新后的子任务列表（JSON格式）。
"""
        
        response = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        
        refined_subtasks = self._parse_subtasks(response, plan.goal)
        plan.subtasks = refined_subtasks
        plan.updated_at = datetime.utcnow()
        
        return plan


# 便捷函数
def create_planner(llm_service: LLMService) -> Planner:
    """创建规划器实例"""
    return Planner(llm_service)
