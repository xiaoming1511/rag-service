"""
模型路由测试（离线）

覆盖：回退默认、任务覆盖、热更新、Generator 按任务取模型、DeepResearch 拆解走路由
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.generation.model_router import ModelRouter
from src.generation.generator import Generator
from src.embedding.client import OMLXClient


def test_router_default_fallback():
    """未配置的任务回退到默认模型"""
    router = ModelRouter(default_model="qwen-base", tasks={})
    assert router.resolve("chat") == "qwen-base"
    assert router.resolve("rewrite") == "qwen-base"
    assert router.resolve("research_subqueries") == "qwen-base"
    assert router.resolve("unknown_task") == "qwen-base"  # 未知任务回退


def test_router_task_override():
    """配置的任务使用指定模型"""
    router = ModelRouter(
        default_model="qwen-base",
        tasks={"rewrite": "gemma-4-e4b-it-mxfp4", "research_subqueries": "fast-model"},
    )
    assert router.resolve("chat") == "qwen-base"
    assert router.resolve("rewrite") == "gemma-4-e4b-it-mxfp4"
    assert router.resolve("research_subqueries") == "fast-model"


def test_router_update_hot():
    """热更新：运行中修改路由立即生效"""
    router = ModelRouter(default_model="qwen-base")
    router.update({"rewrite": "gemma-4-e4b-it-mxfp4"})
    assert router.resolve("rewrite") == "gemma-4-e4b-it-mxfp4"
    router.update({"rewrite": ""})  # 清空 → 回退默认
    assert router.resolve("rewrite") == "qwen-base"
    # 不支持的任务名被忽略
    router.update({"not_a_task": "x"})
    assert "not_a_task" not in router.tasks


class _CapturingClient:
    """捕获每次调用使用的 model 名"""

    def __init__(self):
        self.calls = []

    def chat_sync(self, model, messages, **kwargs):
        self.calls.append(model)
        return "改写结果"

    def chat_stream_sync(self, model, messages, **kwargs):
        self.calls.append(model)
        yield "片段"


def test_generator_model_routing():
    """Generator 按任务取模型：chat → 默认，rewrite → 路由模型"""
    client = _CapturingClient()
    router = ModelRouter(default_model="chat-model", tasks={"rewrite": "rewrite-model"})
    gen = Generator(client=client, model="chat-model", rewrite_query=True, model_router=router)

    # chat 任务
    gen.generate("问题", "上下文")
    list(gen.generate_stream("问题", "上下文"))  # 迭代生成器才能触发调用
    # rewrite 任务
    gen.rewrite_question("追问", [{"role": "user", "content": "历史"}])

    assert client.calls[0] == "chat-model"
    assert client.calls[1] == "chat-model"
    assert client.calls[2] == "rewrite-model"


def test_generator_router_tasks_exposed():
    """路由表可通过 Generator.model_for 查询"""
    router = ModelRouter(default_model="base", tasks={"research_subqueries": "research-model"})
    gen = Generator(client=_CapturingClient(), model="base", model_router=router)
    assert gen.model_for("research_subqueries") == "research-model"
    assert gen.model_for("chat") == "base"