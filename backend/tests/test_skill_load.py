import sys
from pathlib import Path

# 直接运行时确保 app 模块可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from app.skills import skill_loader


class TestSkillLoad:
    """测试技能加载功能"""

    @pytest.mark.asyncio
    async def test_skill_load(self):
        """技能加载测试"""
        skill_loader.reload()
        res = skill_loader.get_skill("random-password")
        print("result:", res)
        test = 1


if __name__ == "__main__":
    # 运行 TestLogin 类下的 test_login_success 方法
    pytest.main(["-k", "TestSkillLoad and test_skill_load"])
    # pytest.main(["-k", "TestWebSearch or TestSearchProvidersMapping"])