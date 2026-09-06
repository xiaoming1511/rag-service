"""
性能优化测试（离线，无需 oMLX）

覆盖（决策 D7）：
- 嵌入内存 LRU 缓存：重复文本不再调用 API
- 响应缓存：相同问题命中、TTL 过期、LRU 淘汰、含历史不缓存
- 索引并行分块：结果与串行一致
"""

import sys
import time
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.embedding.embedder import Embedder
from src.cache.response_cache import ResponseCache
from src.vector_store.base import SearchResult
from src.pipeline.rag_pipeline import RAGPipeline


# ================================================================
# 嵌入内存缓存
# ================================================================

class _CountingClient:
    """统计 embed_sync 调用次数的桩客户端"""

    def __init__(self):
        self.calls = 0

    def embed_sync(self, model, texts):
        self.calls += 1
        return [[float(i + 1) for _ in range(1024)] for i in range(len(texts))]


def test_embed_mem_cache(tmp_path):
    """相同文本重复嵌入：第二次命中内存缓存，不再调用 API"""
    client = _CountingClient()
    embedder = Embedder(
        client=client,
        model="test-model",
        cache_enabled=True,
        cache_dir=str(tmp_path / "cache"),
        mem_cache_capacity=128,
    )

    texts = ["Redis 是内存数据库", "Python 是编程语言"]
    embedder.embed(texts)
    embedder.embed(texts)   # 全命中内存
    embedder.embed(texts[:1])  # 部分命中

    assert client.calls == 1, "第二次及之后应命中内存缓存"


def test_embed_no_cache_at_all(tmp_path):
    """完全禁用缓存时每次调用 API"""
    client = _CountingClient()
    embedder = Embedder(
        client=client,
        model="test-model",
        cache_enabled=False,           # 关闭磁盘缓存
        cache_dir=str(tmp_path / "cache"),
        mem_cache_capacity=0,          # 关闭内存缓存
    )
    embedder.embed(["文本"])
    embedder.embed(["文本"])
    assert client.calls == 2


def test_embed_disk_cache_without_mem(tmp_path):
    """仅磁盘缓存（内存容量 0）：第二次命中磁盘，仍只调用一次 API"""
    client = _CountingClient()
    embedder = Embedder(
        client=client,
        model="test-model",
        cache_enabled=True,
        cache_dir=str(tmp_path / "cache"),
        mem_cache_capacity=0,
    )
    embedder.embed(["文本"])
    embedder.embed(["文本"])
    assert client.calls == 1  # 第二次从磁盘读取


# ================================================================
# 响应缓存
# ================================================================

def test_response_cache_hit():
    """相同键命中；不同键未命中"""
    cache = ResponseCache(enabled=True, ttl=3600, capacity=16)
    key = ResponseCache.make_key("什么是 Redis？", 3, True)
    assert cache.get(key) is None

    cache.put(key, {"answer": "缓存回答"})
    assert cache.get(key) == {"answer": "缓存回答"}


def test_response_cache_ttl_expiry():
    """TTL 过期后视为未命中"""
    cache = ResponseCache(enabled=True, ttl=1, capacity=16)
    key = ResponseCache.make_key("问题", 5, False)
    cache.put(key, "值")
    time.sleep(1.2)
    assert cache.get(key) is None


def test_response_cache_lru_eviction():
    """超出容量淘汰最久未用的键"""
    cache = ResponseCache(enabled=True, ttl=3600, capacity=2)
    k1 = ResponseCache.make_key("问题1", 3, True)
    k2 = ResponseCache.make_key("问题2", 3, True)
    k3 = ResponseCache.make_key("问题3", 3, True)
    cache.put(k1, "a")
    cache.put(k2, "b")
    cache.get(k1)  # 访问 k1，使其成为最近使用
    cache.put(k3, "c")
    assert cache.get(k2) is None  # k2 最久未用，被淘汰
    assert cache.get(k1) == "a"


def test_response_cache_disabled():
    """禁用时不缓存"""
    cache = ResponseCache(enabled=False)
    key = ResponseCache.make_key("问题", 3, True)
    cache.put(key, "值")
    assert cache.get(key) is None


def test_make_key_stability():
    """键稳定：同参数同键，参数不同键不同"""
    a = ResponseCache.make_key("问题", 3, True)
    b = ResponseCache.make_key("问题", 3, True)
    c = ResponseCache.make_key("问题", 5, True)
    assert a == b
    assert a != c


# ================================================================
# Pipeline 响应缓存集成（桩组件）
# ================================================================

class _StubRetriever:
    def __init__(self):
        self.calls = 0

    def retrieve_with_context(self, query, top_k=None, where=None,
                              use_rerank=True, max_context_tokens=2000):
        self.calls += 1
        result = SearchResult(id="x", content="内容", score=0.9,
                              metadata={"file_name": "x.md", "file_path": "/x.md"})
        return f"上下文:{query}", [result]


class _StubGenerator:
    rewrite_query = False  # 与 Generator 的配置属性保持一致

    def __init__(self):
        self.calls = 0

    def generate(self, query, context, history=None, **kwargs):
        self.calls += 1
        return f"回答:{query}"


def _make_pipeline(cache: ResponseCache):
    retriever = _StubRetriever()
    generator = _StubGenerator()
    pipe = RAGPipeline(retriever=retriever, generator=generator, response_cache=cache)
    return pipe, retriever, generator


def test_pipeline_response_cache_hit():
    """相同单轮问题：第二次命中缓存，检索与生成都不再调用"""
    pipe, retriever, generator = _make_pipeline(ResponseCache(enabled=True))
    r1 = pipe.query("什么是 Redis？", top_k=3)
    r2 = pipe.query("什么是 Redis？", top_k=3)

    assert r1 == r2
    assert retriever.calls == 1
    assert generator.calls == 1


def test_pipeline_response_cache_history_not_cached():
    """带历史的查询不缓存（每次都走完整链路）"""
    pipe, retriever, generator = _make_pipeline(ResponseCache(enabled=True))
    history = [{"role": "user", "content": "前置问题"}]
    pipe.query("追问？", history=history)
    pipe.query("追问？", history=history)

    assert retriever.calls == 2
    assert generator.calls == 2


def test_pipeline_cache_disabled():
    """缓存禁用时每次走完整链路"""
    pipe, retriever, generator = _make_pipeline(ResponseCache(enabled=False))
    pipe.query("问题")
    pipe.query("问题")
    assert retriever.calls == 2


# ================================================================
# 索引并行分块
# ================================================================

def test_index_parallel_chunking(tmp_path):
    """并行分块与串行结果一致"""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))

    from src.document.loader import DocumentLoader
    from src.document.chunker import Chunker
    from src.vector_store.chroma_store import ChromaStore
    from src.pipeline.indexer import Indexer

    class FakeEmbedder:
        def embed(self, texts):
            return [[0.5] * 1024 for _ in texts]

        def embed_single(self, text):
            return [0.5] * 1024

        def get_embedding_dimension(self):
            return 1024

    src = tmp_path / "kb"
    src.mkdir()
    for i in range(8):
        (src / f"doc{i}.md").write_text(
            f"# 文档{i}\n\n第 {i} 个文档的内容。\n\n## 小节\n\n内容片段 {i}。",
            encoding="utf-8",
        )

    def build(workers):
        loader = DocumentLoader(source_dirs=[str(src)])
        store = ChromaStore(
            collection_name="p_test",
            persist_directory=str(tmp_path / f"db_{workers}"),
            embedding_dimension=1024,
        )
        return Indexer(
            loader=loader,
            chunker=Chunker(chunk_size=800, overlap=100, strategy="heading"),
            embedder=FakeEmbedder(),
            vector_store=store,
            max_workers=workers,
        )

    serial = build(1)
    parallel = build(4)

    s_stats = serial.index_all(rebuild=True)
    p_stats = parallel.index_all(rebuild=True)

    assert p_stats["total_documents"] == s_stats["total_documents"] == 8
    assert p_stats["total_chunks"] == s_stats["total_chunks"]
    assert p_stats["total_chunks"] >= 8
    assert p_stats["total_vectors"] == p_stats["total_chunks"]