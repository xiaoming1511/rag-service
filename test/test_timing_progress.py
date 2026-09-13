"""
耗时统计 + 索引进度（A/B/C）离线测试

覆盖：
- Retriever 最近一次 retrieve() 的分阶段耗时（embed/recall/rerank/total）
- Pipeline 请求级 timing（rewrite/retrieve/generate/total）+ 同步 query 缓存命中标注
- SSE 流式事件的 phase / timing 顺序与内容
- IngestQueue 结构化进度（percent）与持久化
- Indexer.index_all 的 progress_cb 数字化进度回调
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.retrieval.retriever import Retriever
from src.pipeline.rag_pipeline import RAGPipeline
from src.pipeline.ingest_queue import IngestQueue, IndexJob
from src.pipeline.indexer import Indexer
from src.document.chunker import Chunker
from src.document.loader import Document, DocumentLoader


# ================================================================
# 桩
# ================================================================

class _Store:
    def __init__(self, results=None):
        self._results = results or []
    def search(self, query_embedding, top_k=5, where=None):
        return self._results[:top_k]
    def count(self): return len(self._results)
    def get_all(self): return [{"id": r.id, "document": r.content, "metadata": r.metadata} for r in self._results]
    def add(self, *a, **k): pass
    def delete(self, *a, **k): pass
    def clear(self): pass
    def get(self, ids): return []
    def get_by_doc_id(self, doc_id): return []
    def delete_by_doc_id(self, doc_id): pass


class _Embedder:
    def embed_single(self, text): return [0.5]
    def embed(self, texts): return [[0.5] for _ in texts]
    def get_embedding_dimension(self): return 1024


class _Reranker:
    enabled = True
    def rerank(self, query, results, top_k=None):
        ranked = sorted(results, key=lambda r: r.score, reverse=True)
        return ranked[:top_k] if top_k else ranked


class _Gen:
    rewrite_query = False

    # 模拟 oMLX 的 usage（真实 tokens/toks/s），验证其流入 timing 事件。
    # D6-1 后 usage 经 usage_out 局部 dict 按请求传递（不再依赖共享 last_usage）
    USAGE = {
        "prompt_tokens": 300,
        "completion_tokens": 120,
        "total_tokens": 420,
        "generation_tokens_per_second": 55.5,
        "time_to_first_token": 0.4,
        "generation_duration": 2.2,
        "total_time": 2.7,
    }

    def __init__(self):
        self.last_usage = dict(self.USAGE)  # 兼容保留（诊断用途）

    def generate(self, query, context, history=None, usage_out=None, **kwargs):
        if usage_out is not None:
            usage_out["usage"] = dict(self.USAGE)
        return "生成的回答"

    def generate_stream(self, query, context, history=None, usage_out=None, **kwargs):
        for c in ["你", "好"]:
            yield c
        if usage_out is not None:
            usage_out["usage"] = dict(self.USAGE)

    async def generate_stream_async(self, query, context, history=None, usage_out=None, **kwargs):
        for c in ["你", "好"]:
            yield c
        if usage_out is not None:
            usage_out["usage"] = dict(self.USAGE)


class _RetrieverStub:
    """供 pipeline 测试的检索桩：带 last_timings 属性"""
    def __init__(self, results):
        self._results = results
        self.last_timings = {"embed_ms": 2.0, "recall_ms": 3.0, "rerank_ms": 4.0, "total_ms": 9.0}
    def retrieve_with_context(self, query, top_k=None, use_rerank=True, max_context_tokens=4000):
        return "上下文内容", self._results


def _mk_result(idx, content="内容", score=0.9):
    from src.vector_store.base import SearchResult
    return SearchResult(
        id=f"c{idx}", content=content,
        metadata={"file_name": f"d{idx}.md", "file_path": f"/tmp/d{idx}.md",
                  "heading_path": f"H{idx}", "start_line": 1, "end_line": 2},
        score=score,
    )


def _mk_pipeline(results=None):
    results = results if results is not None else [_mk_result(0), _mk_result(1)]
    return RAGPipeline(retriever=_RetrieverStub(results), generator=_Gen(), save_syntheses=False)


# ================================================================
# A1：Retriever 分阶段耗时
# ================================================================

class TestRetrieverTimings:
    def test_rerank_path_records_all_stages(self):
        store = _Store([_mk_result(i) for i in range(3)])
        r = Retriever(vector_store=store, embedder=_Embedder(), reranker=_Reranker(),
                      top_k=2, rerank_top_k=2, similarity_threshold=0.0,
                      recall_candidates=10, synthesis_weight=1.0)
        r.retrieve("查询", use_rerank=True)
        t = r.last_timings
        assert "embed_ms" in t and "recall_ms" in t and "rerank_ms" in t and "total_ms" in t
        assert all(v >= 0 for v in t.values())
        assert t["rerank_ms"] > 0

    def test_no_rerank_sets_rerank_zero(self):
        from src.retrieval.reranker import Reranker as OffReranker
        off = OffReranker(client=None, enabled=False)
        store = _Store([_mk_result(i) for i in range(3)])
        r = Retriever(vector_store=store, embedder=_Embedder(), reranker=off,
                      top_k=2, similarity_threshold=0.0, recall_candidates=10)
        r.retrieve("查询", use_rerank=False)
        assert r.last_timings["rerank_ms"] == 0.0


# ================================================================
# A2：Pipeline 请求级 timing
# ================================================================

class TestPipelineTimings:
    def test_query_inner_returns_timing(self):
        pipe = _mk_pipeline()
        res = pipe._query_inner("问题", history=None, top_k=3, use_rerank=True)
        t = res["timing_ms"]
        assert set(("rewrite_ms", "retrieve_ms", "generate_ms")) <= set(t)
        assert t["retrieve"] == {"embed_ms": 2.0, "recall_ms": 3.0, "rerank_ms": 4.0, "total_ms": 9.0}
        assert all(isinstance(v, (int, float)) for v in (t["rewrite_ms"], t["retrieve_ms"], t["generate_ms"]))
        # usage（真实 tokens/toks/s）应随 timing 一并返回
        assert t["usage"]["completion_tokens"] == 120
        assert t["usage"]["generation_tokens_per_second"] == 55.5

    def test_query_includes_total(self, tmp_path):
        pipe = _mk_pipeline()
        res = pipe.query("问题")
        assert res["timing_ms"]["total_ms"] >= 0
        assert res["timing_ms"].get("cached") is not True

    def test_cache_hit_stamps_cached_flag(self, tmp_path):
        from src.cache.response_cache import ResponseCache
        cache = ResponseCache(enabled=True, ttl=3600)
        pipe = _mk_pipeline()
        pipe.response_cache = cache
        pipe.query("问题")           # 首次写入缓存
        res2 = pipe.query("问题")    # 命中
        assert res2["timing_ms"]["cached"] is True


# ================================================================
# B：SSE phase / timing 事件
# ================================================================

class TestStreamEvents:
    def _events(self, gen):
        return [json.loads(line[len("data: "):]) for line in gen]

    async def test_async_stream_event_order_and_shape(self):
        pipe = _mk_pipeline()
        events = self._events([e async for e in pipe.query_stream_async("问题", top_k=3, use_rerank=True)])
        types = [e["type"] for e in events]
        assert types[0] == "phase" and events[0]["data"]["phase"] == "检索中"
        assert "sources" in types
        assert "phase" in types and events[[i for i, t in enumerate(types) if t == "phase"][1]]["data"]["phase"] == "生成中"
        assert types.count("chunk") == 2
        # timing 必须出现在 done 之前
        assert types.index("timing") < types.index("done")
        timing = events[types.index("timing")]["data"]
        assert "total_ms" in timing and "retrieve_ms" in timing and "generate_ms" in timing
        assert timing["retrieve"]["rerank_ms"] == 4.0
        assert timing["usage"]["completion_tokens"] == 120

    def test_sync_stream_emits_phase_and_timing(self):
        pipe = _mk_pipeline()
        events = self._events(list(pipe.query_stream("问题", top_k=3, use_rerank=True)))
        types = [e["type"] for e in events]
        assert "phase" in types and types.index("timing") < types.index("done")


# ================================================================
# C：索引进度
# ================================================================

class TestIngestProgress:
    def _queue(self, tmp_path):
        q = IngestQueue(run_fn=lambda job: {"ok": True}, store_path=str(tmp_path / "jobs.json"))
        return q

    def test_update_progress_percent_and_persisted(self, tmp_path):
        q = self._queue(tmp_path)
        job = q.submit("full", {"rebuild": True})
        q.update_progress(job, "嵌入", 30, 100, "批次 1")
        assert job.progress_data["percent"] == 30.0
        assert job.progress_data["stage"] == "嵌入"
        assert job.progress == "嵌入 30/100 (30%)"
        q.shutdown()  # 等 worker 收尾写盘，避免读文件与后台 _save 竞争
        # 持久化到磁盘
        saved = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
        assert saved["jobs"][0]["progress_data"]["percent"] == 30.0

    def test_update_progress_zero_total_no_divide(self, tmp_path):
        q = self._queue(tmp_path)
        job = q.submit("url", {"url": "http://x"})
        q.update_progress(job, "抓取")
        assert job.progress_data["percent"] == 0.0


class TestIndexerProgressCb:
    def _indexer(self):
        loader = DocumentLoader(source_dirs=[], extensions=[".md"])
        # 直接用桩 loader：load() 返回 2 个文档
        docs = [
            Document(id="doc0", file_path="/tmp/a1.md", file_name="a1.md",
                     content="# 标题\n\n内容内容内容"),
            Document(id="doc1", file_path="/tmp/b2.md", file_name="b2.md",
                     content="# 标题2\n\n更多内容更多内容"),
        ]
        loader.load = lambda: docs
        return Indexer(
            loader=loader,
            chunker=Chunker(chunk_size=800, overlap=100, strategy="heading"),
            embedder=_Embedder(),
            vector_store=_Store(),
        ), docs

    def test_index_all_reports_stages(self):
        indexer, docs = self._indexer()
        calls = []
        result = indexer.index_all(progress_cb=lambda s, c, t, n="": calls.append((s, c, t)))
        assert result["total_documents"] == 2
        stages = [c[0] for c in calls]
        assert "加载" in stages and "分块" in stages and "嵌入" in stages and "存储" in stages
        embed_calls = [c for c in calls if c[0] == "嵌入"]
        assert embed_calls and all(c[1] <= c[2] for c in embed_calls)