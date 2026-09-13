"""
第七轮回归：指标口径拆分（D5-1）+ 分块/装配/加载（R7-1/2/3）

全部离线可跑，不依赖 oMLX 与向量库。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.document.chunker import Chunker
from src.document.loader import Document, DocumentLoader
from src.evaluation.metrics import aggregate
from src.evaluation.run_eval import _fmt_metric, build_gold_chunk_totals
from src.retrieval.parent_expander import _assemble_parent


# ================================================================
# D5-1：块级 / 文档级两套口径
# ================================================================

class TestMetricLevelSeparation:
    def test_aggregate_emits_level_prefixed_keys_only(self):
        """键名必须自带层级前缀，不再有二义的 recall@k / precision@k"""
        agg = aggregate([["a"]], [{"a"}], k=1)
        assert set(agg) == {
            "doc_recall@1", "doc_precision@1", "doc_mrr@1", "doc_hit@1",
            "chunk_precision@1", "chunk_recall@1",
        }
        assert "recall@1" not in agg and "precision@1" not in agg

    def test_chunk_recall_is_none_without_denominator(self):
        """分母未知 → None；聚合时不计入均值，也不伪装成 0"""
        agg = aggregate([["a"], ["a"]], [{"a"}, {"a"}], k=1, chunk_totals=[2, 0])
        assert agg["chunk_recall@1"] == pytest.approx(0.5)

    def test_same_topk_observed_by_both_levels(self):
        """两级必须观察同一段 top-k（先截断再去重，而非先去重再截断）"""
        # 前 3 个槽全是同一文档，gold 在第 4 槽
        ranked = [["a", "a", "a", "b"]]
        agg = aggregate(ranked, [{"b"}], k=3)
        assert agg["doc_recall@3"] == 0.0        # b 不在前 3 个块里
        assert agg["chunk_precision@3"] == 0.0


# ================================================================
# R7-1：长章节尾块行号
# ================================================================

class TestChunkerLineRange:
    @staticmethod
    def _long_doc() -> Document:
        paras = [f"第 {i} 段。" * 60 for i in range(1, 9)]
        md = "# 巨型章节\n\n" + "\n\n".join(paras) + "\n"
        return Document(id="d1", file_path="/x/t.md", file_name="t.md",
                        content=md, metadata={})

    def test_all_sub_chunks_carry_line_range(self):
        """切分出的每个子块都必须带行号——漏传会落 0，下游再转成 None"""
        chunks = Chunker(chunk_size=400, overlap=50).chunk_document(self._long_doc())
        assert len(chunks) > 1, "用例前提：该文档应被切成多个子块"
        bad = [c.id for c in chunks if c.metadata["start_line"] <= 0]
        assert not bad, f"以下块的 start_line 为 0（行级引文会消失）: {bad}"

    def test_tail_sub_chunk_matches_siblings(self):
        chunks = Chunker(chunk_size=400, overlap=50).chunk_document(self._long_doc())
        first, last = chunks[0].metadata, chunks[-1].metadata
        assert last["start_line"] == first["start_line"]
        assert last["end_line"] == first["end_line"]

    def test_unsplit_chunk_still_has_line_range(self):
        md = "# 短章节\n\n只有几行。\n"
        doc = Document(id="d2", file_path="/x/s.md", file_name="s.md",
                       content=md, metadata={})
        chunks = Chunker().chunk_document(doc)
        assert len(chunks) == 1
        # 约定：start_line 取「标题的下一行」（可能是空行），end_line 取章节末行；
        # 文档以 \n 结尾时分出的最后一项是空串，故 end_line = 行数。
        assert chunks[0].metadata["start_line"] == 2
        assert chunks[0].metadata["end_line"] == len(md.split("\n"))


# ================================================================
# R7-2：父块装配定序
# ================================================================

class TestParentAssemblerOrder:
    def test_order_uses_sub_index_within_same_chunk_index(self):
        """同一长章节的子块共享 chunk_index，必须靠 sub_index 才能定序"""
        siblings = [
            {"id": "x_0_1", "document": "子块一", "metadata": {"chunk_index": 0, "sub_index": 1}},
            {"id": "x_0_0", "document": "子块零", "metadata": {"chunk_index": 0, "sub_index": 0}},
            {"id": "x_1", "document": "普通块", "metadata": {"chunk_index": 1}},
        ]
        out = _assemble_parent(siblings, "x_0_0", 10000)
        assert out == "子块零\n\n子块一\n\n普通块"

    def test_missing_sub_index_defaults_to_zero(self):
        """普通块（未切分）无 sub_index，应与 sub_index=0 等价，不报错"""
        siblings = [
            {"id": "a", "document": "B", "metadata": {"chunk_index": 1}},
            {"id": "b", "document": "A", "metadata": {"chunk_index": 0}},
        ]
        out = _assemble_parent(siblings, "a", 10000)
        assert out == "A\n\nB"


# ================================================================
# R7-3：结构性元数据优先
# ================================================================

class TestLoaderMetadataAuthority:
    def test_frontmatter_cannot_shadow_structural_keys(self, tmp_path):
        """frontmatter 是文档内容，不能盖掉 file_path / file_name 等磁盘事实"""
        md = (
            "---\n"
            "file_name: 被伪装的文档.md\n"
            "file_path: /etc/passwd\n"
            "custom_key: 保留我\n"
            "---\n\n# 正文\n\n内容。\n"
        )
        p = tmp_path / "真实文件名.md"
        p.write_text(md, encoding="utf-8")

        loader = DocumentLoader(source_dirs=[str(tmp_path)])
        doc = loader.load_single(str(p))
        assert doc is not None
        assert doc.metadata["file_name"] == "真实文件名.md"
        assert doc.metadata["file_path"] == str(p.resolve())
        # 非结构性自定义键仍应保留
        assert doc.metadata["custom_key"] == "保留我"

    def test_frontmatter_title_still_wins_over_h1(self, tmp_path):
        """既有约定：frontmatter 的 title 优先于正文 H1（R7-3 修复不得破坏它）"""
        md = "---\ntitle: 元数据标题\n---\n\n# 正文标题\n\n内容。\n"
        p = tmp_path / "t.md"
        p.write_text(md, encoding="utf-8")
        doc = DocumentLoader(source_dirs=[str(tmp_path)]).load_single(str(p))
        assert doc.metadata["title"] == "元数据标题"

    def test_h1_is_title_fallback_without_frontmatter(self, tmp_path):
        md = "# 正文标题\n\n内容。\n"
        p = tmp_path / "t2.md"
        p.write_text(md, encoding="utf-8")
        doc = DocumentLoader(source_dirs=[str(tmp_path)]).load_single(str(p))
        assert doc.metadata["title"] == "正文标题"

    def test_parser_images_and_format_survive(self, tmp_path):
        md = "# 我的标题\n\n正文。\n"
        p = tmp_path / "a.md"
        p.write_text(md, encoding="utf-8")
        doc = DocumentLoader(source_dirs=[str(tmp_path)]).load_single(str(p))
        assert doc.metadata["format"] == "md"
        assert doc.metadata.get("title") == "我的标题"


# ================================================================
# 基线对比：口径变更必须显式报错，而不是"无差异"
# ================================================================

class TestBaselineCompat:
    def test_matching_schema_reports_diff(self, tmp_path, monkeypatch):
        import src.evaluation.run_eval as run_eval
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps({
            "meta": {"timestamp": "2026-09-11T00:00:00"},
            "metrics": {"doc_recall@5": 0.9},
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(run_eval, "BASELINE_PATH", p)

        out = run_eval.compare_with_baseline({"doc_recall@5": 0.8})
        assert out["diff"]["doc_recall@5"] == pytest.approx(-0.1)

    def test_legacy_schema_key_mismatch_is_reported(self, tmp_path, monkeypatch):
        """旧基线只有 recall@5 这类无层级键 → 必须报口径不一致，不能返回空 diff"""
        import src.evaluation.run_eval as run_eval
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps({
            "meta": {"timestamp": "2026-09-06T16:52:46"},
            "metrics": {"recall@5": 0.90625, "precision@5": 0.5833},
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(run_eval, "BASELINE_PATH", p)

        out = run_eval.compare_with_baseline({"doc_recall@5": 0.8})
        assert out.get("baseline") is None
        assert "口径" in out.get("error", "")
        assert "diff" not in out


# ================================================================
# 评测执行器的块数统计与 None 格式化
# ================================================================

class _StubStore:
    def __init__(self, names):
        self._names = names

    def get_all(self):
        return [{"metadata": {"file_name": n}} for n in self._names]


class TestGoldChunkTotals:
    def test_counts_chunks_per_document(self):
        store = _StubStore(["a.md", "a.md", "b.md", "a.md"])
        assert build_gold_chunk_totals(None, store) == {"a.md": 3, "b.md": 1}

    def test_normalizes_path_like_names(self):
        store = _StubStore(["/x/y/a.md", "a.md"])
        assert build_gold_chunk_totals(None, store) == {"a.md": 2}

    def test_store_failure_degrades_to_empty(self):
        class _Boom:
            def get_all(self):
                raise RuntimeError("chroma down")

        assert build_gold_chunk_totals(None, _Boom()) == {}

    def test_fmt_metric_renders_none_as_na(self):
        assert _fmt_metric(None) == "n/a"
        assert _fmt_metric(0.5) == "0.5000"
