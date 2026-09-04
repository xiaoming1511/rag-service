"""
项目导出/导入测试（离线：临时向量库 + 往返验证）
"""

import sys
import zipfile
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.export_import import export_archive, import_archive
from src.vector_store.chroma_store import ChromaStore


def _make_store(tmp_path):
    """造一个小向量库"""
    store = ChromaStore(
        collection_name="exp",
        persist_directory=str(tmp_path / "vs"),
        embedding_dimension=1024,
    )
    store.add(
        ids=["a1"], embeddings=[[0.1] * 1024],
        documents=["导出测试"], metadatas=[{"file_name": "x.md"}],
    )
    return store


def test_export_import_roundtrip(tmp_path):
    """导出 zip → 导入到另一目录（merge）→ 数据可读"""
    store = _make_store(tmp_path)
    vs_dir = tmp_path / "vs"

    # 造变更清单与会话
    mf = vs_dir / "index_manifest.json"
    mf.write_text('{"version": 1, "docs": {}}', encoding="utf-8")
    conv = tmp_path / "conversations"
    conv.mkdir()
    (conv / "abc.json").write_text('{"id":"abc","title":"对话","messages":[]}', encoding="utf-8")

    zip_path = tmp_path / "out" / "export.zip"
    docs = [{"file_name": "x.md", "file_path": str(tmp_path / "x.md"), "format": "md"}]

    dest = export_archive(
        dest=str(zip_path),
        vector_store_dir=str(vs_dir),
        manifest_path=str(mf),
        documents=docs,
        conversations_dir=str(conv),
        attachments_dir=str(tmp_path / "attachments"),
        config_path=str(tmp_path / "cfg.yaml"),
    )
    assert dest.exists() and zipfile.is_zipfile(dest)

    # 导入到新目录
    new_vs = tmp_path / "vs2"
    new_mf = new_vs / "index_manifest.json"
    stats = import_archive(
        src=str(zip_path),
        vector_store_dir=str(new_vs),
        manifest_path=str(new_mf),
        conversations_dir=str(tmp_path / "conv2"),
        attachments_dir=str(tmp_path / "att2"),
        mode="merge",
    )
    assert stats["mode"] == "merge"
    assert stats["documents"] == 1
    assert stats["files"] >= 1

    # 校验导入后的向量库可读
    imported = ChromaStore(collection_name="exp", persist_directory=str(new_vs), embedding_dimension=1024)
    assert imported.count() == 1
    assert new_mf.exists()
    assert (tmp_path / "conv2" / "abc.json").exists()


def test_import_invalid_archive(tmp_path):
    """无效归档报错"""
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("nope.txt", "x")

    try:
        import_archive(src=str(bad), vector_store_dir=str(tmp_path / "vs"),
                       manifest_path=str(tmp_path / "mf.json"))
        assert False, "应抛出 ValueError"
    except ValueError as e:
        assert "meta.json" in str(e)


def test_export_import_replace_mode(tmp_path):
    """replace 模式：目标向量库先清空再重建"""
    src_store = _make_store(tmp_path)
    vs_dir = tmp_path / "vs"
    dst_vs = tmp_path / "dst_vs"
    dst_store = ChromaStore(collection_name="exp", persist_directory=str(dst_vs), embedding_dimension=1024)
    dst_store.add(ids=["old"], embeddings=[[0.2] * 1024], documents=["旧"], metadatas=[{"f": "old"}])
    assert dst_store.count() == 1

    zip_path = tmp_path / "e.zip"
    export_archive(str(zip_path), str(vs_dir), str(vs_dir / "index_manifest.json"),
                   [], conversations_dir=tmp_path / "c", attachments_dir=tmp_path / "a")
    import_archive(str(zip_path), str(dst_vs), str(dst_vs / "index_manifest.json"),
                   conversations_dir=tmp_path / "c2", attachments_dir=tmp_path / "a2", mode="replace")

    reloaded = ChromaStore(collection_name="exp", persist_directory=str(dst_vs), embedding_dimension=1024)
    assert reloaded.count() == 1  # 旧数据被清空，仅导入的 1 条