"""
会话管理测试（离线）
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.session.store import ConversationStore


def test_session_crud(tmp_path):
    """创建/列表/读取/追加/重命名/删除"""
    store = ConversationStore(dir_path=str(tmp_path / "sessions"))

    # 创建
    s1 = store.create(title="第一轮")
    s2 = store.create(title="第二轮")
    assert s1["title"] == "第一轮"
    assert store.get(s1["id"])["id"] == s1["id"]

    # 列表（新 → 旧）
    listed = store.list()
    assert len(listed) == 2
    assert listed[0]["id"] == s2["id"]  # 更新的在前

    # 追加
    store.append(s1["id"], "user", "你好")
    store.append(s1["id"], "assistant", "回答")
    session = store.get(s1["id"])
    assert len(session["messages"]) == 2
    assert session["messages"][1]["content"] == "回答"

    # history_for 裁剪
    for i in range(30):
        store.append(s1["id"], "user", f"m{i}")
    history = store.history_for(s1["id"], max_messages=10)
    assert len(history) == 10

    # 重命名 + 删除
    store.rename(s1["id"], "改名")
    assert store.get(s1["id"])["title"] == "改名"
    assert store.delete(s1["id"]) is True
    assert store.get(s1["id"]) is None
    assert store.delete("not_exists") is False


def test_session_missing_and_invalid(tmp_path):
    """非法/不存在的会话"""
    store = ConversationStore(dir_path=str(tmp_path / "s"))
    assert store.get("../../etc/passwd") is None  # 非法 id 直接拒绝
    assert store.append("nope", "user", "x") is None
    assert store.rename("nope", "标题") is None