"""
相同问题响应缓存（决策 D7）

以「问题 + top_k + use_rerank」为键缓存非流式查询结果，
命中时直接返回，省去整条检索-生成链路。

注意：
- 仅对无历史（单轮）查询生效——含历史的问题结果依赖上下文，不做缓存
- 结果依赖向量库内容，使用 TTL 缓解索引更新后的陈旧问题
- 流式接口不缓存（只缓非流式 /v1/query）
"""

import hashlib
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, Optional, Tuple


class ResponseCache:
    """LRU + TTL 的响应缓存"""

    def __init__(self, enabled: bool = True, ttl: int = 3600, capacity: int = 512):
        """
        初始化响应缓存

        Args:
            enabled: 是否启用
            ttl: 缓存有效期（秒），过期自动失效
            capacity: LRU 容量（超出后淘汰最久未用的键）
        """
        self.enabled = enabled
        self.ttl = ttl
        self.capacity = max(1, capacity)
        self._cache: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        # OrderedDict 非线程安全：move_to_end/popitem 并发会破坏内部链表。
        # RAGPipeline.query 经 asyncio.to_thread 在多线程池并发调用，需加锁。
        self._lock = Lock()

    def enable(self, enabled: bool = True):
        """动态开关缓存"""
        with self._lock:
            self.enabled = enabled
            if not enabled:
                self._cache.clear()

    # ================================================================
    # 键与存取
    # ================================================================

    @staticmethod
    def make_key(question: str, top_k: Optional[int], use_rerank: bool) -> str:
        """生成缓存键（问题 + 检索参数）"""
        raw = f"{question}|{top_k}|{use_rerank}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        """获取缓存值；未命中或过期返回 None"""
        if not self.enabled:
            return None

        with self._lock:
            item = self._cache.get(key)
            if item is None:
                return None

            created_at, value = item
            if self.ttl > 0 and time.time() - created_at > self.ttl:
                # 已过期：删除并视为未命中
                self._cache.pop(key, None)
                return None

            # LRU：把命中的键移到末尾
            self._cache.move_to_end(key)
            return value

    def put(self, key: str, value: Any):
        """写入缓存（LRU 淘汰）"""
        if not self.enabled:
            return

        with self._lock:
            self._cache[key] = (time.time(), value)
            self._cache.move_to_end(key)

            # 超出容量：淘汰最久未用的键（队首）
            while len(self._cache) > self.capacity:
                self._cache.popitem(last=False)

    # ================================================================
    # 状态
    # ================================================================

    def clear(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()

    def stats(self) -> Dict[str, Any]:
        """缓存统计"""
        with self._lock:
            return {
                "enabled": self.enabled,
                "capacity": self.capacity,
                "size": len(self._cache),
                "ttl": self.ttl,
            }