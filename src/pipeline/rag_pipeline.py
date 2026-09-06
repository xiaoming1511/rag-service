"""
RAG 主流程
整合索引、检索、重排序、生成，提供同步与异步两条查询通道
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any, Iterator, AsyncGenerator
import asyncio
import json
from functools import partial

from src.retrieval.retriever import Retriever
from src.generation.generator import Generator


class RAGPipeline:
    """RAG 主流程"""

    def __init__(
            self,
            retriever: Retriever,
            generator: Generator,
            max_context_tokens: int = 4000,
            include_sources: bool = True,
            response_cache: Optional[Any] = None,
            strict_sources: bool = False,
            save_syntheses: bool = True,
            syntheses_dir: Optional[str] = None,
    ):
        self.retriever = retriever
        self.generator = generator
        self.max_context_tokens = max_context_tokens
        self.include_sources = include_sources
        # 相同问题响应缓存（决策 D7）；None 表示不启用
        self.response_cache = response_cache
        # 严格来源模式：检索结果为空时不调用模型（决策：默认关，可配置开）
        self.strict_sources = strict_sources
        # 问答沉淀（syntheses）：每次问答写为 vault 根 syntheses/ 下的 .md
        self.save_syntheses = save_syntheses
        self.syntheses_dir = syntheses_dir

    # ================================================================
    # 同步查询
    # ================================================================

    def query(
            self,
            question: str,
            history: Optional[List[Dict[str, str]]] = None,
            top_k: Optional[int] = None,
            use_rerank: bool = True,
    ) -> Dict[str, Any]:
        """同步查询（非流式）"""
        # 追问改写（决策 D5，默认关闭）：把"那它呢"改写为独立问题
        if self.generator.rewrite_query and history:
            question = self.generator.rewrite_question(question, history)

        # 响应缓存（决策 D7）：仅对无历史的单轮查询生效
        if self.response_cache is not None and not history:
            from src.cache.response_cache import ResponseCache
            cache_key = ResponseCache.make_key(question, top_k, use_rerank)
            cached = self.response_cache.get(cache_key)
            if cached is not None:
                return cached

        result = self._query_inner(question, history, top_k, use_rerank)

        # 写入缓存
        if self.response_cache is not None and not history:
            self.response_cache.put(cache_key, result)

        # 问答沉淀（仅沉淀有来源的结果，避免记录空查询）
        if self.save_syntheses and result.get("sources"):
            self._save_synthesis(question, result["answer"], result["sources"])

        return result

    @staticmethod
    def _sources_payload(
            results: List[Any],
            truncate_content: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        把检索结果转为统一的来源列表（同步/流式/异步三通道复用）

        Args:
            results: SearchResult 列表
            truncate_content: 是否截断 content 为 200 字符预览
                （插件渲染来源只用 file_name/heading/file_path，content 仅预览用途）
        """
        sources = []
        for r in results[:3]:
            content = r.content
            if truncate_content and len(content) > 200:
                content = content[:200] + "..."
            sources.append({
                "file_name": r.metadata.get("file_name", "unknown"),
                "file_path": r.metadata.get("file_path", ""),
                "heading": r.metadata.get("heading_path", ""),
                "line_start": r.metadata.get("start_line") or None,
                "line_end": r.metadata.get("end_line") or None,
                "images": r.metadata.get("images") or None,
                "content": content,
                "score": r.score,
            })
        return sources

    def _save_synthesis(
            self,
            question: str,
            answer: str,
            sources: List[Dict[str, Any]],
    ):
        """把一次问答沉淀到 syntheses 目录（失败不影响主流程）"""
        from src.pipeline.syntheses import resolve_syntheses_dir, save_syntheses

        if self.syntheses_dir:
            out_dir = resolve_syntheses_dir([], self.syntheses_dir)
        else:
            # 未显式配置时，从索引器 loader 的源目录推导（source_dirs[0]/syntheses）
            loader = getattr(getattr(self, "indexer", None), "loader", None)
            srcs = [str(s) for s in (loader.source_dirs if loader and hasattr(loader, "source_dirs") else [])]
            if not srcs:
                return  # 无法推导沉淀目录，跳过
            out_dir = resolve_syntheses_dir(srcs)

        save_syntheses(question, answer, sources, out_dir)

    def _query_inner(
            self,
            question: str,
            history: Optional[List[Dict[str, str]]],
            top_k: Optional[int],
            use_rerank: bool,
    ) -> Dict[str, Any]:
        """实际执行检索与生成（供同步查询与响应缓存复用）"""
        context, results = self.retriever.retrieve_with_context(
            query=question,
            top_k=top_k,
            use_rerank=use_rerank,
            max_context_tokens=self.max_context_tokens,
        )

        # 严格来源模式：检索结果为空 → 不调用模型，直接告知未找到
        if self.strict_sources and not results:
            answer = (
                f"未从知识库中找到与「{question}」相关的上下文"
                "（严格来源模式已开启）。\n\n"
                "建议换个问法，或先通过 /v1/index 建立/更新索引后再试。"
            )
            return {
                "answer": answer,
                "sources": [],
                "context": "",
                "total_results": 0,
            }

        answer = self.generator.generate(
            query=question,
            context=context,
            history=history,
        )

        return {
            "answer": answer,
            "sources": self._sources_payload(results),
            "context": context,
            "total_results": len(results),
        }

    def query_stream(
            self,
            question: str,
            history: Optional[List[Dict[str, str]]] = None,
            top_k: Optional[int] = None,
            use_rerank: bool = True,
    ) -> Iterator[str]:
        """
        同步流式查询 - 逐条产出 SSE 格式字符串

        每条事件形如 "data: {json}\n\n"，事件类型：
        - sources: 检索到的来源列表
        - chunk:   生成回答的文本片段
        - done:    生成完成（data 为完整回答）
        - error:   流程出错（data 为错误信息）
        """
        # 追问改写（决策 D5，默认关闭）
        if self.generator.rewrite_query and history:
            question = self.generator.rewrite_question(question, history)

        # 1. 检索
        context, results = self.retriever.retrieve_with_context(
            query=question,
            top_k=top_k,
            use_rerank=use_rerank,
            max_context_tokens=self.max_context_tokens,
        )

        # sources_data 先初始化为空列表：检索为空或 include_sources=False 时
        # 也必须已定义（沉淀判断引用它），否则 NameError
        sources_data: List[Dict[str, Any]] = []

        # 严格来源模式：检索为空 → 不调用模型
        if self.strict_sources and not results:
            strict_msg = (
                f"未从知识库中找到与「{question}」相关的上下文"
                "（严格来源模式已开启）。建议换个问法，或先建立/更新索引。"
            )
            yield f"data: {json.dumps({'type': 'chunk', 'data': strict_msg}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'data': strict_msg}, ensure_ascii=False)}\n\n"
            return

        # 2. 先发送来源信息（JSON 格式）
        if self.include_sources and results:
            sources_data = self._sources_payload(results)
            yield f"data: {json.dumps({'type': 'sources', 'data': sources_data}, ensure_ascii=False)}\n\n"

        # 3. 流式生成回答（逐块发送）
        full_answer = ""
        for chunk in self.generator.generate_stream(
                query=question,
                context=context,
                history=history,
        ):
            full_answer += chunk
            yield f"data: {json.dumps({'type': 'chunk', 'data': chunk}, ensure_ascii=False)}\n\n"

        # 4. 发送完成信号
        yield f"data: {json.dumps({'type': 'done', 'data': full_answer}, ensure_ascii=False)}\n\n"

        # 5. 问答沉淀（仅在有来源时）
        if self.save_syntheses and sources_data:
            self._save_synthesis(question, full_answer, sources_data)

    # ================================================================
    # 异步查询
    # ================================================================

    async def query_stream_async(
            self,
            question: str,
            history: Optional[List[Dict[str, str]]] = None,
            top_k: Optional[int] = None,
            use_rerank: bool = True,
    ) -> AsyncGenerator[str, None]:
        """
        异步流式查询 - 逐条产出 SSE 格式字符串（事件格式与 query_stream 一致）

        实现要点：
        1. 检索依赖同步的 ChromaDB，通过 asyncio.to_thread 放入线程池，
           避免阻塞事件循环；
        2. 生成阶段使用 Generator.generate_stream_async 异步流式输出，
           底层已处理 Python 3.13 + httpx 的流关闭兼容性问题。
        """
        # 追问改写（决策 D5，默认关闭，异步通道）
        if self.generator.rewrite_query and history:
            question = await self.generator.rewrite_question_async(question, history)

        # 1. 检索（同步组件放入线程池执行）
        retrieve = partial(
            self.retriever.retrieve_with_context,
            query=question,
            top_k=top_k,
            use_rerank=use_rerank,
            max_context_tokens=self.max_context_tokens,
        )
        context, results = await asyncio.to_thread(retrieve)

        # sources_data 先初始化为空列表（同 query_stream，防 NameError）
        sources_data: List[Dict[str, Any]] = []

        # 严格来源模式：检索为空 → 不调用模型
        if self.strict_sources and not results:
            strict_msg = (
                f"未从知识库中找到与「{question}」相关的上下文"
                "（严格来源模式已开启）。建议换个问法，或先建立/更新索引。"
            )
            yield f"data: {json.dumps({'type': 'chunk', 'data': strict_msg}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'data': strict_msg}, ensure_ascii=False)}\n\n"
            return

        # 2. 先发送来源信息
        if self.include_sources and results:
            sources_data = self._sources_payload(results)
            yield f"data: {json.dumps({'type': 'sources', 'data': sources_data}, ensure_ascii=False)}\n\n"

        # 3. 异步流式生成回答
        full_answer = ""
        async for chunk in self.generator.generate_stream_async(
                query=question,
                context=context,
                history=history,
        ):
            full_answer += chunk
            yield f"data: {json.dumps({'type': 'chunk', 'data': chunk}, ensure_ascii=False)}\n\n"

        # 4. 发送完成信号
        yield f"data: {json.dumps({'type': 'done', 'data': full_answer}, ensure_ascii=False)}\n\n"

        # 5. 问答沉淀（仅在有来源时）
        if self.save_syntheses and sources_data:
            self._save_synthesis(question, full_answer, sources_data)

    # ================================================================
    # 索引
    # ================================================================

    def index(
            self,
            source_dirs: Optional[List[str]] = None,
            rebuild: bool = False,
            cancelled: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        索引文档（全量）

        Args:
            source_dirs: 源目录列表（Indexer 内部只更新目录、保留配置的扩展名）
            rebuild: 是否重建（清空现有索引后重新索引）
            cancelled: 取消检查回调（供 IngestQueue 长任务取消）

        Returns:
            Dict: 索引统计信息；若索引器不可用，返回 {"error": ...}
        """
        if not hasattr(self, 'indexer') or self.indexer is None:
            return {"error": "Indexer not available"}

        return self.indexer.index_all(source_dirs=source_dirs, rebuild=rebuild, cancelled=cancelled)

    def index_incremental(
            self,
            rebuild: bool = False,
            cancelled: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        增量索引：仅处理有变更的文档（新增/修改/删除），避免全量重建

        依赖 IndexSync（manifest + 内容哈希三态同步）；
        若外部已注入 _index_sync 实例（如 run_api.py），则复用同一清单。

        Args:
            rebuild: 为 True 时清空向量库与清单后全量重建
            cancelled: 取消检查回调（文档粒度取消）

        Returns:
            Dict: 增量统计信息 {added, updated, removed, unchanged, skipped}
        """
        if not hasattr(self, 'indexer') or self.indexer is None:
            return {"error": "Indexer not available"}

        index_sync = getattr(self, '_index_sync', None)
        if index_sync is None:
            from src.pipeline.index_sync import IndexSync
            # manifest 默认落在向量库持久化目录下，与向量库一一对应
            index_sync = IndexSync(self.indexer, manifest_path=None)
            self._index_sync = index_sync

        result = index_sync.sync(rebuild=rebuild, cancelled=cancelled)

        return result

    def index_url(self, url: str, timeout: float = 30.0) -> Dict[str, Any]:
        """
        索引远程网页（抓取 → 解析 → 分块 → 嵌入 → 入库）

        Args:
            url: 网页地址
            timeout: 抓取超时秒数

        Returns:
            Dict: 索引结果
        """
        if not hasattr(self, 'indexer') or self.indexer is None:
            return {"error": "Indexer not available"}
        return self.indexer.index_url(url, timeout=timeout)

    def research(
            self,
            question: str,
            sub_queries: Optional[List[str]] = None,
            max_rounds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Deep Research（递归多轮：最大轮次 + 新信息增益停止）

        Args:
            question: 研究问题
            sub_queries: 自定义子查询列表（None 则用 LLM 生成）
            max_rounds: 本轮最大轮次（默认 2）

        Returns:
            Dict: report / sub_queries / sources / total_results / rounds
        """
        from src.pipeline.deep_research import DeepResearch
        dr = DeepResearch(retriever=self.retriever, generator=self.generator)
        return dr.research(question, sub_queries, max_rounds)

    # ================================================================
    # 状态统计
    # ================================================================

    def get_stats(self) -> Dict[str, Any]:
        """获取系统状态"""
        if not hasattr(self, 'vector_store'):
            return {"error": "Vector store not available"}

        return {
            "vector_store": {
                "count": self.vector_store.count(),
                "collection_name": self.vector_store.collection_name,
            },
            "status": "running",
        }