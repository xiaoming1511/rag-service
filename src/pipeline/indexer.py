"""
索引服务
负责文档的加载、分块、向量化和存储（支持并行分块，决策 D7）
"""

import concurrent.futures
import hashlib
import os
from collections import Counter
from typing import List, Optional, Dict, Any, Callable
from pathlib import Path
from datetime import datetime

from src.document.loader import DocumentLoader, Document
from src.document.chunker import Chunker, Chunk
from src.embedding.embedder import Embedder
from src.vector_store.base import BaseVectorStore
from src.logging_setup import get_logger

logger = get_logger(__name__)


class Indexer:
    """索引服务"""

    def __init__(
            self,
            loader: DocumentLoader,
            chunker: Chunker,
            embedder: Embedder,
            vector_store: BaseVectorStore,
            collection_name: str = "knowledge_base",
            max_workers: Optional[int] = None,
    ):
        """
        初始化索引服务

        Args:
            loader: 文档加载器
            chunker: 分块器
            embedder: 嵌入服务
            vector_store: 向量存储
            collection_name: 集合名称
            max_workers: 并行分块的线程数；None 表示自动
                （默认 = min(4, CPU 核数)），1 表示不并行
        """
        self.loader = loader
        self.chunker = chunker
        self.embedder = embedder
        self.vector_store = vector_store
        self.collection_name = collection_name
        self.max_workers = max_workers

    @staticmethod
    def _retrieval_text(chunk: Chunk) -> str:
        """
        上下文增强的检索文本：标题路径 + 正文

        标题是最强的语义信号。纯正文块（尤其是代码/命令块）在稠密检索、
        BM25 和重排序三个环节都无法与自然语言提问建立联系——例如
        "2. 节点管理"章节的 kubectl 命令块，若不带标题，
        中文提问"节点管理命令"与英文命令正文的相似度极低，
        导致该块永远无法被召回。标题路径拼进文本后三环节同时受益。
        """
        hp = (chunk.heading_path or "").strip()
        return f"[{hp}]\n{chunk.content}" if hp else chunk.content

    def index_all(
            self,
            source_dirs: Optional[List[str]] = None,
            rebuild: bool = False,
            max_workers: Optional[int] = None,
            cancelled: Optional[Callable[[], bool]] = None,
            progress_cb: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        索引所有文档

        Args:
            source_dirs: 源目录列表（覆盖默认；只更新目录，扩展名沿用 loader 配置）
            rebuild: 是否重建索引（清空现有数据）
            max_workers: 并行分块线程数（决策 D7）；None 使用构造时的默认值
            cancelled: 取消检查回调（供 IngestQueue 长任务取消）；
                在每个嵌入批次边界检查，返回 True 时返回已完成部分的统计
            progress_cb: 进度回调 (stage, current, total, note)
                （供 IngestQueue 作业的数字化进度条）

        Returns:
            Dict: 索引统计信息（取消时含 cancelled: True）
        """
        def cb(stage: str, cur: int, tot: int, note: str = ""):
            if progress_cb is not None:
                progress_cb(stage, cur, tot, note)

        # 如果指定了源目录，更新 loader
        if source_dirs:
            self.loader.source_dirs = [Path(d).expanduser().resolve() for d in source_dirs]

        # （不在此处清空）—— 旧索引保留到嵌入完全成功之后，见下方入库前 clear，
        # 避免嵌入阶段失败（如 oMLX 宕机）时「旧索引已丢、新索引未建」的数据丢失。

        # 1. 加载文档
        print("📂 加载文档...")
        documents = self.loader.load()
        print(f"   ✅ 加载了 {len(documents)} 个文档")
        cb("加载", len(documents), len(documents))

        if not documents:
            return {"total_documents": 0, "total_chunks": 0, "documents": []}

        # 2. 分块（并行：文档之间相互独立；决策 D7）
        print("✂️ 分块处理...")
        workers = max_workers if max_workers is not None else self.max_workers
        if workers is None:
            workers = min(4, os.cpu_count() or 1)

        if workers > 1 and len(documents) > 1:
            # 多线程并行分块，缩短大语料库的建立时间
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                chunk_lists = list(executor.map(self.chunker.chunk_document, documents))
                for i, chunks in enumerate(chunk_lists):
                    if not chunks:
                        print(f"   ⚠️ 文档分块为空: {documents[i].file_name}")
        else:
            chunk_lists = [self.chunker.chunk_document(d) for d in documents]

        all_chunks = [c for cl in chunk_lists for c in cl]
        print(f"   ✅ 生成 {len(all_chunks)} 个块（并行度 {workers}）")
        cb("分块", len(all_chunks), len(all_chunks))

        if not all_chunks:
            return {"total_documents": len(documents), "total_chunks": 0, "documents": []}

        # 3. 生成向量
        print("🔢 生成嵌入向量...")
        # 检索文本（标题路径 + 正文）只算一次：嵌入与入库用的是同一份文本，
        # 原先在两处各算一遍属于重复的正则/字符串拼接开销
        chunk_texts = [self._retrieval_text(c) for c in all_chunks]

        # 分批处理，避免内存问题
        batch_size = 100
        all_embeddings = []

        for i in range(0, len(chunk_texts), batch_size):
            # 取消检查（批次粒度）：尚未入库，已生成向量在缓存中可复用
            if cancelled is not None and cancelled():
                print("⏹️ 索引任务已取消")
                return {
                    "total_documents": len(documents),
                    "total_chunks": len(all_chunks),
                    # total_vectors 是「已生成向量数」，取消时并未入库，
                    # 故显式带 stored=False + vector_store_count 说明真实落库量
                    "total_vectors": len(all_embeddings),
                    "stored": False,
                    "documents": [],
                    "vector_store_count": self.vector_store.count(),
                    "cancelled": True,
                }

            batch = chunk_texts[i:i + batch_size]
            embeddings = self.embedder.embed(batch)
            all_embeddings.extend(embeddings)

            # 显示进度（控制台 + 数字化进度回调）
            progress = min(i + batch_size, len(chunk_texts))
            print(f"   📊 进度: {progress}/{len(chunk_texts)}")
            cb("嵌入", progress, len(chunk_texts), f"批次 {i // batch_size + 1}")

        print(f"   ✅ 生成 {len(all_embeddings)} 个向量")

        # 4. 存储到向量数据库
        print("💾 存储到向量数据库...")
        ids = [c.id for c in all_chunks]
        documents_content = chunk_texts  # 与嵌入使用同一份检索文本
        metadatas = [c.metadata for c in all_chunks]

        # 重建模式：在嵌入全部成功之后才清空旧索引，再一次性入库，
        # 避免嵌入阶段失败导致旧索引已丢、新索引未建的不可恢复状态
        if rebuild:
            self.vector_store.clear()

        self.vector_store.add(
            ids=ids,
            embeddings=all_embeddings,
            documents=documents_content,
            metadatas=metadatas,
        )
        cb("存储", 1, 1)

        # 统计信息（Counter 分组：避免 文档数 × 块数 的双重遍历）
        doc_chunk_counts = Counter(c.metadata.get('doc_id') for c in all_chunks)
        stats = {
            "total_documents": len(documents),
            "total_chunks": len(all_chunks),
            "total_vectors": len(all_embeddings),
            "documents": [
                {
                    "file_name": d.file_name,
                    "file_path": d.file_path,
                    "chunk_count": doc_chunk_counts.get(d.id, 0),
                }
                for d in documents
            ],
            "vector_store_count": self.vector_store.count(),
        }

        print(f"\n✅ 索引完成!")
        print(f"   - 文档数: {stats['total_documents']}")
        print(f"   - 块总数: {stats['total_chunks']}")
        print(f"   - 向量数: {stats['total_vectors']}")

        return stats

    def index_single(self, file_path: str, overwrite: bool = False) -> Dict[str, Any]:
        """
        索引单个文档

        Args:
            file_path: 文件路径
            overwrite: 已存在时是否覆盖（先删旧块再入新块）。
                用于增量同步的「变更」分支：先完成加载+分块+嵌入，全部成功后
                才删旧块、入新块，避免「先删后失败」导致数据丢失。
        Returns:
            Dict: 索引结果
        """
        # 1. 加载单个文档
        document = self.loader.load_single(file_path)
        if not document:
            return {"success": False, "error": "文档加载失败"}

        # 检查是否已存在（按 doc_id 元数据查询该文档的全部块）
        existing = self.vector_store.get_by_doc_id(document.id)
        if existing and not overwrite:
            print(f"⚠️ 文档已存在，跳过: {document.file_name}")
            return {"success": True, "skipped": True}

        # 2. 分块
        chunks = self.chunker.chunk_document(document)
        if not chunks:
            return {"success": False, "error": "分块失败"}

        # 3. 生成向量
        chunk_texts = [self._retrieval_text(c) for c in chunks]
        embeddings = self.embedder.embed(chunk_texts)

        # 4. 存储（overwrite 时先删旧块；此时嵌入已成功，删除失败不会丢「未写入」的新数据）
        if overwrite and existing:
            self.vector_store.delete_by_doc_id(document.id)

        ids = [c.id for c in chunks]
        documents_content = [self._retrieval_text(c) for c in chunks]
        metadatas = [c.metadata for c in chunks]

        self.vector_store.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents_content,
            metadatas=metadatas,
        )

        return {
            "success": True,
            "file_name": document.file_name,
            "chunks": len(chunks),
        }

    def get_stats(self) -> Dict[str, Any]:
        """获取索引统计信息"""
        return {
            "total_chunks": self.vector_store.count(),
            "collection_name": self.collection_name,
            "embedding_dimension": self.embedder.get_embedding_dimension(),
        }

    def index_url(self, url: str, timeout: float = 30.0) -> Dict[str, Any]:
        """
        索引远程网页（抓取 → 解析 → 分块 → 嵌入 → 入库）

        Args:
            url: 网页地址
            timeout: 抓取超时秒数

        Returns:
            Dict: 索引结果
        """
        # 1. 抓取并解析为文档
        document = self.loader.load_url(url, timeout=timeout)
        if not document:
            return {"success": False, "error": "网页抓取或解析失败"}

        # 2. 检查是否已存在（按 URL 文档 ID）
        existing = self.vector_store.get_by_doc_id(document.id)
        if existing:
            logger.warning("网页已索引，跳过: %s", url)
            return {"success": True, "skipped": True, "url": url}

        # 3. 分块
        chunks = self.chunker.chunk_document(document)
        if not chunks:
            return {"success": False, "error": "分块失败（网页内容为空）"}

        # 4. 生成向量并入库
        chunk_texts = [self._retrieval_text(c) for c in chunks]
        embeddings = self.embedder.embed(chunk_texts)

        ids = [c.id for c in chunks]
        documents_content = [self._retrieval_text(c) for c in chunks]
        metadatas = [c.metadata for c in chunks]

        self.vector_store.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents_content,
            metadatas=metadatas,
        )

        return {
            "success": True,
            "url": url,
            "title": document.metadata.get("title", ""),
            "chunks": len(chunks),
        }