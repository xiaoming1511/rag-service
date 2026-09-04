"""
会话管理路由（决策：服务端 REST + JSON）
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

_pipeline = None


def set_pipeline(pipeline):
    global _pipeline
    _pipeline = pipeline


def _store():
    store = getattr(_pipeline, "conversation_store", None) if _pipeline else None
    if store is None:
        raise HTTPException(status_code=503, detail="会话存储未初始化")
    return store


class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class RenameSessionRequest(BaseModel):
    title: str


class AppendMessageRequest(BaseModel):
    role: str = "user"  # user | assistant
    content: str


@router.get("")
async def list_sessions():
    """会话列表（新 → 旧）"""
    return {"sessions": _store().list()}


@router.post("")
async def create_session(request: CreateSessionRequest):
    """创建会话"""
    session = _store().create(title=request.title)
    return session


@router.get("/{session_id}")
async def get_session(session_id: str):
    """读取会话（含全部消息）"""
    session = _store().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.post("/{session_id}/messages")
async def append_message(session_id: str, request: AppendMessageRequest):
    """向会话追加一条消息"""
    session = _store().append(session_id, request.role, request.content)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.post("/{session_id}/rename")
async def rename_session(session_id: str, request: RenameSessionRequest):
    """重命名会话"""
    session = _store().rename(session_id, request.title)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    """删除会话"""
    ok = _store().delete(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "session_id": session_id}