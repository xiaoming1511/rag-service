"""
检索服务测试（需要本机 oMLX 服务，未启动时自动跳过）
"""

import sys
import pytest
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from conftest import OMLX_BASE_URL, server_available
from src.embedding.client import OMLXClient
from src.embedding.embedder import Embedder
from src.vector_store.chroma_store import ChromaStore
from src.retrieval.reranker import Reranker
from src.retrieval.retriever import Retriever

# oMLX 服务未启动时跳过本模块
pytestmark = pytest.mark.skipif(
    not server_available(),
    reason="oMLX 服务未启动 (127.0.0.1:8000)",
)


TEST_DOCS = [
    {
        'id': 'doc_redis',
        'content': 'Redis 是一个内存数据库，支持字符串、列表、哈希、集合、有序集合等多种数据结构。它的读写速度极快，适合用作缓存。',
        'metadata': {'file_name': 'redis_guide.md', 'topic': 'redis'}
    },
    {
        'id': 'doc_python',
        'content': 'Python 是一种解释型、面向对象的高级编程语言。它支持多种编程范式，包括面向对象、函数式和过程式编程。',
        'metadata': {'file_name': 'python_basics.md', 'topic': 'python'}
    },
    {
        'id': 'doc_ml',
        'content': '机器学习是人工智能的核心分支，通过算法让计算机从数据中学习规律。深度学习是机器学习的一个子集，使用多层神经网络。',
        'metadata': {'file_name': 'ml_intro.md', 'topic': 'ml'}
    },
    {
        'id': 'doc_cache',
        'content': '缓存技术用于提高系统性能，将频繁访问的数据存储在高速存储介质中。Redis 是缓存技术的典型实现之一。',
        'metadata': {'file_name': 'cache_guide.md', 'topic': 'cache'}
    },
]


@pytest.fixture()
def retriever(tmp_path) -> Retriever:
    """构建带测试数据的检索服务（独立向量库与缓存目录）"""
    client = OMLXClient(base_url=OMLX_BASE_URL, api_key="dummy", timeout=30.0)
    embedder = Embedder(
        client=client,
        model="bge-m3-mlx-4bit",
        cache_enabled=True,
        cache_dir=str(tmp_path / "emb_cache"),
    )

    vector_store = ChromaStore(
        collection_name="test_collection",
        persist_directory=str(tmp_path / "test_chroma_db"),
    )

    # 写入测试数据
    embeddings = embedder.embed([d['content'] for d in TEST_DOCS])
    vector_store.add(
        ids=[d['id'] for d in TEST_DOCS],
        embeddings=embeddings,
        documents=[d['content'] for d in TEST_DOCS],
        metadatas=[d['metadata'] for d in TEST_DOCS],
    )

    reranker = Reranker(client=client, model="bge-reranker-v2-m3", enabled=True)
    return Retriever(
        vector_store=vector_store,
        embedder=embedder,
        reranker=reranker,
        top_k=5,
        rerank_top_k=3,
        similarity_threshold=0.3,
    )


def test_retrieve_with_rerank(retriever):
    """带重排序的检索：应返回结果且分数为有效浮点数"""
    results = retriever.retrieve("什么是 Redis？它有哪些数据结构？", use_rerank=True)
    assert len(results) >= 1
    for r in results:
        assert isinstance(r.score, float)
        assert r.metadata.get("file_name")


def test_retrieve_without_rerank(retriever):
    """不带重排序的检索：top_k 应生效"""
    results = retriever.retrieve("什么是 Python？", use_rerank=False, top_k=2)
    assert len(results) <= 2
    assert len(results) >= 1


def test_retrieve_threshold(retriever):
    """相关性最高的结果应排在首位"""
    results = retriever.retrieve(
        "Python 是什么类型的编程语言？",
        use_rerank=False,
        top_k=3,
    )
    assert len(results) >= 1
    # 不重排序时按相似度降序
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_with_context(retriever):
    """检索并构建上下文：上下文应包含来源信息且长度受控"""
    context, results = retriever.retrieve_with_context(
        "缓存的作用是什么？",
        use_rerank=True,
        max_context_tokens=500,
    )
    assert len(results) >= 1
    assert "[" in context  # 上下文带 [来源文件] 标记
    assert len(context) <= 500 + 64  # 截断后附有说明文本，允许少量超出