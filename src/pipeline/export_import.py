"""
项目导出 / 导入（决策：ZIP 完整归档）

导出内容：config.yaml（配置摘要）+ meta.json（统计）+ documents.json（文档清单）
+ vector_store/（向量库目录）+ index_manifest.json（变更清单）
+ conversations/（会话）+ attachments/（多模态附件）

导入：校验 → 解包 → 按 mode 合并（merge=缺省合并 / replace=清空重建）
"""

import json
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _add_dir(zf: zipfile.ZipFile, src: Path, prefix: str):
    if not src.exists():
        return
    for p in sorted(src.rglob("*")):
        if p.is_file():
            zf.write(p, f"{prefix}{p.relative_to(src)}")


def export_archive(
        dest: str,
        vector_store_dir: str,
        manifest_path: str,
        documents: List[Dict[str, Any]],
        conversations_dir: str = "./data/conversations",
        attachments_dir: str = "./data/attachments",
        config_path: str = "config/settings.yaml",
) -> Path:
    """
    导出完整归档

    Args:
        dest: 目标 zip 路径
        vector_store_dir: 向量库持久化目录
        manifest_path: 变更清单路径
        documents: 文档清单 [{file_name, file_path, format}]
        conversations_dir / attachments_dir: 会话与附件目录
        config_path: 配置文件路径

    Returns:
        Path: 归档文件路径
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    meta = {
        "tool": "rag-service",
        "version": "1.0",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "document_count": len(documents),
    }

    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
        cfg = Path(config_path)
        if cfg.exists():
            zf.write(cfg, "config.yaml")
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        zf.writestr("documents.json", json.dumps(documents, ensure_ascii=False, indent=2))
        mf = Path(manifest_path)
        if mf.exists():
            zf.write(mf, "index_manifest.json")
        _add_dir(zf, Path(vector_store_dir), "vector_store/")
        _add_dir(zf, Path(conversations_dir), "conversations/")
        _add_dir(zf, Path(attachments_dir), "attachments/")

    return dest_path


def import_archive(
        src: str,
        vector_store_dir: str,
        manifest_path: str,
        conversations_dir: str = "./data/conversations",
        attachments_dir: str = "./data/attachments",
        mode: str = "merge",
) -> Dict[str, Any]:
    """
    导入归档

    Args:
        src: zip 归档路径
        vector_store_dir: 目标向量库目录（copy 目标）
        manifest_path: 目标变更清单路径
        conversations_dir / attachments_dir: 会话与附件目标目录
        mode: merge 合并（缺省补全，不删除现有）/ replace 重建（先清空向量库）

    Returns:
        Dict: 导入统计
    """
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"归档不存在: {src_path}")

    stats: Dict[str, Any] = {"mode": mode, "documents": 0, "files": 0}

    with tempfile.TemporaryDirectory(prefix="rag_import_") as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(src_path) as zf:
            zf.extractall(tmp_dir)

        # 校验
        if not (tmp_dir / "meta.json").exists():
            raise ValueError("无效的归档：缺少 meta.json")

        # 1. 向量库
        src_vs = tmp_dir / "vector_store"
        dst_vs = Path(vector_store_dir)
        if src_vs.exists():
            if mode == "replace" and dst_vs.exists():
                shutil.rmtree(dst_vs)
            dst_vs.mkdir(parents=True, exist_ok=True)
            _merge_copy(src_vs, dst_vs)
            stats["files"] += sum(1 for _ in dst_vs.rglob("*") if _.is_file())

        # 2. 变更清单
        src_mf = tmp_dir / "index_manifest.json"
        if src_mf.exists():
            dst_mf = Path(manifest_path)
            dst_mf.parent.mkdir(parents=True, exist_ok=True)
            if mode == "replace" or not dst_mf.exists():
                shutil.copy2(src_mf, dst_mf)

        # 3. 会话与附件（总是合并）
        for prefix, target in (
            ("conversations", Path(conversations_dir)),
            ("attachments", Path(attachments_dir)),
        ):
            src_dir = tmp_dir / prefix
            if src_dir.exists():
                _merge_copy(src_dir, target)

        # 4. 文档清单（仅统计）
        doc_file = tmp_dir / "documents.json"
        if doc_file.exists():
            stats["documents"] = len(json.loads(doc_file.read_text(encoding="utf-8")))

    return stats


def _merge_copy(src: Path, dst: Path):
    """递归合并复制（目标已存在的同名文件覆盖）"""
    dst.mkdir(parents=True, exist_ok=True)
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)