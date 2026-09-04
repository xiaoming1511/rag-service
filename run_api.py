#!/usr/bin/env python
"""
RAG API 服务启动脚本
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from src.config import config_manager
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
from src.api.app import create_app


def main():
    """启动 API 服务"""
    print("=" * 60)
    print("🚀 启动 RAG API 服务")
    print("=" * 60)

    # 1. 加载配置
    config = config_manager.load("config/settings.yaml")
    print("✅ 配置加载成功")

    # 2. 初始化所有组件
    print("🔧 初始化 RAG 组件...")

    client = OMLXClient(
        base_url=config.omlx.base_url,
        api_key=config.omlx.api_key,
        timeout=config.omlx.timeout,
    )

    embedder = Embedder(
        client=client,
        model=config.omlx.embedding_model,
        cache_enabled=True,
    )

    vector_store = ChromaStore(
        collection_name=config.vector_store.collection_name,
        persist_directory=config.vector_store.persist_directory,
    )

    loader = DocumentLoader(
        source_dirs=config.documents.source_dirs,
        extensions=config.documents.supported_extensions,
    )

    chunker = Chunker(
        chunk_size=config.chunker.chunk_size,
        overlap=config.chunker.overlap,
        strategy=config.chunker.strategy,
    )

    reranker = Reranker(
        client=client,
        model=config.omlx.reranker_model,
        enabled=config.retrieval.enable_rerank,
    )

    retriever = Retriever(
        vector_store=vector_store,
        embedder=embedder,
        reranker=reranker,
        top_k=config.retrieval.top_k,
        rerank_top_k=config.retrieval.rerank_top_k,
        similarity_threshold=config.retrieval.similarity_threshold,
    )

    generator = Generator(
        client=client,
        model=config.omlx.chat_model,
        max_tokens=config.generation.max_tokens,
        temperature=config.generation.temperature,
        stream=config.generation.stream,
    )

    indexer = Indexer(
        loader=loader,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
    )

    pipeline = RAGPipeline(
        retriever=retriever,
        generator=generator,
        max_context_length=2000,
        include_sources=True,
    )
    pipeline.indexer = indexer
    pipeline.vector_store = vector_store

    # 增量索引：共享同一同步器（/v1/index/refresh 与自动监听复用清单）
    from src.pipeline.index_sync import IndexSync
    from src.pipeline.watcher import IndexWatcher

    index_sync = IndexSync(indexer)  # manifest 落在向量库目录下
    pipeline._index_sync = index_sync

    watcher = IndexWatcher(
        source_dirs=config.documents.source_dirs,
        on_change=lambda: index_sync.sync(),
    )

    print("✅ Pipeline 初始化完成")

    stats = vector_store.get_stats()
    print(f"📊 向量存储: {stats['count']} 个向量")
    if stats['count'] == 0:
        print("⚠️ 索引为空，请通过 POST /v1/index 建立索引")

    # ========== 创建应用 ==========
    app = create_app(pipeline)

    # 挂载静态文件目录
    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

        @app.get("/")
        async def serve_index():
            """提供聊天界面"""
            return FileResponse(str(web_dir / "index.html"))

    print("\n" + "=" * 60)
    print("🌐 API 服务已启动")
    print("   - API 文档: http://127.0.0.1:8080/docs")
    print("   - 聊天界面: http://127.0.0.1:8080")
    print("   - 健康检查: http://127.0.0.1:8080/v1/health")
    print("   - 增量索引: POST /v1/index/refresh")
    print("=" * 60)

    # 启动增量索引自动监听（守护线程）
    watcher.start()

    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8080,
            log_level="info",
        )
    finally:
        watcher.stop()


if __name__ == "__main__":
    main()