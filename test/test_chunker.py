"""
文档分块器测试（纯内存，不依赖外部服务）
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.document.loader import Document
from src.document.chunker import Chunker


def _make_doc(content: str, file_name: str = "test.md") -> Document:
    """构建测试文档对象"""
    return Document(
        id=f"doc_{file_name}",
        file_path=f"/tmp/{file_name}",
        file_name=file_name,
        content=content,
    )


LONG_DOC_HEADING = """# 一级标题

这是第一个章节的内容，用于验证按标题切分。

## 二级标题

这是二级标题下的内容。

### 三级标题

这是三级标题下的内容。

# 第二个一级标题

这是第二个一级标题下的内容。
"""


def test_chunk_by_heading_basic():
    """按标题切分：每个标题下应生成独立块，并携带标题路径元数据"""
    chunker = Chunker(chunk_size=800, overlap=100, strategy="heading")
    doc = _make_doc(LONG_DOC_HEADING)
    chunks = chunker.chunk_document(doc)

    # "一级标题"、"二级标题"、"三级标题"、"第二个一级标题" 应各自成块
    assert len(chunks) >= 3

    # 验证标题路径元数据
    headings = [c.metadata.get("heading_path") for c in chunks]
    assert any("一级标题" in h for h in headings if h)
    assert any("二级标题" in h for h in headings if h)

    # 每个块都应携带文件信息
    for c in chunks:
        assert c.metadata.get("file_name") == "test.md"
        assert c.metadata.get("doc_id") == "doc_test.md"


def test_chunk_by_heading_long_content():
    """超长章节按固定大小二次切分：子块 ID 全局唯一且元数据完整"""
    chunker = Chunker(chunk_size=100, overlap=20, strategy="heading")

    # 构造一个超长章节（超过 chunk_size，应被二次切分）
    long_section = "# 超长章节\n\n" + "\n\n".join(
        f"这是第 {i} 段的内容，用于撑大章节长度以触发二次切分逻辑。" for i in range(30)
    )
    doc = _make_doc(long_section, file_name="long.md")
    chunks = chunker.chunk_document(doc)

    # 超长章节应被切成多个子块
    assert len(chunks) > 1

    # 所有子块 ID 应唯一，且以文档 ID 为前缀（避免跨文档冲突）
    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids)), "子块 ID 不应重复"
    for cid in ids:
        assert cid.startswith("doc_long.md")

    # 子块元数据应完整（含 file_name、heading、sub_index）
    for c in chunks:
        assert c.metadata.get("file_name") == "long.md"
        assert c.metadata.get("heading") == "超长章节"
        assert "sub_index" in c.metadata


def test_chunk_ids_unique_across_docs():
    """不同文档的分块 ID 不应冲突"""
    chunker = Chunker(chunk_size=100, overlap=20, strategy="heading")

    docs = [
        _make_doc("# 标题A\n\n" + "内容内容内容内容内容内容内容内容内容内容内容内容" * 20, "a.md"),
        _make_doc("# 标题B\n\n" + "内容内容内容内容内容内容内容内容内容内容内容内容" * 20, "b.md"),
    ]
    all_chunks = chunker.chunk_documents(docs)
    ids = [c.id for c in all_chunks]
    assert len(ids) == len(set(ids)), "跨文档块 ID 不应冲突"


def test_chunk_by_fixed():
    """按固定大小切分（备用策略）"""
    chunker = Chunker(chunk_size=150, overlap=30, strategy="fixed")
    content = "\n\n".join(f"第 {i} 个段落的内容。" * 10 for i in range(10))
    doc = _make_doc(content)
    chunks = chunker.chunk_document(doc)

    assert len(chunks) >= 2
    # 每个块大小不应显著超过上限（允许一个段落的超限）
    for c in chunks:
        assert len(c.content) <= 150 + 80

    # 固定策略块也应携带文件信息
    for c in chunks:
        assert c.metadata.get("file_name") == "test.md"


def test_chunk_only_headings():
    """只有标题没有内容的文档应返回空列表"""
    chunker = Chunker(strategy="heading")
    doc = _make_doc("# 只有标题\n\n## 也是标题\n")
    assert chunker.chunk_document(doc) == []


def test_get_chunk_info():
    """分块统计信息"""
    chunker = Chunker(chunk_size=100, overlap=20, strategy="heading")
    doc = _make_doc(LONG_DOC_HEADING)
    chunks = chunker.chunk_document(doc)
    info = chunker.get_chunk_info(chunks)

    assert info["total"] == len(chunks)
    assert info["avg_size"] > 0
    assert info["max_size"] >= info["min_size"]

    # 空列表的统计
    empty_info = chunker.get_chunk_info([])
    assert empty_info["total"] == 0