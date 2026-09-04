"""
命令行入口
提供 RAG 服务的命令行交互：索引（index）/ 查询（query）/ 状态（stats）
"""

import sys
import os
import json
import argparse
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

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


def load_config():
    """加载配置（支持 RAG_CONFIG 环境变量覆盖配置文件路径）"""
    config_path = os.getenv("RAG_CONFIG", "config/settings.yaml")
    return config_manager.load(config_path)


def build_pipeline() -> RAGPipeline:
    """
    根据配置构建完整的 RAG Pipeline

    与 run_api.py 保持一致：依次初始化
    客户端 → 嵌入 → 向量存储 → 加载器/分块器 → 重排序 → 检索 → 生成 → 索引
    """
    config = load_config()

    # oMLX 客户端（同步 + 异步）
    client = OMLXClient(
        base_url=config.omlx.base_url,
        api_key=config.omlx.api_key,
        timeout=config.omlx.timeout,
    )

    # 嵌入服务
    embedder = Embedder(
        client=client,
        model=config.omlx.embedding_model,
        cache_enabled=True,
    )

    # 向量存储
    vector_store = ChromaStore(
        collection_name=config.vector_store.collection_name,
        persist_directory=config.vector_store.persist_directory,
    )

    # 文档加载器与分块器
    loader = DocumentLoader(
        source_dirs=config.documents.source_dirs,
        extensions=config.documents.supported_extensions,
    )
    chunker = Chunker(
        chunk_size=config.chunker.chunk_size,
        overlap=config.chunker.overlap,
        strategy=config.chunker.strategy,
    )

    # 重排序与检索
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

    # 生成器
    generator = Generator(
        client=client,
        model=config.omlx.chat_model,
        max_tokens=config.generation.max_tokens,
        temperature=config.generation.temperature,
        stream=config.generation.stream,
    )

    # 索引器
    indexer = Indexer(
        loader=loader,
        chunker=chunker,
        embedder=embedder,
        vector_store=vector_store,
    )

    # 组装 Pipeline
    pipeline = RAGPipeline(
        retriever=retriever,
        generator=generator,
        max_context_length=2000,
        include_sources=True,
    )
    pipeline.indexer = indexer
    pipeline.vector_store = vector_store

    return pipeline


# ================================================================
# 子命令实现
# ================================================================

def cmd_index(args):
    """索引文档命令"""
    print("📚 开始索引文档...")
    print("-" * 50)

    pipeline = build_pipeline()

    # 解析目录参数（逗号分隔）
    source_dirs = None
    if args.dirs:
        source_dirs = [d.strip() for d in args.dirs.split(",")]

    result = pipeline.index(
        source_dirs=source_dirs,
        rebuild=args.rebuild,
    )

    # 索引失败处理
    if result.get("error"):
        print(f"❌ 索引失败: {result['error']}")
        sys.exit(1)

    print("\n" + "-" * 50)
    print(f"✅ 索引完成!")
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_query(args):
    """查询命令（非流式）"""
    pipeline = build_pipeline()

    print("🔍 查询中...")
    print("-" * 50)

    result = pipeline.query(
        question=args.question,
        use_rerank=not args.no_rerank,
        top_k=args.top_k,
    )

    print(f"\n📝 回答:")
    print("-" * 30)
    print(result["answer"])

    print("\n" + "-" * 50)
    print(f"📚 引用来源 ({len(result['sources'])} 个):")
    for i, source in enumerate(result["sources"]):
        print(f"  [{i + 1}] {source['file_name']} (分数: {source['score']:.4f})")


def _parse_sse_line(line: str):
    """解析单条 SSE 数据行（"data: {json}"），返回字典或 None"""
    if not line.startswith("data: "):
        return None
    try:
        return json.loads(line[len("data: "):])
    except json.JSONDecodeError:
        return None


def cmd_query_stream(args):
    """查询命令（流式，解析 pipeline 产出的 SSE 事件）"""
    pipeline = build_pipeline()

    print("🔍 查询中...")
    print("-" * 50)
    print(f"\n📝 回答 (流式):")
    print("-" * 30)

    for event_str in pipeline.query_stream(
            question=args.question,
            use_rerank=not args.no_rerank,
            top_k=args.top_k,
    ):
        # pipeline.query_stream 逐条产出 "data: {json}\n\n"
        for line in event_str.splitlines():
            item = _parse_sse_line(line)
            if item is None:
                continue

            etype = item.get("type")
            if etype == "sources":
                print(f"\n📚 找到 {len(item.get('data', []))} 个来源")
            elif etype == "chunk":
                print(item.get("data", ""), end="", flush=True)
            elif etype == "done":
                print("\n")
            elif etype == "error":
                print(f"\n❌ 错误: {item.get('data')}")

    print("-" * 30)


def cmd_stats(args):
    """状态命令：查看向量库统计信息"""
    pipeline = build_pipeline()
    stats = pipeline.get_stats()
    print(json.dumps(stats, indent=2, ensure_ascii=False))


# ================================================================
# 主入口
# ================================================================

def main():
    """主入口（参数解析 + 命令分发）"""
    parser = argparse.ArgumentParser(description="RAG Service CLI")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # index 命令
    index_parser = subparsers.add_parser("index", help="索引文档")
    index_parser.add_argument("--dirs", help="源目录（逗号分隔）")
    index_parser.add_argument("--rebuild", action="store_true", help="重建索引")

    # query 命令
    query_parser = subparsers.add_parser("query", help="查询")
    query_parser.add_argument("question", help="问题")
    query_parser.add_argument("--no-rerank", action="store_true", help="禁用重排序")
    query_parser.add_argument("--top-k", type=int, default=3, help="返回结果数量")
    query_parser.add_argument("--stream", action="store_true", help="流式输出")

    # stats 命令
    subparsers.add_parser("stats", help="查看状态")

    args = parser.parse_args()

    if args.command == "index":
        cmd_index(args)
    elif args.command == "query":
        if args.stream:
            cmd_query_stream(args)
        else:
            cmd_query(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()