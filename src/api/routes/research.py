"""
深度研究路由
"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from src.api.schemas import ResearchRequest, ResearchResponse, SourceInfo

router = APIRouter(prefix="/v1", tags=["research"])

_pipeline = None


def set_pipeline(pipeline):
    """注入 pipeline 实例"""
    global _pipeline
    _pipeline = pipeline


@router.post("/research", response_model=ResearchResponse)
async def research(request: ResearchRequest) -> Dict[str, Any]:
    """
    Deep Research 简版：问题 → 子查询 → 并行检索 → 汇总报告（一层展开）
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")

    try:
        result = _pipeline.research(
            question=request.question,
            sub_queries=request.sub_queries,
            max_rounds=request.max_rounds,
        )

        return {
            "report": result["report"],
            "sub_queries": result["sub_queries"],
            "sources": [
                SourceInfo(
                    file_name=s["file_name"],
                    file_path=s.get("file_path") or None,
                    heading=s.get("heading") or None,
                    line_start=s.get("line_start"),
                    line_end=s.get("line_end"),
                    images=s.get("images"),
                    content="",
                    score=s["score"],
                )
                for s in result.get("sources", [])
            ],
            "total_results": result.get("total_results", 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))