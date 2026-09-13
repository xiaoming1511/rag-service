"""
RAG 主流程
整合索引、检索、重排序、生成，提供同步与异步两条查询通道
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any, Iterator, AsyncGenerator, Callable
import asyncio
import copy
import json
import threading
import time

from src.config import RetrievalConfig, SynthesesConfig
from src.retrieval.retriever import Retriever
from src.generation.generator import Generator
from src.logging_setup import get_logger
from src.otel import span

logger = get_logger(__name__)


def _fmt_ms(ms: float) -> float:
    """耗时毫秒保留 1 位小数"""
    return round(ms, 1)


class RAGPipeline:
    """RAG 主流程"""

    def __init__(
            self,
            retriever: Retriever,
            generator: Generator,
            max_context_tokens: Optional[int] = None,
            include_sources: bool = True,
            response_cache: Optional[Any] = None,
            strict_sources: Optional[bool] = None,
            save_syntheses: Optional[bool] = None,
            syntheses_dir: Optional[str] = None,
            indexer: Optional[Any] = None,
            vector_store: Optional[Any] = None,
            model_router: Optional[Any] = None,
    ):
        """
        Note:
            `max_context_tokens` / `strict_sources` / `save_syntheses` 传 None
            = 取各自配置模型的**字段默认值**（`RetrievalConfig.context_token_budget`
            / `RetrievalConfig.strict_sources` / `SynthesesConfig.enabled`）。

            此前这三处是字面量默认值，与配置字段构成第二份真源——数值当前恰好
            一致纯属巧合（第四轮 Retriever 就是这么埋下 hybrid / parent_expansion
            等四处相反默认值的）。刻意回落 schema 字段默认而非已加载配置：
            避免构造结果取决于"此刻配置是否已加载"（行为非确定性比默认值不一致
            更难排查）。生产入口 run_api.py 本就逐项显式传入，不受影响。
        """
        _r = RetrievalConfig()
        _s = SynthesesConfig()
        if max_context_tokens is None:
            max_context_tokens = _r.context_token_budget
        if strict_sources is None:
            strict_sources = _r.strict_sources
        if save_syntheses is None:
            save_syntheses = _s.enabled

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
        # 索引器 / 向量库 / 模型路由：可选注入（None 表示对应能力不可用）
        # 统一定义在构造参数中，避免外部 monkey-patch 注入的隐式契约
        self.indexer = indexer
        self.vector_store = vector_store
        self.model_router = model_router
        # 增量索引同步器（惰性创建，见 index_incremental）
        self._index_sync = None
        # 惰性创建需加锁：sync 有多个并发入口（/v1/index/refresh 的 to_thread、
        # watcher 回调线程、IngestQueue worker）。无锁时两个线程会各建一个
        # IndexSync 实例，而 IndexSync 的互斥锁是**实例级**的——两个实例
        # 互不排除，manifest 读-改-写竞争与向量库写交错照旧发生。
        # 生产由 run_api.py 预注入实例故不触发，脚本/测试路径可达。
        self._index_sync_lock = threading.Lock()

    # ================================================================
    # 耗时统计（B：请求级分阶段计时，同步/流式/异步三通道复用）
    # ================================================================

    @staticmethod
    def _timing_event(data: Dict[str, Any]) -> str:
        """SSE timing 事件帧"""
        return f"data: {json.dumps({'type': 'timing', 'data': data}, ensure_ascii=False)}\n\n"

    @staticmethod
    def _phase_event(phase: str) -> str:
        """SSE 阶段事件帧（检索中/生成中…；LLM 无真实百分比，用阶段表示进度）"""
        return f"data: {json.dumps({'type': 'phase', 'data': {'phase': phase}}, ensure_ascii=False)}\n\n"

    @classmethod
    def _merge_timings(cls, own: Dict[str, Any]) -> Dict[str, Any]:
        """
        汇总本次查询的分阶段耗时：
        - 检索内部耗时（embed/recall/rerank）来自 Retriever.last_timings
        - 上层耗时（rewrite/retrieve/generate）由调用方填入
        """
        out: Dict[str, Any] = {}
        for k, v in own.items():
            if isinstance(v, dict):
                out[k] = {kk: (round(vv, 1) if isinstance(vv, float) else vv) for kk, vv in v.items()}
            elif isinstance(v, float):
                out[k] = round(v, 1)
            else:
                out[k] = v
        return out

    def _retriever_stage_timings(self) -> Dict[str, float]:
        """读取检索器最近一次检索的内部阶段耗时（无则空 dict）"""
        last = getattr(self.retriever, "last_timings", None)
        return dict(last) if last else {}

    def _log_timing(self, question: str, timings: Dict[str, Any]):
        """结构化记录一次请求的耗时分布（同步/流式/异步共用）
        JSON 行格式见 windows of log：request_id 由中间件注入，timing 字段结构化"""
        logger.info(
            "request_timing",
            extra={"question": (question or "")[:60], "timing": timings},
        )

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
        t_total = time.perf_counter()

        # 追问改写（决策 D5，默认关闭）：把"那它呢"改写为独立问题
        rewrite_ms = 0.0
        if self.generator.rewrite_query and history:
            t0 = time.perf_counter()
            question = self.generator.rewrite_question(question, history)
            rewrite_ms = (time.perf_counter() - t0) * 1000

        # 响应缓存（决策 D7）：仅对无历史的单轮查询生效
        # cache_key 统一在方法内计算，避免「读取/写入两个独立 if 条件」耦合脆弱
        cache_key = None
        use_cache = self.response_cache is not None and not history
        if use_cache:
            from src.cache.response_cache import ResponseCache
            cache_key = ResponseCache.make_key(question, top_k, use_rerank)
            cached = self.response_cache.get(cache_key)
            if cached is not None:
                # 深拷贝后标注缓存命中（避免改动缓存内对象），命中不再计时
                res = copy.deepcopy(cached)
                res["timing_ms"] = {
                    "cached": True,
                    "total_ms": _fmt_ms((time.perf_counter() - t_total) * 1000),
                }
                self._log_timing(question, res["timing_ms"])
                return res

        result = self._query_inner(question, history, top_k, use_rerank, rewrite_ms=rewrite_ms)
        result.setdefault("timing_ms", {})["total_ms"] = _fmt_ms((time.perf_counter() - t_total) * 1000)

        # 写入缓存（深拷贝隔离，避免后续对 result 的改动污染缓存）
        if use_cache:
            self.response_cache.put(cache_key, copy.deepcopy(result))

        # 问答沉淀（仅沉淀有来源的结果，避免记录空查询）
        if self.save_syntheses and result.get("sources"):
            self._save_synthesis(question, result["answer"], result["sources"])

        self._log_timing(question, result["timing_ms"])
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
        for r in results[:6]:
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
            rewrite_ms: float = 0.0,
    ) -> Dict[str, Any]:
        """实际执行检索与生成（供同步查询与响应缓存复用）

        返回结果带 timing_ms 分阶段耗时（ms）：
        {rewrite_ms, retrieve_ms, retrieve:{embed_ms,recall_ms,rerank_ms,total_ms},
         generate_ms, total_ms(由外层补)}。
        """
        timings: Dict[str, Any] = {"rewrite_ms": _fmt_ms(rewrite_ms)}

        t0 = time.perf_counter()
        with span("pipeline.retrieve", {"top_k": top_k, "use_rerank": use_rerank}):
            context, results = self.retriever.retrieve_with_context(
                query=question,
                top_k=top_k,
                use_rerank=use_rerank,
                max_context_tokens=self.max_context_tokens,
            )
        timings["retrieve_ms"] = _fmt_ms((time.perf_counter() - t0) * 1000)
        retriever_t = self._retriever_stage_timings()
        if retriever_t:
            timings["retrieve"] = self._merge_timings(retriever_t)

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
                "timing_ms": self._merge_timings(timings),
            }

        t1 = time.perf_counter()
        # usage 经局部 holder 按请求传递（D6-1：共享属性在并发下会串号）
        usage_holder: Dict[str, Any] = {}
        with span("pipeline.generate"):
            answer = self.generator.generate(
                query=question,
                context=context,
                history=history,
                usage_out=usage_holder,
            )
        timings["generate_ms"] = _fmt_ms((time.perf_counter() - t1) * 1000)
        usage = usage_holder.get("usage")
        if usage:
            timings["usage"] = usage

        return {
            "answer": answer,
            "sources": self._sources_payload(results),
            "context": context,
            "total_results": len(results),
            "timing_ms": self._merge_timings(timings),
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
        - phase:   阶段进度（检索中 / 生成中；LLM 生成无真实百分比）
        - sources: 检索到的来源列表
        - chunk:   生成回答的文本片段
        - timing:  分阶段耗时（生成完成前发出）
        - done:    生成完成（data 为完整回答）
        - error:   流程出错（data 为错误信息）
        """
        t_total = time.perf_counter()
        timings: Dict[str, Any] = {}

        # 追问改写（决策 D5，默认关闭）
        rewrite_ms = 0.0
        if self.generator.rewrite_query and history:
            t0 = time.perf_counter()
            question = self.generator.rewrite_question(question, history)
            rewrite_ms = (time.perf_counter() - t0) * 1000
        timings["rewrite_ms"] = _fmt_ms(rewrite_ms)

        # 1. 检索
        yield self._phase_event("检索中")
        t1 = time.perf_counter()
        with span("pipeline.retrieve", {"top_k": top_k, "use_rerank": use_rerank}):
            context, results = self.retriever.retrieve_with_context(
                query=question,
                top_k=top_k,
                use_rerank=use_rerank,
                max_context_tokens=self.max_context_tokens,
            )
        timings["retrieve_ms"] = _fmt_ms((time.perf_counter() - t1) * 1000)
        retriever_t = self._retriever_stage_timings()
        if retriever_t:
            timings["retrieve"] = self._merge_timings(retriever_t)

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
        yield self._phase_event("生成中")
        t2 = time.perf_counter()
        full_answer = ""
        # usage 经局部 holder 按请求传递（D6-1）
        usage_holder: Dict[str, Any] = {}
        with span("pipeline.generate_stream"):
            for chunk in self.generator.generate_stream(
                    query=question,
                    context=context,
                    history=history,
                    usage_out=usage_holder,
            ):
                full_answer += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'data': chunk}, ensure_ascii=False)}\n\n"
        timings["generate_ms"] = _fmt_ms((time.perf_counter() - t2) * 1000)
        usage = usage_holder.get("usage")
        if usage:
            timings["usage"] = usage

        # 4. 收尾副作用必须放在最后一次 yield **之前**。
        #    消费方（SSE 客户端）收到 done 后可能立即断开连接，Starlette 随即
        #    aclose() 本生成器，GeneratorExit 在最后一个 yield 处抛出，
        #    其后的代码永不执行——第六轮实证：问答沉淀与 request_timing
        #    日志被静默丢弃，而外部看不出任何异常。
        timings["total_ms"] = _fmt_ms((time.perf_counter() - t_total) * 1000)
        if self.save_syntheses and sources_data:
            self._save_synthesis(question, full_answer, sources_data)
        self._log_timing(question, timings)

        # 5. 完成信号（timing 先于 done，供前端在收到 done 前展示耗时）
        yield self._timing_event(self._merge_timings(timings))
        yield f"data: {json.dumps({'type': 'done', 'data': full_answer}, ensure_ascii=False)}\n\n"

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
        t_total = time.perf_counter()
        timings: Dict[str, Any] = {}

        # 追问改写（决策 D5，默认关闭，异步通道）
        rewrite_ms = 0.0
        if self.generator.rewrite_query and history:
            t0 = time.perf_counter()
            question = await self.generator.rewrite_question_async(question, history)
            rewrite_ms = (time.perf_counter() - t0) * 1000
        timings["rewrite_ms"] = _fmt_ms(rewrite_ms)

        # 1. 检索（同步组件放入线程池执行）
        yield self._phase_event("检索中")

        def _retrieve_and_timings():
            """检索 + 读取检索内部耗时，必须在**同一个线程**内完成。

            Retriever.last_timings 是线程局部的（见 retriever.py）：
            若把读取放在 `await asyncio.to_thread(...)` 之后再执行，
            协程恢复时另一并发请求可能已覆盖本线程外的状态，
            实测 2 并发下稳定有 1/2 请求拿到别人的分阶段耗时。
            """
            ctx, res = self.retriever.retrieve_with_context(
                query=question,
                top_k=top_k,
                use_rerank=use_rerank,
                max_context_tokens=self.max_context_tokens,
            )
            return ctx, res, self._retriever_stage_timings()

        t1 = time.perf_counter()
        with span("pipeline.retrieve", {"top_k": top_k, "use_rerank": use_rerank}):
            context, results, retriever_t = await asyncio.to_thread(_retrieve_and_timings)
        timings["retrieve_ms"] = _fmt_ms((time.perf_counter() - t1) * 1000)
        if retriever_t:
            timings["retrieve"] = self._merge_timings(retriever_t)

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
        yield self._phase_event("生成中")
        t2 = time.perf_counter()
        full_answer = ""
        # usage 经局部 holder 按请求传递（D6-1）：异步流式路径两个交错协程
        # 共享同一线程，读 generator.last_usage 共享属性会拿到并发请求的 usage
        usage_holder: Dict[str, Any] = {}
        with span("pipeline.generate_stream"):
            async for chunk in self.generator.generate_stream_async(
                    query=question,
                    context=context,
                    history=history,
                    usage_out=usage_holder,
            ):
                full_answer += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'data': chunk}, ensure_ascii=False)}\n\n"
        timings["generate_ms"] = _fmt_ms((time.perf_counter() - t2) * 1000)
        usage = usage_holder.get("usage")
        if usage:
            timings["usage"] = usage

        # 4. 收尾副作用必须在最后一次 yield **之前**（理由同 query_stream）。
        #    这是真实用户路径：插件「停止生成」或网络中断都会让服务端
        #    立即 aclose() 本生成器，收尾代码若在 yield 之后即被跳过。
        timings["total_ms"] = _fmt_ms((time.perf_counter() - t_total) * 1000)
        if self.save_syntheses and sources_data:
            self._save_synthesis(question, full_answer, sources_data)
        self._log_timing(question, timings)

        # 5. 完成信号（timing 先于 done）
        yield self._timing_event(self._merge_timings(timings))
        yield f"data: {json.dumps({'type': 'done', 'data': full_answer}, ensure_ascii=False)}\n\n"

    # ================================================================
    # 索引
    # ================================================================

    def index(
            self,
            source_dirs: Optional[List[str]] = None,
            rebuild: bool = False,
            cancelled: Optional[Any] = None,
            progress_cb: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        索引文档（全量）

        Args:
            source_dirs: 源目录列表（Indexer 内部只更新目录、保留配置的扩展名）
            rebuild: 是否重建（清空现有索引后重新索引）
            cancelled: 取消检查回调（供 IngestQueue 长任务取消）
            progress_cb: 进度回调 (stage, current, total, note)（供作业进度条）

        Returns:
            Dict: 索引统计信息；若索引器不可用，返回 {"error": ...}
        """
        if self.indexer is None:
            return {"error": "Indexer not available"}

        return self.indexer.index_all(
            source_dirs=source_dirs,
            rebuild=rebuild,
            cancelled=cancelled,
            progress_cb=progress_cb,
        )

    def index_incremental(
            self,
            rebuild: bool = False,
            cancelled: Optional[Any] = None,
            progress_cb: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        增量索引：仅处理有变更的文档（新增/修改/删除），避免全量重建

        依赖 IndexSync（manifest + 内容哈希三态同步）；
        若外部已注入 _index_sync 实例（如 run_api.py），则复用同一清单。

        Args:
            rebuild: 为 True 时清空向量库与清单后全量重建
            cancelled: 取消检查回调（文档粒度取消）
            progress_cb: 进度回调 (stage, current, total, note)（供作业进度条）

        Returns:
            Dict: 增量统计信息 {added, updated, removed, unchanged, skipped}
        """
        if self.indexer is None:
            return {"error": "Indexer not available"}

        index_sync = self._index_sync
        if index_sync is None:
            with self._index_sync_lock:
                # 双检：拿到锁后再确认一次，避免两个线程各建一个实例
                if self._index_sync is None:
                    from src.pipeline.index_sync import IndexSync
                    # manifest 默认落在向量库持久化目录下，与向量库一一对应
                    self._index_sync = IndexSync(self.indexer, manifest_path=None)
                index_sync = self._index_sync

        result = index_sync.sync(rebuild=rebuild, cancelled=cancelled, progress_cb=progress_cb)

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
        if self.indexer is None:
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
        if self.vector_store is None:
            return {"error": "Vector store not available"}

        rerank_cache = {}
        retriever_stats = getattr(getattr(self, "retriever", None), "rerank_cache_stats", None)
        if retriever_stats:
            rerank_cache = retriever_stats

        return {
            "vector_store": {
                "count": self.vector_store.count(),
                "collection_name": self.vector_store.collection_name,
            },
            "retriever": {"rerank_cache": rerank_cache},
            "status": "running",
        }