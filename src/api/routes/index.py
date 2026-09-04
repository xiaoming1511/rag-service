"""
索引路由
"""

from fastapi import APIRouter, HTTPException

from src.api.schemas import IndexRequest, IndexResponse

router = APIRouter(prefix="/v1", tags=["index"])

# 全局 pipeline 实例
_pipeline = None


def set_pipeline(pipeline):
    global _pipeline
    _pipeline = pipeline


@router.post("/index", response_model=IndexResponse)
async def index_documents(request: IndexRequest):
    """
    索引文档
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