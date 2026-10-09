"""提示词引擎 - 模板管理与动态注入"""
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List

import structlog
from jinja2 import Template, Environment, BaseLoader
from app.models.schemas.task import PromptTemplate, PromptVersion

logger = structlog.get_logger()


class PromptEngine:
    """提示词引擎"""
    
    def __init__(self):
        self.jinja_env = Environment(loader=BaseLoader())
        self._templates: Dict[str, PromptTemplate] = {}
        self._versions: Dict[str, List[PromptVersion]] = {}
        
        # 初始化默认模板
        self._init_default_templates()
    
    def _init_default_templates(self):
        """初始化默认提示词模板"""
        defaults = [
            {
                "name": "system_default",
                "template": "You are a helpful AI assistant. {{ additional_instructions }}",
                "variables": ["additional_instructions"],
            },
            {
                "name": "task_planning",
                "template": """你是一个任务规划专家。请分析以下目标并制定执行计划。

目标: {{ goal }}
{% if context %}
上下文信息:
{{ context }}
{% endif %}
{% if constraints %}
约束条件:
{{ constraints }}
{% endif %}

请制定详细的执行计划。""",
                "variables": ["goal", "context", "constraints"],
            },
            {
                "name": "tool_selection",
                "template": """根据以下任务，选择合适的工具：

任务: {{ task_description }}
可用工具:
{% for tool in available_tools %}
- {{ tool.name }}: {{ tool.description }}
{% endfor %}

请选择最合适的工具并说明原因。""",
                "variables": ["task_description", "available_tools"],
            },
            {
                "name": "reflection",
                "template": """请评估以下执行结果：

任务: {{ task_description }}
执行结果: {{ result }}
{% if expected %}预期结果: {{ expected }}{% endif %}

请分析：
1. 结果是否达到预期？
2. 是否存在问题？
3. 是否需要进一步操作？""",
                "variables": ["task_description", "result", "expected"],
            },
            {
                "name": "context_summary",
                "template": """请对以下对话历史进行摘要：

{% for message in messages %}
{{ message.role }}: {{ message.content }}
{% endfor %}

请提取关键信息和决策点。""",
                "variables": ["messages"],
            },
        ]
        
        for tmpl in defaults:
            template = PromptTemplate(
                id=str(uuid.uuid4()),
                name=tmpl["name"],
                template=tmpl["template"],
                variables=tmpl["variables"],
                version=1,
            )
            self._templates[tmpl["name"]] = template
            self._versions[tmpl["name"]] = [
                PromptVersion(
                    template_id=template.id,
                    version=1,
                    template=tmpl["template"],
                    created_at=datetime.utcnow(),
                )
            ]
    
    async def render(
        self,
        template_name: str,
        variables: Dict[str, Any],
        version: Optional[int] = None,
    ) -> str:
        """渲染提示词模板"""
        template = self._templates.get(template_name)
        if not template:
            raise ValueError(f"Template not found: {template_name}")
        
        # 获取指定版本的模板
        if version:
            versions = self._versions.get(template_name, [])
            version_obj = next((v for v in versions if v.version == version), None)
            if version_obj:
                template_text = version_obj.template
            else:
                template_text = template.template
        else:
            template_text = template.template
        
        # 使用Jinja2渲染
        try:
            jinja_template = self.jinja_env.from_string(template_text)
            rendered = jinja_template.render(**variables)
        except Exception as e:
            logger.error("Template rendering failed", template=template_name, error=str(e), exc_info=True)
            raise
        
        logger.debug("Template rendered", template=template_name, variables=list(variables.keys()))
        return rendered
    
    async def render_string(
        self,
        template_string: str,
        variables: Dict[str, Any],
    ) -> str:
        """渲染模板字符串"""
        try:
            jinja_template = self.jinja_env.from_string(template_string)
            return jinja_template.render(**variables)
        except Exception as e:
            logger.error("String template rendering failed", error=str(e), exc_info=True)
            raise
    
    def register_template(
        self,
        name: str,
        template: str,
        variables: Optional[List[str]] = None,
    ) -> PromptTemplate:
        """注册新的提示词模板"""
        # 提取变量（如果没有提供）
        if variables is None:
            variables = self._extract_variables(template)
        
        prompt_template = PromptTemplate(
            id=str(uuid.uuid4()),
            name=name,
            template=template,
            variables=variables,
            version=1,
        )
        
        self._templates[name] = prompt_template
        self._versions[name] = [
            PromptVersion(
                template_id=prompt_template.id,
                version=1,
                template=template,
                created_at=datetime.utcnow(),
            )
        ]
        
        logger.info("Template registered", name=name)
        return prompt_template
    
    def update_template(
        self,
        name: str,
        new_template: str,
        created_by: Optional[str] = None,
    ) -> PromptTemplate:
        """更新模板（创建新版本）"""
        template = self._templates.get(name)
        if not template:
            raise ValueError(f"Template not found: {name}")
        
        # 更新模板
        template.version += 1
        template.template = new_template
        template.variables = self._extract_variables(new_template)
        
        # 添加版本记录
        version = PromptVersion(
            template_id=template.id,
            version=template.version,
            template=new_template,
            created_at=datetime.utcnow(),
            created_by=created_by,
        )
        self._versions[name].append(version)
        
        logger.info("Template updated", name=name, version=template.version)
        return template
    
    def get_template(self, name: str) -> Optional[PromptTemplate]:
        """获取模板"""
        return self._templates.get(name)
    
    def list_templates(self) -> List[PromptTemplate]:
        """列出所有模板"""
        return list(self._templates.values())
    
    def get_template_versions(self, name: str) -> List[PromptVersion]:
        """获取模板版本历史"""
        return self._versions.get(name, [])
    
    def delete_template(self, name: str) -> bool:
        """删除模板"""
        if name in self._templates:
            del self._templates[name]
            del self._versions[name]
            logger.info("Template deleted", name=name)
            return True
        return False
    
    def _extract_variables(self, template: str) -> List[str]:
        """从模板中提取变量名"""
        import re
        # 匹配 {{ variable_name }} 格式
        pattern = r'\{\{\s*(\w+)\s*\}\}'
        variables = re.findall(pattern, template)
        return list(set(variables))
    
    def build_system_prompt(
        self,
        agent_name: str,
        agent_description: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        additional_context: Optional[str] = None,
        base_system_prompt: Optional[str] = None,
    ) -> str:
        """构建系统提示词（包含技能列表注入）"""
        # 如果提供了 base_system_prompt（来自 agent config），以其为基础
        if base_system_prompt:
            parts = [base_system_prompt]
        else:
            parts = [f"You are {agent_name}."]
            if agent_description:
                parts.append(agent_description)
        
        if tools:
            parts.append("\nYou have access to the following tools:")
            for tool in tools:
                parts.append(f"- {tool['name']}: {tool.get('description', '')}")
        
        # 注入技能列表
        try:
            from app.skills import skill_loader
            skills_section = skill_loader.build_skills_prompt_section()
            if skills_section:
                parts.append(skills_section)
        except Exception as e:
            logger.warning("Failed to inject skills into system prompt", error=str(e))
        
        if additional_context:
            parts.append(f"\nAdditional context:\n{additional_context}")
        
        return "\n".join(parts)


# 便捷函数
def create_prompt_engine() -> PromptEngine:
    """创建提示词引擎实例"""
    return PromptEngine()
