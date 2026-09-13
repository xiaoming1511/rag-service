"""
检索服务
整合向量检索、BM25 稀疏检索（混合检索 B3）、RRF 融合与重排序、
父子块召回（B4：检索时父块扩展）
"""

from typing import List, Optional, Dict, Any
import hashlib
import time
from collections import OrderedDict
from threading import Lock, local

from src.vector_store.base import BaseVectorStore, SearchResult
from src.embedding.embedder import Embedder
from src.retrieval.reranker import Reranker
from src.retrieval.bm25_index import BM25Index, rrf_fuse
from src.retrieval.context_builder import build_context
from src.retrieval.parent_expander import expand_to_parents
from src.config import RetrievalConfig
from src.otel import span


def _result_path(result: SearchResult) -> Optional[str]:
    """从检索结果元数据中提取文件路径（兼容缺失场景）"""
    meta = result.metadata or {}
    fp = meta.get("file_path")
    if fp:
        return str(fp)
    return meta.get("source") or meta.get("path")


def _is_syntheses_path(file_path: Optional[str]) -> bool:
    """判断是否为问答沉淀文件（syntheses 目录内）

    容忍路径缺失：检索结果的 metadata 未必带 file_path / source / path
    （内存态向量库、测试桩、仅有 id 的候选等），此时按「不是沉淀」处理。

    修此处的起因（第四轮）：`retrieve()` 里第三处调用写成了
        if not (_is_syntheses_path(_result_path(rr)) and self.synthesis_weight <= 0)
    少了另两处的 `fp and` 守卫。由于 `and` 会先求值左侧，只要有一个结果
    没有路径、且走了 rerank 分支，`retrieve()` 就整条抛
    `AttributeError: 'NoneType' object has no attribute 'replace'`。
    该路径此前被 `synthesis_weight` 的旧默认值 1.0（短路分支永不进入）
    掩盖着，只在把默认值统一到配置（0.85）后才暴露。
    在函数内统一兜底，三处调用点无需各自守卫。
    """
    if not file_path:
        return False
    return "syntheses" in file_path.replace("\\", "/")


class Retriever:
    """检索服务"""

    def __init__(
            self,
            vector_store: BaseVectorStore,
            embedder: Embedder,
            reranker: Optional[Reranker] = None,
            top_k: Optional[int] = None,
            rerank_top_k: Optional[int] = None,
            similarity_threshold: Optional[float] = None,
            rerank_threshold: Optional[float] = None,
            bm25_index: Optional[BM25Index] = None,
            hybrid: Optional[bool] = None,
            hybrid_candidates: Optional[int] = None,
            recall_candidates: Optional[int] = None,
            rerank_candidates: Optional[int] = None,
            synthesis_weight: Optional[float] = None,
            rrf_k: Optional[int] = None,
            parent_expansion: Optional[bool] = None,
            parent_max_tokens: Optional[int] = None,
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
            recall_candidates: 召回候选窗宽（rerank 前每路抓取的最大块数）。
                旧逻辑在 hybrid=off 时召回数被 top_k(5) 卡死，导致高分历史
                问答沉淀霸榜时真实块排到候选窗外而被漏掉；将此窗放大后
                reranker 才有机会捞回真实高分块。应 >= max(top_k, rerank_top_k)。
            rerank_candidates: rerank 候选池上限（只精排召回窗内降权后 TopN）。
                本地 reranker 一次处理数十对代价高（可达数秒）；收窄后
                大幅降延迟，同时保住窗口内高分块。应 >= rerank_top_k。
            synthesis_weight: 问答沉淀块（文件路径含 "syntheses"）的相关性
                衰减系数 0~1：重排序前把这类块的 cosine 分乘以该系数，抑制
                "历史问答以问代答"。1.0 不过滤；0.6~0.9 折中；0 = 完全排除。
            rrf_k: RRF 融合常数（默认 60）
            parent_expansion: 父子块召回（B4 方案 A）：retrieve_with_context
                把命中小块实时聚合为父块进上下文（来源列表仍为命中小块）
            parent_max_tokens: 单个父块的 token 上限

        Note:
            以上检索参数一律 `None` = 取 `RetrievalConfig` 的字段默认值。
            本方法**不再自带字面量默认值**（此前那份与 RetrievalConfig
            相反：hybrid / parent_expansion / rerank_candidates /
            synthesis_weight 四处不一致，导致裸构造得到的开关与生产配置不同，
            且毫无提示）。
            刻意回落到 schema 默认值而非 config_manager 中已加载的配置：
            后者会让构造结果取决于"此刻配置是否已加载"，同一段代码在服务内
            与单测/脚本里行为不同——非确定性比默认值不一致更难排查。
            （run_api.py / run_eval.build_components 本就显式传入
            config.retrieval 的每一项，不受影响。）
        """
        # ---- 默认值回落：唯一来源 = RetrievalConfig 字段默认值 ----
        _d = RetrievalConfig()
        top_k = _d.top_k if top_k is None else top_k
        rerank_top_k = _d.rerank_top_k if rerank_top_k is None else rerank_top_k
        similarity_threshold = (
            _d.similarity_threshold if similarity_threshold is None else similarity_threshold
        )
        rerank_threshold = _d.rerank_threshold if rerank_threshold is None else rerank_threshold
        hybrid = _d.hybrid if hybrid is None else hybrid
        hybrid_candidates = (
            _d.hybrid_candidates if hybrid_candidates is None else hybrid_candidates
        )
        recall_candidates = (
            _d.recall_candidates if recall_candidates is None else recall_candidates
        )
        rerank_candidates = (
            _d.rerank_candidates if rerank_candidates is None else rerank_candidates
        )
        synthesis_weight = (
            _d.synthesis_weight if synthesis_weight is None else synthesis_weight
        )
        rrf_k = _d.rrf_k if rrf_k is None else rrf_k
        parent_expansion = (
            _d.parent_expansion if parent_expansion is None else parent_expansion
        )
        parent_max_tokens = (
            _d.parent_max_tokens if parent_max_tokens is None else parent_max_tokens
        )

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
        self.recall_candidates = recall_candidates
        self.rerank_candidates = max(rerank_candidates, rerank_top_k)
        self.synthesis_weight = synthesis_weight
        self.rrf_k = rrf_k
        self.parent_expansion = parent_expansion
        self.parent_max_tokens = parent_max_tokens
        # 最近一次 retrieve() 的分阶段耗时（ms），供上层（pipeline/API）展示。
        # 存 thread-local 而非实例属性：Retriever 是长生命周期单例，而
        # retrieve() 经 asyncio.to_thread 多线程并发执行。若存实例属性，
        # A 请求写完、B 请求随即覆盖，A 读到的就是 B 的耗时（第六轮实证：
        # 2 并发下稳定 1/2 串号，污染 X-RAG-Retrieve-Ms 与 request_timing 日志）。
        # 读取方必须与 retrieve() 在**同一线程**内读取（见 rag_pipeline 的
        # _retrieve_and_timings：把读取放进同一个 to_thread 调用里）。
        self._local = local()
        # rerank 结果缓存：同查询+同候选池直接复用（省一次模型精排）
        self._rerank_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._rerank_cache_ttl = 300.0  # 秒（语料变化的陈旧窗口）
        self._rerank_cache_cap = 128
        # retrieve() 经 asyncio.to_thread 多线程并发，且 rerank 分重；
        # 缓存 OrderedDict 需锁保护（move_to_end/popitem 并发会损坏链表）
        self._rerank_lock = Lock()
        # 命中/未命中计数（观测：/v1/status.metrics.rerank_cache）
        self._rerank_hits = 0
        self._rerank_misses = 0

    @property
    def rerank_cache_stats(self) -> Dict[str, Any]:
        with self._rerank_lock:
            return self._snapshot_rerank_stats()

    def _snapshot_rerank_stats(self) -> Dict[str, Any]:
        """（须在 _rerank_lock 内调用）"""
        total = self._rerank_hits + self._rerank_misses
        return {
            "hits": self._rerank_hits,
            "misses": self._rerank_misses,
            "hit_rate": round(self._rerank_hits / total, 3) if total else 0.0,
        }

    def _bump_rerank_stat(self, hit: bool) -> None:
        """命中/未命中计数：必须与缓存操作同锁，否则并发自增会漂移
        （CPython 下 `+=` 非原子，读取与写回之间可被其它线程插入）"""
        with self._rerank_lock:
            if hit:
                self._rerank_hits += 1
            else:
                self._rerank_misses += 1

    def _rerank_cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        import time as _t
        with self._rerank_lock:
            item = self._rerank_cache.get(key)
            if item is None:
                return None
            if _t.monotonic() - item["at"] > self._rerank_cache_ttl:
                self._rerank_cache.pop(key, None)
                return None
            self._rerank_cache.move_to_end(key)
            # 返回浅拷贝，避免调用方改动污染缓存内部结构
            return dict(item)

    def _rerank_cache_put(self, key: str, order: List[str], scores: Dict[str, float]) -> None:
        import time as _t
        with self._rerank_lock:
            self._rerank_cache[key] = {"at": _t.monotonic(), "order": order, "scores": scores}
            self._rerank_cache.move_to_end(key)
            while len(self._rerank_cache) > self._rerank_cache_cap:
                self._rerank_cache.popitem(last=False)

    @property
    def last_timings(self) -> Dict[str, float]:
        """最近一次检索的分阶段耗时（ms）：embed_ms / recall_ms / rerank_ms / total_ms

        注意：这是**线程局部**的。调用方必须与 `retrieve()` 处于同一线程
        （`asyncio.to_thread` 场景下，读取要放进同一个 to_thread 调用内）。
        """
        return dict(getattr(self._local, "timings", None) or {})

    # ------------------------------------------------------------------
    # 内部：各路召回
    # ------------------------------------------------------------------

    def _dense_search(self, query_embedding: List[float],
                      candidates: int,
                      where: Optional[Dict[str, Any]] = None) -> List[SearchResult]:
        """稠密检索（cosine），应用相似度阈值"""
        results = self.vector_store.search(
            query_embedding=query_embedding,
            top_k=candidates,
            where=where,
        )
        if self.similarity_threshold > 0:
            results = [r for r in results if r.score >= self.similarity_threshold]
        return results

    def _recall(self, query: str, query_embedding: List[float],
                candidates: int,
                where: Optional[Dict[str, Any]] = None) -> List[SearchResult]:
        """
        召回阶段：稠密单路 或 混合两路 RRF 融合

        融合只影响排序；候选对象优先取稠密结果实例（自带 cosine 分），
        BM25 独有候选用 BM25 实例补齐。

        注意：where 过滤仅在稠密路径生效（ChromaDB 原生支持）；
        BM25 在元数据全量语料上构建，无法按 where 过滤，融合后会保留
        该路未过滤的候选项。
        """
        dense = self._dense_search(query_embedding, candidates, where=where)

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
        timings: Dict[str, float] = {}
        t_start = time.perf_counter()

        t0 = time.perf_counter()
        with span("retriever.embed"):
            query_embedding = self.embedder.embed_single(query)
        timings["embed_ms"] = (time.perf_counter() - t0) * 1000

        initial_top_k = top_k or self.top_k
        # 召回候选窗：旧逻辑在 hybrid=off 时被 clip 到 top_k，导致真实高分块
        # 落到候选窗外直接丢。现：混合路径沿用 hybrid_candidates（每路）；
        # 非混合路径统一放大到 recall_candidates，rerank 再截断。
        candidates = max(
            initial_top_k,
            self.hybrid_candidates if self.hybrid else self.recall_candidates,
        )
        t1 = time.perf_counter()
        with span("retriever.recall", {"candidates": candidates, "hybrid": self.hybrid}):
            results = self._recall(query, query_embedding, candidates, where=where)
        timings["recall_ms"] = (time.perf_counter() - t1) * 1000

        # 问答沉淀降权：rerank 前把 syntheses 路径块的 cosine 分衰减，
        # 抑制"历史问答以问代答"对 top 榜的占领，让真实文档块有机会反超。
        # synthesis_weight=0 时完全排除该类块；0<w<1 时按比例衰减。
        if self.synthesis_weight < 1.0:
            kept: List[SearchResult] = []
            for r in results:
                fp = _result_path(r)
                if fp and _is_syntheses_path(fp):
                    if self.synthesis_weight <= 0:
                        continue  # 完全排除问答沉淀
                    r.score = r.score * self.synthesis_weight
                kept.append(r)
            results = kept
            # 降权后按（调整后的）分数稳定重排，纯稠密路径的排序语义保持一致
            results.sort(key=lambda r: r.score, reverse=True)

        # 重排序（如果启用且有重排序器）
        if use_rerank and self.reranker and self.reranker.enabled and len(results) > 1:
            t2 = time.perf_counter()
            # 只精排召回窗内「降权后相似度」TopN（rerank_candidates）。
            rerank_pool = results[: self.rerank_candidates]

            # rerank 结果缓存（同查询+同候选池 → 直接复用顺序与分数，省一次模型精排）
            cache_key = hashlib.sha256(
                (query + "\x00" + "|".join(r.id for r in rerank_pool)).encode("utf-8")
            ).hexdigest()
            cached = self._rerank_cache_get(cache_key)
            with span("retriever.rerank", {"pool": len(rerank_pool), "cache_hit": cached is not None}):
                if cached:
                    self._bump_rerank_stat(hit=True)
                    by_id = {r.id: r for r in rerank_pool}
                    reranked = [by_id[i] for i in cached["order"] if i in by_id]
                    for r in reranked:
                        r.score = cached["scores"].get(r.id, r.score)
                else:
                    self._bump_rerank_stat(hit=False)
                    reranked = self.reranker.rerank(query=query, results=rerank_pool, top_k=None)
                    self._rerank_cache_put(cache_key, [r.id for r in reranked], {r.id: r.score for r in reranked})
            # 沉淀惩罚作用于最终 rerank 分（缓存命中同样适用，保证一致性）
            if self.synthesis_weight < 1.0:
                reranked = [
                    rr for rr in reranked
                    if not (_is_syntheses_path(_result_path(rr)) and self.synthesis_weight <= 0)
                ]
                for rr in reranked:
                    fp = _result_path(rr)
                    if fp and _is_syntheses_path(fp):
                        rr.score = rr.score * self.synthesis_weight
                reranked.sort(key=lambda r: r.score, reverse=True)
            results = reranked[: self.rerank_top_k]
            timings["rerank_ms"] = (time.perf_counter() - t2) * 1000
            # 重排序后二次过滤：此时 score 已是 reranker 相关性分（与
            # similarity_threshold 的 cosine 语义不同），用独立阈值
            if self.rerank_threshold > 0:
                results = [r for r in results if r.score >= self.rerank_threshold]
        else:
            # 如果不使用重排序，截取 top_k（用 initial_top_k 含默认值，语义清晰）
            results = results[:initial_top_k]
            timings["rerank_ms"] = 0.0

        timings["total_ms"] = (time.perf_counter() - t_start) * 1000
        self._local.timings = timings
        return results

    def retrieve_with_context(
            self,
            query: str,
            top_k: Optional[int] = None,
            where: Optional[Dict[str, Any]] = None,
            use_rerank: bool = True,
            max_context_tokens: Optional[int] = None,
    ) -> tuple[str, List[SearchResult]]:
        """
        检索并构建上下文（决策 B2：token 感知预算，替代字符硬截断）

        Args:
            query: 查询文本
            top_k: 返回结果数量
            where: 过滤条件
            use_rerank: 是否使用重排序
            max_context_tokens: 上下文 token 预算；None = 取
                RetrievalConfig.context_token_budget 的字段默认值。
                刻意不再写死 4000——那会与配置形成第二份真源，
                改了 settings.yaml 后直接调用本方法的地方会静默沿用旧值。

        Returns:
            tuple[str, List[SearchResult]]: (上下文文本, 检索结果列表)
        """
        if max_context_tokens is None:
            max_context_tokens = RetrievalConfig().context_token_budget
        results = self.retrieve(query, top_k, where, use_rerank)

        # B4 父子块召回（方案 A）：上下文用父块（兄弟块聚合），来源列表
        # 仍返回命中小块（分数与排序不变，展示粒度不膨胀）
        if self.parent_expansion:
            context_results = expand_to_parents(
                results, self.vector_store, self.parent_max_tokens,
            )
        else:
            context_results = results

        context = build_context(context_results, max_context_tokens)
        return context, results
