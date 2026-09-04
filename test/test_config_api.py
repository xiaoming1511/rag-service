"""
配置管理 API 测试（TestClient + 临时配置 + 桩管道）

覆盖：GET 读取、POST 热生效、持久化写回、非法值 422、模型路由热更新
"""

import sys
import yaml
from pathlib import Path
from types import SimpleNamespace

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from src.config import config_manager
from src.api.app import create_app
from src.generation.model_router import ModelRouter
from src.cache.response_cache import ResponseCache


def _fake_pipeline():
    """构造带全部热更新字段的桩管道"""
    return SimpleNamespace(
        strict_sources=False,
        retriever=SimpleNamespace(
            top_k=5,
            rerank_top_k=3,
            similarity_threshold=0.5,
            reranker=SimpleNamespace(enabled=True),
        ),
        generator=SimpleNamespace(
            rewrite_query=False,
            max_history_rounds=10,
            history_token_budget=2000,
        ),
        response_cache=ResponseCache(enabled=True, ttl=3600),
        save_syntheses=True,
        model_router=ModelRouter("qwen-default"),
    )


@pytest.fixture()
def client(tmp_path):
    """隔离环境：临时配置 + 桩管道 + TestClient"""
    cfg_path = tmp_path / "settings.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "omlx": {"base_url": "http://x", "chat_model": "qwen-default",
                     "embedding_model": "bge", "reranker_model": "rerank"},
            "retrieval": {"top_k": 5, "rerank_top_k": 3, "enable_rerank": True,
                          "similarity_threshold": 0.5, "strict_sources": False},
            "generation": {"max_tokens": 512, "temperature": 0.3, "stream": True,
                           "max_history_rounds": 10, "history_token_budget": 2000,
                           "rewrite_query": False},
            "performance": {"embed_cache_capacity": 4096, "index_max_workers": 0,
                            "response_cache": True, "response_cache_ttl": 3600},
            "syntheses": {"enabled": True, "dir": ""},
            "routing": {"chat": "", "rewrite": "", "research_subqueries": ""},
        }, allow_unicode=True),
        encoding="utf-8",
    )
    config_manager.load(str(cfg_path))
    pipeline = _fake_pipeline()
    app = create_app(pipeline)
    tc = TestClient(app)

    yield tc, pipeline, cfg_path

    # 清理单例，避免影响其他测试
    config_manager._config = None
    config_manager._config_path = None


def test_get_config(client):
    """GET /v1/config：返回各配置段"""
    tc, _, _ = client
    resp = tc.get("/v1/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["retrieval"]["strict_sources"] is False
    assert data["retrieval"]["similarity_threshold"] == 0.5
    assert data["syntheses"]["enabled"] is True
    assert set(data["routing"]) == {"chat", "rewrite", "research_subqueries"}
    assert data["omlx"]["chat_model"] == "qwen-default"


def test_post_config_hot_apply_and_persist(client):
    """POST：热生效 + 写回 yaml"""
    tc, pipeline, cfg_path = client

    resp = tc.post("/v1/config", json={"retrieval": {"strict_sources": True, "top_k": 8}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["config"]["retrieval"]["strict_sources"] is True

    # 热生效到管道
    assert pipeline.strict_sources is True
    assert pipeline.retriever.top_k == 8

    # 持久化到 yaml
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["retrieval"]["strict_sources"] is True
    assert saved["retrieval"]["top_k"] == 8

    # 内存配置同步更新
    assert config_manager.config.retrieval.strict_sources is True


def test_post_config_multiple_sections(client):
    """POST：一次更新多个配置段"""
    tc, pipeline, _ = client
    resp = tc.post("/v1/config", json={
        "generation": {"rewrite_query": True, "max_history_rounds": 20},
        "syntheses": {"enabled": False},
        "performance": {"response_cache": False, "response_cache_ttl": 60},
        "routing": {"rewrite": "gemma-4-e4b-it-mxfp4"},
    })
    assert resp.status_code == 200

    assert pipeline.generator.rewrite_query is True
    assert pipeline.generator.max_history_rounds == 20
    assert pipeline.save_syntheses is False
    assert pipeline.response_cache.enabled is False
    assert pipeline.response_cache.ttl == 60
    assert pipeline.model_router.resolve("rewrite") == "gemma-4-e4b-it-mxfp4"


def test_post_config_invalid_value(client):
    """非法值：422，且不修改任何状态"""
    tc, pipeline, cfg_path = client
    before = cfg_path.read_text(encoding="utf-8")

    resp = tc.post("/v1/config", json={"retrieval": {"top_k": "abc"}})
    assert resp.status_code == 422

    # 管道与配置文件都未被修改
    assert pipeline.retriever.top_k == 5
    assert cfg_path.read_text(encoding="utf-8") == before


def test_post_config_empty_body(client):
    """空请求体：400"""
    tc, _, _ = client
    assert tc.post("/v1/config", json={}).status_code == 400