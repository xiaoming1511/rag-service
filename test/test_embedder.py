"""
嵌入服务测试（需要本机 oMLX 服务，未启动时自动跳过）
"""

import sys
import pytest
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from conftest import OMLX_BASE_URL, server_available
from src.embedding.client import OMLXClient
from src.embedding.embedder import Embedder

# oMLX 服务未启动时跳过本模块
pytestmark = pytest.mark.skipif(
    not server_available(),
    reason="oMLX 服务未启动 (127.0.0.1:8000)",
)


@pytest.fixture()
def embedder(tmp_path) -> Embedder:
    """构建使用独立缓存目录的嵌入服务"""
    client = OMLXClient(base_url=OMLX_BASE_URL, api_key="dummy", timeout=30.0)
    return Embedder(
        client=client,
        model="bge-m3-mlx-4bit",
        cache_enabled=True,
        cache_dir=str(tmp_path / "emb_cache"),
    )


def test_embed_batch(embedder):
    """批量嵌入：数量与维度正确"""
    test_texts = [
        "Redis 是一个内存数据库",
        "Python 支持多种数据类型",
        "机器学习是人工智能的一个子集",
    ]
    embeddings = embedder.embed(test_texts)

    assert len(embeddings) == len(test_texts)
    for emb in embeddings:
        assert len(emb) == 1024, "bge-m3 向量维度应为 1024"
        assert all(isinstance(v, float) for v in emb[:5])


def test_embed_single(embedder):
    """单个文本嵌入"""
    emb = embedder.embed_single("测试文本")
    assert len(emb) == 1024


def test_embed_cache_hit(embedder):
    """缓存命中：相同文本两次嵌入结果应一致（并命中缓存）"""
    text = "Redis 是一个内存数据库"
    first = embedder.embed_single(text)
    second = embedder.embed_single(text)

    assert first == second, "缓存命中时两次嵌入结果应完全一致"


def test_embed_empty():
    """空列表直接返回空结果，不调用 API"""
    client = OMLXClient(base_url=OMLX_BASE_URL, api_key="dummy", timeout=30.0)
    embedder = Embedder(client=client, model="bge-m3-mlx-4bit", cache_enabled=False)
    assert embedder.embed([]) == []


def test_embed_no_cache(embedder):
    """禁用缓存时仍可正常嵌入"""
    client = OMLXClient(base_url=OMLX_BASE_URL, api_key="dummy", timeout=30.0)
    embedder = Embedder(client=client, model="bge-m3-mlx-4bit", cache_enabled=False)
    embeddings = embedder.embed(["Python 支持多种数据类型"])
    assert len(embeddings) == 1
    assert len(embeddings[0]) == 1024