"""
第六轮（Round 6）回归：并发契约 · 流式收尾 · 缓存编码

对应本轮修的三处真实缺陷（均先实证复现，修复后转回归）：

1. R6-1  Retriever.last_timings 由实例属性改为线程局部
         修复前：2 并发下稳定 1/2 请求读到别人的分阶段耗时，
         污染 X-RAG-Retrieve-Ms 响应头与 request_timing 日志。
2. R6-2  流式收尾副作用（问答沉淀 / 耗时日志）移到最后一次 yield 之前
         修复前：消费方收到 done 后断开，Starlette aclose() 生成器，
         GeneratorExit 使收尾代码永不执行——沉淀与耗时日志静默丢失。
3. R6-3  嵌入缓存的磁盘读写补显式 encoding="utf-8"
         修复前：非 UTF-8 locale 下 json 读写抛异常被吞，
         缓存静默全 miss（每次都打嵌入 API），且无任何日志。

另含 R6-4：RAGPipeline._index_sync 惰性创建改为双检加锁
（无锁时并发入口会各建一个 IndexSync 实例，而互斥锁是实例级的，
两个实例互不排除 → manifest 读-改-写竞争）。
"""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from src.pipeline.rag_pipeline import RAGPipeline
from src.retrieval.retriever import Retriever
from src.vector_store.base import SearchResult


# ======================================================================
# 夹具：真实 Retriever（桩化 embedder / vector_store）
# ======================================================================

class _StubEmbedder:
    def embed_single(self, query):
        return [0.1, 0.2, 0.3, 0.4]


class _StubVectorStore:
    def search(self, query_embedding, top_k, where=None):
        return [
            SearchResult(id=f"c{i}", content=f"内容{i}", metadata={"file_name": f"d{i}.md"}, score=0.9 - i * 0.01)
            for i in range(min(top_k, 3))
        ]


def _mk_retriever():
    return Retriever(
        vector_store=_StubVectorStore(),
        embedder=_StubEmbedder(),
        reranker=None,
        parent_expansion=False,
    )


def _mk_result(idx=0):
    return SearchResult(
        id=f"c{idx}", content="上下文", score=0.9,
        metadata={"file_name": f"d{idx}.md", "file_path": f"/tmp/d{idx}.md",
                  "heading_path": "H", "start_line": 1, "end_line": 2},
    )


# ======================================================================
# R6-1  Retriever.last_timings 线程局部
# ======================================================================

class TestRetrieverTimingsAreThreadLocal:
    def test_retrieve_records_timings(self):
        """基线：正常路径仍能记录分阶段耗时（不能因改造而丢功能）"""
        r = _mk_retriever()
        r.retrieve("查询")
        t = r.last_timings
        assert "embed_ms" in t and "recall_ms" in t and "total_ms" in t

    def test_other_thread_cannot_see_this_thread_timings(self):
        """线程局部核心断言：本线程写入的耗时，别的线程读不到。

        修复前 last_timings 是实例属性，跨线程可见 → 并发请求互相覆盖。
        """
        r = _mk_retriever()
        r.retrieve("查询")
        assert r.last_timings, "本线程应能看到自己刚写入的耗时"

        seen = {}

        def reader():
            seen["t"] = r.last_timings

        th = threading.Thread(target=reader)
        th.start()
        th.join()
        assert seen["t"] == {}, "另一个线程不应读到本线程的耗时（实例属性时代会读到）"

    def test_concurrent_retrieve_does_not_leak_across_threads(self):
        """并发检索：每个线程读到的必须是自己那次的结果（无则空，不串号）"""
        r = _mk_retriever()
        barrier = threading.Barrier(4)
        results = {}

        def worker(name):
            r.retrieve(f"查询-{name}")
            barrier.wait()          # 等所有线程都写完，最大化覆盖窗口
            results[name] = r.last_timings

        threads = [threading.Thread(target=worker, args=(f"T{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 线程局部：每个线程都读到自己写的（非空）
        assert all(v for v in results.values()), f"每个线程都应有自己的耗时: {results}"


# ======================================================================
# R6-1b 异步流式路径：读取耗时必须与检索同线程
# ======================================================================

class _ThreadAwareRetriever:
    """记录「检索发生在哪个线程」与「last_timings 在哪个线程被读」"""

    def __init__(self):
        self._local = threading.local()
        self.retrieve_threads = []
        self.read_threads = []

    @property
    def last_timings(self):
        self.read_threads.append(threading.get_ident())
        return dict(getattr(self._local, "t", None) or {})

    def retrieve_with_context(self, query, top_k=None, where=None,
                              use_rerank=True, max_context_tokens=None):
        self.retrieve_threads.append(threading.get_ident())
        self._local.t = {"embed_ms": 1.0, "total_ms": 2.0}
        time.sleep(0.05)            # 拉长窗口，让两个请求真正并发
        return "ctx", [_mk_result()]


class _StreamGen:
    rewrite_query = False
    last_usage = None

    async def generate_stream_async(self, query, context=None, history=None, **kw):
        yield "答"


class TestAsyncStreamReadsTimingsInWorkerThread:
    def test_read_happens_in_same_thread_as_retrieve(self):
        """修复前：读取在 `await asyncio.to_thread(...)` 之后，跑在事件循环线程，
        协程恢复的调度延迟就是竞态窗口（实证 3/3 串号）。
        修复后：读取被放进同一个 to_thread 调用内 → 读线程一定是检索线程。"""
        retr = _ThreadAwareRetriever()
        pipe = RAGPipeline(retriever=retr, generator=_StreamGen(), save_syntheses=False)

        async def one(q):
            async for _ in pipe.query_stream_async(q):
                pass

        async def main():
            await asyncio.gather(one("Q0"), one("Q1"))

        asyncio.run(main())

        assert len(retr.read_threads) == 2, f"应发生 2 次耗时读取: {retr.read_threads}"
        assert len(retr.retrieve_threads) == 2
        assert set(retr.read_threads) <= set(retr.retrieve_threads), (
            "耗时必须在检索所在线程内读取；若读到事件循环线程说明竞态窗口仍存在："
            f"read={retr.read_threads} retrieve={retr.retrieve_threads}"
        )


# ======================================================================
# R6-2  流式收尾副作用不得位于最后一次 yield 之后
# ======================================================================

class _TailPipeline(RAGPipeline):
    """用记录调用代替真实副作用，便于断言"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.saved = []
        self.logged = []

    def _save_synthesis(self, question, answer, sources):
        self.saved.append(question)

    def _log_timing(self, question, timings):
        self.logged.append(question)


class _RetrieverStub:
    last_timings = {"embed_ms": 1.0, "total_ms": 2.0}

    def retrieve_with_context(self, query, top_k=None, where=None,
                              use_rerank=True, max_context_tokens=None):
        return "ctx", [_mk_result()]


def _consume_until_done_then_close(pipe, question):
    """模拟「客户端收到 done 后立即断开」：Starlette 会 aclose() 生成器"""

    async def run():
        gen = pipe.query_stream_async(question)
        seen_done = False
        async for event in gen:
            if '"type": "done"' in event:
                seen_done = True
                break
        await gen.aclose()
        return seen_done

    return asyncio.run(run())


class TestStreamTailSideEffects:
    def test_synthesis_and_timing_log_survive_disconnect_after_done(self):
        """修复前：done 之后断开 → 沉淀与耗时日志双双丢失（实证复现）。"""
        pipe = _TailPipeline(
            retriever=_RetrieverStub(), generator=_StreamGen(), save_syntheses=True,
        )
        assert _consume_until_done_then_close(pipe, "问题") is True
        assert pipe.saved == ["问题"], "问答沉淀在客户端断连后丢失"
        assert pipe.logged == ["问题"], "request_timing 日志在客户端断连后丢失"

    def test_normal_full_consumption_saves_once(self):
        """正常消费完整流：副作用仍恰好执行一次（不得重复）"""
        pipe = _TailPipeline(
            retriever=_RetrieverStub(), generator=_StreamGen(), save_syntheses=True,
        )

        async def run():
            async for _ in pipe.query_stream_async("问题2"):
                pass

        asyncio.run(run())
        assert pipe.saved == ["问题2"]
        assert pipe.logged == ["问题2"]

    def test_no_sources_means_no_synthesis(self):
        """无来源时不沉淀（既有语义不能被本轮改动破坏）"""

        class _EmptyRetriever:
            last_timings = {}

            def retrieve_with_context(self, query, top_k=None, where=None,
                                      use_rerank=True, max_context_tokens=None):
                return "", []

        pipe = _TailPipeline(
            retriever=_EmptyRetriever(), generator=_StreamGen(), save_syntheses=True,
        )

        async def run():
            async for _ in pipe.query_stream_async("问题3"):
                pass

        asyncio.run(run())
        assert pipe.saved == []
        assert pipe.logged == ["问题3"], "耗时日志与沉淀条件无关，应始终记录"


# ======================================================================
# R6-3  嵌入缓存磁盘读写编码
# ======================================================================

class TestEmbeddingCacheEncoding:
    def _mk_embedder(self, tmp_path):
        from src.embedding.embedder import Embedder

        class _Client:
            def embed_sync(self, model, texts):
                return [[0.1, 0.2] for _ in texts]

        return Embedder(client=_Client(), model="bge-m3",
                        cache_enabled=True, cache_dir=str(tmp_path))

    def test_cache_roundtrip_preserves_non_ascii(self, tmp_path):
        emb = self._mk_embedder(tmp_path)
        text = "中文内容：缓存往返不应丢字符"
        emb._save_cache(text, [0.1, 0.2])

        # 磁盘文件必须是可解码的 UTF-8（非 ascii 转义），且文本原样保留
        files = list((Path(tmp_path) / "bge-m3").glob("*.json"))
        assert len(files) == 1, f"应写入 1 个缓存文件: {files}"
        raw = files[0].read_text(encoding="utf-8")
        assert "中文内容" in raw, "ensure_ascii=False：中文应以明文落盘"

        # 新实例（内存缓存为空）→ 走磁盘路径
        emb2 = self._mk_embedder(tmp_path)
        got = emb2._get_cache(text)
        assert got == [0.1, 0.2], "磁盘缓存应能读回"


# ======================================================================
# R6-4  _index_sync 惰性创建：双检加锁
# ======================================================================

class TestIndexSyncLazyInitIsLocked:
    def test_concurrent_creation_yields_single_instance(self, monkeypatch):
        """无锁时并发入口会各建一个 IndexSync；而 IndexSync 的互斥锁是实例级的，
        两个实例互不排除 → manifest 读-改-写竞争。"""
        import src.pipeline.index_sync as idx_mod

        created = []
        barrier = threading.Barrier(8)

        class _FakeSync:
            def __init__(self, indexer, manifest_path=None):
                created.append(self)
                time.sleep(0.02)      # 放大构造窗口，让双检失效更容易暴露

            def sync(self, rebuild=False, cancelled=None, progress_cb=None):
                return {"added": [], "updated": [], "removed": [], "unchanged": 0, "skipped": []}

        monkeypatch.setattr(idx_mod, "IndexSync", _FakeSync)

        pipe = RAGPipeline(retriever=_RetrieverStub(), generator=_StreamGen(),
                           indexer=object(), save_syntheses=False)

        def worker():
            barrier.wait()
            pipe.index_incremental()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(created) == 1, f"并发惰性创建应只产生 1 个 IndexSync，实际 {len(created)} 个"
        assert pipe._index_sync is created[0]


# ======================================================================
# R6-5  RAGPipeline 构造默认值：单一真源（关闭 Round 5 遗留 D5-5）
# ======================================================================

class TestPipelineDefaultsFollowConfig:
    def test_bare_construction_matches_config_defaults(self):
        """max_context_tokens / strict_sources / save_syntheses 曾是字面量默认值，
        与 RetrievalConfig / SynthesesConfig 构成第二份真源。当前数值恰好一致，
        但配置一改就会静默分叉（第四轮 Retriever 就是这么埋下四处相反默认值）。"""
        from src.config import RetrievalConfig, SynthesesConfig

        pipe = RAGPipeline(retriever=_RetrieverStub(), generator=_StreamGen())
        d, s = RetrievalConfig(), SynthesesConfig()
        assert pipe.max_context_tokens == d.context_token_budget
        assert pipe.strict_sources == d.strict_sources
        assert pipe.save_syntheses == s.enabled

    def test_explicit_values_still_win(self):
        pipe = RAGPipeline(
            retriever=_RetrieverStub(), generator=_StreamGen(),
            max_context_tokens=1234, strict_sources=True, save_syntheses=False,
        )
        assert pipe.max_context_tokens == 1234
        assert pipe.strict_sources is True
        assert pipe.save_syntheses is False


# ======================================================================
# R6-6  会话清理下界：路由与存储必须同一口径
# ======================================================================

def test_sessions_cleanup_keep_lower_bound_matches_store(monkeypatch):
    """路由曾钳到 0 而 store 内部钳到 1 → 传 keep=0 时实际保留 1 条却回报 keep: 0。"""
    from src.api.routes import sessions as s

    seen = {}

    class _Store:
        def cleanup(self, keep=100):
            seen["keep"] = keep
            return []

    class _Pipe:
        conversation_store = _Store()

    monkeypatch.setattr(s, "_pipeline", _Pipe())
    out = s.cleanup_sessions(s.CleanupRequest(keep=0))
    assert out["keep"] == 1, "响应回报的 keep 必须等于实际使用的下界"
    assert seen["keep"] == 1, "传给 store 的 keep 必须与响应一致"
