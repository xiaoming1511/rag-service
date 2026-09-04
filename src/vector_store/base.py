"""
向量存储抽象基类
定义统一的增删改查接口
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass


@dataclass
class SearchResult:
    """搜索结果"""
    id: str
    content: str
    metadata: Dict[str, Any]
    score: float

    def __repr__(self):
        return f"SearchResult(id={self.id}, score={self.score:.4f})"


class BaseVectorStore(ABC):
    """向量存储抽象基类"""

    @abstractmethod
    def add(
            self,
            ids: List[str],
            embeddings: List[List[float]],
            documents: List[str],
            metadatas: List[Dict[str, Any]]
    ) -> None:
        """
        添加向量和文档

        Args:
            ids: 唯一标识列表
            embeddings: 向量列表
            documents: 文档内容列表
            metadatas: 元数据列表
        """
        pass

    @abstractmethod
    def search(
            self,
            query_embedding: List[float],
            top_k: int = 5,
            where: Optional[Dict[str, Any]] = None
    ) -> List[SearchResult]:
        """
        相似度搜索

        Args:
            query_embedding: 查询向量
            top_k: 返回结果数量
            where: 过滤条件

        Returns:
            List[SearchResult]: 搜索结果列表
        """
        pass

    @abstractmethod
    def delete(self, ids: List[str]) -> None:
        """删除文档"""
        pass

    @abstractmethod
    def count(self) -> int:
        """返回存储的文档数量"""
        pass

    @abstractmethod
    def clear(self) -> None:
        """清空所有数据"""
        pass

    @abstractmethod
    def get(self, ids: List[str]) -> List[Dict[str, Any]]:
        """根据 ID 获取文档"""
        pass