"""
重排序服务
使用 bge-reranker-v2-m3 对检索结果进行精排（相关度重排）
"""

from typing import List, Optional

import httpx

from src.vector_store.base import SearchResult
from src.embedding.client import OMLXClient

from src.logging_setup import get_logger

logger = get_logger(__name__)


class Reranker:
    """重排序服务"""

    def __init__(
            self,
            client: OMLXClient,
            model: str = "bge-reranker-v2-m3",
            enabled: bool = True,
    ):
        """
        初始化重排序服务

        Args:
            client: oMLX 客户端
            model: 重排序模型名称
            enabled: 是否启用
        """
        self.client = client
        self.model = model
        self.enabled = enabled

    def rerank(
            self,
            query: str,
            results: List[SearchResult],
            top_k: Optional[int] = None,
    ) -> List[SearchResult]:
        """
        对检索结果进行重排序

        Args:
            query: 查询文本
            results: 初检索结果列表
            top_k: 保留前 k 个结果（默认保留所有）

        Returns:
            List[SearchResult]: 重排序后的结果列表（按相关度降序）
        """
        # 未启用或结果过少时，直接截取返回
        if not self.enabled or len(results) <= 1:
            return results[:top_k] if top_k else results

        # 准备重排序输入（查询与候选文档对）
        pairs = [[query, r.content] for r in results]

        try:
            scores = self._call_reranker(pairs)

            # 更新分数
            for i, result in enumerate(results):
                if i < len(scores):
                    result.score = scores[i]

            # 按分数降序排列
            sorted_results = sorted(results, key=lambda x: x.score, reverse=True)

            # 返回前 top_k 个
            return sorted_results[:top_k] if top_k else sorted_results

        except Exception as e:
            # 重排序失败时回退到原始相似度结果
            logger.warning("重排序失败: %s，返回原始结果", e)
            return results[:top_k] if top_k else results

    def _call_reranker(self, pairs: List[List[str]]) -> List[float]:
        """
        调用重排序 API，返回每个候选文档的相关性分数

        依次尝试以下接口：
        1. {base_url}/rerank（如 http://127.0.0.1:8000/v1/rerank）
        2. 去掉 base_url 尾部 /v1 后再试 /rerank
        全部失败则抛异常，由上层回退到原始结果。
        """
        payload = {
            "model": self.model,
            "query": pairs[0][0],
            "documents": [p[1] for p in pairs],
        }
        headers = {
            "Authorization": f"Bearer {self.client.api_key}",
            "Content-Type": "application/json",
        }

        # 候选接口地址列表
        urls = [f"{self.client.base_url}/rerank"]
        base = self.client.base_url.rstrip("/")
        if base.endswith("/v1"):
            urls.append(f"{base[:-3]}/rerank")  # 去掉 /v1 前缀
        # 去除重复地址
        urls = list(dict.fromkeys(urls))

        last_error: Optional[Exception] = None
        for url in urls:
            try:
                with httpx.Client(timeout=self.client.timeout) as http_client:
                    response = http_client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    data = response.json()

                # 解析分数（响应格式：{"results": [{"index": i, "relevance_score": s}]}）
                scores = [0.0] * len(pairs)
                results = data.get("results", [])
                # 响应不完整（结果条数少于候选数）时抛异常走重试/回退，而不是
                # 静默用 0 填充导致排序错误
                if len(results) < len(pairs):
                    raise ValueError(
                        f"重排序响应不完整: 候选 {len(pairs)} 条, 返回 {len(results)} 条"
                    )
                for item in results:
                    idx = item.get("index")
                    score = item.get("relevance_score", 0.0)
                    # 拒绝负数与越界 index，避免负索引静默写错位置
                    if idx is None or not isinstance(idx, int) or not (0 <= idx < len(scores)):
                        raise ValueError(f"重排序响应 index 非法: {idx}")
                    scores[idx] = score
                return scores

            except httpx.HTTPStatusError as e:
                # 404 说明该地址没有此接口，继续尝试下一个；其余状态码直接抛出
                if e.response.status_code == 404:
                    last_error = e
                    continue
                raise
            except Exception as e:
                raise

        # 所有接口都不可用
        raise last_error if last_error else RuntimeError("重排序接口不可用")