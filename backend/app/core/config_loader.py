"""配置加载器 - 配置已统一从 .happy-claw/settings.json 加载，本模块提供辅助功能"""
import structlog
from app.config import settings

logger = structlog.get_logger()


def load_subagent_config() -> bool:
    """
    子代理配置已从 settings.json 统一加载，此函数仅记录日志并返回配置状态。
    
    保留此函数以兼容现有启动流程。
    """
    try:
        enabled = settings.AGENT_SUBAGENT_ENABLED
        max_level = settings.AGENT_SUBAGENT_MAX_SPAWN_LEVEL
        max_concurrent = settings.AGENT_SUBAGENT_MAX_CONCURRENT
        
        logger.info(
            "Subagent config loaded from settings.json",
            enabled=enabled,
            max_level=max_level,
            max_concurrent=max_concurrent,
        )
        return True
    except Exception as e:
        logger.error("Failed to read subagent config from settings", error=str(e), exc_info=True)
        return False


def reload_config():
    """重新加载所有配置（热更新）"""
    settings.reload()
    logger.info("Configuration reloaded from settings.json")
