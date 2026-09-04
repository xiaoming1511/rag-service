"""
后台摄入队列

决策已确认：单 worker 串行（避免打爆本地 oMLX）+ 任务状态磁盘持久化
（进程重启可从崩溃恢复）+ 进度查询 / 取消。

任务类型：
- full:        全量重建（rebuild 可选）
- incremental: 增量同步
- url:         网页索引（参数 url）
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class IndexJob:
    """摄入任务"""
    id: str
    kind: str  # full | incremental | url
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = "queued"  # queued | running | done | failed | cancelled
    progress: str = ""
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "status": self.status,
            "progress": self.progress,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class IngestQueue:
    """单 worker 摄入队列（磁盘持久化）"""

    def __init__(
            self,
            run_fn: Callable[[IndexJob], Dict[str, Any]],
            store_path: str = "./data/index_jobs.json",
    ):
        """
        初始化队列

        Args:
            run_fn: 任务执行函数（接收 IndexJob，返回结果 dict；抛异常视为失败）
            store_path: 任务持久化文件；重启时恢复 queued/running → queued
        """
        self.run_fn = run_fn
        self.store_path = Path(store_path)
        self._lock = threading.Lock()
        self._jobs: List[IndexJob] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    # ================================================================
    # 持久化
    # ================================================================

    def _load(self):
        """从磁盘恢复任务（queued/running → queued 以便重跑）"""
        if not self.store_path.exists():
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("jobs", []):
                job = IndexJob(**{k: v for k, v in item.items() if k in IndexJob.__dataclass_fields__})
                if job.status in ("queued", "running"):
                    job.status = "queued"  # 崩溃恢复：未完成任务重新排队
                    job.progress = "已从上次中断恢复，重新排队"
                self._jobs.append(job)
            if self._jobs:
                print(f"♻️ 摄入队列已恢复 {len(self._jobs)} 个历史任务")
        except Exception as e:
            print(f"⚠️ 摄入队列持久化文件读取失败（忽略）: {e}")

    def _save(self):
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump({"jobs": [j.to_dict() for j in self._jobs]}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 摄入队列持久化写入失败: {e}")

    # ================================================================
    # 提交与查询
    # ================================================================

    def submit(self, kind: str, params: Optional[Dict[str, Any]] = None) -> IndexJob:
        """提交任务并入队，返回任务对象"""
        job = IndexJob(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            params=params or {},
        )
        with self._lock:
            self._jobs.append(job)
            self._save()
        self._ensure_worker()
        return job

    def get(self, job_id: str) -> Optional[IndexJob]:
        """按 ID 查询任务"""
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def list(self, limit: int = 50) -> List[IndexJob]:
        """列出最近任务（新 -> 旧）"""
        with self._lock:
            return list(reversed(self._jobs[-limit:]))

    def cancel(self, job_id: str) -> bool:
        """
        取消任务
        - queued 任务直接取消
        - running 任务标记取消（执行函数需自行检查 job.status），完成后记为 cancelled
        """
        job = self.get(job_id)
        if job is None:
            return False
        with self._lock:
            if job.status in ("queued", "running"):
                job.status = "cancelled"
                self._save()
        return True

    def is_cancelled(self, job_id: str) -> bool:
        """供执行函数在长任务中轮询取消状态"""
        job = self.get(job_id)
        return job is not None and job.status == "cancelled"

    # ================================================================
    # 单 worker
    # ================================================================

    def _ensure_worker(self):
        """确保 worker 线程在运行（守护线程）"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._work, daemon=True)
            self._thread.start()

    def _work(self):
        """worker 主循环：串行执行队列中的任务"""
        while not self._stop.is_set():
            job = None
            with self._lock:
                for j in self._jobs:
                    if j.status == "queued":
                        job = j
                        break
                if job is not None:
                    job.status = "running"
                    job.started_at = time.time()
                    self._save()

            if job is None:
                break  # 无任务，退出（下次 submit 会重启 worker）

            try:
                job.progress = "执行中"
                result = self.run_fn(job)
                job.result = result
                # 执行中可能已被取消（执行函数检查后主动失败），统一收尾
                if job.status == "cancelled":
                    job.error = "任务已取消"
                    job.result = None
                else:
                    job.status = "done"
            except Exception as e:
                job.status = "failed"
                job.error = str(e)
                print(f"❌ 摄入任务失败 [{job.kind}/{job.id}]: {e}")
            finally:
                job.finished_at = time.time()
                with self._lock:
                    self._save()

    def shutdown(self):
        """停止 worker（等当前任务完成）"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)