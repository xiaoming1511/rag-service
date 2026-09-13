"""
Wave 5 离线测试（B4 父子块召回，方案 A：检索时父块扩展，零重建）

不依赖 oMLX：用带 get_by_doc_id 的假向量存储打桩。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.retrieval.context_builder import estimate_tokens
from src.retrieval.parent_expander import expand_to_parents
from src.retrieval.retriever import Retriever
from src.vector_store.base import SearchResult


class _FakeVectorStore:
    """内存向量库桩：get_by_doc_id 返回兄弟块，search 不使用"""

    def __init__(self, docs):
        # docs: {chunk_id: (content, metadata)}
        self.docs = docs

    def get_by_doc_id(self, doc_id):
        return [
            {"id": cid, "document": c, "metadata": m}
            for cid, (c, m) in self.docs.items()
            if m.get("doc_id") == doc_id
        ]

    def search(self, query_embedding, top_k=5, where=None):
        return []


def _make_store():
    """一个文档 3 个章节，每章 2 块；doc2 无 doc_id 元数据的异常块"""
    docs = {
        "d1_0_0": ("持久化概念总览", {"doc_id": "doc1", "heading_path": "持久化",
                                       "chunk_index": 0, "file_name": "redis.md"}),
        "d1_0_1": ("RDB 快照机制详解", {"doc_id": "doc1", "heading_path": "持久化",
                                        "chunk_index": 1, "file_name": "redis.md"}),
        "d1_1_0": ("AOF 日志机制", {"doc_id": "doc1", "heading_path": "数据类型",
                                    "chunk_index": 0, "file_name": "redis.md"}),
        "d1_1_1": ("AOF 重写流程", {"doc_id": "doc1", "heading_path": "数据类型",
                                    "chunk_index": 1, "file_name": "redis.md"}),
        "d2_0": ("无 doc_id 的异常块", {"file_name": "odd.md"}),
    }
    return _FakeVectorStore(docs)


def _hit(chunk_id, content, metadata, score=0.9):
    return SearchResult(id=chunk_id, content=content, metadata=metadata, score=score)


class TestExpandToParents:
    def test_hit_expands_to_siblings(self):
        store = _make_store()
        hit = _hit("d1_0_1", "RDB 快照机制详解",
                   store.docs["d1_0_1"][1])
        out = expand_to_parents([hit], store)
        assert len(out) == 1
        assert out[0].id.startswith("doc1::parent::")
        assert "持久化概念总览" in out[0].content
        assert "RDB 快照机制详解" in out[0].content
        assert out[0].metadata["parent_chunk_count"] == 2

    def test_score_and_order_preserved(self):
        store = _make_store()
        hits = [
            _hit("d1_1_0", "AOF 日志机制", store.docs["d1_1_0"][1], score=0.7),
            _hit("d1_0_0", "持久化概念总览", store.docs["d1_0_0"][1], score=0.9),
        ]
        out = expand_to_parents(hits, store)
        # 顺序 = 首现顺序（AOF 0.7 在前），分数保持
        assert [r.score for r in out] == [0.7, 0.9]
        assert "AOF 日志机制" in out[0].content

    def test_duplicate_hits_merge_keep_highest_score(self):
        store = _make_store()
        hits = [
            _hit("d1_0_0", "持久化概念总览", store.docs["d1_0_0"][1], score=0.8),
            _hit("d1_0_1", "RDB 快照机制详解", store.docs["d1_0_1"][1], score=0.95),
        ]
        out = expand_to_parents(hits, store)
        assert len(out) == 1
        assert out[0].score == 0.95

    def test_no_doc_id_kept_as_is(self):
        store = _make_store()
        hit = _hit("d2_0", "无 doc_id 的异常块", store.docs["d2_0"][1])
        out = expand_to_parents([hit], store)
        assert out[0].id == "d2_0"
        assert out[0].content == "无 doc_id 的异常块"

    def test_single_chunk_section_not_expanded(self):
        store = _FakeVectorStore({
            "d3_0": ("孤块", {"doc_id": "doc3", "heading_path": "", "chunk_index": 0}),
        })
        hit = _hit("d3_0", "孤块", {"doc_id": "doc3", "heading_path": ""})
        out = expand_to_parents([hit], store)
        assert out[0].id == "d3_0"  # 无兄弟，原样保留

    def test_parent_cap_centers_on_hit(self):
        # 每块 600 token，上限 1000 → 只能容纳命中块 + 1 个兄弟
        big = "甲" * 600
        store = _FakeVectorStore({
            f"d4_{i}": (big, {"doc_id": "doc4", "heading_path": "大章",
                              "chunk_index": i, "file_name": "big.md"})
            for i in range(4)
        })
        hit = _hit("d4_2", big, {"doc_id": "doc4", "heading_path": "大章"})
        out = expand_to_parents([hit], store, max_parent_tokens=1000)
        content = out[0].content
        # 命中块（index 2）必须在窗口内；最多 2 块
        assert content.count(big) <= 2
        assert big in content

    def test_empty_results(self):
        store = _make_store()
        assert expand_to_parents([], store) == []


class TestRetrieverParentWiring:
    def test_explicit_off(self):
        """显式传 False 时关闭（默认值已改为回落 RetrievalConfig，
        故本用例只保证「显式传参」这条路径，不再代表默认值）"""
        store = _make_store()
        r = Retriever(vector_store=store, embedder=None, parent_expansion=False)
        assert r.parent_expansion is False

    def test_context_uses_parent_results_children_unchanged(self):
        """retrieve_with_context：上下文含父块，返回的来源列表仍是命中小块"""
        store = _make_store()

        class _StubEmbedder:
            def embed_single(self, text):
                return [0.0]

        class _StubReranker:
            enabled = False

        retriever = Retriever(
            vector_store=store, embedder=_StubEmbedder(),
            reranker=_StubReranker(), top_k=5, similarity_threshold=0.0,
            parent_expansion=True, parent_max_tokens=1600,
        )
        # 打桩 recall：直接返回一个命中（where 参数为新增下传，打桩需容忍）
        retriever._recall = lambda q, emb, c, where=None: [
            _hit("d1_0_1", "RDB 快照机制详解", store.docs["d1_0_1"][1])
        ]
        context, results = retriever.retrieve_with_context("持久化")
        assert "持久化概念总览" in context      # 父块（兄弟）进入上下文
        assert results[0].id == "d1_0_1"        # 来源保持命中小块
        assert results[0].metadata.get("heading_path") == "持久化"

    def test_context_tokens_in_eval_details(self):
        """run_eval 的 context_tokens 统计口径可正常工作（纯函数级验证）"""
        assert estimate_tokens("持久化上下文") > 0
