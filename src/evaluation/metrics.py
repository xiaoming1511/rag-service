"""
评测指标（纯函数，无任何外部依赖，可离线单测）

## 两套口径（D5-1 订正）

检索返回的是**块**（chunk），而 gold 标注是**文档**（doc）。此前
`recall@k` 按文档去重、`precision@k` 按块不去重，两者分母不同却并列
展示，会出现「1 篇 gold 的 5 个块占满前 5 名时 recall@5 = precision@5
= 1.0，而文档级 precision 实为 0.2」这种自相矛盾的结果。

现按层级拆成两套，键名自带层级前缀，不再有二义：

- **文档级（doc_*）**：先把块列表折叠为文档列表（同文档只算一次，
  保留首次出现的位置），再计算。衡量「该找的文档找到了没有」。
  - `doc_recall@k`    前 k 个不同文档中命中的 gold 文档数 / gold 文档总数
  - `doc_precision@k` 前 k 个不同文档中 gold 文档占比
  - `doc_mrr@k`       第一个 gold 文档排名倒数（未命中计 0）
  - `doc_hit@k`       前 k 个不同文档中至少命中一个 gold（0/1）
- **块级（chunk_*）**：直接对原始 top-k 块列表计算，不去重。
  衡量「真正塞进上下文的那 k 个块里，有多少是有用的」。
  - `chunk_precision@k` 前 k 个块中属于 gold 文档的块数 / k
  - `chunk_recall@k`    前 k 个块中属于 gold 文档的块数 / gold 文档的块总数
    （分母需调用方提供 `chunk_totals`，即 gold 文档在向量库中的块数；
     拿不到时返回 None 而非 0.0，避免把「未知」伪装成「零收益」）

折叠顺序约定：**先按前 k 个块截断，再对文档去重**（与块级同一截断口径），
因此 `doc_precision@k` 的分母是「前 k 个块里出现过的不同文档数」，可能小于 k。
这样文档级与块级始终观察同一段 top-k，两级数字可直接对照。
"""

from typing import Dict, List, Optional, Sequence, Set


def _hit_ranks(ranked_doc_ids: Sequence[str], gold: Set[str]) -> List[int]:
    """gold 文档在排序结果中的 rank 列表（1-based）"""
    return [i + 1 for i, d in enumerate(ranked_doc_ids) if d in gold]


def dedup_docs(ranked_doc_ids: Sequence[str],
               k: Optional[int] = None) -> List[str]:
    """块级排序列表 → 文档级排序列表（同文档只保留首次出现）

    Args:
        ranked_doc_ids: 每项为结果所在文档的标识（可有重复）
        k: 先截断到前 k 个块再去重；None 表示全量去重
    """
    source = ranked_doc_ids if k is None else ranked_doc_ids[:k]
    out: List[str] = []
    seen: Set[str] = set()
    for d in source:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


# ---------- 文档级 ----------

def doc_recall_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的文档级 Recall@k：前 k 个块中命中的 gold 文档数 / gold 总数"""
    if not gold:
        return 0.0
    hits = {d for d in ranked_doc_ids[:k] if d in gold}
    return len(hits) / len(gold)


def doc_precision_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的文档级 Precision@k：前 k 个块里的不同文档中 gold 占比

    分母是「前 k 个块出现过的不同文档数」，而非 k——同一文档的多个块
    不应把精度算高（这正是旧 `precision@k` 的问题所在）。
    """
    top = dedup_docs(ranked_doc_ids, k)
    if not top:
        return 0.0
    return sum(1 for d in top if d in gold) / len(top)


def doc_mrr_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的文档级 MRR@k"""
    for rank in _hit_ranks(ranked_doc_ids, gold):
        if rank <= k:
            return 1.0 / rank
    return 0.0


def doc_hit_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的文档级 Hit@k（0/1）"""
    return 1.0 if _hit_ranks(ranked_doc_ids[:k], gold) else 0.0


# ---------- 块级 ----------

def chunk_precision_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int) -> float:
    """单条 query 的块级 Precision@k：前 k 个块中属于 gold 文档的块占比"""
    top = ranked_doc_ids[:k]
    if not top:
        return 0.0
    return sum(1 for d in top if d in gold) / len(top)


def chunk_recall_at_k(ranked_doc_ids: Sequence[str], gold: Set[str], k: int,
                      gold_chunk_total: Optional[int] = None) -> Optional[float]:
    """单条 query 的块级 Recall@k：命中的 gold 块数 / gold 文档的块总数

    Args:
        gold_chunk_total: gold 文档在向量库中的块总数（分母）。
            为 None / 0 时返回 None——分母未知不能当作 0 收益。

    Returns:
        float 或 None（分母不可得）
    """
    if not gold_chunk_total:
        return None
    hits = sum(1 for d in ranked_doc_ids[:k] if d in gold)
    return hits / gold_chunk_total


# ---------- 兼容别名 ----------
# 旧名保留为同实现，避免外部脚本静默失效；新代码请用带层级前缀的名字。
recall_at_k = doc_recall_at_k        # 旧名：本就是文档级
mrr_at_k = doc_mrr_at_k              # 旧名：本就是文档级
hit_at_k = doc_hit_at_k              # 旧名：本就是文档级
precision_at_k = chunk_precision_at_k  # 旧名：本就是块级（口径已在键名中显式化）


def aggregate(ranked_lists: Sequence[Sequence[str]],
              gold_sets: Sequence[Set[str]],
              k: int,
              chunk_totals: Optional[Sequence[Optional[int]]] = None) -> Dict[str, Optional[float]]:
    """
    聚合多条 query 的指标（文档级 + 块级两套）

    Args:
        ranked_lists: 每条 query 的结果文档 ID 列表（按相关性排序，块级可重复）
        gold_sets: 每条 query 的 gold 文档 ID 集合
        k: 截断位置
        chunk_totals: 每条 query 的「gold 文档块总数」（块级 Recall 分母）；
            None 表示不可得，此时 `chunk_recall@k` 为 None

    Returns:
        见模块 docstring 的键名表
    """
    if len(ranked_lists) != len(gold_sets):
        raise ValueError("ranked_lists 与 gold_sets 数量不一致")
    if chunk_totals is not None and len(chunk_totals) != len(ranked_lists):
        raise ValueError("chunk_totals 与 ranked_lists 数量不一致")

    keys = {
        "doc_recall": f"doc_recall@{k}",
        "doc_precision": f"doc_precision@{k}",
        "doc_mrr": f"doc_mrr@{k}",
        "doc_hit": f"doc_hit@{k}",
        "chunk_precision": f"chunk_precision@{k}",
        "chunk_recall": f"chunk_recall@{k}",
    }
    if not ranked_lists:
        return {
            keys["doc_recall"]: 0.0,
            keys["doc_precision"]: 0.0,
            keys["doc_mrr"]: 0.0,
            keys["doc_hit"]: 0.0,
            keys["chunk_precision"]: 0.0,
            keys["chunk_recall"]: None,
        }

    n = len(ranked_lists)
    pairs = list(zip(ranked_lists, gold_sets))
    result: Dict[str, Optional[float]] = {
        keys["doc_recall"]: sum(doc_recall_at_k(r, g, k) for r, g in pairs) / n,
        keys["doc_precision"]: sum(doc_precision_at_k(r, g, k) for r, g in pairs) / n,
        keys["doc_mrr"]: sum(doc_mrr_at_k(r, g, k) for r, g in pairs) / n,
        keys["doc_hit"]: sum(doc_hit_at_k(r, g, k) for r, g in pairs) / n,
        keys["chunk_precision"]: sum(chunk_precision_at_k(r, g, k) for r, g in pairs) / n,
    }

    if chunk_totals is None:
        result[keys["chunk_recall"]] = None
    else:
        vals = [
            chunk_recall_at_k(r, g, k, t)
            for (r, g), t in zip(pairs, chunk_totals)
        ]
        known = [v for v in vals if v is not None]
        # 分母可得的条目才计入均值；全不可得则置 None（而非 0.0）
        result[keys["chunk_recall"]] = (sum(known) / len(known)) if known else None

    return result


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
