"""
文档加载器测试（使用临时目录，不依赖外部数据）
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.document.loader import DocumentLoader


def _create_sample_vault(tmp_path: Path) -> Path:
    """在临时目录中构建一个示例 Markdown 文档库（含 frontmatter、嵌套目录、非支持文件）"""
    source = tmp_path / "docs"
    source.mkdir()

    # 带 frontmatter 的文档
    (source / "redis.md").write_text(
        "---\ntitle: Redis 指南\ntags: [redis, cache]\n---\n"
        "Redis 是一个内存数据库，支持多种数据结构。\n\n"
        "- String（字符串）\n- List（列表）",
        encoding="utf-8",
    )

    # 嵌套目录下的普通文档
    nested = source / "python"
    nested.mkdir()
    (nested / "basic.md").write_text(
        "# Python 基础\n\nPython 是解释型、面向对象的编程语言。",
        encoding="utf-8",
    )

    # 不支持的扩展名（应被忽略）
    (source / "notes.txt").write_text("这不是 Markdown 文件。", encoding="utf-8")

    # 空文件（应被跳过）
    (source / "empty.md").write_text("", encoding="utf-8")

    return source


def test_loader(tmp_path):
    """测试文档加载器：递归加载、frontmatter 解析、扩展名过滤、空文件跳过"""
    source = _create_sample_vault(tmp_path)

    loader = DocumentLoader(
        source_dirs=[str(source)],
        extensions=[".md", ".markdown"],
    )
    docs = loader.load()

    # 应加载 2 个有效文档（redis.md 与 python/basic.md）
    assert len(docs) == 2, f"期望 2 个文档，实际 {len(docs)}"

    # 按文件名索引
    by_name = {d.file_name: d for d in docs}
    assert set(by_name) == {"redis.md", "basic.md"}

    redis_doc = by_name["redis.md"]
    basic_doc = by_name["basic.md"]

    # 基本字段
    assert redis_doc.content, "文档内容不应为空"
    assert redis_doc.id, "文档 ID 不应为空"
    assert redis_doc.file_path.endswith("redis.md")

    # frontmatter 解析：title 与 tags 应进入元数据
    assert redis_doc.metadata.get("title") == "Redis 指南"
    assert redis_doc.metadata.get("tags") == ["redis", "cache"]

    # frontmatter 应从正文中剥离
    assert not redis_doc.content.startswith("---")
    assert "Redis 是一个内存数据库" in redis_doc.content

    # 嵌套目录中的文档也应被加载
    assert "Python 是解释型" in basic_doc.content

    # 文档 ID 应稳定且互不相同（基于文件路径哈希）
    assert redis_doc.id != basic_doc.id

    # 不存在的目录应被跳过而不是报错
    empty_loader = DocumentLoader(source_dirs=["/不存在的目录/xyz"])
    assert empty_loader.load() == []


def test_loader_single(tmp_path):
    """测试加载单个文件"""
    source = _create_sample_vault(tmp_path)
    redis_path = source / "redis.md"

    loader = DocumentLoader(source_dirs=[str(source)])
    doc = loader.load_single(str(redis_path))

    assert doc is not None
    assert doc.file_name == "redis.md"
    assert doc.metadata.get("title") == "Redis 指南"

    # 不存在的文件返回 None
    assert loader.load_single(str(source / "not_exist.md")) is None