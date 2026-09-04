"""
生成服务测试（需要本机 oMLX 服务，未启动时自动跳过）

覆盖：
- 同步非流式生成
- 同步流式生成
- 异步流式生成（验证 Python 3.13 + httpx 兼容性修复）
"""

import sys
import pytest
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from conftest import OMLX_BASE_URL, server_available
from src.embedding.client import OMLXClient
from src.generation.generator import Generator

# oMLX 服务未启动时跳过本模块
pytestmark = pytest.mark.skipif(
    not server_available(),
    reason="oMLX 服务未启动 (127.0.0.1:8000)",
)

CONTEXT = """
[Redis详细指南.md]
Redis 是一个内存数据库，支持多种数据结构：
- String（字符串）
- List（列表）
- Hash（哈希）
- Set（集合）
- Sorted Set（有序集合）
"""


def _make_generator(model: str = "qwen3.5-4b-mlx-4bit", max_tokens: int = 256) -> Generator:
    """构建生成器"""
    client = OMLXClient(base_url=OMLX_BASE_URL, api_key="dummy", timeout=60.0)
    return Generator(
        client=client,
        model=model,
        max_tokens=max_tokens,
        temperature=0.3,
        stream=True,
    )


def test_generate_sync():
    """同步非流式生成"""
    generator = _make_generator()
    answer = generator.generate(
        "Redis 支持哪些数据结构？",
        CONTEXT,
    )
    assert isinstance(answer, str)
    assert len(answer.strip()) > 0, "回答不应为空"


def test_generate_stream_sync():
    """同步流式生成"""
    generator = _make_generator()
    chunks = []
    try:
        for chunk in generator.generate_stream_sync(
            "Redis 常见的使用场景有哪些？",
            CONTEXT,
        ):
            chunks.append(chunk)
    finally:
        pass  # 同步流无需额外清理

    assert len(chunks) > 0, "同步流式至少应产出一个片段"
    assert "".join(chunks).strip(), "流式拼接结果不应为空"


@pytest.mark.asyncio
async def test_generate_stream_async():
    """异步流式生成（关键回归项）"""
    generator = _make_generator()
    collected = []
    async for chunk in generator.generate_stream_async(
        "Redis 支持哪些数据结构？",
        CONTEXT,
    ):
        collected.append(chunk)

    # 确保没有触发 "generator didn't stop after athrow()"（见 docs/async-stream-issue.md）
    assert len(collected) > 0, "异步流式至少应产出一个片段"
    assert "".join(collected).strip(), "异步流式拼接结果不应为空"

    # 提前中断场景（模拟客户端断开）也不应报错
    agen = generator.generate_stream_async("数到 10", CONTEXT)
    async for _ in agen:
        break
    await agen.aclose()  # 显式关闭生成器


@pytest.mark.asyncio
async def test_generate_async():
    """异步非流式生成"""
    generator = _make_generator()
    answer = await generator.generate_async(
        "什么是机器学习？",
        "[机器学习入门.md]\n机器学习是人工智能的一个子集。",
    )
    assert isinstance(answer, str)
    assert len(answer.strip()) > 0