"""
严格来源模式测试（离线桩）

覆盖：
- 开启严格模式：检索为空时不调用生成模型，回答包含"未找到"
- 关闭严格模式：检索为空时仍调用生成模型（默认行为）
- 有结果时严格模式不影响正常流程
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.vector_store.base import SearchResult
from src.pipeline.rag_pipeline import RAGPipeline


class _EmptyRetriever:
    """检索永远为空"""

    def retrieve_with_context(self, query, top_k=None, where=None,
                              use_rerank=True, max_context_length=2000):
        return "", []


class _HitRetriever:
    """检索总有结果"""

    def __init__(self):
        self.r = SearchResult(id="x", content="内容", score=0.9,
                              metadata={"file_name": "x.md", "file_path": "/x.md"})

    def retrieve_with_context(self, query, top_k=None, where=None,
                              use_rerank=True, max_context_length=2000):
        return "上下文", [self.r]


class _CountingGenerator:
    rewrite_query = False

    def __init__(self):
        self.calls = 0

    def generate(self, query, context, history=None, **kwargs):
        self.calls += 1
        return f"回答:{query}"

    def generate_stream(self, query, context, history=None, **kwargs):
        self.calls += 1
        yield f"回答:{query}"


def _make_pipeline(retriever, generator, strict: bool) -> RAGPipeline:
    return RAGPipeline(retriever=retriever, generator=generator,
                       save_syntheses=False, strict_sources=strict)


def test_strict_mode_blocks_generation():
    """严格模式 + 检索为空：不调用模型，回答含未找到提示"""
    gen = _CountingGenerator()
    pipe = _make_pipeline(_EmptyRetriever(), gen, strict=True)

    result = pipe.query("不存在的主题")
    assert gen.calls == 0
    assert "未从知识库中" in result["answer"]
    assert "严格来源模式" in result["answer"]
    assert result["sources"] == []
    assert result["total_results"] == 0


def test_non_strict_mode_generates():
    """默认（非严格）+ 检索为空：仍调用模型"""
    gen = _CountingGenerator()
    pipe = _make_pipeline(_EmptyRetriever(), gen, strict=False)

    result = pipe.query("不存在的主题")
    assert gen.calls == 1
    assert result["answer"].startswith("回答:")
    assert result["total_results"] == 0


def test_strict_mode_with_hits_normal():
    """严格模式 + 有结果：正常生成"""
    gen = _CountingGenerator()
    pipe = _make_pipeline(_HitRetriever(), gen, strict=True)

    result = pipe.query("存在的主题")
    assert gen.calls == 1
    assert result["sources"]


def test_strict_mode_stream():
    """严格模式流式：产出 chunk+done，不调用生成"""
    gen = _CountingGenerator()
    pipe = _make_pipeline(_EmptyRetriever(), gen, strict=True)

    events = list(pipe.query_stream("不存在的主题"))
    assert gen.calls == 0
    joined = "\n".join(events)
    assert "data: " in joined
    assert "未从知识库中" in joined
    assert '"type": "done"' in joined