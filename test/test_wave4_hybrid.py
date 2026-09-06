"""
Wave 4 离线测试（B2 token 感知截断 + B3 BM25 混合检索）

不依赖 oMLX：用假向量存储 / 假 embedder / 假 reranker 打桩。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.retrieval.context_builder import build_context, estimate_tokens
from src.retrieval.bm25_index import BM25Index, rrf_fuse, tokenize
from src.retrieval.retriever import Retriever
from src.vector_store.base import SearchResult


# ---------- B2: token 估算与上下文预算 ----------

class TestTokenEstimator:
    def test_cjk_dominant(self):
        # 中文 ≈ 1 token/字
        assert estimate_tokens("你好世界测试") == 6

    def test_latin_words(self):
        # 拉丁词按每 4 字符 1 token 粗算
        assert 1 <= estimate_tokens("hello") <= 3

    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_monotonic(self):
        # 更长文本估算值不减小
        a = "检索增强生成Retrieval augmented generation 123"
        assert estimate_tokens(a + a) >= estimate_tokens(a)


class TestBuildContext:
    def _result(self, idx: int, content: str, file_name: str = "a.md"):
        return SearchResult(
            id=f"chunk-{idx}", content=content,
            metadata={"file_name": file_name, "heading_path": f"H{idx}"},
            score=0.9 - idx * 0.1,
        )

    def test_all_fit_keeps_whole(self):
        results = [self._result(i, f"内容{i} " * 10) for i in range(3)]
        ctx = build_context(results, max_tokens=10_000)
        for i in range(3):
            assert f"内容{i}" in ctx

    def test_overflow_gets_fair_share_not_dropped(self):
        # 两个大块，预算只够 1.5 块 → 第二块应保留头部片段
        big1 = "甲" * 600
        big2 = "乙" * 600
        ctx = build_context([self._result(0, big1), self._result(1, big2)],
                            max_tokens=800)
        assert "甲甲" in ctx           # 第一块完整
        assert "乙乙" in ctx           # 第二块保留片段
        assert "乙" * 600 not in ctx   # 但不是完整块

    def test_tiny_budget_returns_something(self):
        results = [self._result(0, "第一块内容")]
        ctx = build_context(results, max_tokens=10)
        assert ctx != ""

    def test_zero_budget_empty(self):
        assert build_context([self._result(0, "x")], max_tokens=0) == ""

    def test_empty_results(self):
        assert build_context([], max_tokens=1000) == ""


# ---------- B3: 分词 / RRF / BM25 索引 ----------

class TestTokenize:
    def test_chinese_words(self):
        toks = tokenize("Redis支持哪两种持久化方式")
        assert "持久化" in toks or "持久" in toks
        assert "redis" in toks

    def test_english_lowercase(self):
        assert "pytorch" in tokenize("PyTorch and TensorFlow")


class TestRRF:
    def test_both_lists_agree(self):
        fused = rrf_fuse([["a", "b"], ["a", "b"]], k=60, top_n=5)
        assert fused[0] == "a"

    def test_complement_lists(self):
        # 密集路 [a, b]，稀疏路 [b, c] → b 应排最前（两路都命中）
        fused = rrf_fuse([["a", "b"], ["b", "c"]], k=60, top_n=5)
        assert fused[0] == "b"
        assert set(fused) == {"a", "b", "c"}

    def test_top_n(self):
        fused = rrf_fuse([["a", "b", "c", "d"]], k=60, top_n=2)
        assert len(fused) == 2

    def test_stable_tiebreak(self):
        # 只出现在各自一列表首位的两项同分 → 按首次出现顺序
        fused = rrf_fuse([["x"], ["y"]], k=60, top_n=5)
        assert fused[0] == "x"


class _FakeVectorStore:
    """最小向量库桩：内存语料 + 线性 scoring 的稠密检索"""

    def __init__(self, docs):
        # docs: {id: (content, metadata, dense_score_for_keyword)}
        self.docs = docs

    def count(self):
        return len(self.docs)

    def get_all(self):
        return [
            {"id": i, "document": c, "metadata": m}
            for i, (c, m, _) in self.docs.items()
        ]

    def search(self, query_embedding, top_k=5, where=None):
        # 测试里用 query_embedding 携带「期望关键词」做伪稠密检索
        keyword = query_embedding
        scored = []
        for i, (c, m, keys) in self.docs.items():
            if keyword and keyword in c:
                scored.append(SearchResult(id=i, content=c, metadata=m, score=0.9))
        return scored[:top_k]

    # 其余接口不参与本测试
    def add(self, *a, **k): pass
    def delete(self, *a, **k): pass
    def clear(self): pass
    def get(self, ids): return []
    def get_by_doc_id(self, doc_id): return []
    def delete_by_doc_id(self, doc_id): pass


def _make_store():
    """最小向量库桩：内存语料 + 线性 scoring 的稠密检索

    注意：BM25Okapi 的 IDF 在「词出现在约半数文档」时会趋近 0，
    测试语料需保证查询词足够稀疏（补充无关填充文档）。
    """
    docs = {
        "d1_0": ("Redis 是内存数据库，支持持久化 RDB 和 AOF", {"file_name": "redis.md"}, "redis"),
        "d1_1": ("Redis 持久化包括 RDB 快照与 AOF 日志两种方式", {"file_name": "redis.md"}, "redis"),
        "d2_0": ("Python 是一门动态语言，支持多种编程范式", {"file_name": "python.md"}, "python"),
        "d3_0": ("深度学习使用多层神经网络", {"file_name": "dl.md"}, "深度学习"),
        "d4_0": ("今天的会议记录：项目排期与人员安排", {"file_name": "meeting.md"}, ""),
        "d4_1": ("周末爬山路线推荐与装备清单", {"file_name": "trip.md"}, ""),
        "d4_2": ("咖啡冲泡指南：手冲的水温与粉水比", {"file_name": "coffee.md"}, ""),
        "d4_3": ("番茄的常见病虫害防治方法", {"file_name": "tomato.md"}, ""),
    }
    return _FakeVectorStore(docs)


class TestBM25Index:
    def _store(self):
        return _make_store()

    def test_keyword_search(self):
        idx = BM25Index(self._store())
        results = idx.search("持久化", top_n=3)
        assert results, "BM25 应命中含关键词的块"
        assert all("持久化" in r.content or "RDB" in r.content or "AOF" in r.content
                   for r in results)

    def test_no_match_returns_empty(self):
        idx = BM25Index(self._store())
        assert idx.search("区块链", top_n=3) == []

    def test_rebuild_on_count_change(self):
        store = self._store()
        idx = BM25Index(store)
        assert idx.search("神经网络")  # 命中 d3_0
        # 模拟新增文档后计数变化
        store.docs["d5_0"] = ("新增的神经网络内容块", {"file_name": "new.md"}, "")
        idx._ensure_fresh()
        results = idx.search("新增", top_n=3)
        assert any(r.id == "d5_0" for r in results)

    def test_invalidate(self):
        idx = BM25Index(self._store())
        idx._ensure_fresh()
        idx.invalidate()
        assert idx._built_count == -1

    def test_empty_corpus(self):
        idx = BM25Index(_FakeVectorStore({}))
        assert idx.search("任意") == []


class _StubEmbedder:
    """embed_single 直接把 query 当作「伪稠密检索关键词」传给 store"""

    def embed_single(self, text):
        return text

    def get_embedding_dimension(self):
        return 1024


class _StubReranker:
    enabled = False


class TestRetrieverHybrid:
    def _retriever(self, store, hybrid=True):
        return Retriever(
            vector_store=store,
            embedder=_StubEmbedder(),
            reranker=_StubReranker(),
            top_k=3,
            similarity_threshold=0.0,
            bm25_index=BM25Index(store),
            hybrid=hybrid,
            hybrid_candidates=10,
        )

    def _store(self):
        return _make_store()

    def test_hybrid_surfaces_sparse_only_hits(self):
        # 查询「Redis 神经网络」：伪稠密路（整串子串匹配）命中不了
        # d3_0（神经网络块），BM25 分词路应把它带回来
        store = self._store()
        dense_only = Retriever(
            vector_store=store, embedder=_StubEmbedder(),
            reranker=_StubReranker(), top_k=5, similarity_threshold=0.0,
            hybrid=False,
        )
        assert all("神经网络" not in r.content for r in dense_only.retrieve("Redis"))

        hybrid = self._retriever(store)
        ids = {r.id for r in hybrid.retrieve("Redis 神经网络")}
        assert "d3_0" in ids  # BM25 命中的稀疏独有候选被融合进来

    def test_no_hybrid_keeps_dense_only(self):
        store = self._store()
        r = self._retriever(store, hybrid=False)
        results = r.retrieve("Redis")
        assert results and all(r2.score >= 0.5 for r2 in results)

    def test_hybrid_threshold_not_applied_to_sparse(self):
        # similarity_threshold=0.9 时稠密全被过滤，BM25 路仍能召回
        store = self._store()
        retriever = Retriever(
            vector_store=store, embedder=_StubEmbedder(),
            reranker=_StubReranker(), top_k=5,
            similarity_threshold=0.95,
            bm25_index=BM25Index(store), hybrid=True,
        )
        results = retriever.retrieve("Python 动态语言")
        assert any(r.id == "d2_0" for r in results)

    def test_hybrid_requires_bm25_index(self):
        r = Retriever(
            vector_store=self._store(), embedder=_StubEmbedder(),
            reranker=_StubReranker(), hybrid=True,  # 未传 bm25_index
        )
        assert r.hybrid is False  # 自动降级为纯稠密
