"""
索引路由
"""

from fastapi import APIRouter, HTTPException

from src.api.schemas import (
    IndexRequest,
    IndexResponse,
    IndexRefreshResponse,
    IndexUrlRequest,
    IndexUrlResponse,
)

router = APIRouter(prefix="/v1", tags=["index"])

# 全局 pipeline 实例
_pipeline = None


def set_pipeline(pipeline):
    global _pipeline
    _pipeline = pipeline


@router.post("/index", response_model=IndexResponse)
async def index_documents(request: IndexRequest):
    """
    索引文档（全量）
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")

    try:
        result = _pipeline.index(
            source_dirs=request.source_dirs,
            rebuild=request.rebuild,
        )

        # 如果返回了错误
        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])

        return IndexResponse(
            success=True,
            total_documents=result.get("total_documents", 0),
            total_chunks=result.get("total_chunks", 0),
            vector_count=result.get("total_vectors", 0),
            message="索引完成",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/index/url", response_model=IndexUrlResponse)
async def index_url(request: IndexUrlRequest):
    """
    索引远程网页：抓取 → 解析 → 分块 → 嵌入 → 入库

    Args:
        request: {url, timeout}
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")

    try:
        result = _pipeline.index_url(url=request.url, timeout=request.timeout)

        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "索引失败"))

        return IndexUrlResponse(
            success=True,
            url=request.url,
            title=result.get("title") or None,
            chunk_count=result.get("chunks", 0),
            message="网页索引完成",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/index/refresh", response_model=IndexRefreshResponse)
async def refresh_index():
    """
    增量索引：仅处理有变更的文档（新增/修改/删除），避免全量重建

    供外部（Obsidian 插件、脚本等）手动触发增量同步。
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")

    try:
        result = _pipeline.index_incremental()

        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])

        return IndexRefreshResponse(
            success=True,
            added=len(result.get("added", [])),
            updated=len(result.get("updated", [])),
            removed=len(result.get("removed", [])),
            unchanged=result.get("unchanged", 0),
            skipped=len(result.get("skipped", [])),
            message="增量索引完成",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))