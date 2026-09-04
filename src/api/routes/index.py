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
    JobRequest,
    JobResponse,
    JobListResponse,
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


def _job_to_response(job) -> JobResponse:
    """把队列任务对象转为 API 响应模型"""
    return JobResponse(
        id=job.id,
        kind=job.kind,
        status=job.status,
        progress=job.progress,
        error=job.error,
        result=job.result,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _get_queue():
    """获取后台摄入队列（pipeline 上挂载）"""
    if _pipeline is None or not hasattr(_pipeline, "ingest_queue") or _pipeline.ingest_queue is None:
        raise HTTPException(status_code=503, detail="后台摄入队列未初始化")
    return _pipeline.ingest_queue


@router.post("/index/async", response_model=JobResponse)
async def submit_index_job(request: JobRequest):
    """
    后台摄入任务（异步）：full | incremental | url

    单 worker 串行执行，任务状态磁盘持久化（重启可恢复），
    进度与结果通过 GET /v1/index/jobs 查询。
    """
    if request.kind not in ("full", "incremental", "url"):
        raise HTTPException(status_code=400, detail=f"不支持的任务类型: {request.kind}")
    if request.kind == "url" and not request.url:
        raise HTTPException(status_code=400, detail="url 任务必须提供 url")

    queue = _get_queue()
    params = {}
    if request.kind == "full":
        params["rebuild"] = request.rebuild
    if request.kind == "url":
        params["url"] = request.url
        params["timeout"] = request.timeout or 30.0

    job = queue.submit(request.kind, params)
    return _job_to_response(job)


@router.get("/index/jobs", response_model=JobListResponse)
async def list_index_jobs(limit: int = 50):
    """列出摄入任务（新 -> 旧）"""
    queue = _get_queue()
    jobs = queue.list(limit=max(1, min(limit, 200)))
    return JobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        total=len(jobs),
    )


@router.get("/index/jobs/{job_id}", response_model=JobResponse)
async def get_index_job(job_id: str):
    """查询单个摄入任务（进度/结果）"""
    queue = _get_queue()
    job = queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    return _job_to_response(job)


@router.post("/index/jobs/{job_id}/cancel")
async def cancel_index_job(job_id: str):
    """取消摄入任务（queued 立即取消；running 标记取消）"""
    queue = _get_queue()
    ok = queue.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    return {"ok": True, "job_id": job_id}


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