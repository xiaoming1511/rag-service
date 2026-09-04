"""
问答路由
提供同步问答与流式问答（SSE）两类接口
"""

from typing import Dict, Any
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from src.api.schemas import QueryRequest, QueryResponse, SourceInfo

router = APIRouter(prefix="/v1", tags=["query"])

# 全局 pipeline 实例（由 app.py 注入）
_pipeline = None


def set_pipeline(pipeline):
    """注入 pipeline 实例"""
    global _pipeline
    _pipeline = pipeline


@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest) -> Dict[str, Any]:
    """
    同步问答接口
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")

    try:
        # 同步查询（阻塞调用同步 oMLX 客户端；
        # 如需完全异步，可改为 await _pipeline.query_async(...)）
        result = _pipeline.query(
            question=request.question,
            top_k=request.top_k,
            use_rerank=request.use_rerank,
            history=request.history,
        )

        return {
            "answer": result["answer"],
            "sources": [
                SourceInfo(
                    file_name=s["file_name"],
                    file_path=s.get("file_path") or None,
                    heading=s.get("heading") or None,
                    content=s["content"],
                    score=s["score"],
                )
                for s in result.get("sources", [])
            ],
            "total_results": result.get("total_results", 0),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/query/stream")
async def query_stream(request: QueryRequest):
    """
    流式问答接口（SSE - 结构化 JSON）

    使用 pipeline.query_stream_async 异步流式生成：
    - 检索阶段在线程池中执行，不阻塞事件循环；
    - 生成阶段通过 AsyncOpenAI 异步流式输出，
      规避 Python 3.13 + httpx 的流关闭兼容性问题。
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")

    async def generate():
        try:
            async for chunk in _pipeline.query_stream_async(
                question=request.question,
                top_k=request.top_k,
                use_rerank=request.use_rerank,
                history=request.history,
            ):
                yield chunk
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'data': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )