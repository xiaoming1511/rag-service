"""
状态路由
"""

from fastapi import APIRouter, HTTPException

from src.api.schemas import StatusResponse

router = APIRouter(prefix="/v1", tags=["status"])

_pipeline = None


def set_pipeline(pipeline):
    global _pipeline
    _pipeline = pipeline


@router.get("/status", response_model=StatusResponse)
async def get_status():
    """
    获取系统状态
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")

    try:
        stats = _pipeline.get_stats()
        return StatusResponse(
            status="running",
            vector_count=stats.get("vector_store", {}).get("count", 0),
            collection_name=stats.get("vector_store", {}).get("collection_name", "unknown"),
            config={},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """健康检查"""
    if _pipeline is None:
        return {"status": "uninitialized"}
    return {"status": "healthy"}