"""
第八轮（Round 8）扫描修复回归：队列端点阻塞 + zip 解包上限

1. R8-2  /v1/index 队列四端点（submit/list/get/cancel）由 async def 改 def——
         同步 sqlite/文件 IO 直跑事件循环会冻结整个进程；
         守卫测试并入 test_round5_contracts 的 _HEAVY_ENDPOINTS 参数化清单。
2. R8-3  import_archive 解包上限：解压总量 / 条目数超限直接拒绝（防解压炸弹）。
"""

import json
import zipfile

import pytest

import src.pipeline.export_import as ei
from src.pipeline.export_import import import_archive


def _write_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path


class TestImportArchiveSizeGuard:
    def test_oversized_total_rejected(self, tmp_path, monkeypatch):
        """清单头声明的解压总量超限 → 解包前拒绝（不白耗磁盘）"""
        monkeypatch.setattr(ei, "MAX_UNCOMPRESSED_BYTES", 10)
        z = _write_zip(tmp_path / "big.zip", {
            "meta.json": "{}",
            "vector_store/blob.bin": "x" * 100,
        })
        with pytest.raises(ValueError, match="超过上限"):
            import_archive(
                src=str(z),
                vector_store_dir=str(tmp_path / "vs"),
                manifest_path=str(tmp_path / "mf.json"),
                mode="merge",
            )
        assert not (tmp_path / "vs").exists(), "被拒归档不应产生向量库目录"

    def test_too_many_entries_rejected(self, tmp_path, monkeypatch):
        """条目数超限 → 解包前拒绝"""
        monkeypatch.setattr(ei, "MAX_ARCHIVE_ENTRIES", 2)
        z = _write_zip(tmp_path / "many.zip", {
            "meta.json": "{}",
            "f1.txt": "1",
            "f2.txt": "2",
            "f3.txt": "3",
        })
        with pytest.raises(ValueError, match="条目数"):
            import_archive(
                src=str(z),
                vector_store_dir=str(tmp_path / "vs"),
                manifest_path=str(tmp_path / "mf.json"),
                mode="merge",
            )

    def test_normal_archive_still_imports(self, tmp_path):
        """正向路径不能被守卫改坏"""
        z = _write_zip(tmp_path / "ok.zip", {
            "meta.json": json.dumps({"tool": "rag-service"}),
            "vector_store/chroma.sqlite3": "x",
        })
        stats = import_archive(
            src=str(z),
            vector_store_dir=str(tmp_path / "vs"),
            manifest_path=str(tmp_path / "mf.json"),
            mode="merge",
        )
        assert stats["mode"] == "merge"
        assert (tmp_path / "vs" / "chroma.sqlite3").exists()
