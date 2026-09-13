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
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

from src.pipeline.indexer import Indexer
from src.logging_setup import get_logger

logger = get_logger(__name__)


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
        # 进程内互斥锁：sync 有多个并发入口（watcher 回调线程、/v1/index/refresh、
        # IngestQueue worker），不加锁会出现 manifest 读-改-写竞争与向量库写交错
        self._lock = threading.Lock()
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
                logger.warning("manifest 版本不匹配，重置清单")
                self.manifest = {"version": self.MANIFEST_VERSION, "docs": {}}
        except Exception as e:
            logger.warning("manifest 读取失败，重置清单: %s", e)
            self.manifest = {"version": self.MANIFEST_VERSION, "docs": {}}

    def save_manifest(self):
        """保存变更清单（原子写：先写临时文件再替换，中断不会损坏清单）"""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.manifest_path.with_suffix(".json.tmp")
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, ensure_ascii=False, indent=2)
        temp.replace(self.manifest_path)

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
                        # 统一 resolve，保证 doc_id 与 DocumentLoader 入库路径一致
                        # （否则符号链接/相对路径会导致 doc_id 与库内元数据对不上）
                        p = p.resolve()
                        files[str(p)] = p
        return files

    # ================================================================
    # 核心：三态同步
    # ================================================================

    def sync(
            self,
            rebuild: bool = False,
            cancelled: Optional[Callable[[], bool]] = None,
            progress_cb: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        执行一次增量同步（进程内互斥）

        Args:
            rebuild: 为 True 时清空向量库与清单后全量重建
            cancelled: 取消检查回调（供 IngestQueue 长任务取消）；在每个文档
                处理边界轮询，返回 True 时停止后续处理（已处理部分保留，
                增量同步天然幂等，下次同步续跑）
            progress_cb: 进度回调 (stage, current, total, note)（供作业进度条）

        Returns:
            Dict: 统计信息 {added, updated, removed, unchanged, skipped, cancelled}
        """
        with self._lock:
            return self._sync_locked(rebuild=rebuild, cancelled=cancelled, progress_cb=progress_cb)

    def _sync_locked(
            self,
            rebuild: bool = False,
            cancelled: Optional[Callable[[], bool]] = None,
            progress_cb: Optional[Callable[[str, int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """实际执行同步（须持有 self._lock 调用）"""
        from src.document.loader import DocumentLoader

        def cb(stage: str, cur: int, tot: int, note: str = ""):
            if progress_cb is not None:
                progress_cb(stage, cur, tot, note)

        self.load_manifest()

        if rebuild:
            logger.info("增量同步：清空向量库与清单，执行全量重建")
            self.indexer.vector_store.clear()
            self.manifest = {"version": self.MANIFEST_VERSION, "docs": {}}
            cb("清空", 1, 1)

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
        total_files = len(current_files)
        done = 0
        if total_files:
            cb("增量同步", 0, total_files)
        for path_str, path in current_files.items():
            done += 1
            cb("增量同步", done, total_files, path.name)
            # 取消检查（文档粒度）：已处理部分保留，未处理文件下次同步续跑
            if cancelled is not None and cancelled():
                stats["cancelled"] = True
                break

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
                    # —— 变更：先加载+分块+嵌入，成功后才删旧块再入新块 ——
                    # （overwrite=True 让 index_single 内部完成「先嵌入后删旧」，
                    #   避免旧块先删后 index_single 失败导致的永久丢失）
                    doc_id = DocumentLoader.doc_id_for(path_str)
                    result = self.indexer.index_single(path_str, overwrite=True)
                    if result.get("success") and not result.get("skipped"):
                        docs_map[path_str] = self._entry_for(path, doc_id, st)
                        stats["updated"].append(path.name)
                    else:
                        stats["skipped"].append(path.name)

        # ---------- 阶段二：处理已消失的文件（已取消时跳过，下次同步续跑） ----------
        if not stats.get("cancelled"):
            removed_pending = [p for p in docs_map.keys() if p not in current_files]
            for idx, path_str in enumerate(removed_pending, 1):
                cb("删除清理", idx, len(removed_pending) or 1, Path(path_str).name)
                entry = docs_map.pop(path_str)
                doc_id = entry.get("doc_id")
                if doc_id:
                    store.delete_by_doc_id(doc_id)
                stats["removed"].append(Path(path_str).name)

        self.save_manifest()
        cb("完成", 1, 1)

        if stats["added"] or stats["updated"] or stats["removed"]:
            logger.info(
                "增量同步完成: 新增 %d | 更新 %d | 删除 %d | 未变 %d",
                len(stats["added"]), len(stats["updated"]),
                len(stats["removed"]), stats["unchanged"],
            )

        return stats