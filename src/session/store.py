"""
会话管理
data/conversations/{id}.json 持久化，多会话支持（决策：服务端 REST + JSON）
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional


class ConversationStore:
    """会话存储（JSON 文件，进程重启不丢失）"""

    def __init__(self, dir_path: str = "./data/conversations"):
        self.dir_path = Path(dir_path)
        self.dir_path.mkdir(parents=True, exist_ok=True)
        # 读-改-写（append/truncate/rename）非原子，并发对同一会话操作会
        # lost update（消息静默丢失）；用全局锁串行化所有写操作。
        # 个人本地场景会话操作极快，全局锁的串行化开销可忽略。
        self._lock = Lock()

    # ---------- 工具 ----------
    @staticmethod
    def _valid(sid: str) -> bool:
        return bool(sid) and len(sid) <= 64 and all(
            c.isalnum() or c in "-_" for c in sid
        )

    def _path(self, sid: str) -> Path:
        return self.dir_path / f"{sid}.json"

    def _write(self, sid: str, data: Dict[str, Any]):
        self.dir_path.mkdir(parents=True, exist_ok=True)
        # 用唯一临时文件名 + 原子 replace，避免并发/中断读到半写或 tmp 冲突
        tmp = self.dir_path / f".{sid}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(sid))
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ---------- CRUD ----------
    def create(self, title: str = "新对话") -> Optional[Dict[str, Any]]:
        sid = uuid.uuid4().hex[:12]
        data = {
            "id": sid,
            "title": (title or "新对话").strip()[:50],
            "messages": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._write(sid, data)
        return data

    @staticmethod
    def _mtime(p: Path) -> float:
        """安全取文件 mtime：文件可能在本进程 glob 之后被删除（并发清理），
        直接 stat 会抛 FileNotFoundError 并冒泡成 API 500——排序键失败不应
        让整个会话列表接口挂掉，取不到就按 0（排到最后）处理。"""
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    def list(self) -> List[Dict[str, Any]]:
        """按更新时间倒序返回会话（不含消息体，仅元信息）"""
        items = []
        for f in sorted(self.dir_path.glob("*.json"),
                        key=self._mtime, reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                items.append({
                    "id": data.get("id", f.stem),
                    "title": data.get("title", "未命名"),
                    "message_count": len(data.get("messages", [])),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                })
            except Exception:
                continue
        return items

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        if not self._valid(sid):
            return None
        path = self._path(sid)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def append(self, sid: str, role: str, content: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self.get(sid)
            if data is None:
                return None
            data.setdefault("messages", []).append({
                "role": role, "content": content, "ts": time.time(),
            })
            data["updated_at"] = time.time()
            self._write(sid, data)
            return data

    def truncate(self, sid: str, keep_count: int) -> Optional[Dict[str, Any]]:
        """保留前 keep_count 条消息（供插件「撤回/编辑重发」截断后续消息）

        边界：keep_count 为负数时按 0 处理（等价于清空消息），而不是
        「负数比较为假 → 静默什么都不做」——后者会让调用方以为截断已生效。
        """
        keep_count = max(0, keep_count)
        with self._lock:
            data = self.get(sid)
            if data is None:
                return None
            msgs = data.get("messages", [])
            if keep_count < len(msgs):
                data["messages"] = msgs[:keep_count]
                data["updated_at"] = time.time()
                self._write(sid, data)
            return data

    def rename(self, sid: str, title: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self.get(sid)
            if data is None:
                return None
            data["title"] = (title or "未命名").strip()[:50]
            self._write(sid, data)
            return data

    def delete(self, sid: str) -> bool:
        if not self._valid(sid):
            return False
        with self._lock:
            path = self._path(sid)
            if path.exists():
                path.unlink()
                return True
            return False

    def history_for(self, sid: str, max_messages: int = 20) -> List[Dict[str, str]]:
        """取会话历史（消息裁剪后供查询接口使用）"""
        data = self.get(sid)
        if not data:
            return []
        msgs = data.get("messages", [])
        return [
            {"role": m.get("role"), "content": m.get("content")}
            for m in msgs[-max_messages:]
        ]

    def cleanup(self, keep: int = 100) -> List[str]:
        """按最近更新时间保留最新 keep 条，删除更早的会话；返回被删除 id 列表

        下界保护：keep<=0 会删除**全部**会话（不可逆），钳到 1 避免误传参数
        清空历史。
        """
        keep = max(1, keep)
        items = sorted(self.list(), key=lambda x: (x.get("updated_at") or 0), reverse=True)
        removed: List[str] = []
        for it in items[keep:]:
            sid = it.get("id")
            if sid and self.delete(sid):
                removed.append(sid)
        return removed

    def export_all(self) -> Dict[str, Any]:
        """导出全部会话（完整消息）"""
        sessions = []
        for meta in self.list():
            data = self.get(meta["id"])
            if data:
                sessions.append(data)
        return {"exported_at": time.time(), "count": len(sessions), "sessions": sessions}