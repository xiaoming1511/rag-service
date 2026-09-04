#!/usr/bin/env python
"""
RAG API 服务启动脚本
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn
from fastapi.responses import RedirectResponse

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

    # 1. 加载配置（支持 RAG_CONFIG 环境变量覆盖配置文件路径）
    config = config_manager.load(os.getenv("RAG_CONFIG", "config/settings.yaml"))
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
        mem_cache_capacity=config.performance.embed_cache_capacity,
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

    # 生成器（模型路由：chat/rewrite/research_subqueries 可分别配置模型）
    from src.generation.model_router import ModelRouter

    model_router = ModelRouter(config.omlx.chat_model, config.routing.model_dump())
    generator = Generator(
        client=client,
        model=config.omlx.chat_model,
        max_tokens=config.generation.max_tokens,
        temperature=config.generation.temperature,
        stream=config.generation.stream,
        max_history_rounds=config.generation.max_history_rounds,
        history_token_budget=config.generation.history_token_budget,
        rewrite_query=config.generation.rewrite_query,
        model_router=model_router,
    )

    indexer = Indexer(
        loader=loader,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
        max_workers=config.performance.index_max_workers or None,
    )

    from src.cache.response_cache import ResponseCache
    from src.pipeline.rag_pipeline import RAGPipeline

    # 问答沉淀目录（默认 = 源目录根/syntheses，会被加载器索引，形成知识复利）
    syntheses_dir = config.syntheses.dir
    if not syntheses_dir and config.documents.source_dirs:
        syntheses_dir = str(
            Path(config.documents.source_dirs[0]).expanduser().resolve() / "syntheses"
        )

    pipeline = RAGPipeline(
        retriever=retriever,
        generator=generator,
        max_context_length=2000,
        include_sources=True,
        response_cache=ResponseCache(
            enabled=config.performance.response_cache,
            ttl=config.performance.response_cache_ttl,
        ),
        strict_sources=config.retrieval.strict_sources,
        save_syntheses=config.syntheses.enabled,
        syntheses_dir=syntheses_dir,
    )
    pipeline.indexer = indexer
    pipeline.vector_store = vector_store
    pipeline.model_router = model_router  # 供 /v1/config 热更新路由

    # 增量索引：共享同一同步器（/v1/index/refresh 与自动监听复用清单）
    from src.pipeline.index_sync import IndexSync
    from src.pipeline.watcher import IndexWatcher

    index_sync = IndexSync(indexer)  # manifest 落在向量库目录下
    pipeline._index_sync = index_sync

    # 后台摄入队列（单 worker + 磁盘持久化）
    from src.pipeline.ingest_queue import IngestQueue

    def run_index_job(job):
        """队列任务执行函数（pipeline 同步接口的非阻塞包装）"""
        if job.kind == "full":
            return pipeline.index(rebuild=job.params.get("rebuild", False))
        if job.kind == "incremental":
            return pipeline.index_incremental(rebuild=job.params.get("rebuild", False))
        if job.kind == "url":
            return pipeline.index_url(url=job.params["url"], timeout=job.params.get("timeout", 30.0))
        raise ValueError(f"未知任务类型: {job.kind}")

    ingest_queue = IngestQueue(run_fn=run_index_job, store_path="./data/index_jobs.json")
    pipeline.ingest_queue = ingest_queue

    def on_vault_change():
        """文件变化回调：增量同步（xu/wiki 由外部 LLM Wiki 管理，本系统不处理）"""
        index_sync.sync()

    watcher = IndexWatcher(
        source_dirs=config.documents.source_dirs,
        on_change=on_vault_change,
    )

    print("✅ Pipeline 初始化完成")

    stats = vector_store.get_stats()
    print(f"📊 向量存储: {stats['count']} 个向量")
    if stats['count'] == 0:
        print("⚠️ 索引为空，请通过 POST /v1/index 建立索引")

    # ========== 创建应用 ==========
    app = create_app(pipeline)

    # 根路径 → 跳转到 API 文档（Web 聊天页已移除，查询统一走 Obsidian 插件）
    @app.get("/")
    async def serve_root():
        """根路径重定向到 API 文档"""
        return RedirectResponse(url="/docs")

    print("\n" + "=" * 60)
    print("🌐 API 服务已启动")
    print("   - API 文档: http://127.0.0.1:8080/docs")
    print("   - 健康检查: http://127.0.0.1:8080/v1/health")
    print("   - 增量索引: POST /v1/index/refresh")
    print("   - 后台摄入: POST /v1/index/async · GET /v1/index/jobs")
    print("   - 查询入口: Obsidian 插件「RAG 聊天面板」（Web 页已移除）")
    print("=" * 60)

    # 启动增量索引自动监听（守护线程）与摄入队列 worker（惰性启动）
    watcher.start()
    ingest_queue._ensure_worker()

    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8080,
            log_level="info",
        )
    finally:
        watcher.stop()
        ingest_queue.shutdown()


if __name__ == "__main__":
    main()