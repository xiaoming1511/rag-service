"""
对话历史工具与追问改写测试

覆盖：
- trim_history 的轮数裁剪 / token 预算裁剪 / 边界与不可变性
- Generator 追问改写默认关闭（原样返回，不调用模型）
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.generation.history import estimate_tokens, trim_history
from src.generation.generator import Generator
from src.embedding.client import OMLXClient


def _msg(role, text):
    return {"role": role, "content": text}


def test_estimate_tokens():
    """token 估算：非空文本至少 1，空文本为 0"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 1
    assert estimate_tokens("你好世界" * 10) == 20


def test_trim_history_empty():
    """空历史原样返回空列表"""
    assert trim_history([]) == []
    assert trim_history(None) == []


def test_trim_history_rounds():
    """轮数裁剪：30 条消息（15 轮）裁到最近 10 轮（20 条）"""
    history = [_msg("user" if i % 2 == 0 else "assistant", f"内容{i}") for i in range(30)]
    trimmed = trim_history(history, max_rounds=10, token_budget=0)
    assert len(trimmed) == 20
    # 保留的是最近的消息
    assert trimmed[-1]["content"] == "内容29"
    assert trimmed[0]["content"] == "内容10"


def test_trim_history_token_budget():
    """token 预算裁剪：总估算 token 收拢到预算内（至少保留 1 轮）"""
    history = [_msg("user", "这段内容比较长" * 50) for _ in range(4)]
    history += [_msg("assistant", "回答" * 60) for _ in range(4)]
    budget = 100
    trimmed = trim_history(history, max_rounds=10, token_budget=budget)
    total = sum(estimate_tokens(h["content"]) for h in trimmed)
    assert len(trimmed) >= 2  # 至少保留最近一问一答
    assert total <= budget + estimate_tokens(trimmed[0]["content"])  # 允许单条微超


def test_trim_history_does_not_mutate():
    """裁剪不应修改传入的历史列表"""
    history = [_msg("user", f"问题{i}") for i in range(50)]
    snapshot = list(history)
    trim_history(history, max_rounds=5, token_budget=0)
    assert history == snapshot


def test_trim_history_no_limit_keeps_all():
    """轮数与预算都为 0（不限）时保留全部"""
    history = [_msg("user", "内容") for _ in range(50)]
    assert len(trim_history(history, max_rounds=0, token_budget=0)) == 50


# ================================================================
# Generator 追问改写（默认关闭）
# ================================================================

def _make_generator(rewrite_query: bool = False) -> Generator:
    """构造生成器（不触发网络调用场景足够）"""
    client = OMLXClient(base_url="http://127.0.0.1:8000/v1", api_key="dummy", timeout=5.0)
    return Generator(
        client=client,
        model="qwen3.5-4b-mlx-4bit",
        rewrite_query=rewrite_query,
    )


def test_rewrite_query_disabled_by_default():
    """默认关闭：追问原样返回，不调用模型"""
    gen = _make_generator(rewrite_query=False)
    history = [_msg("user", "Redis 支持哪些数据结构？"), _msg("assistant", "字符串、列表等。")]
    assert gen.rewrite_question("那它们适合什么场景？", history) == "那它们适合什么场景？"


def test_rewrite_query_without_history():
    """无历史时不改写（即使开关打开）"""
    gen = _make_generator(rewrite_query=True)
    assert gen.rewrite_question("什么是数据库？", None) == "什么是数据库？"


def test_build_messages_applies_trim():
    """消息构建时应用历史裁剪：超长历史不会全部拼入"""
    gen = _make_generator()
    long_history = [_msg("user", "问题内容" * 100) for _ in range(20)]
    long_history += [_msg("assistant", "回答内容" * 100) for _ in range(20)]

    messages = gen._build_messages("当前问题", "上下文", long_history)
    # system + 裁剪后的历史 + user（原始 40 条历史被大幅裁剪）
    assert 3 <= len(messages) < 25