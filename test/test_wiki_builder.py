"""
AI 知识层生成器（wiki-builder）测试（离线：桩 LLM）

覆盖：来源摘要页、概念/实体页创建与追加、互链、index/log、图谱、增量跳过
"""

import sys
import json
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.document.loader import DocumentLoader
from src.wikibuilder.builder import WikiBuilder


class _StubClient:
    """桩 LLM：摘要与抽取返回固定内容"""

    def chat_sync(self, model, messages, **kwargs):
        system = messages[0]["content"]
        if "JSON" in system:
            return json.dumps({
                "concepts": ["概念甲", "概念乙"],
                "entities": ["项目X"],
                "definitions": [
                    {"name": "概念甲", "definition": "概念甲的简短定义。"},
                ],
            }, ensure_ascii=False)
        return "## 概述\n这是文档的概要。\n## 关键要点\n- 要点一\n- 要点二"


class _StubGenerator:
    client = _StubClient()
    model = "test-model"
    rewrite_query = False

    def generate(self, *a, **kw):
        return "摘要"


@pytest.fixture()
def setup(tmp_path):
    """临时文档库 + 临时知识层目录 + 构建器"""
    docs_dir = tmp_path / "kb"
    docs_dir.mkdir()
    (docs_dir / "Redis 详细指南.md").write_text(
        "# Redis\n\nRedis 是一个内存数据库，支持字符串、列表、哈希等。\n\n" * 3,
        encoding="utf-8",
    )
    (docs_dir / "Python 基础.md").write_text(
        "# Python\n\nPython 是解释型语言，支持面向对象。",
        encoding="utf-8",
    )

    loader = DocumentLoader(source_dirs=[str(docs_dir)])
    builder = WikiBuilder(
        loader=loader,
        generator=_StubGenerator(),
        wiki_dir=str(tmp_path / "wiki"),
        enabled=True,
    )
    return {"docs_dir": docs_dir, "builder": builder, "wiki": tmp_path / "wiki"}


def test_build_pending_creates_pages(setup):
    """首次构建：为每个文档生成来源摘要页 + 概念/实体页"""
    stats = setup["builder"].build_pending()

    assert len(stats["built"]) == 2
    sources_dir = setup["wiki"] / "sources"
    assert (sources_dir / "Redis 详细指南.md").exists()
    assert (sources_dir / "Python 基础.md").exists()

    # 概念/实体页
    assert (setup["wiki"] / "concepts" / "概念甲.md").exists()
    assert (setup["wiki"] / "concepts" / "概念乙.md").exists()
    assert (setup["wiki"] / "entities" / "项目X.md").exists()
    assert stats["concepts"] >= 4  # 两篇文档各抽取 2 个概念
    assert stats["entities"] >= 2


def test_page_content_and_links(setup):
    """摘要页包含概要、要点与 [[互链]]；概念页带定义与来源"""
    setup["builder"].build_pending()

    summary = (setup["wiki"] / "sources" / "Redis 详细指南.md").read_text(encoding="utf-8")
    assert summary.startswith("---")
    assert "type: source" in summary
    assert "Redis 详细指南" in summary.split("---", 2)[2]  # frontmatter 之后
    assert "这是文档的概要" in summary
    assert "要点一" in summary
    assert "[[概念甲]]" in summary  # 互链

    concept = (setup["wiki"] / "concepts" / "概念甲.md").read_text(encoding="utf-8")
    assert "概念甲的简短定义" in concept
    assert "Redis 详细指南" in concept  # 来源引用


def test_build_pending_skips_fresh(setup):
    """二次构建：页面已最新 → 全部跳过"""
    setup["builder"].build_pending()
    stats2 = setup["builder"].build_pending()
    assert stats2["built"] == []
    assert len(stats2["skipped"]) == 2


def test_index_and_log(setup):
    """index.md 列出全部页面；log.md 记录构建日志"""
    setup["builder"].build_pending()

    index = (setup["wiki"] / "index.md").read_text(encoding="utf-8")
    assert "Wiki 索引" in index
    assert "[[Redis 详细指南]]" in index
    assert "[[概念甲]]" in index

    log = (setup["wiki"] / "log.md").read_text(encoding="utf-8")
    assert "构建知识层" in log


def test_update_appends_existing_concept(setup):
    """第二篇文档引用同一概念时，概念页追加来源而非覆盖"""
    builder = setup["builder"]
    builder.build_pending()
    concept_path = setup["wiki"] / "concepts" / "概念甲.md"
    content_before = concept_path.read_text(encoding="utf-8")
    assert content_before.count("[[Redis 详细指南]]") == 1

    # 强制重建：概念页应追加 Python 来源（通过新增来源抽取同一概念验证追加路径）
    # 简化：直接再调用 upsert 逻辑两次模拟
    docs = setup["builder"].loader.load()
    from src.document.loader import Document as _D
    # 用第二个文档再次构建同一概念（桩 LLM 总是返回概念甲）
    builder.build_for_document(docs[1])
    content_after = concept_path.read_text(encoding="utf-8")
    assert "[[Python 基础]]" in content_after


def test_disabled_builder(tmp_path):
    """禁用时 build_pending 直接返回空统计"""
    docs_dir = tmp_path / "kb"
    docs_dir.mkdir()
    (docs_dir / "a.md").write_text("# A\n\n内容", encoding="utf-8")
    loader = DocumentLoader(source_dirs=[str(docs_dir)])
    builder = WikiBuilder(
        loader=loader,
        generator=_StubGenerator(),
        wiki_dir=str(tmp_path / "wiki"),
        enabled=False,
    )
    stats = builder.build_pending()
    assert stats == {"enabled": False, "built": [], "skipped": []}