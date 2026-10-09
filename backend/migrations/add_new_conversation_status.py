"""数据库迁移脚本：为 conversationstatusenum 枚举类型添加 NEW 值

背景：conversations.status 使用 PostgreSQL 原生枚举（SQLAlchemy SAEnum(ConversationStatusEnum)），
新增会话状态 NEW（已创建但未发送任何消息）需要向 PG 枚举类型追加标签。

注意：
- SAEnum 默认以枚举成员「名称」作为持久化值（如 ACTIVE / NEW），因此新增标签为 'NEW'。
- ALTER TYPE ... ADD VALUE 在 PG 12 之前不能在事务块内执行，这里使用 AUTOCOMMIT 连接执行。
- 通过 pg_catalog 动态解析 conversations.status 列实际使用的枚举类型名，避免硬编码。
"""
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

# ✅ 自举 sys.path：同时加入项目根（供 backend.app.* 导入）与 backend 目录（供 app.* 导入）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "backend"
for _p in (str(_PROJECT_ROOT), str(_BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.app.utils.database import engine


async def run_migration():
    """执行迁移：确保会话状态枚举包含 NEW 值"""
    # 1) 解析 conversations.status 列所使用的枚举类型名
    async with engine.connect() as conn:
        result = await conn.execute(text("""
            SELECT t.typname
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE c.relname = 'conversations' AND a.attname = 'status'
        """))
        row = result.fetchone()

    if not row:
        print("[WARN] 未找到 conversations.status 列（可能表尚未创建），跳过迁移")
        return

    type_name = row[0]
    print(f"conversations.status 使用枚举类型：{type_name}")

    # 2) 检查 NEW 标签是否已存在
    async with engine.connect() as conn:
        result = await conn.execute(
            text("""
                SELECT 1
                FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = :type_name AND e.enumlabel = 'NEW'
            """),
            {"type_name": type_name},
        )
        exists = result.fetchone() is not None

    if exists:
        print("[OK] 枚举 NEW 值已存在，无需迁移")
        return

    # 3) 使用 AUTOCOMMIT 连接执行 ALTER TYPE（不能在事务块内）
    print(f"Adding 'NEW' to enum type {type_name} ...")
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            text(f'ALTER TYPE "{type_name}" ADD VALUE IF NOT EXISTS \'NEW\'')
        )
    print("[OK] 枚举 NEW 值添加成功")


if __name__ == "__main__":
    asyncio.run(run_migration())
