"""
第八轮（Round 8）回归：D6-1 usage 竞态修复（加法式 usage_out 契约）

对应本轮修的真实缺陷（先实证复现，修复后转回归）：

D6-1  异步流式路径的 usage 竞态：OMLXClient.last_chat_usage 与
      Generator.last_usage 都是长生命周期共享单例上的实例属性。
      纯事件循环路径（generate_stream_async）中两个交错协程共享同一线程，
      线程局部救不了；A 请求的 usage 写点（流末块）与读取点之间存在
      挂起点（等待流 EOF），期间 B 请求完成并覆盖共享属性 →
      A 的 timing.usage 报成 B 的 token 数。并发越多越明显，且永不报错。

修复方式（A 方案）：加法式 usage_out 契约——
  - client 四个 chat 方法的 usage 同时写入调用方传入的 usage_out 字典
    （per-call 局部对象，无共享）；
  - generator 四个方法向下传递并向上转发 usage_out；
  - pipeline 三处消费点改为读自己的局部 holder，
    不再读共享的 generator.last_usage（旧属性保留但降级为诊断用途）。

桩时序说明（忠实还原真实流）：usage 写点位于流末块，写点之后仍有一次
「等待流 EOF」的挂起，随后才 StopAsyncIteration——这个挂起正是
写点→读取点之间的竞态窗口。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.generation.generator import Generator
from src.pipeline.rag_pipeline import RAGPipeline
from src.vector_store.base import SearchResult

USAGE_A = {"total_tokens": 111}
USAGE_B = {"total_tokens": 222}


# ======================================================================
# 桩
# ======================================================================

class _RetrieverStub:
    last_timings = {}

    def retrieve_with_context(self, query, top_k=None, where=None,
                              use_rerank=True, max_context_tokens=None):
        return "ctx", [SearchResult(id="c0", content="内容", score=0.9,
                                    metadata={"file_name": "d.md"})]


class _GatedUsageClient:
    """忠实还原真实时序的流式桩。

    - usage 写点位于流末块（include_usage 末块，无 choices）；
    - 写点之后仍有一次 EOF 等待挂起（asyncio.sleep(0)），随后流结束；
    - last_chat_usage 是共享实例属性（与真实 OMLXClient 一致）；
    - 支持 usage_out 新契约（修复后的 generator 会传入）。
    """

    def __init__(self):
        self.last_chat_usage = None
        self.a_usage_written = asyncio.Event()

    async def chat_stream_async(self, model, messages, usage_out=None, **kwargs):
        # Generator 会把 query 包进用户消息模板（…【问题】\n{query}\n\n【要求】…），
        # 故从「【问题】」之后提取请求标识
        content = messages[-1]["content"]
        marker = content.split("【问题】", 1)[1] if "【问题】" in content else ""
        tag = "A" if marker.lstrip().startswith("A") else "B"
        if tag == "B":
            # 保证 B 的覆盖写发生在 A 的写点之后、读取点之前
            await self.a_usage_written.wait()
        yield f"{tag}-答"
        usage = dict(USAGE_A if tag == "A" else USAGE_B)
        if usage_out is not None:
            usage_out["usage"] = dict(usage)
        self.last_chat_usage = dict(usage)      # 真实共享写点
        if tag == "A":
            self.a_usage_written.set()
        await asyncio.sleep(0)                  # EOF 等待挂起：竞态窗口


def _mk_pipeline(client):
    return RAGPipeline(
        retriever=_RetrieverStub(),
        generator=Generator(client=client, model="test-model"),
        save_syntheses=False,
    )


async def _collect_timing(pipe, question):
    timing = None
    async for ev in pipe.query_stream_async(question):
        if '"type": "timing"' in ev:
            timing = json.loads(ev[len("data: "):])["data"]
    return timing


# ======================================================================
# D6-1 核心：并发流式请求的 usage 隔离
# ======================================================================

class TestAsyncStreamUsageIsolation:
    def test_concurrent_streams_get_own_usage(self):
        """修复前：A 的写点（111）之后挂起，B 完成并覆盖共享属性（222），
        A 恢复后读到 222 → timing.usage 串号。修复后各自读 per-call holder。"""
        client = _GatedUsageClient()
        pipe = _mk_pipeline(client)

        async def one(q):
            return await _collect_timing(pipe, q)

        async def main():
            return await asyncio.gather(one("A"), one("B"))

        ta, tb = asyncio.run(main())

        assert ta is not None and tb is not None, "两条流都应发出 timing 事件"
        assert ta["usage"]["total_tokens"] == USAGE_A["total_tokens"], (
            f"A 请求拿到了别人的 usage: {ta.get('usage')}"
        )
        assert tb["usage"]["total_tokens"] == USAGE_B["total_tokens"], (
            f"B 请求拿到了别人的 usage: {tb.get('usage')}"
        )

    def test_single_stream_still_reports_usage(self):
        """基线：单请求 usage 功能不得因改造丢失。"""
        pipe = _mk_pipeline(_GatedUsageClient())
        timing = asyncio.run(_collect_timing(pipe, "A"))
        assert timing["usage"]["total_tokens"] == USAGE_A["total_tokens"]


# ======================================================================
# client 四方法的 usage_out 契约
# ======================================================================

def _usage_obj():
    return SimpleNamespace(
        prompt_tokens=1, completion_tokens=2, total_tokens=3,
        generation_tokens_per_second=9.9, time_to_first_token=0.1,
        generation_duration=0.2, total_time=0.3,
    )


class TestClientUsageOutContract:
    """四个 chat 方法的加法式契约：usage 同时写入共享属性（兼容）与 usage_out。"""

    def test_chat_sync_writes_usage_out(self):
        from src.embedding.client import OMLXClient

        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=_usage_obj(),
        )

        class _FakeSync:
            class chat:  # noqa: N801
                class completions:  # noqa: N801
                    @staticmethod
                    def create(**kwargs):
                        return resp

        client = OMLXClient(base_url="http://x")
        client._sync_client = _FakeSync()
        holder = {}
        text = client.chat_sync("m", [{"role": "user", "content": "q"}], usage_out=holder)
        assert text == "ok"
        assert holder["usage"]["total_tokens"] == 3
        assert client.last_chat_usage["total_tokens"] == 3

    def test_chat_async_writes_usage_out(self):
        from src.embedding.client import OMLXClient

        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=_usage_obj(),
        )

        class _Completions:
            async def create(self, **kwargs):
                return resp

        client = OMLXClient(base_url="http://x")
        client._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions()))
        holder = {}

        async def run():
            return await client.chat_async("m", [{"role": "user", "content": "q"}],
                                           usage_out=holder)

        assert asyncio.run(run()) == "ok"
        assert holder["usage"]["total_tokens"] == 3

    def test_chat_stream_sync_writes_usage_out(self):
        from src.embedding.client import OMLXClient

        usage_obj = _usage_obj()
        sent = {"n": 0}

        class _Chunk:
            choices = []
            usage = usage_obj

        class _FakeStream:
            def __iter__(self):
                return self

            def __next__(self):
                if sent["n"]:
                    raise StopIteration
                sent["n"] = 1
                return _Chunk()

            def close(self):
                pass

        class _Completions:
            @staticmethod
            def create(**kwargs):
                assert kwargs.get("stream") is True
                return _FakeStream()

        client = OMLXClient(base_url="http://x")
        client._sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions()))
        holder = {}

        chunks = list(client.chat_stream_sync("m", [{"role": "user", "content": "q"}],
                                              usage_out=holder))
        assert chunks == []
        assert holder["usage"]["total_tokens"] == 3
        assert client.last_chat_usage["total_tokens"] == 3

    def test_chat_stream_async_writes_usage_out(self):
        from src.embedding.client import OMLXClient

        usage_obj = _usage_obj()
        sent = {"n": 0}

        class _Chunk:
            choices = []
            usage = usage_obj

        class _FakeStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if sent["n"]:
                    raise StopAsyncIteration
                sent["n"] = 1
                return _Chunk()

            async def close(self):
                pass

        class _Completions:
            async def create(self, **kwargs):
                assert kwargs.get("stream") is True
                return _FakeStream()

        client = OMLXClient(base_url="http://x")
        client._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions()))
        holder = {}

        async def run():
            return [c async for c in client.chat_stream_async(
                "m", [{"role": "user", "content": "q"}], usage_out=holder)]

        assert asyncio.run(run()) == []
        assert holder["usage"]["total_tokens"] == 3

    def test_usage_out_survives_missing_usage(self):
        """上游未返回 usage（旧版 oMLX）时 holder 不应产生脏数据。"""
        from src.embedding.client import OMLXClient

        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        )

        class _Completions:
            async def create(self, **kwargs):
                return resp

        client = OMLXClient(base_url="http://x")
        client._async_client = SimpleNamespace(
            chat=SimpleNamespace(completions=_Completions()))
        holder = {}

        async def run():
            return await client.chat_async("m", [{"role": "user", "content": "q"}],
                                           usage_out=holder)

        asyncio.run(run())
        assert holder.get("usage") is None


# ======================================================================
# generator 层透传 + pipeline 同步通道
# ======================================================================

class _UsageEchoClient:
    """非流式桩：chat_sync/chat_async 把 usage 写入 usage_out（模拟真实 client）"""

    def __init__(self, usage):
        self.last_chat_usage = None
        self._usage = usage

    def chat_sync(self, model, messages, usage_out=None, **kwargs):
        if usage_out is not None:
            usage_out["usage"] = dict(self._usage)
        self.last_chat_usage = dict(self._usage)
        return "答"

    async def chat_async(self, model, messages, usage_out=None, **kwargs):
        if usage_out is not None:
            usage_out["usage"] = dict(self._usage)
        self.last_chat_usage = dict(self._usage)
        return "答"


class TestGeneratorUsageOutPassThrough:
    def test_generate_forwards_usage_out(self):
        gen = Generator(client=_UsageEchoClient(USAGE_A), model="m")
        holder = {}
        gen.generate("q", "ctx", usage_out=holder)
        assert holder["usage"]["total_tokens"] == 111

    def test_generate_async_forwards_usage_out(self):
        gen = Generator(client=_UsageEchoClient(USAGE_B), model="m")
        holder = {}

        async def run():
            await gen.generate_async("q", "ctx", usage_out=holder)

        asyncio.run(run())
        assert holder["usage"]["total_tokens"] == 222


class TestPipelineSyncPathUsage:
    """同步 query / 流式 query_stream 的 usage 仍随 timing 返回（改造不丢功能）"""

    def test_query_inner_usage_from_holder(self):
        client = _UsageEchoClient(USAGE_A)
        pipe = _mk_pipeline(client)
        res = pipe._query_inner("问题", history=None, top_k=3, use_rerank=True)
        assert res["timing_ms"]["usage"]["total_tokens"] == 111

    def test_query_stream_emits_usage_in_timing(self):
        class _SyncStreamClient(_UsageEchoClient):
            def chat_stream_sync(self, model, messages, usage_out=None, **kwargs):
                if usage_out is not None:
                    usage_out["usage"] = dict(self._usage)
                self.last_chat_usage = dict(self._usage)
                yield "你"
                yield "好"

        pipe = _mk_pipeline(_SyncStreamClient(USAGE_B))
        timing = None
        for ev in pipe.query_stream("问题"):
            if '"type": "timing"' in ev:
                timing = json.loads(ev[len("data: "):])["data"]
        assert timing is not None
        assert timing["usage"]["total_tokens"] == 222
