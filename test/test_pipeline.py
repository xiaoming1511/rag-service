"""
RAG Pipeline 端到端测试（需要本机 oMLX 服务，未启动时自动跳过）

使用临时向量库与临时文档库，不触碰生产数据（./data/chroma_db）。
覆盖：索引 → 同步查询 → 同步流式查询 → 异步流式查询 → 状态统计
"""

import sys
import json
import pytest
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from conftest import OMLX_BASE_URL, server_available
from src.document.loader import DocumentLoader
from src.document.chunker import Chunker
from src.embedding.client import OMLXClient
from src.embedding.embedder import Embedder
from src.vector_store.chroma_store import ChromaStore
from src.retrieval.reranker import Reranker
from src.retrieval.retriever import Retriever
from src.generation.generator import Generator
from src.pipeline.indexer import Indexer
from src.pipeline.rag_pipeline import RAGPipeline

# oMLX 服务未启动时跳过本模块
pytestmark = pytest.mark.skipif(
    not server_available(),
    reason="oMLX 服务未启动 (127.0.0.1:8000)",
)


def _create_sample_docs(tmp_path: Path) -> Path:
    """在临时目录创建少量示例 Markdown 文档（模拟知识库）"""
    source = tmp_path / "kb"
    source.mkdir()

    (source / "redis.md").write_text(
        "# Redis\n\n## 什么是 Redis？\n\n"
        "Redis 是一个内存数据库，支持字符串、列表、哈希、集合、有序集合等数据结构。"
        "它常用于缓存、消息队列等场景。\n\n"
        "## 常用数据结构\n\n- String（字符串）\n- List（列表）\n- Hash（哈希）",
        encoding="utf-8",
    )
    (source / "python.md").write_text(
        "# Python\n\n## 数据类型\n\n"
        "Python 的数据类型包括整数、浮点数、字符串、列表、字典、元组等。"
        "Python 是解释型、面向对象的编程语言。",
        encoding="utf-8",
    )
    return source


@pytest.fixture()
def pipeline(tmp_path) -> RAGPipeline:
    """构建一个使用临时库的完整 Pipeline"""
    docs_dir = _create_sample_docs(tmp_path)

    client = OMLXClient(base_url=OMLX_BASE_URL, api_key="dummy", timeout=60.0)
    embedder = Embedder(
        client=client,
        model="bge-m3-mlx-4bit",
        cache_enabled=True,
        cache_dir=str(tmp_path / "emb_cache"),
    )
    vector_store = ChromaStore(
        collection_name="test_pipeline_collection",
        persist_directory=str(tmp_path / "chroma_db"),
    )
    loader = DocumentLoader(source_dirs=[str(docs_dir)])
    chunker = Chunker(chunk_size=800, overlap=100, strategy="heading")
    reranker = Reranker(client=client, model="bge-reranker-v2-m3", enabled=True)
    retriever = Retriever(
        vector_store=vector_store,
        embedder=embedder,
        reranker=reranker,
        top_k=5,
        rerank_top_k=3,
        similarity_threshold=0.3,
    )
    generator = Generator(
        client=client,
        model="qwen3.5-4b-mlx-4bit",
        max_tokens=256,
        temperature=0.3,
        stream=True,
    )
    indexer = Indexer(
        loader=loader,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
    )

    pipe = RAGPipeline(
        retriever=retriever,
        generator=generator,
        max_context_length=2000,
        include_sources=True,
    )
    pipe.indexer = indexer
    pipe.vector_store = vector_store
    return pipe


def test_pipeline_index(pipeline):
    """索引：文档与分块应全部入库"""
    stats = pipeline.index(rebuild=True)

    assert stats.get("error") is None
    assert stats["total_documents"] >= 1
    assert stats["total_chunks"] >= 1
    assert stats["total_vectors"] == stats["total_chunks"]
    assert pipeline.vector_store.count() == stats["total_chunks"]


def test_pipeline_query(pipeline):
    """同步查询：应得到非空回答与来源"""
    pipeline.index(rebuild=True)

    result = pipeline.query("Redis 支持哪些数据结构？", use_rerank=True)
    assert result["answer"].strip(), "回答不应为空"
    assert len(result["sources"]) >= 1
    assert "total_results" in result


def test_pipeline_query_stream_sync(pipeline):
    """同步流式查询：应产出 sources / chunk / done 三类事件"""
    pipeline.index(rebuild=True)

    event_types = set()
    answer_parts = []
    for event_str in pipeline.query_stream("Redis 的常用场景有哪些？", use_rerank=True):
        # 解析 SSE 行
        for line in event_str.splitlines():
            if line.startswith("data: "):
                item = json.loads(line[len("data: "):])
                event_types.add(item["type"])
                if item["type"] == "chunk":
                    answer_parts.append(item["data"])

    assert {"sources", "chunk", "done"} <= event_types
    assert "".join(answer_parts).strip(), "流式回答不应为空"


@pytest.mark.asyncio
async def test_pipeline_query_stream_async(pipeline):
    """异步流式查询：事件格式与同步一致，且无 athrow 异常"""
    pipeline.index(rebuild=True)

    event_types = set()
    answer_parts = []
    async for event_str in pipeline.query_stream_async("Python 有哪些数据类型？", use_rerank=True):
        for line in event_str.splitlines():
            if line.startswith("data: "):
                item = json.loads(line[len("data: "):])
                event_types.add(item["type"])
                if item["type"] == "chunk":
                    answer_parts.append(item["data"])

    assert {"sources", "chunk", "done"} <= event_types
    assert "".join(answer_parts).strip(), "异步流式回答不应为空"


def test_pipeline_stats(pipeline):
    """状态统计"""
    pipeline.index(rebuild=True)
    stats = pipeline.get_stats()

    assert stats["status"] == "running"
    assert stats["vector_store"]["count"] >= 1
    assert stats["vector_store"]["collection_name"] == "test_pipeline_collection"