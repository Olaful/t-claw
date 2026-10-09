"""数据库迁移脚本：为conversations表添加session_model字段"""
import asyncio
from sqlalchemy import text
from backend.app.utils.database import engine


async def run_migration():
    """执行迁移"""
    async with engine.begin() as conn:
        # 检查字段是否已存在
        result = await conn.execute(text("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'conversations' AND column_name = 'session_model'
        """))
        
        if not result.fetchone():
            print("Adding session_model column to conversations table...")
            await conn.execute(text("""
                ALTER TABLE conversations ADD COLUMN session_model JSON
            """))
            print("✓ session_model column added successfully")
        else:
            print("✓ session_model column already exists")


if __name__ == "__main__":
    asyncio.run(run_migration())
