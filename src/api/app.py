"""
FastAPI 应用主入口
"""

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import query, index, status, research, config, sessions, archive
from src.config import get_config


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
    if not auth.api_key or header != f"Bearer {auth.api_key}":
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    return app