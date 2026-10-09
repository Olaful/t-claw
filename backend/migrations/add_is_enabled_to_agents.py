"""
数据库迁移脚本：为 agents 表添加 is_enabled 字段

使用方法:
1. 确保数据库连接正常
2. 运行此脚本: python migrations/add_is_enabled_to_agents.py
"""
import asyncio
from sqlalchemy import text
from app.utils.database import engine


async def migrate():
    """执行迁移"""
    print("开始迁移：为 agents 表添加 is_enabled 字段...")
    
    async with engine.begin() as conn:
        # 检查字段是否已存在
        result = await conn.execute(text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'agents' AND column_name = 'is_enabled'
        """))
        
        if result.fetchone():
            print("OK: is_enabled field already exists, skipping migration")
            return
        
        # 添加 is_enabled 字段（默认值为 True）
        await conn.execute(text("""
            ALTER TABLE agents 
            ADD COLUMN is_enabled BOOLEAN NOT NULL DEFAULT TRUE
        """))
        
        # 添加索引
        await conn.execute(text("""
            CREATE INDEX idx_agent_is_enabled ON agents (is_enabled)
        """))
        
        print("OK: Successfully added is_enabled field and index")


if __name__ == "__main__":
    asyncio.run(migrate())
    print("迁移完成！")
