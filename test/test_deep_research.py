"""
Deep Research 简版测试（离线桩）

覆盖：子查询解析、LLM 拆解 + 并行检索合并 + 汇总报告
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.deep_research import DeepResearch, parse_sub_queries
from src.vector_store.base import SearchResult


def test_parse_sub_queries():
    """容忍编号/符号前缀的行解析"""
    text = "1. 概念定义是什么？\n2. 原理机制如何？\n- 应用场景有哪些？\n* 对比优劣"
    assert parse_sub_queries(text) == ["概念定义是什么？", "原理机制如何？", "应用场景有哪些？", "对比优劣"]


def test_parse_sub_queries_count_limit():
    """count 限制数量；空行忽略；去重"""
    text = "1. 甲\n\n2. 乙\n3. 甲\n4. 丙"
    # 去重后为 [甲,乙,丙]；count 上限 2 时取前两个
    assert parse_sub_queries(text, count=2) == ["甲", "乙"]
    assert parse_sub_queries(text, count=3) == ["甲", "乙", "丙"]
    assert parse_sub_queries(text) == ["甲", "乙", "丙"]


class _StubClient:
    """返回固定子查询的桩客户端"""

    def chat_sync(self, model, messages, **kwargs):
        return "1. 什么是量子计算？\n2. 量子计算原理是什么？\n3. 量子计算有哪些应用？"


class _StubGenerator:
    client = _StubClient()
    model = "test-model"
    rewrite_query = False

    def generate(self, query, context, history=None, **kwargs):
        self.last_context = context
        return f"研究报告：{query}"


class _StubRetriever:
    def __init__(self):
        self.queries = []

    def retrieve(self, query, top_k=None, where=None, use_rerank=True):
        self.queries.append(query)
        i = len(self.queries)
        return [SearchResult(
            id=f"r{i}",
            content=f"关于{query}的内容",
            score=0.9 - i * 0.01,
            metadata={"file_name": f"f{i}.md", "file_path": f"/f{i}.md"},
        )]


def test_deep_research_full_flow():
    """完整流程：LLM 拆解 → 并行检索 → 合并去重 → 汇总报告"""
    generator = _StubGenerator()
    retriever = _StubRetriever()
    dr = DeepResearch(retriever=retriever, generator=generator, sub_query_count=3, max_rounds=1)  # 单轮

    result = dr.research("量子计算是什么？")

    # 子查询来自 LLM 输出
    assert result["sub_queries"] == ["什么是量子计算？", "量子计算原理是什么？", "量子计算有哪些应用？"]
    # 每个子查询都检索了一次（并行执行，调用顺序不保证，按集合比较）
    assert sorted(retriever.queries) == sorted(result["sub_queries"])
    # 报告已生成，且上下文包含检索内容
    assert result["report"] == "研究报告：量子计算是什么？"
    assert "什么是量子计算？" in generator.last_context
    # 来源
    assert len(result["sources"]) == 3
    assert result["total_results"] == 3


def test_deep_research_custom_sub_queries():
    """自定义子查询：不调用 LLM 拆解"""
    generator = _StubGenerator()
    retriever = _StubRetriever()
    dr = DeepResearch(retriever=retriever, generator=generator, max_rounds=1)  # 单轮

    result = dr.research("量子计算是什么？", sub_queries=["定义", "原理"])
    assert result["sub_queries"] == ["定义", "原理"]
    assert retriever.queries == ["定义", "原理"]
    assert result["total_results"] == 2


def test_deep_research_merge_dedup():
    """不同子查询返回相同 id 时去重"""
    generator = _StubGenerator()

    class _SameIdRetriever:
        def retrieve(self, query, top_k=None, where=None, use_rerank=True):
            return [SearchResult(id="same", content=f"内容:{query}", score=0.5,
                                 metadata={"file_name": "x.md"})]

    dr = DeepResearch(retriever=_SameIdRetriever(), generator=generator, sub_query_count=3)
    result = dr.research("问题", sub_queries=["a", "b", "c"])
    assert result["total_results"] == 1  # 三个查询命中同一结果，去重


def test_generate_sub_queries_fallback():
    """LLM 失败时退化为原问题"""

    class _FailClient:
        def chat_sync(self, *a, **kw):
            raise RuntimeError("模型不可用")

    class _FailGenerator:
        client = _FailClient()
        model = "m"

    dr = DeepResearch(retriever=_StubRetriever(), generator=_FailGenerator())
    assert dr._generate_sub_queries("原始问题") == ["原始问题"]