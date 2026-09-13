"""
项目导出 / 导入路由
"""

import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

from src.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["archive"])

# 数据/向量库导出单文件体积上限（导入归档用于本地备份，上限足够宽松）
_MAX_UPLOAD = 200 * 1024 * 1024  # 200MB
# 保留最近 N 份导出（避免 data/exports 无限增长占满磁盘）
_MAX_EXPORTS = 5

_pipeline = None


def set_pipeline(pipeline):
    global _pipeline
    _pipeline = pipeline


def _prune_exports(out_dir: Path, keep: int = _MAX_EXPORTS) -> None:
    """只保留最近 keep 份导出 ZIP，其余删除（失败不影响本次导出）"""
    try:
        files = sorted(
            out_dir.glob("rag-export-*.zip"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
            reverse=True,
        )
        for old in files[max(1, keep):]:
            try:
                old.unlink()
                logger.info("已清理旧导出: %s", old)
            except OSError as e:
                logger.warning("清理旧导出失败 %s: %s", old, e)
    except OSError as e:
        logger.warning("导出目录清理跳过: %s", e)


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
def export_archive():
    """
    导出完整归档（ZIP）：配置 + 文档清单 + 向量库 + 变更清单 + 会话 + 附件
    归档落盘到 data/exports/，返回文件下载

    声明为 def（非 async def）：打包向量库/会话目录是磁盘密集型同步工作，
    在 async def 里会冻结事件循环；def 端点由 Starlette 放入线程池执行。
    """
    from src.pipeline.export_import import export_archive

    persist, manifest = _paths()
    out_dir = Path("./data/exports")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 文件名带 uuid 短片段：int(time.time()) 精度到秒，同一秒并发导出
    # 会写到同一个文件，后写者覆盖前写者（正在被 FileResponse 读的那份）
    dest = out_dir / f"rag-export-{int(time.time())}-{uuid.uuid4().hex[:6]}.zip"
    try:
        export_archive(
            dest=str(dest),
            vector_store_dir=persist,
            manifest_path=manifest,
            documents=_scan_documents(),
        )
    except Exception as e:
        logger.exception("导出失败: %s", e)
        raise HTTPException(status_code=500, detail="导出失败，请查看服务日志")
    _prune_exports(out_dir)
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

    import asyncio

    # 大小护栏：先用 UploadFile.size（Starlette 解析 multipart 时已知）判断，
    # 再读入内存——否则 200MB 上限是在「整包已经读进内存」之后才生效的
    declared = getattr(file, "size", None)
    if declared is not None and declared > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail=f"上传文件过大（>{_MAX_UPLOAD // 1024 // 1024}MB）")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail=f"上传文件过大（>{_MAX_UPLOAD // 1024 // 1024}MB）")

    # 用 uuid 避免同一秒内并发导入互相覆盖临时文件
    tmp_path = Path("./data/tmp") / f"upload_{uuid.uuid4().hex}.zip"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    # 落盘（最大 200MB）与「解包 + 合并复制」都是阻塞 I/O：
    # 本端点因 `await file.read()` 必须是 async，故显式放入线程池执行
    await asyncio.to_thread(tmp_path.write_bytes, data)
    try:
        stats = await asyncio.to_thread(
            import_archive,
            src=str(tmp_path),
            vector_store_dir=persist,
            manifest_path=manifest,
            mode=mode,
        )
    except Exception as e:
        logger.exception("导入失败: %s", e)
        raise HTTPException(status_code=400, detail="导入失败（归档格式或内容无效）")
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"ok": True, "stats": stats, "note": "请重启服务使配置/集合生效"}