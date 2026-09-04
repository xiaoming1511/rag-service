"""
会话管理
data/conversations/{id}.json 持久化，多会话支持（决策：服务端 REST + JSON）
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConversationStore:
    """会话存储（JSON 文件，进程重启不丢失）"""

    def __init__(self, dir_path: str = "./data/conversations"):
        self.dir_path = Path(dir_path)
        self.dir_path.mkdir(parents=True, exist_ok=True)

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
        temp = self._path(sid).with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp.replace(self._path(sid))

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

    def list(self) -> List[Dict[str, Any]]:
        """按更新时间倒序返回会话（不含消息体，仅元信息）"""
        items = []
        for f in sorted(self.dir_path.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
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
        data = self.get(sid)
        if data is None:
            return None
        data.setdefault("messages", []).append({
            "role": role, "content": content, "ts": time.time(),
        })
        data["updated_at"] = time.time()
        self._write(sid, data)
        return data

    def rename(self, sid: str, title: str) -> Optional[Dict[str, Any]]:
        data = self.get(sid)
        if data is None:
            return None
        data["title"] = (title or "未命名").strip()[:50]
        self._write(sid, data)
        return data

    def delete(self, sid: str) -> bool:
        if not self._valid(sid):
            return False
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