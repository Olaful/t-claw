"""记忆系统API"""
from fastapi import APIRouter, HTTPException, Query

from app.core.memory_system.store import memory_store
from app.core.memory_system.scanner import memory_scanner
from app.core.memory_system.session_memory import (
    session_memory_manager,
    SESSION_NOTE_FILENAME,
)

router = APIRouter()


def _check_enabled():
    if not memory_store.enabled:
        raise HTTPException(status_code=404, detail="Memory system is disabled")


@router.get("/index")
async def get_memory_index(scope: str = Query("personal", pattern="^(personal|team)$")):
    """获取 MEMORY.md 索引内容"""
    _check_enabled()
    content = memory_store.load_index_content(scope)
    return {"scope": scope, "content": content}


@router.get("/list")
async def list_memories(
    scope: str = Query("personal", pattern="^(personal|team)$"),
    refresh: bool = Query(False, description="强制重新扫描（忽略缓存）"),
):
    """扫描记忆清单（name/description/type/mtime/scope）"""
    _check_enabled()
    entries = memory_scanner.scan_manifest(scope=scope, force=refresh)
    return {
        "scope": scope,
        "total": len(entries),
        "items": [
            {
                "filename": e.filename,
                "path": str(e.path.relative_to(memory_store.memory_root("personal")))
                if e.path.is_relative_to(memory_store.memory_root("personal"))
                else str(e.path),
                "name": e.name,
                "description": e.description,
                "type": e.memory_type,
                "scope": e.scope,
                "mtime": e.mtime,
            }
            for e in entries
        ],
    }


@router.get("/file")
async def read_memory_file(
    path: str = Query(..., description="相对记忆根目录的路径，如 user/preferences.md"),
    scope: str = Query("personal", pattern="^(personal|team)$"),
):
    """读取单个记忆文件完整内容（路径校验限定 memory 目录）"""
    _check_enabled()

    root = memory_store.memory_root(scope)
    target = (root / path).resolve()

    # 路径校验：必须在记忆目录内，防止目录穿越
    if not memory_store.is_within_memory_root(target):
        raise HTTPException(status_code=403, detail="Path is outside the memory directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Memory file not found")

    fm, content, truncated = memory_store.read_memory(target)
    stat = target.stat()
    return {
        "path": path,
        "scope": scope,
        "frontmatter": fm,
        "content": content,
        "truncated": truncated,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }


@router.get("/session/{conversation_id}")
async def get_session_memory(conversation_id: str):
    """获取指定会话的 Session Memory 内容"""
    _check_enabled()
    sess_dir = memory_store.session_memory_dir(conversation_id)
    note_path = sess_dir / SESSION_NOTE_FILENAME
    if not note_path.is_file():
        return {"conversation_id": conversation_id, "content": "", "exists": False}

    content = memory_store.read_memory(note_path)
    _fm, body, _truncated = content
    state = session_memory_manager.get_state(conversation_id)
    return {
        "conversation_id": conversation_id,
        "content": body,
        "exists": True,
        "mtime": note_path.stat().st_mtime,
        "stats": {
            "total_tokens": state.total_tokens if state else 0,
            "messages_covered": state.messages_covered if state else 0,
        } if state else None,
    }
