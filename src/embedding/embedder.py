"""
嵌入服务
提供文本向量化功能，支持批量处理、磁盘缓存，以及同步/异步两种调用方式
"""

import hashlib
import json
from typing import List, Optional, Dict, Any
from pathlib import Path

from src.embedding.client import OMLXClient


class Embedder:
    """文本嵌入服务"""

    def __init__(
            self,
            client: OMLXClient,
            model: str,
            cache_enabled: bool = True,
            cache_dir: str = "./data/cache/embeddings"
    ):
        """
        初始化嵌入服务

        Args:
            client: oMLX 客户端
            model: 嵌入模型名称
            cache_enabled: 是否启用缓存
            cache_dir: 缓存目录
        """
        self.client = client
        self.model = model
        self.cache_enabled = cache_enabled

        if cache_enabled:
            self.cache_dir = Path(cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        同步嵌入文本

        优先命中磁盘缓存（以文本 MD5 为键），
        未命中的文本分批量调用 API，并写回缓存。

        Args:
            texts: 文本列表

        Returns:
            List[List[float]]: 向量列表
        """
        if not texts:
            return []

        # 检查缓存
        if self.cache_enabled:
            results = []
            uncached_texts = []
            uncached_indices = []

            for i, text in enumerate(texts):
                cached = self._get_cache(text)
                if cached is not None:
                    results.append(cached)
                else:
                    uncached_texts.append(text)
                    uncached_indices.append(i)
                    results.append(None)  # 占位

            # 批量处理未缓存的文本
            if uncached_texts:
                # 分批处理，避免单次请求过大
                batch_size = 100
                for i in range(0, len(uncached_texts), batch_size):
                    batch = uncached_texts[i:i + batch_size]
                    embeddings = self.client.embed_sync(self.model, batch)

                    for j, emb in enumerate(embeddings):
                        idx = uncached_indices[i + j]
                        results[idx] = emb
                        self._save_cache(uncached_texts[i + j], emb)

            # 过滤掉 None（理论上不应该有）
            return [r for r in results if r is not None]

        # 不使用缓存，直接调用 API
        return self.client.embed_sync(self.model, texts)

    async def embed_async(self, texts: List[str]) -> List[List[float]]:
        """
        异步嵌入文本（与同步版本共享同一套缓存逻辑）

        Args:
            texts: 文本列表

        Returns:
            List[List[float]]: 向量列表
        """
        if not texts:
            return []

        # 检查缓存
        if self.cache_enabled:
            results = []
            uncached_texts = []
            uncached_indices = []

            for i, text in enumerate(texts):
                cached = self._get_cache(text)
                if cached is not None:
                    results.append(cached)
                else:
                    uncached_texts.append(text)
                    uncached_indices.append(i)
                    results.append(None)

            if uncached_texts:
                batch_size = 100
                for i in range(0, len(uncached_texts), batch_size):
                    batch = uncached_texts[i:i + batch_size]
                    embeddings = await self.client.embed_async(self.model, batch)

                    for j, emb in enumerate(embeddings):
                        idx = uncached_indices[i + j]
                        results[idx] = emb
                        self._save_cache(uncached_texts[i + j], emb)

            return [r for r in results if r is not None]

        return await self.client.embed_async(self.model, texts)

    def embed_single(self, text: str) -> List[float]:
        """嵌入单个文本（同步）"""
        result = self.embed([text])
        return result[0] if result else []

    async def embed_single_async(self, text: str) -> List[float]:
        """嵌入单个文本（异步）"""
        result = await self.embed_async([text])
        return result[0] if result else []

    # ================================================================
    # 缓存工具
    # ================================================================

    def _get_cache_key(self, text: str) -> str:
        """生成缓存键（文本的 MD5 哈希）"""
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def _get_cache(self, text: str) -> Optional[List[float]]:
        """从缓存获取向量，未命中返回 None"""
        cache_key = self._get_cache_key(text)
        cache_file = self.cache_dir / f"{cache_key}.json"

        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                return data['embedding']
            except Exception:
                return None
        return None

    def _save_cache(self, text: str, embedding: List[float]):
        """保存向量到缓存（写入失败不影响主流程）"""
        cache_key = self._get_cache_key(text)
        cache_file = self.cache_dir / f"{cache_key}.json"

        try:
            with open(cache_file, 'w') as f:
                json.dump({
                    'text': text,
                    'embedding': embedding,
                }, f)
        except Exception:
            pass  # 缓存写入失败不影响主流程

    def get_embedding_dimension(self) -> int:
        """
        获取嵌入向量维度

        注：bge-m3 的向量维度固定为 1024，暂以常量返回；
        如需精确获取，可调用一次嵌入接口探测。
        """
        # bge-m3 的维度是 1024
        return 1024