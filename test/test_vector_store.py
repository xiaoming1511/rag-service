"""
向量存储测试（使用临时目录，不依赖外部服务）
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.vector_store.chroma_store import ChromaStore


def _sample_documents():
    """构造测试文档数据"""
    return [
        {
            'id': 'doc_1',
            'content': 'Redis 是一个内存数据库，支持多种数据结构',
            'metadata': {'source': 'redis_guide.md', 'topic': 'redis'}
        },
        {
            'id': 'doc_2',
            'content': 'Python 是一种编程语言，支持面向对象编程',
            'metadata': {'source': 'python_basics.md', 'topic': 'python'}
        },
        {
            'id': 'doc_3',
            'content': '机器学习是人工智能的一个分支',
            'metadata': {'source': 'ml_intro.md', 'topic': 'ml'}
        },
    ]


def _random_embedding(dim: int = 1024):
    """生成一个确定性随机向量（避免测试间抖动）"""
    import random
    rng = random.Random(42)
    vec = [rng.random() for _ in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec]


def test_vector_store(tmp_path):
    """增、查、过滤、删、清空全流程"""
    store = ChromaStore(
        collection_name="test_collection",
        persist_directory=str(tmp_path / "test_chroma_db"),
        embedding_dimension=1024,
    )

    docs = _sample_documents()
    embeddings = [_random_embedding() for _ in docs]

    # 初始为空
    assert store.count() == 0

    # 1. 添加
    store.add(
        ids=[d['id'] for d in docs],
        embeddings=embeddings,
        documents=[d['content'] for d in docs],
        metadatas=[d['metadata'] for d in docs],
    )
    assert store.count() == len(docs)

    # 2. 搜索（用第 1 个文档自身的向量查询，应能召回相关内容）
    results = store.search(_random_embedding(), top_k=2)
    assert len(results) == 2
    for r in results:
        assert isinstance(r.score, float)
        assert r.content
        assert r.metadata

    # 3. 按条件过滤
    filtered = store.search(_random_embedding(), top_k=5, where={"topic": "redis"})
    assert len(filtered) == 1
    assert filtered[0].id == "doc_1"

    # 4. 删除
    store.delete(['doc_1'])
    assert store.count() == len(docs) - 1

    # 5. 清空
    store.clear()
    assert store.count() == 0

    # 6. 统计信息
    stats = store.get_stats()
    assert stats['collection_name'] == "test_collection"
    assert stats['count'] == 0


def test_vector_store_add_empty(tmp_path):
    """添加空列表应无副作用"""
    store = ChromaStore(
        collection_name="test_collection",
        persist_directory=str(tmp_path / "test_chroma_db"),
    )
    store.add(ids=[], embeddings=[], documents=[], metadatas=[])
    assert store.count() == 0

    # 获取不存在的 ID 返回空列表
    assert store.get(["not_exist"]) == []