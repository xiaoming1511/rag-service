"""
上下文构建（决策 B2：token 感知截断）

修复问题：原实现对整个上下文做 2000 字符硬截断，而单块常态内容约 2400 字符，
导致第 3 块尾部固定丢失。改为按 token 预算装配：
- 能整体放入预算的块保持完整
- 预算不足以容纳下一整块时，把剩余预算分给该块（截断头部保留，
  而非整块丢弃或固定位置腰斩），其后块不再装入

token 估算为轻量启发式（无分词器依赖）：
- CJK 字符 ≈ 1 token/字
- 连续拉丁/数字串 ≈ 每 4 字符 1 token
"""

import re
from typing import List

from src.vector_store.base import SearchResult

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9_]+")


def estimate_tokens(text: str) -> int:
    """
    估算文本 token 数（启发式）

    对中文为主的文本误差通常在 ±20% 内，足够做预算分配；
    不追求精确，只追求稳定单调（更长文本 → 更大估算值）。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    latin_tokens = sum(max(1, len(w) // 4) for w in _LATIN_RUN_RE.findall(text))
    # 其余字符（空格、标点、markdown 符号等）按 1/3 token 粗算
    other = len(text) - sum(len(w) for w in _LATIN_RUN_RE.findall(text)) - cjk
    return cjk + latin_tokens + max(0, other) // 3


def _format_chunk(result: SearchResult) -> str:
    """与原 retrieve_with_context 相同的块格式：[来源 > 标题] 内容"""
    source = result.metadata.get("file_name", "unknown")
    heading = result.metadata.get("heading_path", "")
    header = f"[{source}]{f' > {heading}' if heading else ''}"
    return f"{header}\n{result.content}"


def build_context(results: List[SearchResult], max_tokens: int) -> str:
    """
    按 token 预算装配上下文

    Args:
        results: 检索结果（按相关性排序）
        max_tokens: 上下文 token 预算

    Returns:
        str: 装配后的上下文；各块以 "\\n\\n---\\n\\n" 分隔
    """
    if not results or max_tokens <= 0:
        return ""

    separator = "\n\n---\n\n"
    sep_tokens = estimate_tokens(separator)
    parts: List[str] = []
    used = 0

    for result in results:
        chunk = _format_chunk(result)
        cost = estimate_tokens(chunk) + (sep_tokens if parts else 0)

        if used + cost <= max_tokens:
            # 整块放入
            parts.append(chunk)
            used += cost
            continue

        # 预算不足以容纳整块：剩余预算若足够放「块头的有意义片段」，
        # 截断放入；否则放弃该块及其后所有块（相关性更弱，无需硬塞）
        remaining = max_tokens - used - (sep_tokens if parts else 0)
        if remaining >= 64:  # 最小有效片段阈值（token）
            # 按比例把块截到剩余预算内，尽量在行边界截断
            ratio = remaining / max(1, estimate_tokens(chunk))
            cut = max(1, int(len(chunk) * ratio))
            newline_pos = chunk.rfind("\n", 0, cut)
            if newline_pos > cut // 2:
                cut = newline_pos
            parts.append(chunk[:cut].rstrip())
        break

    return separator.join(parts)
