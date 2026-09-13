"""
状态路由
"""

from fastapi import APIRouter, HTTPException

from src.api.schemas import StatusResponse
from src.api.metrics import snapshot as metrics_snapshot
from src.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["status"])

_pipeline = None


def set_pipeline(pipeline):
    global _pipeline
    _pipeline = pipeline


@router.get("/status", response_model=StatusResponse)
def get_status():
    """
    获取系统状态

    刻意声明为 def 而非 async def：get_stats() / queue.list() 是阻塞调用
    （向量库计数 + sqlite 查询），写在 async def 里会冻结整个事件循环，
    连 /v1/health 与插件的 30s 状态轮询都会一起卡住。
    def 端点由 Starlette 自动放入线程池执行。
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")

    try:
        stats = _pipeline.get_stats()

        # 当前摄入任务（运行中/排队中优先，否则最近一个）：供进度条轮询
        index_job = None
        queue = getattr(_pipeline, "ingest_queue", None)
        if queue is not None:
            jobs = queue.list(limit=10)
            pick = next((j for j in jobs if j.status in ("running", "queued")), jobs[0] if jobs else None)
            if pick is not None:
                index_job = {
                    "id": pick.id,
                    "kind": pick.kind,
                    "status": pick.status,
                    "progress": pick.progress,
                    "progress_data": pick.progress_data,
                }

        return StatusResponse(
            status="running",
            vector_count=stats.get("vector_store", {}).get("count", 0),
            collection_name=stats.get("vector_store", {}).get("collection_name", "unknown"),
            config={},
            index_job=index_job,
            metrics=metrics_snapshot(),
            rerank_cache=stats.get("retriever", {}).get("rerank_cache"),
        )
    except Exception as e:
        logger.exception("状态查询失败: %s", e)
        raise HTTPException(status_code=500, detail="状态查询失败，请查看服务日志")


@router.get("/health")
def health_check():
    """健康检查（def：与 /status 同为同步取 pipeline 状态）"""
    if _pipeline is None:
        return {"status": "uninitialized"}
    return {"status": "healthy"}