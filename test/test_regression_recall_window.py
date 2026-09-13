"""
回归测试：召回候选窗放大 + 问答沉淀降权（检索保真修复）

背景（复盘结论）：
- 旧逻辑在 hybrid=off 时，召回候选数被 clip 到 top_k(5)，导致历史问答沉淀
  （syntheses 目录）霸榜 cos top-5 时，真实文档块排到候选窗外直接丢失，
  reranker 再强也捞不回来 → 问题「请查看个人RAG知识库相关的全部API接口」
  答非所问、来源指向沉淀文件。
- 修复：召回候选窗统一放大到 recall_candidates（默认 30，>= top_k 且 >=
  rerank_top_k）；rerank 前对 syntheses 路径块按 synthesis_weight 衰减
  （0 完全排除，0<w<1 按比例）。

本文件不依赖 oMLX：用规格化打桩模拟稠密 cosine 分数与 reranker 相关性分。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.retrieval.retriever import Retriever
from src.vector_store.base import SearchResult

SYN_PATH = "/Users/xuhuaming/projects/obsidian/xu/syntheses/2026-09-08-历史问答.md"
REAL_PATH = "/Users/xuhuaming/projects/obsidian/xu/x/个人 RAG 知识库系统.md"


class _SpecEmbedder:
    """embed_single 返回规格 dict：
    {"dense": {id: cosine}, "rerank": {id: reranker 相关性分}}"""

    def __init__(self, spec):
        self.spec = spec

    def embed_single(self, text):
        return self.spec

    def get_embedding_dimension(self):
        return 1024


class _SpecStore:
    """内存向量库桩：按规格 dense 分数降序返回 top_k"""

    def __init__(self, docs):
        # docs: {id: (content, metadata)}
        self.docs = docs

    def search(self, query_embedding, top_k=5, where=None):
        dense = query_embedding["dense"]
        scored = [
            SearchResult(id=i, content=c, metadata=m, score=dense.get(i, 0.0))
            for i, (c, m) in self.docs.items()
            if dense.get(i, 0.0) > 0
        ]
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    def count(self):
        return len(self.docs)

    def get_all(self):
        return [
            {"id": i, "document": c, "metadata": m}
            for i, (c, m) in self.docs.items()
        ]

    # 其余接口不参与本测试
    def add(self, *a, **k): pass
    def delete(self, *a, **k): pass
    def clear(self): pass
    def get(self, ids): return []
    def get_by_doc_id(self, doc_id): return []
    def delete_by_doc_id(self, doc_id): pass


class _FakeReranker:
    """模拟真实 Reranker：用规格 rerank 分覆盖 score 再降序排列（真实
    Reranker.rerank 同样会把 result.score 覆写为相关性分）"""

    enabled = True

    def __init__(self, spec):
        self.spec = spec
        self.call_count = 0       # rerank 调用次数（缓存命中不会 +1）
        self.last_input_count = 0  # 最近一次收到的候选数

    def rerank(self, query, results, top_k):
        self.call_count += 1
        self.last_input_count = len(results)
        rel = self.spec["rerank"]
        for r in results:
            r.score = rel.get(r.id, 0.0)
        ranked = sorted(results, key=lambda x: x.score, reverse=True)
        return ranked[:top_k] if top_k else ranked


class _OffReranker:
    enabled = False


def _api_fixture(n_real: int = 26, api_real_index: int = 3):
    """构造「沉淀霸榜 + 真实 API 文档块」场景：
    - 5 个沉淀块 dense 0.80~0.82 霸榜；rerank 分低（0.5）
    - n_real 个真实文档块；api_real_index 号块 dense 0.70（排在沉淀之后），
      rerank 分高（0.9），其余真实块 rerank 分低（0.4）
    """
    docs, dense, rel = {}, {}, {}
    for i in range(5):
        pid = f"syn_{i}"
        docs[pid] = (
            f"历史问答沉淀块 {i}：关于 API 接口的问题与回答",
            {"file_name": f"2026-09-08-q{i}.md", "file_path": SYN_PATH},
        )
        dense[pid] = 0.82 - i * 0.005
        rel[pid] = 0.5
    for i in range(n_real):
        pid = f"api_{i}"
        docs[pid] = (
            f"真实文档 API 接口说明块 {i}：/v1/query、/v1/stream 等接口定义",
            {"file_name": "个人 RAG 知识库系统.md", "file_path": REAL_PATH},
        )
        dense[pid] = 0.70 - i * 0.01
        rel[pid] = 0.9 if i == api_real_index else 0.4
    spec = {"dense": dense, "rerank": rel}
    return docs, spec


def _retriever(store, spec, reranker=None, **kw):
    defaults = dict(
        top_k=5,
        rerank_top_k=3,
        similarity_threshold=0.5,
        hybrid=False,
        recall_candidates=30,
        rerank_candidates=12,
        synthesis_weight=0.85,
    )
    defaults.update(kw)
    return Retriever(
        vector_store=store,
        embedder=_SpecEmbedder(spec),
        reranker=reranker if reranker is not None else _FakeReranker(spec),
        **defaults,
    )


class TestRecallWindowWidened:
    """召回候选窗放大：真实高分块必须进入 rerank 候选集"""

    def test_real_block_rises_with_rerank(self):
        """新逻辑（recall_candidates=30）：真实 API 块进入候选窗，
        rerank 把高相关块提到 top-3"""
        docs, spec = _api_fixture()
        store = _SpecStore(docs)
        r = _retriever(store, spec)
        results = r.retrieve("请查看个人RAG知识库相关的全部API接口", use_rerank=True)
        assert len(results) == 3
        assert any(res.id == "api_3" for res in results), \
            f"真实文档块应进入最终 top-3，实际: {[res.id for res in results]}"
        assert results[0].id == "api_3", "高相关真实块应排在首位"

    def test_old_logic_loses_real_block(self):
        """旧逻辑（recall_candidates 被 clip 到 top_k=5）：真实块排第 6，
        根本进不了候选窗，rerank 也捞不回 → 回归缺陷复现"""
        docs, spec = _api_fixture()
        store = _SpecStore(docs)
        r = _retriever(store, spec, recall_candidates=5)
        results = r.retrieve("请查看个人RAG知识库相关的全部API接口", use_rerank=True)
        assert all(res.id != "api_3" for res in results), \
            "旧行为下真实块不应被召回（候选窗外）"

    def test_rerank_pool_limits_inputs(self):
        """rerank_candidates 限制精排输入规模（性能：减少 rerank 对数）"""
        docs, spec = _api_fixture()
        store = _SpecStore(docs)
        r = _retriever(store, spec, rerank_candidates=4)
        results = r.retrieve("请查看个人RAG知识库相关的全部API接口", use_rerank=True)
        # 召回窗 30，池限 4 → reranker 只收到 4 个候选
        assert r.reranker.last_input_count == 4
        assert len(results) == 3

    def test_rerank_cache_reuses_results(self):
        """同查询+同候选池第二次命中 rerank 缓存（不再调用 reranker）"""
        docs, spec = _api_fixture()
        store = _SpecStore(docs)
        r = _retriever(store, spec)
        first = r.retrieve("同一个问题", use_rerank=True)
        calls_after_first = r.reranker.call_count
        second = r.retrieve("同一个问题", use_rerank=True)
        # 第二次走缓存：调用次数不变
        assert r.reranker.call_count == calls_after_first
        assert [x.id for x in first] == [x.id for x in second]
        assert r._rerank_cache, "缓存应有条目"
        # 不同问题（缓存键含 query）应重新精排
        r.retrieve("另一个问题", use_rerank=True)
        assert r.reranker.call_count > calls_after_first


class TestSynthesisDownweight:
    """问答沉淀降权：不重排序的纯稠密路径也应有反超机会"""

    def _build(self, weight):
        docs = {
            "syn": ("历史问答：API 接口有哪些", {"file_name": "q.md", "file_path": SYN_PATH}),
            "real": ("真实文档：/v1/query 接口定义", {"file_name": "api.md", "file_path": REAL_PATH}),
        }
        spec = {"dense": {"syn": 0.60, "real": 0.55}, "rerank": {}}
        store = _SpecStore(docs)
        return _retriever(
            store, spec, top_k=2, similarity_threshold=0.0,
            reranker=_OffReranker(), synthesis_weight=weight,
            recall_candidates=5,
        )

    def test_weight_flips_order(self):
        """synthesis_weight=0.9：沉淀 0.6→0.54 < 真实 0.55 → 真实反超"""
        r = self._build(0.9)
        results = r.retrieve("API 接口", use_rerank=False, top_k=2)
        assert results[0].id == "real"

    def test_weight_one_keeps_original_order(self):
        """synthesis_weight=1.0（不过滤）：沉淀仍居首"""
        r = self._build(1.0)
        results = r.retrieve("API 接口", use_rerank=False, top_k=2)
        assert results[0].id == "syn"

    def test_weight_zero_excludes_syntheses(self):
        """synthesis_weight=0：沉淀块被完全排除"""
        docs, spec = _api_fixture(n_real=3)
        store = _SpecStore(docs)
        r = _retriever(store, spec, reranker=_OffReranker(),
                       synthesis_weight=0.0, top_k=5)
        results = r.retrieve("API 接口", use_rerank=False, top_k=5)
        assert results, "应仍有真实块被召回"
        assert all("syntheses" not in (res.metadata or {}).get("file_path", "")
                   for res in results)