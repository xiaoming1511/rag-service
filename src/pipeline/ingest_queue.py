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

from src.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class IndexJob:
    """摄入任务"""
    id: str
    kind: str  # full | incremental | url
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = "queued"  # queued | running | done | failed | cancelled
    progress: str = ""
    progress_data: Optional[Dict[str, Any]] = None  # 结构化进度 {stage,current,total,percent,note}
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
            "progress_data": self.progress_data,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class IngestQueue:
    """单 worker 摄入队列（磁盘持久化）"""

    # 进度落盘限频：进度 tick 可能每文档一次，每次全量重写 jobs JSON
    # 会把磁盘写成热点；内存态始终最新，落盘最多滞后该间隔（任务收尾必落盘）。
    PROGRESS_PERSIST_INTERVAL = 1.0

    def __init__(
            self,
            run_fn: Callable[[IndexJob], Dict[str, Any]],
            store_path: str = "./data/index_jobs.json",
            max_finished_jobs: int = 100,
    ):
        """
        初始化队列

        Args:
            run_fn: 任务执行函数（接收 IndexJob，返回结果 dict；抛异常视为失败）
            store_path: 任务持久化文件；重启时恢复 queued/running → queued
            max_finished_jobs: 终态（done/failed/cancelled）任务保留上限，
                超出裁剪最旧的（内存与持久化文件同步收敛，防跨重启无限累积）
        """
        self.run_fn = run_fn
        self.store_path = Path(store_path)
        self.max_finished_jobs = max(1, int(max_finished_jobs))
        self._lock = threading.Lock()
        self._jobs: List[IndexJob] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_progress_save = 0.0
        self._load()
        with self._lock:
            if self._prune_locked():
                self._save()

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
                    job.progress_data = {"stage": "恢复", "current": 0, "total": 0,
                                         "percent": 0.0, "note": "中断恢复", "updated_at": time.time()}
                self._jobs.append(job)
            if self._jobs:
                logger.info("♻️ 摄入队列已恢复 %d 个历史任务", len(self._jobs))
        except Exception as e:
            logger.warning("摄入队列持久化文件读取失败（忽略）: %s", e)

    def _save(self):
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump({"jobs": [j.to_dict() for j in self._jobs]}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("摄入队列持久化写入失败: %s", e)

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
            self._prune_locked()
            self._save()
        self._ensure_worker()
        return job

    @staticmethod
    def _snapshot(job: IndexJob) -> IndexJob:
        """任务快照：公开查询返回副本，避免 API 线程读到 worker 撕裂中的状态，
        也防止调用方误改内部对象（params/progress_data/result 均为整体替换，浅拷贝安全）"""
        return IndexJob(**job.to_dict())

    def get(self, job_id: str) -> Optional[IndexJob]:
        """按 ID 查询任务（返回快照副本）"""
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            return self._snapshot(job) if job is not None else None

    def list(self, limit: int = 50) -> List[IndexJob]:
        """列出最近任务（新 -> 旧，快照副本）"""
        with self._lock:
            return [self._snapshot(j) for j in reversed(self._jobs[-limit:])]

    def cancel(self, job_id: str) -> bool:
        """
        取消任务
        - queued 任务直接取消
        - running 任务标记取消（执行函数需自行检查 job.status），完成后记为 cancelled
        """
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None:
                return False
            if job.status in ("queued", "running"):
                job.status = "cancelled"
                self._save()
        return True

    def is_cancelled(self, job_id: str) -> bool:
        """供执行函数在长任务中轮询取消状态"""
        job = self.get(job_id)
        return job is not None and job.status == "cancelled"

    def update_progress(self, job: IndexJob, stage: str, current: int = 0,
                        total: int = 0, note: str = ""):
        """
        更新任务的结构化进度（作业进度条）：

        progress_data = {stage, current, total, percent, note, updated_at}
        progress（字符串版）同步为 "阶段 cur/total (p%)"，兼容旧调用方。
        """
        percent = round(current / total * 100, 1) if total else 0.0
        with self._lock:
            job.progress_data = {
                "stage": stage,
                "current": current,
                "total": total,
                "percent": percent,
                "note": note,
                "updated_at": time.time(),
            }
            job.progress = f"{stage} {current}/{total} ({percent:.0f}%)" if total else f"{stage}"
            # D6-3：限频落盘——内存态每次都更新，磁盘最多滞后一个间隔；
            # 任务收尾（完成/失败/取消）在锁内必然 _save。
            now = time.time()
            if now - self._last_progress_save >= self.PROGRESS_PERSIST_INTERVAL:
                self._last_progress_save = now
                self._save()

    def _prune_locked(self) -> bool:
        """裁剪超上限的终态任务（须持锁调用）；返回是否发生裁剪"""
        finished = [j for j in self._jobs if j.status in ("done", "failed", "cancelled")]
        if len(finished) <= self.max_finished_jobs:
            return False
        drop = {id(j) for j in finished[:-self.max_finished_jobs]}
        self._jobs = [j for j in self._jobs if id(j) not in drop]
        return True

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
                    # D6-3：状态迁移必须在锁内完成——查询线程与 _save 的序列化
                    # 都在同一把锁下读这些字段，锁外写入会让并发读者看到撕裂态。
                    job.status = "running"
                    job.started_at = time.time()
                    job.progress = "执行中"
                    self._save()
                else:
                    # 队列已空：必须在同一把锁内置空 self._thread 再退出。
                    # 否则 submit() 可能在本线程「已决定退出、尚未真正结束」
                    # 的窗口内看到旧的 is_alive()==True 而跳过启动新 worker，
                    # 导致刚入队的任务永远停留在 queued（静默卡死）。
                    self._thread = None

            if job is None:
                break  # 无任务，退出（下次 submit 会重启 worker）

            try:
                result = self.run_fn(job)
                with self._lock:
                    job.result = result
                    # 执行中可能已被 cancel() 置为 cancelled，统一收尾
                    if job.status == "cancelled":
                        job.error = "任务已取消"
                        job.result = None
                    else:
                        job.status = "done"
                    job.finished_at = time.time()
                    self._prune_locked()
                    self._save()
            except Exception as e:
                # 取消导致的异常优先保留 cancelled 状态，不要覆盖成 failed
                with self._lock:
                    if job.status == "cancelled":
                        job.error = "任务已取消"
                        job.result = None
                    else:
                        job.status = "failed"
                        job.error = str(e)
                        logger.error("❌ 摄入任务失败 [%s/%s]: %s", job.kind, job.id, e)
                    job.finished_at = time.time()
                    self._prune_locked()
                    self._save()

    def shutdown(self):
        """停止 worker（等当前任务完成）"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)