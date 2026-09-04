"""
知识层与图谱路由
"""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/v1", tags=["wiki"])

_pipeline = None


def set_pipeline(pipeline):
    """注入 pipeline 实例"""
    global _pipeline
    _pipeline = pipeline


@router.post("/wiki/build")
async def build_wiki(force: bool = False) -> Dict[str, Any]:
    """
    手动触发知识层生成：来源摘要页 / 概念页 / 实体页 + 互链 + 图谱重建

    Args:
        force: True 时强制重建所有页面
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")
    try:
        result = _pipeline.build_wiki(force=force)
        if result.get("error"):
            raise HTTPException(status_code=500, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/graph")
async def get_graph() -> Dict[str, Any]:
    """
    知识图谱：节点=知识层页面（sources/concepts/entities），边=[[互链]]/引用
    """
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")
    return _pipeline.get_graph()