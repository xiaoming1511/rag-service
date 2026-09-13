"""
深度研究路由
"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from src.api.schemas import ResearchRequest, ResearchResponse, SourceInfo
from src.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["research"])

_pipeline = None


def set_pipeline(pipeline):
    """注入 pipeline 实例"""
    global _pipeline
    _pipeline = pipeline


@router.post("/research", response_model=ResearchResponse)
def research(request: ResearchRequest) -> Dict[str, Any]:
    """
    Deep Research 简版：问题 → 子查询 → 并行检索 → 汇总报告（一轮展开 + 追问递归）

    声明为 def（非 async def）：research() 内部是多次阻塞 LLM 调用
    （子查询生成 / 汇总生成 / 追问生成）+ 线程池检索，耗时数十秒。
    写在 async def 里会冻结整个事件循环——实测 /v1/health 会被拖到
    研究结束才返回（同 /v1/index、/v1/query 的处理方式）。
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
            # 实际执行轮次（受 max_rounds 上限与"新信息增益提前停止"影响，
            # 可能与请求值不同），此前已算出但被路由丢弃
            "rounds": result.get("rounds", 1),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("深度研究失败: %s", e)
        raise HTTPException(status_code=500, detail="深度研究处理失败，请查看服务日志")