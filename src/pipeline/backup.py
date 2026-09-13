"""
每日自动备份（4：数据安全）

- 把「全部会话 + 索引清单摘要」写为 data/backups/rag-backup-<时间戳>.json
- 保留最近 keep 份（默认 7），更早删除
- 环境变量：
  RAG_BACKUP     0 关闭，默认开启
  RAG_BACKUP_KEEP 保留份数（默认 7）
  RAG_BACKUP_INTERVAL 秒（默认 86400=每天一次）
- 后台守护线程；启动时先做一次，之后按间隔循环；出错只记 warning，不影响服务
"""

import glob
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.logging_setup import get_logger

logger = get_logger("rag.backup")


def _prune(backups_dir: Path, keep: int) -> None:
    """按文件名时间戳保留最新 keep 份

    注意：keep<=0 时若直接「删除全部」，会把刚刚写出的这份备份也删掉
    （RAG_BACKUP_KEEP=0 或负数即触发），因此下界钳到 1——至少要留一份。
    """
    keep = max(1, keep)
    files = sorted(glob.glob(str(backups_dir / "rag-backup-*.json")))
    for old in files[:-keep]:
        try:
            Path(old).unlink()
            logger.info("已清理旧备份: %s", old)
        except OSError as e:
            logger.warning("清理旧备份失败 %s: %s", old, e)


def run_once(conversation_store, backups_dir: str, keep: int) -> Optional[str]:
    """执行一次备份，返回备份文件路径（失败返回 None）"""
    try:
        data: Dict[str, Any] = {
            "backup_at": datetime.now().isoformat(timespec="seconds"),
            "sessions": (conversation_store.export_all() if conversation_store is not None else {"count": 0, "sessions": []}),
        }
        path_dir = Path(backups_dir)
        path_dir.mkdir(parents=True, exist_ok=True)
        # 文件名带微秒：同一秒内两次备份不会互相覆盖
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = path_dir / f"rag-backup-{stamp}.json"

        # 原子写（与 session/store.py、index_sync.py 一致）：
        # 先写临时文件再 replace，避免备份进程被中断时留下半写 JSON
        # ——备份文件正是「出事后用来恢复」的东西，不能自己先坏。
        fd, tmp_path = tempfile.mkstemp(dir=str(path_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        _prune(path_dir, keep)
        logger.info("备份完成: %s", path)
        return str(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("备份失败（忽略，不影响服务）: %s", e)
        return None


def start_backup_loop(
    conversation_store,
    backups_dir: str = "./data/backups",
    interval: int = 86400,
    keep: int = 7,
) -> threading.Thread:
    """启动每日备份守护线程（启动先备份一次）"""
    keep = max(1, keep)  # 下界保护：至少保留 1 份

    def _loop():
        run_once(conversation_store, backups_dir, keep)
        while True:
            time.sleep(max(60, interval))
            run_once(conversation_store, backups_dir, keep)

    t = threading.Thread(target=_loop, daemon=True, name="rag-daily-backup")
    t.start()
    return t