"""
BM25 稀疏检索索引（决策 B3：混合检索）

设计：
- 语料来自向量库全量块（get_all），jieba 分词 + 英文小写归一
- 惰性构建；以 vector_store.count() 判断新鲜度——计数变化即重建
  （同数量的文档更新在下次计数变化前使用旧索引，个人场景可接受；
  需要立即失效可调 invalidate()，IndexSync 每次同步后已接线调用）
- 查询侧与文档侧同一切分器
- RRF（Reciprocal Rank Fusion）：score = Σ 1/(k + rank)，k 默认 60，
  只用排名不用原始分，天然规避 BM25 分与 cosine 分的量纲问题
"""

import re
from threading import Lock
from typing import Any, Dict, List, Sequence

from src.logging_setup import get_logger
from src.vector_store.base import BaseVectorStore, SearchResult

logger = get_logger(__name__)

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


def tokenize(text: str) -> List[str]:
    """
    混合语料分词：jieba 处理中文，正则提取英文/数字词并小写归一

    jieba 首次调用会加载词典（约 1 秒），后续调用开销可忽略。
    """
    import jieba

    tokens = [t.strip() for t in jieba.lcut(text) if t.strip()]
    final: List[str] = []
    for t in tokens:
        if _PUNCT_RE.fullmatch(t):
            continue  # 纯标点/符号
        if _WORD_RE.fullmatch(t):
            final.append(t.lower())  # 英文/数字词统一小写
        else:
            final.extend(m.lower() for m in _WORD_RE.findall(t) if m)
            if not _WORD_RE.search(t):
                final.append(t)  # 中文词原样保留
    return final


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = 60,
             top_n: int = 20) -> List[str]:
    """
    Reciprocal Rank Fusion：按多个排名列表融合出统一排序

    Args:
        rankings: 每路检索的候选 id 列表（按该路相关性降序）
        k: RRF 常数（论文默认 60，越大越平滑）
        top_n: 融合后保留数量

    Returns:
        List[str]: 融合后的 id 列表（降序），同分按首次出现顺序稳定排序
    """
    scores: Dict[str, float] = {}
    first_seen: Dict[str, int] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
            if item_id not in first_seen:
                first_seen[item_id] = len(first_seen)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return [item_id for item_id, _ in ordered[:top_n]]


class BM25Index:
    """向量库语料上的 BM25 稀疏索引"""

    def __init__(self, vector_store: BaseVectorStore):
        self.vector_store = vector_store
        self._bm25 = None
        self._corpus: List[Dict[str, Any]] = []  # [{id, content, metadata}]
        self._built_count: int = -1
        # 重建/失效与查询之间存在竞态：多线程并发 search 会在 _ensure_fresh 里
        # 交叉重写 _corpus/_bm25，导致「corpus 长度与索引不一致」的越界或错位。
        # 用锁串行化重建 + 局部构建完再一次原子替换。
        self._lock = Lock()

    def invalidate(self) -> None:
        """标记索引过期（索引同步/重建后调用，下次查询时重建）"""
        with self._lock:
            self._built_count = -1

    def _ensure_fresh(self) -> None:
        """计数变化则重建（增量同步后块数必然变化，天然触发）"""
        with self._lock:
            current = self.vector_store.count()
            if self._bm25 is not None and current == self._built_count:
                return
            from rank_bm25 import BM25Okapi

            # 在局部变量构建完整的新索引，最后一次原子替换，
            # 避免并发 search 读到「corpus 已更新但 bm25 未更新」的中间态
            corpus_items = self.vector_store.get_all()
            new_corpus = [
                {"id": item["id"], "content": item["document"], "metadata": item["metadata"]}
                for item in corpus_items
            ]
            tokenized = [tokenize(c["content"]) for c in new_corpus]
            new_bm25 = BM25Okapi(tokenized) if tokenized else None

            self._corpus = new_corpus
            self._bm25 = new_bm25
            self._built_count = current
            logger.info("BM25 索引已构建: %d 块", len(self._corpus))

    def search(self, query: str, top_n: int = 20) -> List[SearchResult]:
        """
        BM25 稀疏检索

        Returns:
            List[SearchResult]: 按 BM25 分降序；score 为 BM25 分（与 cosine 不同量纲）
        """
        self._ensure_fresh()
        # 读取索引与语料时持有锁，保证二者同源（重建中不会被读到中间态）
        with self._lock:
            if self._bm25 is None or not query.strip():
                return []
            scores = self._bm25.get_scores(tokenize(query))
            order = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_n]
            return [
                SearchResult(
                    id=self._corpus[i]["id"],
                    content=self._corpus[i]["content"],
                    metadata=self._corpus[i]["metadata"],
                    score=float(scores[i]),
                )
                for i in order
                if scores[i] > 0
            ]
