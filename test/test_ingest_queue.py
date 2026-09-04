"""
后台摄入队列测试（离线：桩执行函数 + 临时持久化文件）

覆盖：提交执行、状态流转、取消、磁盘持久化恢复
"""

import sys
import time
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.ingest_queue import IngestQueue


def _wait_until(queue, job_id, status, timeout=5.0):
    """轮询等待任务进入指定状态"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = queue.get(job_id)
        if job is not None and job.status == status:
            return job
        time.sleep(0.05)
    raise AssertionError(f"任务 {job_id} 未在 {timeout}s 内进入 {status} 状态")


def test_queue_submit_and_run(tmp_path):
    """提交的任务应由 worker 执行并完成"""
    executed = []

    def run(job):
        executed.append(job.kind)
        return {"total_chunks": 42}

    queue = IngestQueue(run_fn=run, store_path=str(tmp_path / "jobs.json"))
    job = queue.submit("incremental")

    _wait_until(queue, job.id, "done")
    done = queue.get(job.id)
    assert done.status == "done"
    assert done.result == {"total_chunks": 42}
    assert executed == ["incremental"]


def test_queue_failure(tmp_path):
    """执行抛异常 → 任务标记 failed 并记录错误"""

    def run(job):
        raise RuntimeError("模拟失败")

    queue = IngestQueue(run_fn=run, store_path=str(tmp_path / "jobs.json"))
    job = queue.submit("full")

    _wait_until(queue, job.id, "failed")
    failed = queue.get(job.id)
    assert "模拟失败" in failed.error


def test_queue_cancel_queued(tmp_path):
    """排队中的任务可取消且不被执行"""
    executed = []

    def run(job):
        if job.kind == "url":
            time.sleep(2)  # 阻塞 worker，让第二个任务停留在排队状态
        executed.append(job.id)
        return {}

    queue = IngestQueue(run_fn=run, store_path=str(tmp_path / "jobs.json"))
    # 第一个任务占住 worker
    blocker = queue.submit("url", {"url": "http://slow"})
    cancellable = queue.submit("full")

    # 等 blocker 进入 running
    _wait_until(queue, blocker.id, "running")

    assert queue.cancel(cancellable.id) is True
    time.sleep(0.3)
    cancelled = queue.get(cancellable.id)
    assert cancelled.status == "cancelled"
    assert cancellable.id not in executed
    # 清理阻塞任务，避免后台线程残留
    queue.cancel(blocker.id)
    queue.shutdown()


def test_queue_persistence_reload(tmp_path):
    """磁盘持久化：重建队列实例后历史任务仍在，未完成任务恢复 queued"""
    store = str(tmp_path / "jobs.json")

    def run(job):
        time.sleep(0.3)  # 模拟长任务
        return {}

    queue1 = IngestQueue(run_fn=run, store_path=store)
    job = queue1.submit("full")
    _wait_until(queue1, job.id, "done")
    queue1.shutdown()

    # 重建队列实例（模拟重启）
    queue2 = IngestQueue(run_fn=run, store_path=store)
    restored = queue2.get(job.id)
    assert restored is not None
    assert restored.status == "done"  # 已完成任务保持 done


def test_queue_restores_running_as_queued(tmp_path):
    """崩溃恢复：running 任务重启后回到 queued 可重跑"""
    store = str(tmp_path / "jobs.json")
    executed = []

    def run(job):
        executed.append(job.id)
        return {}

    queue1 = IngestQueue(run_fn=run, store_path=store)
    # 直接模拟"写入一个 running 状态的任务"（手工构造持久化文件）
    queue1.shutdown()
    import json
    store_path = Path(store)
    store_path.write_text(json.dumps({"jobs": [{
        "id": "abc", "kind": "full", "params": {}, "status": "running",
        "progress": "", "error": None, "result": None,
        "created_at": 0, "started_at": None, "finished_at": None,
    }]}), encoding="utf-8")

    queue2 = IngestQueue(run_fn=run, store_path=store)
    restored = queue2.get("abc")
    assert restored.status == "queued"  # 恢复为排队，可重跑
    job2 = queue2.submit("incremental")
    _wait_until(queue2, job2.id, "done")


def test_queue_list_and_get_missing(tmp_path):
    """列表与缺失查询"""

    def run(job):
        return {}

    queue = IngestQueue(run_fn=run, store_path=str(tmp_path / "jobs.json"))
    queue.submit("incremental")
    assert queue.get("not_exists") is None
    assert len(queue.list()) == 1
    assert queue.cancel("not_exists") is False