"""
索引服务
负责文档的加载、分块、向量化和存储
"""

from typing import List, Optional, Dict, Any
from pathlib import Path
import hashlib
from datetime import datetime

from src.document.loader import DocumentLoader, Document
from src.document.chunker import Chunker, Chunk
from src.embedding.embedder import Embedder
from src.vector_store.base import BaseVectorStore


class Indexer:
    """索引服务"""

    def __init__(
            self,
            loader: DocumentLoader,
            chunker: Chunker,
            embedder: Embedder,
            vector_store: BaseVectorStore,
            collection_name: str = "knowledge_base",
    ):
        """
        初始化索引服务

        Args:
            loader: 文档加载器
            chunker: 分块器
            embedder: 嵌入服务
            vector_store: 向量存储
            collection_name: 集合名称
        """
        self.loader = loader
        self.chunker = chunker
        self.embedder = embedder
        self.vector_store = vector_store
        self.collection_name = collection_name

    def index_all(
            self,
            source_dirs: Optional[List[str]] = None,
            rebuild: bool = False,
    ) -> Dict[str, Any]:
        """
        索引所有文档

        Args:
            source_dirs: 源目录列表（覆盖默认）
            rebuild: 是否重建索引（清空现有数据）

        Returns:
            Dict: 索引统计信息
        """
        # 如果指定了源目录，更新 loader
        if source_dirs:
            self.loader.source_dirs = [Path(d).expanduser().resolve() for d in source_dirs]

        # 清空现有数据（如果需要）
        if rebuild:
            print("🧹 清空现有索引...")
            self.vector_store.clear()

        # 1. 加载文档
        print("📂 加载文档...")
        documents = self.loader.load()
        print(f"   ✅ 加载了 {len(documents)} 个文档")

        if not documents:
            return {"total_documents": 0, "total_chunks": 0, "documents": []}

        # 2. 分块
        print("✂️ 分块处理...")
        all_chunks = self.chunker.chunk_documents(documents)
        print(f"   ✅ 生成 {len(all_chunks)} 个块")

        if not all_chunks:
            return {"total_documents": len(documents), "total_chunks": 0, "documents": []}

        # 3. 生成向量
        print("🔢 生成嵌入向量...")
        chunk_texts = [c.content for c in all_chunks]

        # 分批处理，避免内存问题
        batch_size = 100
        all_embeddings = []

        for i in range(0, len(chunk_texts), batch_size):
            batch = chunk_texts[i:i + batch_size]
            embeddings = self.embedder.embed(batch)
            all_embeddings.extend(embeddings)

            # 显示进度
            progress = min(i + batch_size, len(chunk_texts))
            print(f"   📊 进度: {progress}/{len(chunk_texts)}")

        print(f"   ✅ 生成 {len(all_embeddings)} 个向量")

        # 4. 存储到向量数据库
        print("💾 存储到向量数据库...")
        ids = [c.id for c in all_chunks]
        documents_content = [c.content for c in all_chunks]
        metadatas = [c.metadata for c in all_chunks]

        self.vector_store.add(
            ids=ids,
            embeddings=all_embeddings,
            documents=documents_content,
            metadatas=metadatas,
        )

        # 统计信息
        stats = {
            "total_documents": len(documents),
            "total_chunks": len(all_chunks),
            "total_vectors": len(all_embeddings),
            "documents": [
                {
                    "file_name": d.file_name,
                    "file_path": d.file_path,
                    "chunk_count": len([c for c in all_chunks if c.metadata.get('doc_id') == d.id]),
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

    def index_single(self, file_path: str) -> Dict[str, Any]:
        """
        索引单个文档

        Args:
            file_path: 文件路径

        Returns:
            Dict: 索引结果
        """
        # 1. 加载单个文档
        document = self.loader.load_single(file_path)
        if not document:
            return {"success": False, "error": "文档加载失败"}

        # 检查是否已存在（按 doc_id 元数据查询该文档的全部块）
        existing = self.vector_store.get_by_doc_id(document.id)
        if existing:
            print(f"⚠️ 文档已存在，跳过: {document.file_name}")
            return {"success": True, "skipped": True}

        # 2. 分块
        chunks = self.chunker.chunk_document(document)
        if not chunks:
            return {"success": False, "error": "分块失败"}

        # 3. 生成向量
        chunk_texts = [c.content for c in chunks]
        embeddings = self.embedder.embed(chunk_texts)

        # 4. 存储
        ids = [c.id for c in chunks]
        documents_content = [c.content for c in chunks]
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