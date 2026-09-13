"""
后端优化（P1/P2）测试：客户端重试、请求指标
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class _Err(Exception):
    """带 response.status_code 的伪错误"""

    def __init__(self, status):
        super().__init__(f"err {status}")
        self.response = type("R", (), {"status_code": status})()


class TestClientRetry:
    def test_retryable_5xx(self):
        from src.embedding.client import OMLXClient
        assert OMLXClient._retryable(_Err(500)) is True
        assert OMLXClient._retryable(_Err(503)) is True

    def test_not_retryable_4xx(self):
        from src.embedding.client import OMLXClient
        assert OMLXClient._retryable(_Err(400)) is False
        assert OMLXClient._retryable(_Err(404)) is False
        assert OMLXClient._retryable(_Err(429)) is False

    def test_transport_error_retryable(self):
        from src.embedding.client import OMLXClient
        assert OMLXClient._retryable(RuntimeError("connect failed")) is True

    def test_retry_sync_succeeds_after_failure(self):
        from src.embedding.client import OMLXClient
        client = OMLXClient(base_url="http://x", api_key="k", timeout=1)
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Err(500)
            return "ok"

        assert client._retry_sync(flaky, attempts=2, backoff=0) == "ok"
        assert calls["n"] == 2

    def test_retry_sync_gives_up_on_4xx(self):
        from src.embedding.client import OMLXClient
        client = OMLXClient(base_url="http://x", api_key="k", timeout=1)

        def bad():
            raise _Err(400)

        with pytest.raises(_Err):
            client._retry_sync(bad, attempts=2, backoff=0)

    async def test_retry_async_succeeds_after_failure(self):
        from src.embedding.client import OMLXClient
        client = OMLXClient(base_url="http://x", api_key="k", timeout=1)
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Err(500)
            return "ok"

        assert await client._retry_async(flaky, attempts=2, backoff=0) == "ok"


class TestRequestMetrics:
    def test_record_and_snapshot(self):
        from src.api import metrics
        metrics.clear()
        metrics.record("GET", "/v1/health", 200, 1.2)
        metrics.record("POST", "/v1/query", 200, 25000.0)
        metrics.record("GET", "/v1/nope", 404, 3.0)
        snap = metrics.snapshot()
        assert snap["total"] == 3
        assert snap["errors"] == 1
        assert snap["max_latency_ms"] == 25000.0
        assert len(snap["recent"]) == 3
        assert snap["recent"][0]["path"] == "/v1/health"

    def test_middleware_populates_metrics(self):
        from fastapi.testclient import TestClient
        from src.api import metrics
        from src.api.app import create_app
        metrics.clear()
        app = create_app(None)
        client = TestClient(app)
        client.get("/v1/health")
        snap = metrics.snapshot()
        assert snap["total"] >= 1
        assert any(r["path"] == "/v1/health" for r in snap["recent"])
        metrics.clear()

class TestHotApplyGenerationTier:
    """/v1/config 热更新新增字段：context_token_budget / generation.max_tokens"""

    def test_apply_hot_updates_pipeline(self):
        from src.api.routes.config import _apply_hot

        class Retr:
            pass

        class Gen:
            max_tokens = 2048

        class Pipe:
            max_context_tokens = 8000
            retriever = Retr()
            generator = Gen()

        pipe = Pipe()
        _apply_hot(
            pipe,
            {
                "retrieval": {"context_token_budget": 3000},
                "generation": {"max_tokens": 512},
            },
        )
        assert pipe.max_context_tokens == 3000
        assert pipe.generator.max_tokens == 512
        # 边界：非法值收敛到 >=1
        _apply_hot(pipe, {"retrieval": {"context_token_budget": 0}, "generation": {"max_tokens": -5}})
        assert pipe.max_context_tokens >= 1
        assert pipe.generator.max_tokens >= 1


class TestRateLimit:
    def test_disabled_by_default_passes(self):
        from fastapi.testclient import TestClient
        from src.api.app import create_app
        app = create_app(None)
        client = TestClient(app)
        for _ in range(5):
            assert client.get("/v1/health").status_code == 200

    def test_enabled_returns_429_after_limit(self):
        from fastapi.testclient import TestClient
        from src.api.app import create_app
        from src.config import config_manager
        config_manager.load()
        cfg = config_manager.config
        old = (cfg.rate_limit.enabled, cfg.rate_limit.max_per_minute)
        cfg.rate_limit.enabled = True
        cfg.rate_limit.max_per_minute = 2
        try:
            app = create_app(None)
            client = TestClient(app)
            assert client.get("/v1/health").status_code == 200
            assert client.get("/v1/health").status_code == 200
            assert client.get("/v1/health").status_code == 429
        finally:
            cfg.rate_limit.enabled, cfg.rate_limit.max_per_minute = old


class TestSessionCleanupExport:
    def test_cleanup_keeps_newest(self, tmp_path):
        from src.session.store import ConversationStore
        store = ConversationStore(dir_path=str(tmp_path / "conv"))
        for i in range(5):
            store.create(title=f"会话{i}")
        # 更新前面两条时间戳，保证排序稳定
        removed = store.cleanup(keep=2)
        assert len(removed) == 3
        assert store.list().__len__() == 2

    def test_export_all_includes_messages(self, tmp_path):
        from src.session.store import ConversationStore
        store = ConversationStore(dir_path=str(tmp_path / "conv"))
        s = store.create(title="t")
        store.append(s["id"], "user", "问")
        store.append(s["id"], "assistant", "答")
        out = store.export_all()
        assert out["count"] == 1
        assert out["sessions"][0]["messages"][0]["content"] == "问"


class TestAnswerStyle:
    def test_brief_changes_system_prompt(self):
        from src.embedding.client import OMLXClient
        from src.generation.generator import Generator
        g = Generator(client=OMLXClient(base_url="http://x", api_key="k", timeout=1), model="m")
        assert "务必简要" not in g._effective_system_prompt()
        g.answer_style = "brief"
        eff = g._effective_system_prompt()
        assert "务必简要" in eff
        assert "务必简要" in g._build_messages("q", "ctx")[0]["content"]
        g.answer_style = "detailed"
        assert "完整详实" in g._effective_system_prompt()
        g.answer_style = "balanced"
        assert "务必简要" not in g._effective_system_prompt()

    def test_hot_apply_answer_style(self):
        from src.api.routes.config import _apply_hot

        class Gen:
            answer_style = "balanced"

        class Pipe:
            generator = Gen()
            max_context_tokens = 8000

        pipe = Pipe()
        _apply_hot(pipe, {"generation": {"answer_style": "brief"}})
        assert pipe.generator.answer_style == "brief"
        _apply_hot(pipe, {"generation": {"answer_style": "hacker"}})  # 非法值忽略
        assert pipe.generator.answer_style == "brief"


class TestRerankCacheStats:
    def test_hits_and_misses_counted(self):
        from test_regression_recall_window import _api_fixture, _SpecStore, _SpecEmbedder, _FakeReranker, _retriever
        docs, spec = _api_fixture()
        store = _SpecStore(docs)
        r = _retriever(store, spec)
        s1 = r.rerank_cache_stats
        assert s1["hits"] == 0 and s1["misses"] == 0 and s1["hit_rate"] == 0.0
        r.retrieve("同题", use_rerank=True)  # miss
        r.retrieve("同题", use_rerank=True)  # hit
        s2 = r.rerank_cache_stats
        assert s2["hits"] == 1 and s2["misses"] == 1 and s2["hit_rate"] == 0.5


class TestDailyBackup:
    def test_run_once_writes_and_prunes(self, tmp_path):
        from src.pipeline.backup import run_once
        from src.session.store import ConversationStore
        store = ConversationStore(dir_path=str(tmp_path / "conv"))
        s = store.create(title="t")
        store.append(s["id"], "user", "q")
        path = run_once(store, str(tmp_path / "backups"), keep=1)
        assert path and Path(path).exists()
        import time as _t
        _t.sleep(1.1)  # 备份文件名精确到秒，避免同秒覆盖
        # 再备一次 → 保留 1 份，旧的被清掉
        path2 = run_once(store, str(tmp_path / "backups"), keep=1)
        assert path2 != path
        from pathlib import Path as P
        files = list(P(tmp_path / "backups").glob("rag-backup-*.json"))
        assert len(files) == 1
        import json as _j
        assert _j.load(open(files[0], encoding="utf-8"))["sessions"]["count"] == 1
