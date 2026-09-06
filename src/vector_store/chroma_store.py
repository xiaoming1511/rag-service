"""
ChromaDB 向量存储实现
"""

import uuid
from typing import List, Optional, Dict, Any

import chromadb
from chromadb.config import Settings

from src.vector_store.base import BaseVectorStore, SearchResult


class ChromaStore(BaseVectorStore):
    """ChromaDB 向量存储"""

    def __init__(
            self,
            collection_name: str = "knowledge_base",
            persist_directory: str = "./data/chroma_db",
            embedding_dimension: int = 1024,
    ):
        """
        初始化 ChromaDB

        Args:
            collection_name: 集合名称
            persist_directory: 持久化目录
            embedding_dimension: 向量维度
        """
        self.collection_name = collection_name
        self.persist_directory = persist_directory
        self.embedding_dimension = embedding_dimension

        # 初始化 ChromaDB 客户端
        self._client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(
                anonymized_telemetry=False,
                allow_reset=True,
            )
        )

        # 获取或创建集合
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},  # 使用余弦相似度
        )

    def add(
            self,
            ids: List[str],
            embeddings: List[List[float]],
            documents: List[str],
            metadatas: List[Dict[str, Any]]
    ) -> None:
        """
        添加向量和文档
        """
        if not ids:
            return

        # 确保所有 ID 都是字符串
        ids = [str(id) for id in ids]

        # 处理元数据中的特殊字符（ChromaDB 要求元数据值必须是 str, int, float, bool）
        processed_metadatas = []
        for meta in metadatas:
            processed = {}
            for key, value in meta.items():
                if isinstance(value, (str, int, float, bool)):
                    processed[key] = value
                elif value is None:
                    processed[key] = ""
                else:
                    processed[key] = str(value)
            processed_metadatas.append(processed)

        # 添加向量
        self._collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=processed_metadatas,
        )

    def search(
            self,
            query_embedding: List[float],
            top_k: int = 5,
            where: Optional[Dict[str, Any]] = None
    ) -> List[SearchResult]:
        """
        相似度搜索
        """
        results = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
        )

        # 解析结果
        search_results = []

        if results['ids'] and results['ids'][0]:
            for i, doc_id in enumerate(results['ids'][0]):
                score = 1.0 - results['distances'][0][i] if results.get('distances') else 1.0
                search_results.append(SearchResult(
                    id=doc_id,
                    content=results['documents'][0][i] if results.get('documents') else "",
                    metadata=results['metadatas'][0][i] if results.get('metadatas') else {},
                    score=score,
                ))

        return search_results

    def delete(self, ids: List[str]) -> None:
        """
        删除文档
        """
        if ids:
            self._collection.delete(ids=[str(id) for id in ids])

    def count(self) -> int:
        """
        返回存储的文档数量
        """
        return self._collection.count()

    def clear(self) -> None:
        """
        清空所有数据
        """
        # 删除集合
        self._client.delete_collection(self.collection_name)
        # 重新创建集合
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def get(self, ids: List[str]) -> List[Dict[str, Any]]:
        """
        根据 ID 获取文档
        """
        if not ids:
            return []

        results = self._collection.get(ids=[str(id) for id in ids])

        documents = []
        if results['ids']:
            for i, doc_id in enumerate(results['ids']):
                doc = {
                    'id': doc_id,
                    'document': results['documents'][i] if results.get('documents') else "",
                    'metadata': results['metadatas'][i] if results.get('metadatas') else {},
                }
                documents.append(doc)

        return documents

    def get_by_doc_id(self, doc_id: str) -> List[Dict[str, Any]]:
        """
        根据文档 ID（doc_id 元数据）查询该文档的全部块

        增量索引中用于判断文档是否已入库、以及变更前定位待删除块。
        """
        results = self._collection.get(where={"doc_id": str(doc_id)})

        documents = []
        ids = results.get('ids') or []
        for i, cid in enumerate(ids):
            documents.append({
                'id': cid,
                'document': (results.get('documents') or [])[i] if results.get('documents') else "",
                'metadata': (results.get('metadatas') or [])[i] if results.get('metadatas') else {},
            })
        return documents

    def delete_by_doc_id(self, doc_id: str) -> None:
        """
        删除某文档（doc_id 元数据）的全部块

        用于文档变更（先删后加）或文件删除时的增量清理。
        """
        self._collection.delete(where={"doc_id": str(doc_id)})

    def get_all(self) -> List[Dict[str, Any]]:
        """
        返回库内全部块（BM25 索引构建等全量语料场景）
        """
        results = self._collection.get()
        items = []
        ids = results.get("ids") or []
        documents = results.get("documents") or []
        metadatas = results.get("metadatas") or []
        for i, cid in enumerate(ids):
            items.append({
                "id": cid,
                "document": documents[i] if i < len(documents) else "",
                "metadata": metadatas[i] if i < len(metadatas) else {},
            })
        return items

    def get_stats(self) -> Dict[str, Any]:
        """获取存储统计信息"""
        return {
            'collection_name': self.collection_name,
            'count': self.count(),
            'persist_directory': self.persist_directory,
            'embedding_dimension': self.embedding_dimension,
        }