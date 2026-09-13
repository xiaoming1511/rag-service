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


# 解包防护（防解压炸弹）：按 zip 清单头声明的解压总量/条目数设上限，
# 超限直接拒绝解包——清单头读取是 O(entries) 且不解压，先查再解不白耗磁盘。
# 导出侧的正常归档（向量库 + 会话 + 附件）远低于该上限；确有超大归档需求时
# 再调高常量。
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_ARCHIVE_ENTRIES = 200_000


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
    # mode 是行为开关（replace 会先清空向量库），非法值必须报错而不是
    # 静默退化成 merge——否则用户把 "replace" 拼错会以为已重建知识库
    if mode not in ("merge", "replace"):
        raise ValueError(f"mode 仅支持 merge / replace，收到: {mode!r}")

    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"归档不存在: {src_path}")

    # files 口径 = 本次导入实际复制的文件数（向量库 + 会话 + 附件），
    # 不是目标目录的全量文件数（R9-3 修正：旧实现在复制后对 dst 全量 rglob，
    # 把目标库已有文件也计入，导入量被放大）
    stats: Dict[str, Any] = {"mode": mode, "documents": 0, "files": 0}

    with tempfile.TemporaryDirectory(prefix="rag_import_") as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(src_path) as zf:
            # 先按清单校验再解包：无效归档不必先完整落盘（白耗磁盘），
            # 也避免把明显不是本工具产物的包解开
            if "meta.json" not in zf.namelist():
                raise ValueError("无效的归档：缺少 meta.json")
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(
                    f"归档条目数 {len(infos)} 超过上限 {MAX_ARCHIVE_ENTRIES}，已拒绝解包"
                )
            total = sum(i.file_size for i in infos)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"归档解压总量 {total} 字节超过上限 {MAX_UNCOMPRESSED_BYTES} 字节，已拒绝解包"
                )
            zf.extractall(tmp_dir)

        # 校验（双重保险）
        if not (tmp_dir / "meta.json").exists():
            raise ValueError("无效的归档：缺少 meta.json")

        # 1. 向量库
        src_vs = tmp_dir / "vector_store"
        dst_vs = Path(vector_store_dir)
        if src_vs.exists():
            if mode == "replace" and dst_vs.exists():
                shutil.rmtree(dst_vs)
            dst_vs.mkdir(parents=True, exist_ok=True)
            stats["files"] += _merge_copy(src_vs, dst_vs)

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
                stats["files"] += _merge_copy(src_dir, target)

        # 4. 文档清单（仅统计）
        doc_file = tmp_dir / "documents.json"
        if doc_file.exists():
            stats["documents"] = len(json.loads(doc_file.read_text(encoding="utf-8")))

    return stats


def _merge_copy(src: Path, dst: Path) -> int:
    """递归合并复制（目标已存在的同名文件覆盖）；返回实际复制的文件数"""
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            copied += 1
    return copied