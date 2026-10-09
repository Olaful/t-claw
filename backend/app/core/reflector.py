"""反思器 - 错误检测、分析与恢复计划生成"""
import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List

import structlog
from app.models.schemas.task import ErrorReport, RecoveryPlan, Plan, SubTaskStatus
from app.services.llm_service import LLMService

logger = structlog.get_logger()


class RecoveryStrategy(str):
    """恢复策略"""
    RETRY = "retry"
    FALLBACK = "fallback"
    ESCALATE = "escalate"
    ABORT = "abort"
    REPLAN = "replan"


class Reflector:
    """反思器 - 分析错误并生成恢复方案"""
    
    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service
        self.error_history: Dict[str, List[ErrorReport]] = {}
    
    async def analyze_error(
        self,
        agent_id: str,
        error: Exception,
        task_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> ErrorReport:
        """分析错误并生成报告"""
        error_report = ErrorReport(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            task_id=task_id,
            error_type=type(error).__name__,
            error_message=str(error),
            stack_trace=self._get_stack_trace(error),
            context=context or {},
        )
        
        # 记录错误历史
        if agent_id not in self.error_history:
            self.error_history[agent_id] = []
        self.error_history[agent_id].append(error_report)
        
        logger.warning(
            "Error analyzed",
            agent_id=agent_id,
            error_type=error_report.error_type,
            error_report_id=error_report.id,
        )
        
        return error_report
    
    async def generate_recovery_plan(
        self,
        error_report: ErrorReport,
        current_plan: Optional[Plan] = None,
    ) -> RecoveryPlan:
        """生成恢复计划"""
        logger.info("Generating recovery plan", error_report_id=error_report.id)
        
        # 根据错误类型选择恢复策略
        strategy = self._determine_strategy(error_report)
        
        if strategy == RecoveryStrategy.RETRY:
            return await self._create_retry_plan(error_report)
        elif strategy == RecoveryStrategy.REPLAN:
            return await self._create_replan(error_report, current_plan)
        elif strategy == RecoveryStrategy.FALLBACK:
            return await self._create_fallback_plan(error_report)
        elif strategy == RecoveryStrategy.ESCALATE:
            return await self._create_escalation_plan(error_report)
        else:
            return await self._create_abort_plan(error_report)
    
    def _determine_strategy(self, error_report: ErrorReport) -> str:
        """根据错误类型确定恢复策略"""
        error_history = self.error_history.get(error_report.agent_id, [])
        repeat_count = sum(
            1 for e in error_history
            if e.error_type == error_report.error_type
        )
        
        # 重复错误超过3次，升级处理
        if repeat_count >= 3:
            return RecoveryStrategy.ESCALATE
        
        # 根据错误类型选择策略
        error_type = error_report.error_type.lower()
        
        if "timeout" in error_type or "network" in error_type:
            return RecoveryStrategy.RETRY
        elif "tool" in error_type or "function" in error_type:
            return RecoveryStrategy.FALLBACK
        elif "context" in error_type or "token" in error_type:
            return RecoveryStrategy.REPLAN
        elif "permission" in error_type or "auth" in error_type:
            return RecoveryStrategy.ESCALATE
        else:
            return RecoveryStrategy.RETRY
    
    async def _create_retry_plan(
        self,
        error_report: ErrorReport,
    ) -> RecoveryPlan:
        """创建重试计划"""
        return RecoveryPlan(
            id=str(uuid.uuid4()),
            error_report_id=error_report.id,
            strategy=RecoveryStrategy.RETRY,
            actions=[
                {
                    "type": "retry",
                    "delay_seconds": 2,
                    "max_retries": 3,
                    "backoff_factor": 2,
                }
            ],
            max_retries=3,
        )
    
    async def _create_replan(
        self,
        error_report: ErrorReport,
        current_plan: Optional[Plan],
    ) -> RecoveryPlan:
        """创建重新规划方案"""
        prompt = f"""任务执行遇到错误，需要重新规划。

错误类型: {error_report.error_type}
错误信息: {error_report.error_message}
上下文: {error_report.context}

当前目标: {current_plan.goal if current_plan else '未知'}

请分析错误原因，并提供修正后的执行计划（JSON格式）。
"""
        
        response = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        
        return RecoveryPlan(
            id=str(uuid.uuid4()),
            error_report_id=error_report.id,
            strategy=RecoveryStrategy.REPLAN,
            actions=[
                {
                    "type": "replan",
                    "llm_response": response,
                }
            ],
            max_retries=1,
        )
    
    async def _create_fallback_plan(
        self,
        error_report: ErrorReport,
    ) -> RecoveryPlan:
        """创建降级方案"""
        return RecoveryPlan(
            id=str(uuid.uuid4()),
            error_report_id=error_report.id,
            strategy=RecoveryStrategy.FALLBACK,
            actions=[
                {
                    "type": "fallback",
                    "fallback_action": "use_alternative_tool",
                }
            ],
            max_retries=1,
        )
    
    async def _create_escalation_plan(
        self,
        error_report: ErrorReport,
    ) -> RecoveryPlan:
        """创建升级处理方案"""
        return RecoveryPlan(
            id=str(uuid.uuid4()),
            error_report_id=error_report.id,
            strategy=RecoveryStrategy.ESCALATE,
            actions=[
                {
                    "type": "escalate",
                    "notify_user": True,
                    "message": f"任务执行遇到需要人工介入的错误: {error_report.error_message}",
                }
            ],
            max_retries=0,
        )
    
    async def _create_abort_plan(
        self,
        error_report: ErrorReport,
    ) -> RecoveryPlan:
        """创建终止方案"""
        return RecoveryPlan(
            id=str(uuid.uuid4()),
            error_report_id=error_report.id,
            strategy=RecoveryStrategy.ABORT,
            actions=[
                {
                    "type": "abort",
                    "reason": error_report.error_message,
                }
            ],
            max_retries=0,
        )
    
    async def reflect_on_result(
        self,
        task_description: str,
        result: str,
        expected_outcome: Optional[str] = None,
    ) -> Dict[str, Any]:
        """反思执行结果，评估质量"""
        prompt = f"""请评估以下任务执行结果：

任务描述: {task_description}
执行结果: {result}
{f'预期结果: {expected_outcome}' if expected_outcome else ''}

请从以下维度评估：
1. 结果完整性 (0-1)
2. 结果准确性 (0-1)
3. 是否需要进一步操作

返回JSON格式:
{{"completeness": 0.8, "accuracy": 0.9, "needs_further_action": false, "suggestions": "..."}}
"""
        
        response = await self.llm_service.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        
        # 解析评估结果
        import json
        try:
            evaluation = json.loads(response)
        except json.JSONDecodeError:
            evaluation = {
                "completeness": 0.5,
                "accuracy": 0.5,
                "needs_further_action": True,
                "suggestions": response,
            }
        
        return evaluation
    
    def _get_stack_trace(self, error: Exception) -> str:
        """获取堆栈跟踪"""
        import traceback
        return "".join(traceback.format_exception(type(error), error, error.__traceback__))


# 便捷函数
def create_reflector(llm_service: LLMService) -> Reflector:
    """创建反思器实例"""
    return Reflector(llm_service)
