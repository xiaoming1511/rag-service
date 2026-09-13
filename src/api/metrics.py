"""
请求指标（P2：/v1/status 的最近请求观测）

轻量内存环形缓冲：中间件每完成/异常一个请求推一条记录；
`snapshot()` 供 /v1/status 返回最近 N 条 + 汇总（总数/错误数/平均与最大耗时）。
进程重启清零，无需持久化（观测用）。
"""

import time
from collections import deque
from threading import Lock
from typing import Any, Dict, List, Optional

_POOL: deque = deque(maxlen=200)
# snapshot() 迭代 deque 时若并发 record() 触发 maxlen 淘汰，会抛
# "deque mutated during iteration"；加锁保护读写一致性
_LOCK = Lock()


def record(method: str, path: str, status: int, latency_ms: float, ts: Optional[float] = None) -> None:
    entry = {"ts": ts if ts is not None else time.time(), "method": method, "path": path,
             "status": status, "latency_ms": round(latency_ms, 1)}
    with _LOCK:
        _POOL.append(entry)


def clear() -> None:
    with _LOCK:
        _POOL.clear()


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        items = list(_POOL)
    if not items:
        return {"total": 0, "errors": 0, "avg_latency_ms": 0.0, "max_latency_ms": 0.0, "recent": []}
    lat: List[float] = [r["latency_ms"] for r in items]
    return {
        "total": len(items),
        "errors": sum(1 for r in items if r["status"] >= 400),
        "avg_latency_ms": round(sum(lat) / len(lat), 1),
        "max_latency_ms": round(max(lat), 1),
        "recent": items[-10:],
    }