"""
对话历史工具

多轮对话（决策 D6）：保留最近 N 轮，并按 token 预算从旧到新裁剪，
避免历史撑爆上下文窗口。
"""

from typing import Dict, List


def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数

    中文约 1 字符 ≈ 0.6~1 token，英文约 4 字符 ≈ 1 token；
    这里统一按 2 字符 ≈ 1 token 的近似值，足够用于裁剪判断。
    """
    if not text:
        return 0
    return max(1, len(text) // 2)


def trim_history(
        history: List[Dict[str, str]],
        max_rounds: int = 10,
        token_budget: int = 2000,
) -> List[Dict[str, str]]:
    """
    裁剪对话历史

    规则（决策 D6）：
    1. 轮数裁剪：最多保留最近 max_rounds 轮（一条轮 = 一问一答 = 2 条消息）
    2. token 预算裁剪：总估算 token 超过 budget 时，从最旧的消息开始丢弃，
       最少保留最近 1 轮（2 条），避免把最后一问也裁掉

    Args:
        history: 对话历史消息列表 [{role, content}, ...]
        max_rounds: 保留的最大轮数（0 表示不限制轮数）
        token_budget: 估算 token 预算（0 表示不限制）

    Returns:
        List[Dict[str, str]]: 裁剪后的历史
    """
    if not history:
        return []

    history = list(history)  # 浅拷贝，不修改调用方数据

    # 1. 轮数裁剪
    if max_rounds > 0 and len(history) > max_rounds * 2:
        history = history[-(max_rounds * 2):]

    # 2. token 预算裁剪（从最旧开始丢弃，至少保留 1 轮）
    if token_budget > 0:
        total = sum(estimate_tokens(h.get("content", "")) for h in history)
        while total > token_budget and len(history) > 2:
            dropped = history.pop(0)
            total -= estimate_tokens(dropped.get("content", ""))

    return history