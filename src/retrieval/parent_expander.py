"""
父子块召回（B4，方案 A：检索时父块扩展，零重建）

small-to-big 策略：用小块（chunk）做精准召回，命中后在检索时实时
把同一「父块」（同 doc_id + heading_path 的全部兄弟块，按 chunk_index
拼接）装配出来送入上下文。

相对「入库双写父子块」（方案 B）的优点：
- 零重建索引、向量库体积不变
- 父块粒度（token 上限）可运行时调整
- 命中小块的分数与排序保持不变，来源展示仍指向命中小块

父块超过 token 上限时，以命中块为中心向前后兄弟扩展，保证命中
内容始终在窗口内。
"""

from typing import Dict, List, Optional, Tuple

from src.retrieval.context_builder import estimate_tokens
from src.vector_store.base import BaseVectorStore, SearchResult


def _parent_key(result: SearchResult) -> Optional[Tuple[str, str]]:
    """父块分组键 (doc_id, heading_path)；无 doc_id 的块（如异常数据）不扩展"""
    doc_id = result.metadata.get("doc_id")
    if not doc_id:
        return None
    return (str(doc_id), result.metadata.get("heading_path", ""))


def _assemble_parent(
        siblings: List[Dict],
        hit_id: str,
        max_tokens: int,
) -> str:
    """
    从兄弟块列表装配父块内容

    Args:
        siblings: 同一父块的块列表（dict: id/document/metadata）
        hit_id: 命中块 ID（保证其在窗口内）
        max_tokens: 父块 token 上限

    Returns:
        str: 按 chunk_index 顺序拼接的父块内容
    """
    siblings = sorted(
        siblings,
        key=lambda d: (d.get("metadata") or {}).get("chunk_index", 0),
    )

    # 预算内可全量拼接
    full = [d.get("document") or "" for d in siblings]
    total = estimate_tokens("\n\n".join(full))
    if total <= max_tokens:
        return "\n\n".join(full)

    # 超限：以命中块为中心，前后交替纳入兄弟块
    hit_pos = 0
    for i, d in enumerate(siblings):
        if d.get("id") == hit_id:
            hit_pos = i
            break

    selected = {hit_pos}
    used = estimate_tokens(full[hit_pos])
    lo, hi = hit_pos - 1, hit_pos + 1
    while (lo >= 0 or hi < len(siblings)) and used < max_tokens:
        # 优先扩展后块（下文通常承接语义），预算够才纳入
        if hi < len(siblings):
            cost = estimate_tokens(full[hi])
            if used + cost <= max_tokens:
                selected.add(hi)
                used += cost
            hi += 1
            continue
        if lo >= 0:
            cost = estimate_tokens(full[lo])
            if used + cost <= max_tokens:
                selected.add(lo)
                used += cost
            lo -= 1

    return "\n\n".join(full[i] for i in sorted(selected))


def expand_to_parents(
        results: List[SearchResult],
        vector_store: BaseVectorStore,
        max_parent_tokens: int = 1600,
) -> List[SearchResult]:
    """
    把命中小块实时扩展为父块（不改变结果顺序与分数）

    同一父块的多个命中只保留一个实例（首现位置），分数取最高命中。
    无 doc_id 的结果原样保留。

    Args:
        results: 检索结果（命中小块）
        vector_store: 向量存储（用于 get_by_doc_id 拉取兄弟块）
        max_parent_tokens: 单个父块的 token 上限

    Returns:
        List[SearchResult]: 父块结果列表（顺序 = 首个命中块的顺序）
    """
    if not results:
        return results

    # 缓存同一 doc 的兄弟块，避免重复查询
    doc_cache: Dict[str, List[Dict]] = {}
    expanded: List[SearchResult] = []
    seen: Dict[Tuple[str, str], SearchResult] = {}

    for result in results:
        key = _parent_key(result)
        if key is None:
            expanded.append(result)
            continue

        if key in seen:
            # 同父块重复命中：父块内容相同，仅同步更高分（维持首现顺序）
            if result.score > seen[key].score:
                seen[key].score = result.score
            continue

        doc_id, heading = key
        if doc_id not in doc_cache:
            try:
                doc_cache[doc_id] = vector_store.get_by_doc_id(doc_id)
            except Exception:
                doc_cache[doc_id] = []

        siblings = [
            d for d in doc_cache[doc_id]
            if (d.get("metadata") or {}).get("heading_path", "") == heading
        ]

        # 父块只有命中块自身（无兄弟）→ 无扩展意义，原样保留
        if len(siblings) <= 1:
            expanded.append(result)
            seen[key] = result
            continue

        parent_content = _assemble_parent(siblings, result.id, max_parent_tokens)
        if estimate_tokens(parent_content) <= estimate_tokens(result.content):
            # 异常兜底：扩展结果反而更小，保留原块
            expanded.append(result)
            seen[key] = result
            continue

        metadata = dict(result.metadata)
        metadata["parent_chunk_count"] = len(siblings)
        parent = SearchResult(
            id=f"{doc_id}::parent::{heading}",
            content=parent_content,
            score=result.score,
            metadata=metadata,
        )
        expanded.append(parent)
        seen[key] = parent

    # 重复命中的高分替换不改变位置：按首现顺序重建
    return expanded
