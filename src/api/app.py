"""
FastAPI 应用主入口
"""

import secrets
import time
import uuid
from collections import defaultdict, deque
from threading import Lock

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from src.api.routes import query, index, status, research, config, sessions, archive, convert
from src.api.metrics import record as record_metric
from src.config import get_config
from src.logging_setup import get_logger, reset_request_id, set_request_id

logger = get_logger("rag.api")

# 插件状态栏轮询探针：这些高频 GET 只记 DEBUG，避免刷屏；错误仍在 ERROR
_QUIET_PROBES = {("/v1/health", "GET"), ("/v1/status", "GET")}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """简易每 IP 限流（滑动窗口 60s；读 config.rate_limit，/v1/config 可热开关）

    - enabled=false（默认）：直接放行（本地行为不变）
    - enabled=true：同一来源 IP 每分钟超过 max_per_minute 次 → 429
    - 来源默认取直连 IP（request.client.host）；不信任 X-Forwarded-For，
      因为直连本地服务时该头可被客户端任意伪造，信任它等于解除限流。
    - 定期清理过期 IP 条目，避免字典无界增长（内存泄漏）。
    """

    WINDOW = 60.0
    # 每次清理间隔（秒）：超过则该次 dispatch 顺带清理一次过期/空条目
    _CLEANUP_INTERVAL = 300.0

    def __init__(self, app):
        super().__init__(app)
        self._hits: "defaultdict[str, deque[float]]" = defaultdict(deque)
        self._lock = Lock()
        self._last_cleanup = 0.0

    def _client_ip(self, request: Request) -> str:
        # 直连 IP：不信任可被客户端伪造的 X-Forwarded-For
        return request.client.host if request.client else "unknown"

    def _cleanup_if_due(self, now: float) -> None:
        """定期清理：删除窗口外空条目，防止冷 IP 的过期记录永久残留"""
        if now - self._last_cleanup < self._CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        stale = [
            ip for ip, q in self._hits.items()
            if not q or now - q[-1] > self.WINDOW
        ]
        for ip in stale:
            del self._hits[ip]

    async def dispatch(self, request: Request, call_next):
        try:
            rl = get_config().rate_limit
        except RuntimeError:
            rl = None
        if rl is None or not rl.enabled:
            return await call_next(request)
        ip = self._client_ip(request)
        now = time.monotonic()
        with self._lock:
            self._cleanup_if_due(now)
            q = self._hits[ip]
            while q and now - q[0] > self.WINDOW:
                q.popleft()
            if len(q) >= max(1, rl.max_per_minute):
                return Response("请求过于频繁，请稍后再试", status_code=429, media_type="text/plain; charset=utf-8")
            q.append(now)
        return await call_next(request)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件（结构化日志方案 C 的 A 部分）

    - 为每个请求生成 request_id（或沿用 X-Request-ID 头），写入 contextvar
      → 该请求生命周期内 pipeline/检索/生成/异常日志自动携带同 id，可跨模块关联定位
    - 记录 完成（状态/耗时）与 异常（完整堆栈），异常仍向上抛（HTTP 500 逻辑不变）
    - 插件状态栏的 /health、/status 探针仅记 DEBUG（降噪）
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = set_request_id(rid)
        start = time.perf_counter()
        path = request.url.path
        method = request.method
        try:
            response = await call_next(request)
            ms = round((time.perf_counter() - start) * 1000, 1)
            record_metric(method, path, response.status_code, ms)
            extra = {"method": method, "path": path, "status": response.status_code, "latency_ms": ms}
            if (path, method) in _QUIET_PROBES and response.status_code < 400:
                logger.debug("request", extra=extra)
            else:
                logger.info("request", extra=extra)
            response.headers["X-Request-Id"] = rid
            return response
        except Exception as e:
            ms = round((time.perf_counter() - start) * 1000, 1)
            record_metric(method, path, 500, ms)
            logger.error(
                "request_error",
                exc_info=e,
                extra={"method": method, "path": path, "latency_ms": ms, "error_msg": str(e)[:500]},
            )
            raise
        finally:
            reset_request_id(token)


async def verify_bearer(request: Request) -> None:
    """
    Bearer 认证依赖（公网开放预留，P0.5-lite）

    - auth.enabled=false（默认）：直接放行，行为与历史版本一致
    - auth.enabled=true：校验 Authorization: Bearer <api_key>；
      Obsidian 插件已默认携带该请求头，无需插件端改动
    - 配置未加载（如部分测试环境）时不校验
    """
    try:
        auth = get_config().auth
    except RuntimeError:
        return
    if not auth.enabled:
        return
    header = request.headers.get("Authorization", "")
    # 常量时间比较，避免时序侧信道探测 api_key
    if not auth.api_key or not secrets.compare_digest(header, f"Bearer {auth.api_key}"):
        raise HTTPException(status_code=401, detail="未授权：请携带有效的 Authorization: Bearer <api_key>")


def create_app(pipeline=None) -> FastAPI:
    """
    创建 FastAPI 应用
    """
    app = FastAPI(
        title="RAG Service API",
        description="个人知识库 RAG 服务",
        version="1.0.0",
    )

    # CORS 配置（允许 Obsidian 等本地应用调用）
    # 认证走 Authorization: Bearer 头（非 cookie），故 allow_credentials=False 即可。
    # 若启用 cookie 型认证，allow_origins 不得为 *，需收敛为已知来源。
    #
    # ⚠️ 已知风险（D6-2，2026-09-11 决策：保持现状 + 文档标注）：
    #   服务默认监听本机且 auth.enabled=false，此时 allow_origins=["*"] 意味着
    #   用户浏览器中**任意网页**都能 fetch('http://127.0.0.1:8000/v1/query')
    #   向本机知识库提问并读回答案，也能读 /v1/status、/v1/sessions。
    #   注释里"允许 Obsidian 调用"的理由并不依赖通配——Obsidian 的常规请求
    #   不受同源策略约束（不经浏览器 CORS 检查），通配是为别的本地工具留的。
    #
    #   推翻本决策的条件（满足任一即应收敛为白名单）：
    #     1. 将服务暴露到非 127.0.0.1（局域网 / 反代 / 容器端口映射）；
    #     2. auth.enabled 保持 false 且用户会在打开任意网页时同时运行本服务
    #        （即"浏览器里挂着别的标签页"成为常态）；
    #     3. 上线任何按 cookie 或浏览器自动携带凭据鉴权的形态。
    #   收窄时改为显式来源列表：app://obsidian.md、null、127.0.0.1:*、localhost:*，
    #   并先在真实 Obsidian 中确认流式（SSE）仍可用。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 中间件注册顺序 = 由内到外（后注册者在外层）。
    # 限流注册在日志之前 → 日志中间件处于更外层，被限流拒绝的请求
    # （429 由限流中间件直接返回）同样会进入请求日志与 metrics；
    # 否则限流命中会成为观测盲区（既不打日志也不计数），难以排查。
    app.add_middleware(RateLimitMiddleware)
    # 请求日志中间件（外层：覆盖所有请求的收尾记录）
    app.add_middleware(RequestLoggingMiddleware)

    # 注入 pipeline
    if pipeline:
        query.set_pipeline(pipeline)
        index.set_pipeline(pipeline)
        status.set_pipeline(pipeline)
        research.set_pipeline(pipeline)
        config.set_pipeline(pipeline)
        sessions.set_pipeline(pipeline)
        archive.set_pipeline(pipeline)

    # 注册路由（挂载认证依赖；默认关闭，公网开放时只需改配置）
    auth_deps = [Depends(verify_bearer)]
    app.include_router(query.router, dependencies=auth_deps)
    app.include_router(index.router, dependencies=auth_deps)
    app.include_router(status.router, dependencies=auth_deps)
    app.include_router(research.router, dependencies=auth_deps)
    app.include_router(config.router, dependencies=auth_deps)
    app.include_router(sessions.router, dependencies=auth_deps)
    app.include_router(archive.router, dependencies=auth_deps)
    app.include_router(convert.router, dependencies=auth_deps)

    # 全局兜底异常处理：避免未捕获异常直接泄露 traceback/内部路径给客户端；
    # 详细错误记入日志，客户端只收到通用错误信息
    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("未处理异常: %s %s", request.method, request.url.path)
        return Response(
            content='{"detail":"服务器内部错误"}',
            status_code=500,
            media_type="application/json",
        )

    return app