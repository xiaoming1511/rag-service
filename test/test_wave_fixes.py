"""
Wave 1/2 修复回归测试（离线：桩对象 + 临时目录，无需 oMLX 服务）

覆盖：
- C2: 流式通道检索为空时不再 NameError（sources_data 初始化）
- C1: IndexSync 多入口并发互斥（manifest 与向量库一致）
- W3: 嵌入返回数量不匹配时 fail-fast
- W5: rerank_threshold 默认关闭 / 启用时二次过滤
- P0.5-lite: Bearer 认证依赖（默认放行 / 启用校验）
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.config import AppConfig
from src.document.loader import DocumentLoader
from src.document.chunker import Chunker
from src.vector_store.base import SearchResult
from src.vector_store.chroma_store import ChromaStore
from src.pipeline.indexer import Indexer
from src.pipeline.index_sync import IndexSync
from src.pipeline.rag_pipeline import RAGPipeline
from src.embedding.embedder import Embedder
from src.retrieval.retriever import Retriever


# ================================================================
# C2: 流式通道检索为空不再 NameError
# ================================================================

class _EmptyRetriever:
    """检索结果为空的桩"""

    def retrieve_with_context(self, query, top_k=None, where=None,
                              use_rerank=True, max_context_length=2000):
        return "", []


class _OneHitRetriever:
    """返回 1 条结果的桩"""

    def retrieve_with_context(self, query, top_k=None, where=None,
                              use_rerank=True, max_context_length=2000):
        result = SearchResult(
            id="d_0", content="相关内容", score=0.9,
            metadata={"file_name": "a.md", "file_path": "/x/a.md",
                      "heading_path": "标题", "start_line": 1, "end_line": 2},
        )
        return "[a.md] > 标题\n相关内容", [result]


class _StubGenerator:
    rewrite_query = False

    def generate_stream(self, query, context, history=None, **kwargs):
        yield "答案"

    async def generate_stream_async(self, query, context, history=None, **kwargs):
        yield "答案"


def _parse_events(chunks):
    import json
    events = []
    for c in chunks:
        assert c.startswith("data: "), f"非 SSE 事件: {c!r}"
        events.append(json.loads(c[len("data: "):]))
    return events


def test_stream_empty_retrieval_no_nameerror(tmp_path):
    """C2: 检索为空 + save_syntheses 开启时，流式通道正常收尾不抛 NameError"""
    pipeline = RAGPipeline(
        retriever=_EmptyRetriever(),
        generator=_StubGenerator(),
        save_syntheses=True,
        syntheses_dir=str(tmp_path),
    )
    events = _parse_events(list(pipeline.query_stream("问题")))
    assert events[-1]["type"] == "done"
    assert not any(e["type"] == "error" for e in events)


def test_stream_async_empty_retrieval_no_nameerror(tmp_path):
    """C2: 异步流式通道同样不抛 NameError"""
    import asyncio

    async def run():
        pipeline = RAGPipeline(
            retriever=_EmptyRetriever(),
            generator=_StubGenerator(),
            save_syntheses=True,
            syntheses_dir=str(tmp_path),
        )
        out = []
        async for chunk in pipeline.query_stream_async("问题"):
            out.append(chunk)
        return out

    events = _parse_events(asyncio.run(run()))
    assert events[-1]["type"] == "done"
    assert not any(e["type"] == "error" for e in events)


def test_stream_include_sources_false_no_nameerror(tmp_path):
    """C2: 有结果但 include_sources=False 时同样正常收尾"""
    pipeline = RAGPipeline(
        retriever=_OneHitRetriever(),
        generator=_StubGenerator(),
        include_sources=False,
        save_syntheses=True,
        syntheses_dir=str(tmp_path),
    )
    events = _parse_events(list(pipeline.query_stream("问题")))
    assert events[-1]["type"] == "done"
    assert not any(e["type"] == "error" for e in events)
    assert not any(e["type"] == "sources" for e in events)


def test_sources_payload_unified():
    """C2: 三通道来源构建统一——截断行为一致（200 字符预览）"""
    long_content = "x" * 500
    result = SearchResult(id="d_0", content=long_content, score=0.8,
                          metadata={"file_name": "a.md"})
    payload = RAGPipeline._sources_payload([result])
    assert payload[0]["content"] == "x" * 200 + "..."
    full = RAGPipeline._sources_payload([result], truncate_content=False)
    assert full[0]["content"] == long_content


# ================================================================
# C1: IndexSync 并发互斥
# ================================================================

class _SlowEmbedder:
    """带延迟的假嵌入器：放大并发窗口"""

    def __init__(self, delay=0.01):
        self.delay = delay

    def embed(self, texts):
        import time
        time.sleep(self.delay)
        return [[0.1] * 8 for _ in texts]

    def embed_single(self, text):
        import time
        time.sleep(self.delay)
        return [0.1] * 8

    def get_embedding_dimension(self):
        return 8


@pytest.fixture()
def sync_setup(tmp_path):
    docs_dir = tmp_path / "kb"
    docs_dir.mkdir()
    for i in range(6):
        (docs_dir / f"f{i}.md").write_text(f"# 文档{i}\n\n内容{i}", encoding="utf-8")

    loader = DocumentLoader(source_dirs=[str(docs_dir)], extensions=[".md"])
    chunker = Chunker(chunk_size=800, overlap=100, strategy="heading")
    store = ChromaStore(
        collection_name="test_concurrent",
        persist_directory=str(tmp_path / "chroma_db"),
        embedding_dimension=8,
    )
    indexer = Indexer(loader=loader, chunker=chunker,
                      embedder=_SlowEmbedder(), vector_store=store)
    sync = IndexSync(indexer, manifest_path=str(tmp_path / "manifest.json"))
    return {"docs_dir": docs_dir, "store": store, "sync": sync}


def test_concurrent_sync_consistent(sync_setup):
    """C1: 两个线程并发 sync（同实例互斥），结束后清单与向量库一致"""
    sync = sync_setup["sync"]
    store = sync_setup["store"]

    errors = []

    def worker():
        try:
            sync.sync()
        except Exception as e:  # 并发冲突会在这里暴露
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发同步抛出异常: {errors}"

    sync.load_manifest()
    md_files = list(sync_setup["docs_dir"].glob("*.md"))
    assert len(sync.manifest["docs"]) == len(md_files), "清单条目数与文件数不一致"
    # 清单中登记的文档都能在向量库找到块（无重复、无丢失）
    for path_str, entry in sync.manifest["docs"].items():
        blocks = store.get_by_doc_id(entry["doc_id"])
        assert blocks, f"清单已登记但向量库无块: {path_str}"

    # 收敛验证：再次同步应全部命中"未变"
    result = sync.sync()
    assert result["unchanged"] == len(md_files)
    assert not result["added"] and not result["updated"] and not result["removed"]


def test_manifest_atomic_write(sync_setup):
    """W6: manifest 原子写——写入后无残留临时文件"""
    sync = sync_setup["sync"]
    sync.sync()
    manifest_path = sync.manifest_path
    assert manifest_path.exists()
    assert not manifest_path.with_suffix(".json.tmp").exists(), "临时文件未清理"


# ================================================================
# W3: 嵌入数量不匹配 fail-fast
# ================================================================

class _ShortCountClient:
    """返回数量少于请求的桩客户端"""

    def embed_sync(self, model, texts):
        return [[0.1] * 4] * max(0, len(texts) - 1)

    async def embed_async(self, model, texts):
        return self.embed_sync(model, texts)


class _OkClient:
    def embed_sync(self, model, texts):
        return [[0.1] * 4 for _ in texts]


def test_embed_fail_fast_on_count_mismatch(tmp_path):
    """W3: 嵌入 API 少返回向量时抛 RuntimeError，而不是静默错位"""
    embedder = Embedder(client=_ShortCountClient(), model="m",
                        cache_enabled=True, cache_dir=str(tmp_path / "cache"))
    with pytest.raises(RuntimeError, match="数量不匹配"):
        embedder.embed(["a", "b"])


def test_embed_normal_path_unchanged(tmp_path):
    """W3: 数量匹配时行为不变（回归保护）"""
    embedder = Embedder(client=_OkClient(), model="m",
                        cache_enabled=True, cache_dir=str(tmp_path / "cache"))
    result = embedder.embed(["a", "b"])
    assert len(result) == 2 and all(len(v) == 4 for v in result)


def test_embed_cache_per_model(tmp_path):
    """W2: 缓存按模型隔离——不同模型同名文本不命中彼此的缓存"""
    calls = []

    class CountingClient:
        def embed_sync(self, model, texts):
            calls.append(model)
            return [[float(len(model))] * 4 for _ in texts]

    client = CountingClient()
    cache_dir = tmp_path / "cache"
    e1 = Embedder(client=client, model="modelA", cache_enabled=True, cache_dir=str(cache_dir))
    e2 = Embedder(client=client, model="modelB", cache_enabled=True, cache_dir=str(cache_dir))

    v1 = e1.embed_single("相同文本")
    v2 = e2.embed_single("相同文本")
    assert v1[0] == 6.0 and v2[0] == 6.0
    assert calls.count("modelA") == 1 and calls.count("modelB") == 1, "模型间缓存串味"

    # 同模型二次调用命中缓存，不再调 API
    e1.embed_single("相同文本")
    assert calls.count("modelA") == 1

    # 磁盘缓存目录按模型分子目录
    assert (cache_dir / "modelA").is_dir() and (cache_dir / "modelB").is_dir()


# ================================================================
# W5: rerank 后二次过滤
# ================================================================

class _StubEmbedder:
    def embed_single(self, text):
        return [0.0]

    def get_embedding_dimension(self):
        return 1


class _StubStore:
    def search(self, query_embedding, top_k=5, where=None):
        return [
            SearchResult(id="1", content="a", score=0.9, metadata={}),
            SearchResult(id="2", content="b", score=0.8, metadata={}),
            SearchResult(id="3", content="c", score=0.7, metadata={}),
        ]


class _LowScoreReranker:
    enabled = True

    def rerank(self, query, results, top_k=None):
        scores = [0.9, 0.2, 0.1]  # 两个低相关分
        for r, s in zip(results, scores):
            r.score = s
        ordered = sorted(results, key=lambda x: x.score, reverse=True)
        return ordered[:top_k] if top_k else ordered


def test_rerank_threshold_off_by_default():
    """W5: rerank_threshold 默认 0 关闭，行为与历史版本一致"""
    retriever = Retriever(vector_store=_StubStore(), embedder=_StubEmbedder(),
                          reranker=_LowScoreReranker(), top_k=5, rerank_top_k=3)
    results = retriever.retrieve("q")
    assert len(results) == 3, "默认关闭时不应过滤"


def test_rerank_threshold_filters_low_relevance():
    """W5: 启用后低相关文档被丢弃"""
    retriever = Retriever(vector_store=_StubStore(), embedder=_StubEmbedder(),
                          reranker=_LowScoreReranker(), top_k=5, rerank_top_k=3,
                          rerank_threshold=0.5)
    results = retriever.retrieve("q")
    assert [r.id for r in results] == ["1"], "低于阈值的 reranker 分应被过滤"


# ================================================================
# W4: 取消检查（索引批次粒度 + 同步文档粒度）
# ================================================================

def test_index_all_cancelled(sync_setup):
    """W4: index_all 响应取消回调，返回部分统计"""
    sync_setup["sync"].sync()  # 先建索引

    # 重建场景 + 立即取消：应返回 cancelled 标记
    indexer = sync_setup["sync"].indexer
    result = indexer.index_all(rebuild=True, cancelled=lambda: True)
    assert result.get("cancelled") is True


def test_sync_cancelled_between_docs(sync_setup):
    """W4: sync 在文档边界响应取消，已处理部分保留且清单一致"""
    sync = sync_setup["sync"]
    store = sync_setup["store"]

    calls = {"n": 0}

    def cancel_after_two():
        calls["n"] += 1
        return calls["n"] > 2  # 从第 3 个文档起取消

    result = sync.sync(cancelled=cancel_after_two)
    assert result.get("cancelled") is True
    # 已处理部分登记进清单且向量库可查
    sync.load_manifest()
    for entry in sync.manifest["docs"].values():
        assert store.get_by_doc_id(entry["doc_id"])
    # 后续全量同步可续跑完成
    final = sync.sync()
    assert final.get("cancelled") is not True


# ================================================================
# P0.5-lite: Bearer 认证依赖
# ================================================================

def test_auth_disabled_by_default(monkeypatch):
    """P0.5-lite: auth.enabled=false 时所有请求放行（历史行为不变）"""
    from fastapi.testclient import TestClient
    import src.api.app as app_module

    monkeypatch.setattr(app_module, "get_config", lambda: AppConfig())
    client = TestClient(app_module.create_app())
    resp = client.get("/v1/health")
    assert resp.status_code == 200


def test_auth_enabled_rejects_missing_or_wrong_key(monkeypatch):
    """P0.5-lite: 启用后无头/错头 401，正确 Bearer 放行"""
    from fastapi.testclient import TestClient
    import src.api.app as app_module

    cfg = AppConfig()
    cfg.auth.enabled = True
    cfg.auth.api_key = "secret-key"
    monkeypatch.setattr(app_module, "get_config", lambda: cfg)

    client = TestClient(app_module.create_app())
    assert client.get("/v1/health").status_code == 401
    assert client.get("/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    resp = client.get("/v1/health", headers={"Authorization": "Bearer secret-key"})
    assert resp.status_code == 200
