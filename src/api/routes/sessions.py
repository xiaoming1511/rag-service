"""
会话管理路由（决策：服务端 REST + JSON）

本模块端点声明为 def（非 async def）：会话存储是
data/conversations/*.json 的阻塞文件 I/O（读-改-写整文件），
写在 async def 里会冻结事件循环；def 端点由 Starlette 自动放入
线程池执行，与 /v1/query、/v1/index 等端点的约定一致。
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

# 允许写入的角色白名单：避免任意字符串进入会话文件（前端按 role 渲染气泡，
# 脏值会让历史渲染出未定义样式，也会污染后续 history 回传给模型的 role 字段）
_ALLOWED_ROLES = ("user", "assistant", "system")

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

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _ALLOWED_ROLES:
            raise ValueError(f"role 仅支持 {', '.join(_ALLOWED_ROLES)}")
        return v


class TruncateRequest(BaseModel):
    keep_count: int = 0  # 保留前 N 条消息，其余截断（负数按 0 处理，见 store.truncate）


@router.get("")
def list_sessions():
    """会话列表（新 → 旧）"""
    return {"sessions": _store().list()}


@router.post("")
def create_session(request: CreateSessionRequest):
    """创建会话"""
    session = _store().create(title=request.title)
    return session


class CleanupRequest(BaseModel):
    keep: int = 100  # 保留最近更新的 N 条，删除更早的


@router.post("/cleanup")
def cleanup_sessions(request: CleanupRequest):
    """清理旧会话（D：保留最新 keep 条，其余删除）

    下界钳到 1 与 `ConversationStore.cleanup` 的内部保护保持一致：
    此前路由钳到 0 而 store 内部钳到 1，传 keep=0 时会**实际保留 1 条
    却回报 keep: 0**——响应契约与实际行为不符。
    """
    keep = max(1, min(request.keep, 10000))
    removed = _store().cleanup(keep=keep)
    return {"ok": True, "keep": keep, "deleted": removed, "deleted_count": len(removed)}


@router.get("/export")
def export_sessions():
    """导出全部会话（完整消息，JSON）——便于备份/迁移"""
    return _store().export_all()


@router.get("/{session_id}")
def get_session(session_id: str):
    """读取会话（含全部消息）"""
    session = _store().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.post("/{session_id}/messages")
def append_message(session_id: str, request: AppendMessageRequest):
    """向会话追加一条消息"""
    session = _store().append(session_id, request.role, request.content)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.post("/{session_id}/truncate")
def truncate_session(session_id: str, request: TruncateRequest):
    """截断会话：仅保留前 keep_count 条消息（撤回/编辑重发）"""
    session = _store().truncate(session_id, request.keep_count)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.post("/{session_id}/rename")
def rename_session(session_id: str, request: RenameSessionRequest):
    """重命名会话"""
    session = _store().rename(session_id, request.title)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@router.delete("/{session_id}")
def delete_session(session_id: str):
    """删除会话"""
    ok = _store().delete(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "session_id": session_id}