"""
项目导出 / 导入路由
"""

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

router = APIRouter(prefix="/v1", tags=["archive"])

_pipeline = None


def set_pipeline(pipeline):
    global _pipeline
    _pipeline = pipeline


def _paths():
    """从 pipeline 推导归档涉及的路径"""
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline 未初始化")
    vs = getattr(_pipeline, "vector_store", None)
    persist = getattr(vs, "persist_directory", "./data/chroma_db") if vs else "./data/chroma_db"
    manifest = str(Path(persist) / "index_manifest.json")
    return persist, manifest


def _scan_documents() -> list:
    """扫描源目录得到文档清单（供导出）"""
    from pathlib import Path
    docs = []
    loader = getattr(getattr(_pipeline, "indexer", None), "loader", None)
    if loader is None:
        return docs
    extensions = set(loader.extensions)
    for src in getattr(loader, "source_dirs", []):
        for p in Path(src).rglob("*"):
            if p.is_file() and p.suffix.lower() in extensions:
                docs.append({
                    "file_name": p.name,
                    "file_path": str(p),
                    "format": p.suffix.lower().lstrip("."),
                })
    return docs


@router.get("/export")
async def export_archive():
    """
    导出完整归档（ZIP）：配置 + 文档清单 + 向量库 + 变更清单 + 会话 + 附件
    归档落盘到 data/exports/，返回文件下载
    """
    from src.pipeline.export_import import export_archive

    persist, manifest = _paths()
    out_dir = Path("./data/exports")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"rag-export-{int(time.time())}.zip"
    try:
        export_archive(
            dest=str(dest),
            vector_store_dir=persist,
            manifest_path=manifest,
            documents=_scan_documents(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出失败: {e}")
    return FileResponse(path=dest, media_type="application/zip", filename=dest.name)


@router.post("/import")
async def import_archive_upload(file: UploadFile, mode: str = "merge"):
    """
    导入归档（ZIP 上传）：校验 → 解包 → 合并（merge）或重建（replace）
    upload 完成后建议重启服务以重新加载配置/集合。
    """
    from src.pipeline.export_import import import_archive

    if mode not in ("merge", "replace"):
        raise HTTPException(status_code=400, detail="mode 仅支持 merge / replace")

    persist, manifest = _paths()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")

    tmp_path = Path("./data/tmp") / f"upload_{int(time.time())}.zip"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_bytes(data)
    try:
        stats = import_archive(
            src=str(tmp_path),
            vector_store_dir=persist,
            manifest_path=manifest,
            mode=mode,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"导入失败: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"ok": True, "stats": stats, "note": "请重启服务使配置/集合生效"}