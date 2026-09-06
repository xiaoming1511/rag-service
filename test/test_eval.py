"""
评测基建离线测试（决策 B1）：指标纯函数、数据集校验、judge 分数解析、
run_eval 报告对比逻辑。全部离线可跑，不依赖 oMLX。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.evaluation.dataset import EvalDataset, normalize_doc_id
from src.evaluation.metrics import (
    aggregate,
    hit_at_k,
    mrr_at_k,
    parse_judge_score,
    precision_at_k,
    recall_at_k,
)
from src.evaluation.judge import LLMJudge


# ---------- 指标纯函数 ----------

class TestMetrics:
    def test_recall_basic(self):
        assert recall_at_k(["a", "b", "c"], {"a"}, 3) == 1.0
        assert recall_at_k(["a", "b", "c"], {"a", "d"}, 3) == 0.5
        assert recall_at_k(["b", "c", "a"], {"a"}, 2) == 0.0
        assert recall_at_k(["a"], {"a"}, 1) == 1.0

    def test_recall_empty_gold(self):
        assert recall_at_k(["a"], set(), 3) == 0.0

    def test_mrr(self):
        assert mrr_at_k(["x", "a", "b"], {"a"}, 3) == 0.5
        assert mrr_at_k(["a"], {"a"}, 3) == 1.0
        assert mrr_at_k(["x", "y"], {"a"}, 3) == 0.0
        # gold 在 k 之外不计分
        assert mrr_at_k(["x", "y", "a"], {"a"}, 2) == 0.0

    def test_hit(self):
        assert hit_at_k(["x", "a"], {"a"}, 2) == 1.0
        assert hit_at_k(["x", "y"], {"a"}, 2) == 0.0

    def test_precision(self):
        assert precision_at_k(["a", "b", "x"], {"a", "b"}, 3) == pytest.approx(2 / 3)
        assert precision_at_k([], {"a"}, 3) == 0.0

    def test_aggregate(self):
        ranked = [["a", "b"], ["x", "y"]]
        golds = [{"a"}, {"z"}]
        agg = aggregate(ranked, golds, k=2)
        assert agg["recall@2"] == pytest.approx(0.5)   # 1.0 与 0.0 的均值
        assert agg["mrr@2"] == pytest.approx(0.5)
        assert agg["hit@2"] == pytest.approx(0.5)
        assert agg["precision@2"] == pytest.approx(0.25)

    def test_aggregate_length_mismatch(self):
        with pytest.raises(ValueError):
            aggregate([["a"]], [{"a"}, {"b"}], k=1)

    def test_aggregate_empty(self):
        agg = aggregate([], [], k=5)
        assert agg["recall@5"] == 0.0

    def test_parse_judge_score_formats(self):
        assert parse_judge_score("SCORE: 0.8") == 0.8
        assert parse_judge_score("SCORE: 1") == 1.0
        assert parse_judge_score("0.75") == 0.75
        assert parse_judge_score("评分 SCORE：0.9") == 0.9
        assert parse_judge_score("85") == 0.85      # 百分制容错
        assert parse_judge_score("很好") == -1.0
        assert parse_judge_score("") == -1.0
        assert parse_judge_score("SCORE: 1.5") == -1.0  # 越界无效


# ---------- 数据集加载与校验 ----------

class TestDataset:
    def _write(self, tmp_path, data):
        p = tmp_path / "eval_qa.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_load_valid(self, tmp_path):
        path = self._write(tmp_path, [
            {"id": "q1", "question": "Q?", "gold_docs": ["a.md"]},
            {"id": "q2", "question": "Q2?", "gold_docs": ["x/b.md"], "gold_keywords": ["k"]},
        ])
        ds = EvalDataset.load(path)
        assert len(ds) == 2
        assert ds.gold_set(ds.items[1]) == {"b.md"}  # 路径归一为 basename

    def test_missing_field_raises(self, tmp_path):
        path = self._write(tmp_path, [{"id": "q1", "question": "Q?"}])
        with pytest.raises(ValueError, match="gold_docs"):
            EvalDataset.load(path)

    def test_duplicate_id_raises(self, tmp_path):
        path = self._write(tmp_path, [
            {"id": "q1", "question": "Q?", "gold_docs": ["a.md"]},
            {"id": "q1", "question": "Q2?", "gold_docs": ["a.md"]},
        ])
        with pytest.raises(ValueError, match="重复"):
            EvalDataset.load(path)

    def test_empty_gold_raises(self, tmp_path):
        path = self._write(tmp_path, [{"id": "q1", "question": "Q?", "gold_docs": []}])
        with pytest.raises(ValueError):
            EvalDataset.load(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            EvalDataset.load(str(tmp_path / "nope.json"))

    def test_normalize_doc_id(self):
        assert normalize_doc_id("/a/b/笔记.md") == "笔记.md"
        assert normalize_doc_id("笔记.md") == "笔记.md"


# ---------- LLMJudge（打桩，离线） ----------

class _StubJudgeClient:
    """按 prompt 类型返回不同分数"""

    def chat_sync(self, model, messages, **kwargs):
        content = messages[0]["content"]
        if "忠实度" in content:
            return "SCORE: 0.9"
        return "SCORE: 0.6"


class TestLLMJudge:
    def test_evaluate_generation(self):
        judge = LLMJudge(_StubJudgeClient(), model="stub")
        scores = judge.evaluate_generation(
            question="Q?", context="上下文", answer="回答",
            gold_keywords=["k1"],
        )
        assert scores["faithfulness"] == 0.9
        assert scores["answer_relevance"] == 0.6

    def test_judge_invalid_score_marked(self):
        class _Bad:
            def chat_sync(self, model, messages, **kwargs):
                return "我无法评分"

        judge = LLMJudge(_Bad(), model="stub")
        scores = judge.evaluate_generation("Q?", "C", "A", [])
        # 解析失败统一返回 -1.0，由 runner 记为无效不计入均值
        assert scores["faithfulness"] == -1.0
        assert scores["answer_relevance"] == -1.0


# ---------- 真实评测集自检（保证数据集本身始终合法） ----------

def test_real_dataset_valid():
    dataset_path = Path(__file__).parent.parent / "data" / "eval" / "eval_qa.json"
    if not dataset_path.exists():
        pytest.skip("评测集尚未创建")
    ds = EvalDataset.load(str(dataset_path))
    assert len(ds) >= 30, "评测集应不少于 30 条"
    ids = [it["id"] for it in ds.items]
    assert len(ids) == len(set(ids))
