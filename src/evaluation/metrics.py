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

import re
from typing import Any, Dict, List, Optional, Sequence, Set


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


# ================================================================
#  OCR 识别率（字符级，与检索指标无关的另一套口径）
# ================================================================
#
# 检索指标衡量「找没找到」，OCR 指标衡量「认没认对」——输入、分母、
# 可解释性完全不同，故独立成节，函数名前缀 `ocr_*` / `char_*` / `edit_*`。
#
# 两档 CER（字符错误率）刻意都保留，它们回答两个不同的问题：
#
#   - **原样 CER（cer_raw）**：ground truth 与模型输出逐字符比，不做任何
#     归一化。回答「端到端可用性」——把模型的 Markdown 直接喂给下游
#     （切块 / 检索）时，换行、全角半角、标题符号这些格式差异算不算错。
#     这个数偏高往往意味着「字认对了，但排版与预期不一致」。
#   - **归一化 CER（cer_normalized）**：两侧都先过 `normalize_ocr_text`
#     （全角转半角、剥 Markdown、去空白）再比。回答「模型认字能力」——
#     剔除格式差异后，还有多少字符是真的认错。
#
# 两档之差**通常**反映格式差异带来的损耗；但「归一化 CER ≤ 原样 CER」**不是**
# 普适不变量——归一化会删除字符、收缩 CER 的分母，因此 norm > raw 在某些输入下
# 必然发生。反例：ref="a*b" / hyp="a*c" → raw = 1/3 ≈ 0.333，norm = 1/2 = 0.5
# （删 `*` 后分母 3→2，分子仍为 1）。两档都高才是真的认字差。

# 失败样本纪律：OCR 失败（服务超时 / 非 200 / 图片解不开）返回空串，这是
# **请求失败**而非「认字很差」。`aggregate_ocr` 因此把失败样本单列
# （n_failed），CER 均值只对成功样本计算——否则「oMLX 打不通」会被伪装成
# 「模型识别率极低」。这与 `chunk_recall@k` 分母不可得时返回 None 同源。


def edit_distance(a: str, b: str) -> int:
    """两串的最小编辑距离（Levenshtein：插入 / 删除 / 替换，代价各 1）

    回答：「把 a 改成 b 至少要动几个字符」，是字符错误率分子 / 分母的来源。

    纯函数、无外部依赖。用两行滚动数组，空间 O(min(len(a), len(b)))，因此
    可以直接吃掉上千字符的 OCR 全文而不爆内存。
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # 让较短的一串做「列」，滚动数组只需 min+1 个格子
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(
                prev[j] + 1,          # 删除 a[i-1]
                cur[j - 1] + 1,       # 插入 b[j-1]
                prev[j - 1] + cost,   # 替换
            ))
        prev = cur
    return prev[-1]


def char_error_rate(reference: str, hypothesis: str) -> Optional[float]:
    """字符错误率 CER = edit_distance(reference, hypothesis) / len(reference)

    回答：「模型把这段文字认错了百分之多少的字符」。

    Args:
        reference: 标准答案（ground truth）
        hypothesis: 模型输出

    Returns:
        float，或 None——**reference 为空串时返回 None**。分母不可得不能
        当「零错误」，与 `chunk_recall@k` 分母未知返回 None 是同一条纪律。

    Note:
        - hypothesis 为空串是**合法输入**：此时 edit_distance = len(reference)，
          CER 恰为 1.0（整段没认出来）。
        - CER **可以大于 1.0**（模型输出比原文长很多、插入占主导时），这是
          标准行为，不做钳制——钳到 1.0 会掩盖「输出失控膨胀」。
    """
    if not reference:
        return None
    return edit_distance(reference, hypothesis) / len(reference)


# CJK 标点里不在 U+FF01..U+FF5E 全角块内的字符需显式映射
_CJK_PUNCT_MAP = {
    "。": ".", "、": ",", "《": "<", "》": ">",
    "【": "[", "】": "]",
    "「": '"', "」": '"', "『": "'", "』": "'",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "–": "-", "―": "-",   # 破折号 / 连接号（"——" 即两个 em dash）
    "…": "...",                      # 省略号（"……" 即两个 …）
    "　": " ",                       # U+3000 全角空格
}


def _unify_punct(text: str) -> str:
    """全角标点 / 全角字母数字 → 半角（含 U+3000 空格）。

    U+FF01..U+FF5E 全角块统一减 0xFEE0 映射到 ASCII；CJK 专有标点走显式表。
    输出只含 ASCII，再跑一次不会再变（幂等的一环）。
    """
    # 先把成对符号折叠成单个，避免 "——" → "--"、"……" → "......"
    text = text.replace("——", "—").replace("……", "…")
    out = []
    for ch in text:
        mapped = _CJK_PUNCT_MAP.get(ch)
        if mapped is not None:
            out.append(mapped)
        elif 0xFF01 <= ord(ch) <= 0xFF5E:
            out.append(chr(ord(ch) - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


# 行首块级标记：仅剥「模型添加的装饰」——标题 # 与引用 >。
#
# 刻意**不**剥列表符（- * +）与有序列表（1. / 1)）：这些是**内容**而非装饰，
# ground truth 是纯文本、里面本来就有「- 」「1. 」（如 "-5 是负数"、"2024. 年度"），
# 剥掉会把 GT 与 hyp 都篡改；而删空白已足以抹平 "- 项目" 与 "-项目" 的差异。
#
# `#{1,6}\s*` 用 `\s*`（零或多个）而非 `\s+`：GT 写 "# 标题"、模型漏空格写 "#标题"
# 时两侧必须归一成同一结果——否则归一化对两侧做了**不同**变换，凭空造出错误。
#
# 已知取舍（刻意，非缺陷）：行首 `#` 被**无条件**剥离（无论其后是否有空白）。
# 这是消除上述不对称（P1）的必要条件，代价是当 GT 行首 `#` 本身是内容、而模型
# 漏识该 `#` 时，误差不会被计入（如 "#1 号方案" / "#tag 话题" / "#!"：raw 有值
# 而 norm=0）。取舍方向经权衡确认——修前是「假 FAIL」：归一化后 CER 不降反升
# （"# 索引配置" norm 0.25、"2024. …" norm 0.833），会把「只差一个空格」判成
# 「认字很差」，并击穿默认阈值 max_cer_normalized=0.2 造成误报失败；现在是
# 「窄范围假 PASS」，仅当 GT 行首 `#` 为内容时才漏报。假 FAIL 的危害远大于假
# PASS：评测虚报失败会让人不敢拿它当门禁，而漏报一个罕见用例无关痛痒。
# 推翻判据：若真实语料里行首 `#` 内容的占比显著、漏报成为可复现痛点 → 改为
# 「仅当 `#` 后跟空白或位于行尾时才视为标题装饰」；但注意这会重新引入 P1 的
# 不对称，届时必须同时引入别的对称化手段。
_LEADING_BLOCK_RE = re.compile(r"^\s*(?:#{1,6}\s*|[>]+\s*)+")
# 表格分隔行只由竖线 / 减号 / 冒号 / 空白构成
_TABLE_SEP_RE = re.compile(r"[|\-\s:]+")


def _is_table_separator(line: str) -> bool:
    """是否为 Markdown 表格分隔行（如 `|---|---|`、`| :--: | ---: |`）

    判定：去掉竖线 / 空白 / 冒号后只剩减号，且至少含一个减号。
    """
    t = line.strip()
    return bool(t) and "-" in t and _TABLE_SEP_RE.fullmatch(t) is not None


def _drop_markdown(text: str, *, strip_underscore: bool = False) -> str:
    """剥掉 Markdown 标记，只留文字本身。

    逐行处理：表格分隔行整行丢弃；行首块级标记（标题 / 引用）剥掉；
    行内强调符（`*` `~`，以及可选的 `_`）、反引号、表格竖线移除。

    Args:
        text: 待处理文本
        strip_underscore: 是否把 `_` 当作强调符删除。默认 **False**——保留
            `snake_case` 辨识力（否则 `ingest_queue` 被识别成 `ingestqueue`
            时误差会被完全抹平，评测虚高）。置 True 则强调符场景更干净，代价
            是漏报「漏识下划线」这类真实错误。`*` `~` 与反引号始终删除。
    """
    out = []
    delete = {ord("*"): None, ord("~"): None, ord("`"): None}
    if strip_underscore:
        delete[ord("_")] = None
    for raw in text.split("\n"):
        if _is_table_separator(raw):
            continue
        line = _LEADING_BLOCK_RE.sub("", raw)
        line = line.translate(delete)
        line = line.replace("|", "")   # 表格竖线
        out.append(line)
    return "\n".join(out)


def normalize_ocr_text(text: str, *, drop_markdown: bool = True,
                       unify_punct: bool = True,
                       keep_whitespace: bool = False,
                       strip_underscore: bool = False) -> str:
    """把 OCR 的 Markdown 输出归一成「只比较文字本身」的形式。

    回答：「抛开排版差异，模型的输出和标准答案在文字层面差在哪」。

    Args:
        text: 待归一化文本（ground truth 或模型输出，两侧同样处理）
        unify_punct: 全角标点 / 全角字母数字 → 半角（含破折号 ——、省略号 ……）
        drop_markdown: 剥掉 Markdown 标记（标题 #、强调 */~/可选 _、反引号、
            引用 >、表格分隔行与竖线）。列表符（- * +）与有序列表（1.）
            视为内容，不剥——理由见 `_LEADING_BLOCK_RE` 注释
        keep_whitespace: False（默认）移除**所有**空白，含换行 / 制表 / 全角
            空格 U+3000——OCR 的换行位置不受控，换行差异不是识别错误；
            True 则原样保留空白
        strip_underscore: 把 `_` 当强调符删除？两种取舍：
            - False（默认）：保留 `snake_case` 辨识力——`ingest_queue` 若被识别
              成 `ingestqueue` 会如实计为错误；代价是模型真用 `_斜体_` 时会被
              算成差异。
            - True：强调符场景更干净；代价是**漏报漏识下划线**的错误。

    Returns:
        归一化后的字符串

    Note:
        幂等：`normalize_ocr_text(normalize_ocr_text(x)) == normalize_ocr_text(x)`，
        因此可以对同一文本反复叠加归一化而不漂移。
    """
    t = text or ""
    if unify_punct:
        t = _unify_punct(t)
    if drop_markdown:
        t = _drop_markdown(t, strip_underscore=strip_underscore)
    if not keep_whitespace:
        t = "".join(ch for ch in t if not ch.isspace())
    return t


def ocr_accuracy_report(reference: str, hypothesis: str) -> Dict[str, Any]:
    """单条样本的 OCR 度量（原样 + 归一化两档）。

    回答：「这一张图，模型认对了多少 / 完全正确吗 / 请求成功吗」。

    Args:
        reference: 渲染前的标准文本（ground truth）
        hypothesis: OCR 模型输出（失败为空串）

    Returns:
        dict:
            - ok: bool                hypothesis 去空白后非空 = 请求成功
            - ref_chars: int          len(reference)
            - hyp_chars: int          len(hypothesis)
            - cer_raw: Optional[float]
            - cer_normalized: Optional[float]
            - exact_raw: bool         原样完全一致
            - exact_normalized: bool  归一化后完全一致

    Note:
        ok=False（空输出）时 **cer_raw 与 cer_normalized 均为 None**：空输出
        是「请求失败 / 无结果」，不是「100% 认错」。把失败记成 CER=1.0 会让
        均值把「服务打不通」误读为「模型很差」，因此这里显式置 None，并由
        `aggregate_ocr` 单列统计。exact_* 仍按直接比较给出（对空输出自然为
        False），但它们不参与失败样本的均值。
    """
    hyp = hypothesis or ""
    ok = bool(hyp.strip())
    norm_ref = normalize_ocr_text(reference or "")
    norm_hyp = normalize_ocr_text(hyp)
    if ok:
        cer_raw: Optional[float] = char_error_rate(reference, hyp)
        cer_norm: Optional[float] = char_error_rate(norm_ref, norm_hyp)
    else:
        cer_raw = None
        cer_norm = None
    return {
        "ok": ok,
        "ref_chars": len(reference or ""),
        "hyp_chars": len(hyp),
        "cer_raw": cer_raw,
        "cer_normalized": cer_norm,
        "exact_raw": (reference or "") == hyp,
        "exact_normalized": norm_ref == norm_hyp,
    }


def aggregate_ocr(samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """聚合多条样本的 OCR 度量；**失败样本单列，绝不混入 CER 均值**。

    回答：「这个 OCR 配置整体认字有多准 / 服务成功率多高」。

    Args:
        samples: `ocr_accuracy_report` 返回的 dict 列表（每项须含 ok 及各
            cer_* 键；缺键按缺省处理）

    Returns:
        dict:
            - n_total: int                   样本总数
            - n_ok: int                      成功样本数（ok=True）
            - n_failed: int                  失败样本数（ok=False）
            - success_rate: Optional[float]  n_ok / n_total；n_total=0 → None
            - cer_raw_mean: Optional[float]  只对**成功**样本的原样 CER 求均值；
                 无成功样本 → None
            - cer_normalized_mean: Optional[float]  同上，归一化 CER 均值
            - exact_match_rate: Optional[float]  成功样本中「归一化后完全
                 正确」的占比，**分母是 n_ok**；n_ok=0 → None

    Note:
        纪律来源：一个失败样本若按 CER=1.0 计入均值，会让「oMLX 打不通」伪装成
        「模型认字很差」。故失败只体现在 success_rate / n_failed，不污染任何
        CER 均值（其 cer_* 本就为 None）。

        与检索指标 `aggregate()` 的**刻意差异**：本函数在 `samples` 为空时
        `success_rate` 返回 **None**，而 `aggregate()` 对空输入返回 0.0。原因：
        空样本集根本不存在「成功率」这个量，返回 0.0 会被读成「0% 成功率」；
        而检索的空输入可解释为「没有命中」（recall=0 有明确含义）。两者都遵循
        「分母不可得 → None」的同一纪律，只是检索侧另有可解释的 0 语义。
    """
    n_total = len(samples)
    ok_samples = [s for s in samples if s.get("ok")]
    n_ok = len(ok_samples)
    n_failed = n_total - n_ok

    raw_vals = [s.get("cer_raw") for s in ok_samples if s.get("cer_raw") is not None]
    norm_vals = [s.get("cer_normalized") for s in ok_samples
                 if s.get("cer_normalized") is not None]
    exact_hits = sum(1 for s in ok_samples if s.get("exact_normalized"))

    return {
        "n_total": n_total,
        "n_ok": n_ok,
        "n_failed": n_failed,
        "success_rate": (n_ok / n_total) if n_total else None,
        "cer_raw_mean": (sum(raw_vals) / len(raw_vals)) if raw_vals else None,
        "cer_normalized_mean": (sum(norm_vals) / len(norm_vals)) if norm_vals else None,
        "exact_match_rate": (exact_hits / n_ok) if n_ok else None,
    }
