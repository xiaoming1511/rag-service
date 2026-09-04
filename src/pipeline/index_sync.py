"""
增量索引同步器

基于变更清单（manifest）+ 三态同步算法，实现免全量重建的增量索引：

- 新增文档：分块嵌入后入库，写入清单
- 变更文档：先按 doc_id 删除旧块，再重新入库（内容 MD5 相同则跳过）
- 删除文档：按 doc_id 清理全部块，移除清单记录

变更检测采用"两段式"（决策 D1）：
先比较 mtime+size 快速筛选，只有时间戳/大小不一致时才计算内容 MD5 精确判定。
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Any, List, Optional

from src.pipeline.indexer import Indexer


class IndexSync:
    """增量索引同步器"""

    MANIFEST_VERSION = 1

    def __init__(
            self,
            indexer: Indexer,
            manifest_path: Optional[str] = None,
    ):
        """
        初始化增量同步器

        Args:
            indexer: 索引器（含 loader/chunker/embedder/vector_store）
            manifest_path: 变更清单文件路径；
                默认放在向量库持久化目录下（index_manifest.json），
                使清单与向量库一一对应，互不干扰
        """
        self.indexer = indexer
        if manifest_path is None:
            persist_dir = getattr(indexer.vector_store, "persist_directory", "./data")
            manifest_path = str(Path(persist_dir) / "index_manifest.json")
        self.manifest_path = Path(manifest_path)
        self.manifest: Dict[str, Any] = {
            "version": self.MANIFEST_VERSION,
            "docs": {},
        }

    # ================================================================
    # 清单读写
    # ================================================================

    def load_manifest(self):
        """加载变更清单；不存在或版本不符时重置为空清单"""
        if not self.manifest_path.exists():
            self.manifest = {"version": self.MANIFEST_VERSION, "docs": {}}
            return
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                self.manifest = json.load(f)
            if self.manifest.get("version") != self.MANIFEST_VERSION:
                print("⚠️ manifest 版本不匹配，重置清单")
                self.manifest = {"version": self.MANIFEST_VERSION, "docs": {}}
        except Exception as e:
            print(f"⚠️ manifest 读取失败，重置清单: {e}")
            self.manifest = {"version": self.MANIFEST_VERSION, "docs": {}}

    def save_manifest(self):
        """保存变更清单"""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, ensure_ascii=False, indent=2)

    # ================================================================
    # 文件指纹
    # ================================================================

    @staticmethod
    def file_md5(path: Path) -> str:
        """计算文件内容 MD5（分块读取，避免大文件占用内存）"""
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _entry_for(path: Path, doc_id: str, st: os.stat_result) -> Dict[str, Any]:
        """构造清单条目"""
        return {
            "doc_id": doc_id,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "hash": IndexSync.file_md5(path),
        }

    # ================================================================
    # 文件扫描
    # ================================================================

    def _scan_files(self) -> Dict[str, Path]:
        """
        扫描所有源目录，返回支持的文档文件映射（绝对路径 str -> Path）

        目录过滤规则与 DocumentLoader._walk_files 保持一致：
        跳过隐藏目录、wiki 与 node_modules。
        """
        files: Dict[str, Path] = {}
        extensions = set(self.indexer.loader.extensions)

        for source_dir in self.indexer.loader.source_dirs:
            if not source_dir.exists():
                continue
            for root, dirs, filenames in os.walk(source_dir):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'wiki' and d != 'node_modules']
                for filename in filenames:
                    p = Path(root) / filename
                    if p.suffix.lower() in extensions:
                        files[str(p)] = p
        return files

    # ================================================================
    # 核心：三态同步
    # ================================================================

    def sync(self, rebuild: bool = False) -> Dict[str, Any]:
        """
        执行一次增量同步

        Args:
            rebuild: 为 True 时清空向量库与清单后全量重建

        Returns:
            Dict: 统计信息 {added, updated, removed, unchanged, skipped}
        """
        from src.document.loader import DocumentLoader

        self.load_manifest()

        if rebuild:
            print("🧹 增量同步：清空向量库与清单，执行全量重建...")
            self.indexer.vector_store.clear()
            self.manifest = {"version": self.MANIFEST_VERSION, "docs": {}}

        docs_map = self.manifest["docs"]
        current_files = self._scan_files()
        store = self.indexer.vector_store

        stats: Dict[str, Any] = {
            "added": [],
            "updated": [],
            "removed": [],
            "skipped": [],
            "unchanged": 0,
        }

        # ---------- 阶段一：处理当前存在的文件 ----------
        for path_str, path in current_files.items():
            try:
                st = path.stat()
            except OSError:
                continue  # 读取状态失败（如文件被占用），跳过本次

            entry = docs_map.get(path_str)

            if entry is None:
                # —— 新增 ——
                doc_id = DocumentLoader.doc_id_for(path_str)
                result = self.indexer.index_single(path_str)
                if result.get("success"):
                    # 登记清单（含"已存在跳过"的情况：把历史向量纳入清单管理，避免重复嵌入）
                    docs_map[path_str] = self._entry_for(path, doc_id, st)
                    if result.get("skipped"):
                        stats["skipped"].append(path.name)
                    else:
                        stats["added"].append(path.name)
                else:
                    stats["skipped"].append(path.name)
            elif entry.get("mtime") == st.st_mtime and entry.get("size") == st.st_size:
                # —— 未变（快路径：时间戳 + 大小一致） ——
                stats["unchanged"] += 1
            else:
                # —— 疑似变更：计算内容 MD5 精确判定 ——
                try:
                    cur_hash = self.file_md5(path)
                except OSError:
                    continue

                if entry.get("hash") == cur_hash:
                    # 内容未变（git checkout 恢复等场景），仅刷新时间戳
                    entry["mtime"] = st.st_mtime
                    entry["size"] = st.st_size
                    stats["unchanged"] += 1
                else:
                    # —— 变更：先删旧块，再重新入库 ——
                    old_doc_id = entry.get("doc_id")
                    if old_doc_id:
                        store.delete_by_doc_id(old_doc_id)
                    doc_id = DocumentLoader.doc_id_for(path_str)
                    result = self.indexer.index_single(path_str)
                    if result.get("success"):
                        docs_map[path_str] = self._entry_for(path, doc_id, st)
                        if result.get("skipped"):
                            stats["skipped"].append(path.name)
                        else:
                            stats["updated"].append(path.name)
                    else:
                        stats["skipped"].append(path.name)

        # ---------- 阶段二：处理已消失的文件 ----------
        for path_str in list(docs_map.keys()):
            if path_str not in current_files:
                entry = docs_map.pop(path_str)
                doc_id = entry.get("doc_id")
                if doc_id:
                    store.delete_by_doc_id(doc_id)
                stats["removed"].append(Path(path_str).name)

        self.save_manifest()

        if stats["added"] or stats["updated"] or stats["removed"]:
            print(
                f"♻️ 增量同步完成: 新增 {len(stats['added'])} | "
                f"更新 {len(stats['updated'])} | 删除 {len(stats['removed'])} | "
                f"未变 {stats['unchanged']}"
            )

        return stats