"""
检索服务
整合向量检索和重排序
"""

from typing import List, Optional, Dict, Any

from src.vector_store.base import BaseVectorStore, SearchResult
from src.embedding.embedder import Embedder
from src.retrieval.reranker import Reranker


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
    ):
        """
        初始化检索服务

        Args:
            vector_store: 向量存储
            embedder: 嵌入服务
            reranker: 重排序服务（可选）
            top_k: 初检索返回数量
            rerank_top_k: 重排序后保留数量
            similarity_threshold: 相似度阈值（rerank 前，基于 cosine 分数过滤）
            rerank_threshold: 重排序后分数阈值（低于此值的结果被丢弃）；
                0 表示关闭（默认）。rerank 后分数语义已变为 reranker 相关性分，
                与 cosine 阈值不可混用；建议评测基线建立后再启用（0.2~0.3 起试）
        """
        self.vector_store = vector_store
        self.embedder = embedder
        self.reranker = reranker
        self.top_k = top_k
        self.rerank_top_k = rerank_top_k
        self.similarity_threshold = similarity_threshold
        self.rerank_threshold = rerank_threshold

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
        # 1. 生成查询向量
        query_embedding = self.embedder.embed_single(query)

        # 2. 初检索
        initial_top_k = top_k or self.top_k
        results = self.vector_store.search(
            query_embedding=query_embedding,
            top_k=initial_top_k,
            where=where,
        )

        # 3. 过滤低相似度结果
        if self.similarity_threshold > 0:
            results = [r for r in results if r.score >= self.similarity_threshold]

        # 4. 重排序（如果启用且有重排序器）
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
            max_context_length: int = 2000,
    ) -> tuple[str, List[SearchResult]]:
        """
        检索并构建上下文

        Args:
            query: 查询文本
            top_k: 返回结果数量
            where: 过滤条件
            use_rerank: 是否使用重排序
            max_context_length: 最大上下文长度

        Returns:
            tuple[str, List[SearchResult]]: (上下文文本, 检索结果列表)
        """
        results = self.retrieve(query, top_k, where, use_rerank)

        # 构建上下文
        context_parts = []
        for i, result in enumerate(results):
            source = result.metadata.get('file_name', 'unknown')
            heading = result.metadata.get('heading_path', '')

            # 格式：来源 > 标题: 内容
            header = f"[{source}]{f' > {heading}' if heading else ''}"
            part = f"{header}\n{result.content}"
            context_parts.append(part)

        context = "\n\n---\n\n".join(context_parts)

        # 截断上下文（如果过长）
        if len(context) > max_context_length:
            context = context[:max_context_length] + "\n\n...(内容过长，已截断)"

        return context, results