"""
配置管理路由

GET /v1/config  返回当前可调配置（延续前段的子集）
POST /v1/config 部分更新：pydantic 校验 → 写回 settings.yaml → 热应用到运行组件

热生效字段（改完立即生效，无需重启）：
- retrieval.strict_sources / top_k / rerank_top_k / similarity_threshold / enable_rerank
  / recall_candidates / rerank_candidates / synthesis_weight / context_token_budget
- generation.rewrite_query / max_history_rounds / history_token_budget / max_tokens / answer_style
- generation.rewrite_query / max_history_rounds / history_token_budget
- performance.response_cache / response_cache_ttl
- syntheses.enabled
- routing.*（模型路由）
- ocr.*（OCR 开关/模型/护栏；客户端每次现读配置，下一轮索引或转换即生效）
其余字段（模型名、超时、嵌入缓存容量等）将持久化但需重启生效。
"""

import threading
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from src.config import AppConfig, config_manager
from src.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["config"])

_pipeline = None

# 读-改-写序列化锁：避免并发 POST /v1/config 基于同一份快照合并而互相覆盖字段
_config_lock = threading.Lock()

# 允许通过 /v1/config 修改的顶层字段白名单（安全护栏）：
# 禁止修改 auth / documents / vector_store / omlx 等敏感或影响数据源的字段。
# ocr 在列：OCR 客户端由 src/document/ocr.get_ocr_client() **每次调用现读配置**
# 构造，因此改完下一轮索引/转换即生效，不需要在这里写热更新钩子。
_ALLOWED_TOP_LEVEL = {"retrieval", "generation", "performance", "syntheses", "routing", "ocr"}


def set_pipeline(pipeline):
    """注入 pipeline 实例"""
    global _pipeline
    _pipeline = pipeline


def _public_config() -> Dict[str, Any]:
    """对外暴露的配置子集（可热调的部分 + 模型信息）"""
    cfg = config_manager.config.model_dump()
    return {
        "omlx": {
            k: cfg["omlx"][k]
            for k in ("base_url", "chat_model", "embedding_model", "reranker_model", "timeout")
        },
        "retrieval": cfg["retrieval"],
        "generation": cfg["generation"],
        "performance": cfg["performance"],
        "syntheses": cfg["syntheses"],
        "ocr": cfg["ocr"],
        "routing": cfg["routing"],
    }


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并 dict（overlay 覆盖 base）"""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _apply_hot(pipeline, payload: Dict[str, Any]):
    """把本次请求涉及的字段热应用到运行中的组件"""
    if pipeline is None:
        return

    retrieval = payload.get("retrieval", {})
    if "strict_sources" in retrieval:
        pipeline.strict_sources = bool(retrieval["strict_sources"])
    if "context_token_budget" in retrieval and hasattr(pipeline, "max_context_tokens"):
        pipeline.max_context_tokens = max(1, int(retrieval["context_token_budget"]))
    retriever = getattr(pipeline, "retriever", None)
    if retriever is not None:
        if "top_k" in retrieval:
            retriever.top_k = int(retrieval["top_k"])
        if "rerank_top_k" in retrieval:
            retriever.rerank_top_k = int(retrieval["rerank_top_k"])
        if "similarity_threshold" in retrieval:
            retriever.similarity_threshold = float(retrieval["similarity_threshold"])
        if "recall_candidates" in retrieval:
            retriever.recall_candidates = int(retrieval["recall_candidates"])
        if "rerank_candidates" in retrieval:
            retriever.rerank_candidates = max(int(retrieval["rerank_candidates"]),
                                              getattr(retriever, "rerank_top_k", 3))
        if "synthesis_weight" in retrieval:
            retriever.synthesis_weight = float(retrieval["synthesis_weight"])
        if "enable_rerank" in retrieval and getattr(retriever, "reranker", None) is not None:
            retriever.reranker.enabled = bool(retrieval["enable_rerank"])

    generation = payload.get("generation", {})
    generator = getattr(pipeline, "generator", None)
    if generator is not None:
        if "rewrite_query" in generation:
            generator.rewrite_query = bool(generation["rewrite_query"])
        if "max_history_rounds" in generation:
            generator.max_history_rounds = int(generation["max_history_rounds"])
        if "history_token_budget" in generation:
            generator.history_token_budget = int(generation["history_token_budget"])
        if "max_tokens" in generation:
            generator.max_tokens = max(1, int(generation["max_tokens"]))
        if "answer_style" in generation and generation["answer_style"] in ("brief", "balanced", "detailed"):
            generator.answer_style = generation["answer_style"]
    # synthesis_weight 等字段已热更；context_token_budget/max_tokens 见上（生成档位联动）

    performance = payload.get("performance", {})
    cache = getattr(pipeline, "response_cache", None)
    if cache is not None:
        if "response_cache" in performance:
            cache.enable(bool(performance["response_cache"]))
        if "response_cache_ttl" in performance:
            cache.ttl = int(performance["response_cache_ttl"])

    syntheses = payload.get("syntheses", {})
    if "enabled" in syntheses:
        pipeline.save_syntheses = bool(syntheses["enabled"])

    routing = payload.get("routing", {})
    router = getattr(pipeline, "model_router", None)
    if router is not None and routing:
        router.update(routing)


@router.get("/config")
def get_config() -> Dict[str, Any]:
    """获取当前可调配置"""
    try:
        return _public_config()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="配置未加载")


@router.post("/config")
def update_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    部分更新配置：校验 → 持久化写回 → 热生效

    声明为 def（非 async def）：整段持 _config_lock 做读-改-写并落盘，
    是同步阻塞工作；def 端点由 Starlette 放入线程池执行，不冻结事件循环。
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")
    if not payload or not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须为非空 JSON 对象")

    # 安全护栏：仅允许修改白名单内的顶层字段，
    # 禁止通过此接口篡改 auth / documents / vector_store / omlx 等敏感或危险字段
    forbidden = set(payload.keys()) - _ALLOWED_TOP_LEVEL
    if forbidden:
        raise HTTPException(
            status_code=400,
            detail=f"不允许修改的字段: {', '.join(sorted(forbidden))}（仅允许 {', '.join(sorted(_ALLOWED_TOP_LEVEL))}）",
        )

    # 1. 合并并校验（pydantic 保证类型与合法值）
    #    整个读-改-写过程持锁，避免并发 POST 相互覆盖字段
    with _config_lock:
        current = config_manager.config.model_dump()
        merged = _deep_merge(current, payload)
        try:
            new_config = AppConfig(**merged)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"配置校验失败: {e}")

        # 2. 持久化写回 yaml + 更新内存
        try:
            config_manager.replace(new_config)
            config_manager.save()
        except Exception as e:
            # 配置校验/保存的内部细节只进日志，不回传客户端
            logger.exception("配置保存失败: %s", e)
            raise HTTPException(status_code=500, detail="配置保存失败，请查看服务日志")

    # 3. 热应用本次请求涉及的字段
    _apply_hot(_pipeline, payload)

    return {
        "ok": True,
        "config": _public_config(),
        "note": "检索/生成/沉淀/缓存/路由相关字段已热生效；模型名等字段需重启生效",
    }