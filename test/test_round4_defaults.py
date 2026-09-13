"""
第四轮：Retriever 构造默认值单一来源 + 由此暴露的空路径崩溃

背景：
    `Retriever.__init__` 原先自带一份字面量默认值，与 `RetrievalConfig`
    的字段默认值**相反**（hybrid / parent_expansion / rerank_candidates /
    synthesis_weight 四处）。生产（run_api）与评测（run_eval）都显式传入
    config 值，所以线上没问题；但任何 `Retriever(...)` 裸构造都会静默拿到
    与生产不同的开关。

    改法是全部参数默认 `None` → 回落 `RetrievalConfig()` 的字段默认值。

    修完立刻暴露一个被旧默认值掩盖的真 bug：
    `retrieve()` 里第三处 syntheses 判定少了空值守卫
        if not (_is_syntheses_path(_result_path(rr)) and self.synthesis_weight <= 0)
    旧默认 synthesis_weight=1.0 会让整个降权分支短路、永不进入；
    统一到 0.85 后该行开始执行，任何「metadata 没有 file_path」的检索结果
    都会让 retrieve() 抛 AttributeError。
"""

from typing import Any, Dict, List, Optional

import pytest


# ================================================================
# 1. 默认值单一来源
# ================================================================

_DEFAULT_KEYS = (
    "top_k", "rerank_top_k", "similarity_threshold", "rerank_threshold",
    "hybrid", "hybrid_candidates", "recall_candidates", "rerank_candidates",
    "synthesis_weight", "rrf_k", "parent_expansion", "parent_max_tokens",
)


class _Null:
    """占位依赖（构造期不会被调用）"""


class TestRetrieverDefaultsFollowConfig:

    def _bare(self):
        from src.retrieval.retriever import Retriever

        return Retriever(vector_store=_Null(), embedder=_Null())

    def test_all_params_match_retrieval_config(self):
        from src.config import RetrievalConfig

        r = self._bare()
        d = RetrievalConfig()
        mismatched = {
            k: (getattr(r, k), getattr(d, k))
            for k in _DEFAULT_KEYS
            if getattr(r, k) != getattr(d, k)
        }
        assert not mismatched, f"裸构造与配置默认值不一致: {mismatched}"

    def test_no_literal_defaults_remain(self):
        """回归：那 4 处「与配置相反」的字面量不得复活"""
        from src.config import RetrievalConfig

        r = self._bare()
        d = RetrievalConfig()
        # 旧字面量：hybrid=True / parent_expansion=False / rerank_candidates=12
        # / synthesis_weight=1.0
        assert r.hybrid == d.hybrid
        assert r.parent_expansion == d.parent_expansion
        assert r.rerank_candidates == d.rerank_candidates
        assert r.synthesis_weight == d.synthesis_weight

    def test_explicit_values_still_win(self):
        from src.retrieval.retriever import Retriever

        r = Retriever(
            vector_store=_Null(), embedder=_Null(),
            hybrid=False, parent_expansion=False,
            rerank_candidates=7, synthesis_weight=0.0, top_k=9,
        )
        assert r.hybrid is False
        assert r.parent_expansion is False
        assert r.rerank_candidates == 7
        assert r.synthesis_weight == 0.0
        assert r.top_k == 9

    def test_hybrid_still_requires_bm25_index(self):
        """hybrid=True 但没有 bm25_index → 自动降级（既有行为不变）"""
        from src.retrieval.retriever import Retriever

        r = Retriever(vector_store=_Null(), embedder=_Null(), hybrid=True)
        assert r.hybrid is False


# ================================================================
# 2. 空路径不再崩溃（本次修出的真 bug）
# ================================================================

class _NoPathStore:
    """返回结果 metadata 完全不带路径的向量库"""

    def search(self, **kwargs):
        from src.vector_store.base import SearchResult

        return [
            SearchResult(id=f"c{i}", content=f"内容{i}", score=0.9 - i * 0.1, metadata={})
            for i in range(3)
        ]

    def count(self) -> int:
        return 3


class _StubEmbedder:
    def embed_single(self, text: str) -> List[float]:
        return [0.1, 0.2]

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[0.1, 0.2] for _ in texts]


class _OnReranker:
    enabled = True

    def rerank(self, query: str, results, top_k=None):
        return sorted(results, key=lambda r: -r.score)


class TestSynthesesDampingWithoutPaths:

    def _retriever(self, **kw):
        from src.retrieval.retriever import Retriever

        base: Dict[str, Any] = dict(
            vector_store=_NoPathStore(),
            embedder=_StubEmbedder(),
            reranker=_OnReranker(),
            similarity_threshold=0.0,
            recall_candidates=10,
            rerank_top_k=3,
        )
        base.update(kw)
        return Retriever(**base)

    def test_rerank_path_with_synthesis_weight_below_one(self):
        """synthesis_weight<1 且结果无 file_path → 不得抛 AttributeError"""
        r = self._retriever(synthesis_weight=0.85)
        out = r.retrieve("查询", use_rerank=True)
        assert len(out) == 3

    def test_weight_zero_does_not_drop_pathless_results(self):
        """weight=0 只应排除「确定是 syntheses 的块」，无路径的块应保留"""
        r = self._retriever(synthesis_weight=0.0)
        out = r.retrieve("查询", use_rerank=True)
        assert len(out) == 3, "无路径结果被误当沉淀排除"

    def test_real_syntheses_block_is_excluded_at_zero(self):
        from src.retrieval.retriever import Retriever
        from src.vector_store.base import SearchResult

        class _MixedStore:
            def search(self, **kwargs):
                return [
                    SearchResult(id="a", content="真实文档块", score=0.9,
                                 metadata={"file_path": "/vault/notes/a.md"}),
                    SearchResult(id="b", content="历史问答沉淀", score=0.95,
                                 metadata={"file_path": "/vault/syntheses/q.md"}),
                ]

            def count(self):
                return 2

        r = Retriever(
            vector_store=_MixedStore(), embedder=_StubEmbedder(),
            reranker=_OnReranker(), similarity_threshold=0.0,
            recall_candidates=10, rerank_top_k=5, synthesis_weight=0.0,
        )
        ids = [x.id for x in r.retrieve("查询", use_rerank=True)]
        assert "b" not in ids and "a" in ids
