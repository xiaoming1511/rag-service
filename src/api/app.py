"""
FastAPI 应用主入口
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import query, index, status, research, config


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

    # 注册路由
    app.include_router(query.router)
    app.include_router(index.router)
    app.include_router(status.router)
    app.include_router(research.router)
    app.include_router(config.router)

    return app