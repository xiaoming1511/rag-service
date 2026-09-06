"""
检索服务
整合向量检索、BM25 稀疏检索（混合检索 B3）、RRF 融合与重排序
"""

from typing import List, Optional, Dict, Any

from src.vector_store.base import BaseVectorStore, SearchResult
from src.embedding.embedder import Embedder
from src.retrieval.reranker import Reranker
from src.retrieval.bm25_index import BM25Index, rrf_fuse
from src.retrieval.context_builder import build_context


class Retriever:
    """检索服务"""

    def __init__(
            self,
            vector_store: BaseVectorStore,
            embedder: Embedder,
            reranker: Optional[Reranker] = None,
            top_k: int = 5,
            rerank_top_k: int = 3,
            similarity_threshold: float = 0.5,
            rerank_threshold: float = 0.0,
            bm25_index: Optional[BM25Index] = None,
            hybrid: bool = True,
            hybrid_candidates: int = 20,
            rrf_k: int = 60,
    ):
        """
        初始化检索服务

        Args:
            vector_store: 向量存储
            embedder: 嵌入服务
            reranker: 重排序服务（可选）
            top_k: 初检索返回数量
            rerank_top_k: 重排序后保留数量
            similarity_threshold: 相似度阈值（rerank 前，仅作用于稠密
                cosine 分数；BM25 路径的候选不受此阈值约束，由 rerank 兜底）
            rerank_threshold: 重排序后分数阈值（低于此值的结果被丢弃）；
                0 表示关闭（默认）。rerank 后分数语义已变为 reranker 相关性分，
                与 cosine 阈值不可混用；建议评测基线建立后再启用（0.2~0.3 起试）
            bm25_index: BM25 稀疏索引（可选；提供且 hybrid=True 时启用混合检索）
            hybrid: 是否启用混合检索（稠密 + BM25 RRF 融合）
            hybrid_candidates: 每路召回的候选数量（融合前）
            rrf_k: RRF 融合常数（默认 60）
        """
        self.vector_store = vector_store
        self.embedder = embedder
        self.reranker = reranker
        self.top_k = top_k
        self.rerank_top_k = rerank_top_k
        self.similarity_threshold = similarity_threshold
        self.rerank_threshold = rerank_threshold
        self.bm25_index = bm25_index
        self.hybrid = hybrid and bm25_index is not None
        self.hybrid_candidates = hybrid_candidates
        self.rrf_k = rrf_k

    # ------------------------------------------------------------------
    # 内部：各路召回
    # ------------------------------------------------------------------

    def _dense_search(self, query_embedding: List[float],
                      candidates: int) -> List[SearchResult]:
        """稠密检索（cosine），应用相似度阈值"""
        results = self.vector_store.search(
            query_embedding=query_embedding,
            top_k=candidates,
        )
        if self.similarity_threshold > 0:
            results = [r for r in results if r.score >= self.similarity_threshold]
        return results

    def _recall(self, query: str, query_embedding: List[float],
                candidates: int) -> List[SearchResult]:
        """
        召回阶段：稠密单路 或 混合两路 RRF 融合

        融合只影响排序；候选对象优先取稠密结果实例（自带 cosine 分），
        BM25 独有候选用 BM25 实例补齐。
        """
        dense = self._dense_search(query_embedding, candidates)

        if not self.hybrid:
            return dense

        assert self.bm25_index is not None
        sparse = self.bm25_index.search(query, top_n=candidates)

        by_id_dense = {r.id: r for r in dense}
        by_id_sparse = {r.id: r for r in sparse}
        fused_ids = rrf_fuse(
            [[r.id for r in dense], [r.id for r in sparse]],
            k=self.rrf_k,
            top_n=candidates,
        )
        return [
            by_id_dense[fid] if fid in by_id_dense else by_id_sparse[fid]
            for fid in fused_ids
            if fid in by_id_dense or fid in by_id_sparse
        ]

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def retrieve(
            self,
            query: str,
            top_k: Optional[int] = None,
            where: Optional[Dict[str, Any]] = None,
            use_rerank: bool = True,
    ) -> List[SearchResult]:
        """
        检索相关文档

        Args:
            query: 查询文本
            top_k: 返回结果数量（默认使用配置值）
            where: 过滤条件
            use_rerank: 是否使用重排序

        Returns:
            List[SearchResult]: 检索结果列表
        """
        query_embedding = self.embedder.embed_single(query)

        initial_top_k = top_k or self.top_k
        # 混合/重排序路径需要更多候选，融合后再截断
        candidates = max(initial_top_k, self.hybrid_candidates if self.hybrid else initial_top_k)
        results = self._recall(query, query_embedding, candidates)

        # 重排序（如果启用且有重排序器）
        if use_rerank and self.reranker and self.reranker.enabled and len(results) > 1:
            results = self.reranker.rerank(
                query=query,
                results=results,
                top_k=self.rerank_top_k,
            )
            # 重排序后二次过滤：此时 score 已是 reranker 相关性分（与
            # similarity_threshold 的 cosine 语义不同），用独立阈值
            if self.rerank_threshold > 0:
                results = [r for r in results if r.score >= self.rerank_threshold]
        else:
            # 如果不使用重排序，截取 top_k
            results = results[:top_k] if top_k else results

        return results

    def retrieve_with_context(
            self,
            query: str,
            top_k: Optional[int] = None,
            where: Optional[Dict[str, Any]] = None,
            use_rerank: bool = True,
            max_context_tokens: int = 4000,
    ) -> tuple[str, List[SearchResult]]:
        """
        检索并构建上下文（决策 B2：token 感知预算，替代字符硬截断）

        Args:
            query: 查询文本
            top_k: 返回结果数量
            where: 过滤条件
            use_rerank: 是否使用重排序
            max_context_tokens: 上下文 token 预算

        Returns:
            tuple[str, List[SearchResult]]: (上下文文本, 检索结果列表)
        """
        results = self.retrieve(query, top_k, where, use_rerank)
        context = build_context(results, max_context_tokens)
        return context, results
