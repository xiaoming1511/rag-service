"""
评测指标（纯函数，无任何外部依赖，可离线单测）

检索指标基于「命中文档级 gold 标注」计算：
- Recall@k:     gold 文档出现在前 k 结果中的比例（按 query 平均）
- MRR@k:        第一个 gold 结果排名倒数的平均（1/rank，未命中计 0）
- HitRate@k:    至少命中一个 gold 的 query 比例
- Precision@k:  前 k 结果中 gold 块占比（多 gold 时更有意义）
"""

from typing import Dict, List, Sequence, Set


def _hit_ranks(ranked_doc_ids: Sequence[str], gold: Set[str]) -> List[int]:
    """gold 文档在排序结果中的 rank 列表（1-based）"""
    return [i + 1 for i, d in enumerate(ranked_doc_ids) if d in gold]


def recall_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的 Recall@k：前 k 中命中的 gold 数 / gold 总数"""
    if not gold:
        return 0.0
    hits = {d for d in ranked_doc_ids[:k] if d in gold}
    return len(hits) / len(gold)


def mrr_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的 MRR@k"""
    for rank in _hit_ranks(ranked_doc_ids, gold):
        if rank <= k:
            return 1.0 / rank
    return 0.0


def hit_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的 Hit@k（0/1）"""
    return 1.0 if _hit_ranks(ranked_doc_ids[:k], gold) else 0.0


def precision_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的 Precision@k"""
    top = ranked_doc_ids[:k]
    if not top:
        return 0.0
    hits = sum(1 for d in top if d in gold)
    return hits / len(top)


def aggregate(ranked_lists: Sequence[Sequence[str]],
              gold_sets: Sequence[Set[str]],
              k: int) -> Dict[str, float]:
    """
    聚合多条 query 的指标

    Args:
        ranked_lists: 每条 query 的结果文档 ID 列表（按相关性排序）
        gold_sets: 每条 query 的 gold 文档 ID 集合
        k: 截断位置

    Returns:
        {"recall@k": ..., "mrr@k": ..., "hit@k": ..., "precision@k": ...}
    """
    if len(ranked_lists) != len(gold_sets):
        raise ValueError("ranked_lists 与 gold_sets 数量不一致")
    if not ranked_lists:
        return {f"recall@{k}": 0.0, f"mrr@{k}": 0.0,
                f"hit@{k}": 0.0, f"precision@{k}": 0.0}

    n = len(ranked_lists)
    return {
        f"recall@{k}": sum(recall_at_k(r, g, k) for r, g in zip(ranked_lists, gold_sets)) / n,
        f"mrr@{k}": sum(mrr_at_k(r, g, k) for r, g in zip(ranked_lists, gold_sets)) / n,
        f"hit@{k}": sum(hit_at_k(r, g, k) for r, g in zip(ranked_lists, gold_sets)) / n,
        f"precision@{k}": sum(precision_at_k(r, g, k) for r, g in zip(ranked_lists, gold_sets)) / n,
    }


def parse_judge_score(raw: str) -> float:
    """
    解析 LLM-as-judge 输出为 0~1 分数

    约定 judge 返回格式为 "SCORE: 0.8" 或单独一行数字；
    解析失败返回 -1.0（调用方按无效处理，不计入均值）。
    """
    import re
    if not raw:
        return -1.0
    m = re.search(r"SCORE\s*[:：=]?\s*([0-9]+(?:\.[0-9]+)?)", raw)
    if not m:
        # 兜底：文本中最后一个独立数字（含百分制，如 "85" → 0.85）
        nums = re.findall(r"(?<![\w.])([0-9]+(?:\.[0-9]+)?)(?![\w.])", raw)
        if not nums:
            return -1.0
        m_val = nums[-1]
    else:
        m_val = m.group(1)
    try:
        val = float(m_val)
    except ValueError:
        return -1.0
    if val > 1.0 and val <= 100 and float(val).is_integer():
        val = val / 100.0  # 容错：模型输出整数十百分制（如 85 → 0.85）；1.5 这类小数越界仍无效
    return val if 0.0 <= val <= 1.0 else -1.0
