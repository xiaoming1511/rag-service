"""
增量索引同步器测试（离线：假嵌入器 + 临时目录，无需 oMLX 服务）

覆盖三态同步：新增 / 变更（先删后加）/ 删除，以及无变化跳过与全量重建。
"""

import sys
import json
import os
import time
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.document.loader import DocumentLoader
from src.document.chunker import Chunker
from src.vector_store.chroma_store import ChromaStore
from src.pipeline.indexer import Indexer
from src.pipeline.index_sync import IndexSync


class FakeEmbedder:
    """离线假嵌入器：固定向量，避免网络依赖"""

    def embed(self, texts):
        return [[0.1] * 1024 for _ in texts]

    def embed_single(self, text):
        return [0.1] * 1024

    def get_embedding_dimension(self):
        return 1024


@pytest.fixture()
def setup(tmp_path):
    """构建：临时文档库 + 假嵌入 + 临时向量库 + 增量同步器"""
    docs_dir = tmp_path / "kb"
    docs_dir.mkdir()

    (docs_dir / "a.md").write_text(
        "# 文档A\n\n这是文档 A 的内容。\n\n## 章节一\n\nA 的章节一内容。",
        encoding="utf-8",
    )
    (docs_dir / "b.md").write_text(
        "# 文档B\n\n这是文档 B 的内容。",
        encoding="utf-8",
    )

    loader = DocumentLoader(source_dirs=[str(docs_dir)], extensions=[".md"])
    chunker = Chunker(chunk_size=800, overlap=100, strategy="heading")
    embedder = FakeEmbedder()

    store = ChromaStore(
        collection_name="test_inc",
        persist_directory=str(tmp_path / "chroma_db"),
        embedding_dimension=1024,
    )
    indexer = Indexer(loader=loader, chunker=chunker, embedder=embedder, vector_store=store)
    sync = IndexSync(indexer, manifest_path=str(tmp_path / "manifest.json"))

    return {
        "docs_dir": docs_dir,
        "store": store,
        "sync": sync,
        "loader": loader,
    }


def _write(path: Path, content: str):
    """写文件并向前拨动 mtime（确保与上次 stat 不一致）"""
    path.write_text(content, encoding="utf-8")
    t = time.time() + 10
    os.utime(path, (t, t))


def _read_manifest(sync) -> dict:
    """读取清单"""
    return json.loads(Path(sync.manifest_path).read_text(encoding="utf-8"))


def test_sync_add(setup):
    """首次同步：全部新增，清单与向量库一致"""
    stats = setup["sync"].sync()
    assert sorted(stats["added"]) == ["a.md", "b.md"]
    assert stats["updated"] == []
    assert stats["removed"] == []
    assert setup["store"].count() > 0
    assert len(_read_manifest(setup["sync"])["docs"]) == 2


def test_sync_nochange(setup):
    """再次同步：无变化，不触发任何增删改"""
    setup["sync"].sync()
    count_before = setup["store"].count()

    stats = setup["sync"].sync()
    assert stats["added"] == []
    assert stats["updated"] == []
    assert stats["removed"] == []
    assert stats["unchanged"] == 2
    assert setup["store"].count() == count_before


def test_sync_update(setup):
    """修改文档：先删旧块再重新入库，旧内容被替换"""
    setup["sync"].sync()
    count_before = setup["store"].count()

    doc_a = setup["docs_dir"] / "a.md"
    _write(doc_a, "# 文档A\n\n这是文档 A 的【全新内容】。\n\n## 章节一\n\n新的章节一内容。")

    stats = setup["sync"].sync()
    assert stats["updated"] == ["a.md"]
    # 删旧块 + 加新块，块数相同则总数不变
    assert setup["store"].count() == count_before

    # 变更后的内容应可在向量库中查到，旧内容已被替换
    doc_id = setup["loader"].doc_id_for(str(doc_a))
    texts = " ".join(b["document"] for b in setup["store"].get_by_doc_id(doc_id))
    assert "全新内容" in texts
    assert "这是文档 A 的内容" not in texts


def test_sync_add_after(setup):
    """新增文件：增量入库，不影响已有文档"""
    setup["sync"].sync()
    count_before = setup["store"].count()

    _write(setup["docs_dir"] / "c.md", "# 文档C\n\nC 的内容。")

    stats = setup["sync"].sync()
    assert stats["added"] == ["c.md"]
    assert setup["store"].count() > count_before
    assert len(_read_manifest(setup["sync"])["docs"]) == 3


def test_sync_remove(setup):
    """删除文件：按 doc_id 清空全部块并移除清单记录"""
    setup["sync"].sync()
    count_before = setup["store"].count()

    (setup["docs_dir"] / "b.md").unlink()

    stats = setup["sync"].sync()
    assert stats["removed"] == ["b.md"]
    assert setup["store"].count() < count_before
    assert len(_read_manifest(setup["sync"])["docs"]) == 1


def test_sync_content_hash_only(setup):
    """内容未变但时间戳变化（如 git checkout）：不应重新索引"""
    setup["sync"].sync()
    count_before = setup["store"].count()

    # 只拨动 mtime，不改内容
    doc_a = setup["docs_dir"] / "a.md"
    t = time.time() + 20
    os.utime(doc_a, (t, t))

    stats = setup["sync"].sync()
    assert stats["updated"] == []
    assert stats["unchanged"] == 2
    assert setup["store"].count() == count_before


def test_sync_rebuild(setup):
    """rebuild=True：清空向量库与清单后全量重建"""
    setup["sync"].sync()
    maybe = setup["store"].count()

    setup["sync"].sync(rebuild=True)
    assert setup["store"].count() == maybe  # 视觉上一致
    assert setup["store"].count() > 0
    assert len(_read_manifest(setup["sync"])["docs"]) == 2