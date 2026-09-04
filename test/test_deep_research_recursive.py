"""
递归 Deep Research 测试（离线桩）

覆盖：多轮展开、新信息增益提前停止、追问生成
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.deep_research import DeepResearch
from src.vector_store.base import SearchResult


class _StubClient:
    """第一次调用返回初端子问题，之后返回追问"""

    def __init__(self):
        self.calls = 0

    def chat_sync(self, model, messages, **kwargs):
        self.calls += 1
        system = messages[0]["content"]
        if "拆解" in system:
            return "1. 子问题A\n2. 子问题B"
        return "1. 追问C\n2. 追问D"


class _StubGenerator:
    client = _StubClient()
    model = "m"

    def generate(self, query, context, history=None, **kwargs):
        self.last_context = context
        return f"报告:{query}"


class _StubRetriever:
    """轮次 1 返回 r1/r2；轮次 2 返回 r2/r3（有新信息）"""

    def __init__(self):
        self.round = 0
        self.queries = []

    def retrieve(self, query, top_k=None, where=None, use_rerank=True):
        self.queries.append(query)
        self.round += 1
        ids = ["r1", "r2"] if self.round <= 2 else ["r2", "r3"]
        return [SearchResult(id=i, content=f"内容{i}", score=0.9,
                             metadata={"file_name": f"{i}.md", "file_path": f"/{i}.md"})
                for i in ids]


def test_recursive_research_two_rounds():
    """默认 2 轮：子查询 + 追问两轮检索，报告与轮次信息正确"""
    gen = _StubGenerator()
    retriever = _StubRetriever()
    dr = DeepResearch(retriever=retriever, generator=gen, sub_query_count=2)

    result = dr.research("研究什么？")

    assert result["rounds"] == 2
    assert len(result["sub_queries"]) >= 4  # 初始 2 + 追问 2
    assert result["report"].startswith("报告:")
    assert "内容" in gen.last_context
    assert result["total_results"] == 3  # r1/r2/r3 去重后


class _NoNewRetriever:
    """两轮都返回相同 id → 新信息增益为 0 → 提前停止"""

    def __init__(self):
        self.queries = []

    def retrieve(self, query, top_k=None, where=None, use_rerank=True):
        self.queries.append(query)
        return [SearchResult(id="same", content="内容", score=0.8,
                             metadata={"file_name": "x.md"})]


def test_recursive_stops_on_no_new_info():
    """追问轮无新结果 → 提前停止（执行 2 轮而非最大 3 轮）"""
    gen = _StubGenerator()
    dr = DeepResearch(retriever=_NoNewRetriever(), generator=gen, sub_query_count=2, max_rounds=3)
    result = dr.research("研究什么？")
    assert result["rounds"] == 2  # 初始轮 + 追问轮（无新信息即停，未到 3）
    assert result["total_results"] == 1


def test_max_rounds_override():
    """max_rounds 参数覆盖构造值"""
    gen = _StubGenerator()
    dr = DeepResearch(retriever=_NoNewRetriever(), generator=gen, max_rounds=5)
    res1 = dr.research("q", max_rounds=1)
    assert res1["rounds"] == 1