"""
OCR 识别率指标离线测试

覆盖：
  1. `src/evaluation/metrics.py` 的字符级 OCR 指标（编辑距离、CER、文本
     归一化、单样本报告、聚合）——含「失败样本不污染均值」的关键纪律。
  2. `scripts/ocr_accuracy.py` 的可导入纯逻辑（合成样本描述、配置矩阵）
     与 `--help` 可运行性。

全部离线，不调真实模型；真实精度由主理人统一在真机验证。
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.evaluation.metrics import (  # noqa: E402
    aggregate_ocr,
    char_error_rate,
    edit_distance,
    normalize_ocr_text,
    ocr_accuracy_report,
)


# ================================================================
#  edit_distance
# ================================================================

class TestEditDistance:

    def test_empty(self):
        assert edit_distance("", "") == 0
        assert edit_distance("", "abc") == 3
        assert edit_distance("abc", "") == 3

    def test_identical(self):
        assert edit_distance("abc", "abc") == 0
        assert edit_distance("中文文本", "中文文本") == 0

    def test_single_substitution(self):
        assert edit_distance("abc", "abd") == 1
        assert edit_distance("猫", "狗") == 1

    def test_insert_and_delete(self):
        assert edit_distance("abc", "abcd") == 1     # 插入
        assert edit_distance("abcd", "abc") == 1     # 删除
        assert edit_distance("abc", "abXcd") == 2

    def test_symmetry(self):
        for a, b in [("kitten", "sitting"), ("", "x"), ("中文", "中文字"),
                     ("abc", "xyz"), ("知识库配置", "知识库设置")]:
            assert edit_distance(a, b) == edit_distance(b, a)

    def test_known_value(self):
        assert edit_distance("kitten", "sitting") == 3

    def test_long_input_no_blowup(self):
        """两行滚动数组：上千字符也能算（内存不随长度平方增长）"""
        base = "知识库" * 200            # 600 字符
        assert edit_distance(base, base) == 0
        assert edit_distance(base, base + "x") == 1


# ================================================================
#  char_error_rate
# ================================================================

class TestCharErrorRate:

    def test_empty_reference_returns_none(self):
        """分母不可得 → None，绝不当作「零错误」"""
        assert char_error_rate("", "") is None
        assert char_error_rate("", "abc") is None

    def test_hypothesis_empty_is_legal(self):
        """空输出是合法输入：整段没认出来 → CER = 1.0"""
        assert char_error_rate("abcd", "") == pytest.approx(1.0)

    def test_perfect_match(self):
        assert char_error_rate("abcd", "abcd") == 0.0

    def test_partial_substitution(self):
        assert char_error_rate("abcd", "abce") == pytest.approx(0.25)

    def test_cer_can_exceed_one(self):
        """插入占主导时 CER > 1，属标准行为，不得钳制到 1.0"""
        assert char_error_rate("ab", "abcdef") == pytest.approx(2.0)
        assert char_error_rate("a", "aaaa") == pytest.approx(3.0)


# ================================================================
#  normalize_ocr_text
# ================================================================

class TestNormalizeOCRText:

    def test_fullwidth_punct_to_halfwidth(self):
        assert normalize_ocr_text("你好，世界。") == "你好,世界."
        assert normalize_ocr_text("（测试）") == "(测试)"
        assert normalize_ocr_text("「引用」") == '"引用"'
        assert normalize_ocr_text("【标注】") == "[标注]"

    def test_dash_and_ellipsis(self):
        assert normalize_ocr_text("破折号——省略……") == "破折号-省略..."

    def test_drop_markdown_heading_emphasis(self):
        assert normalize_ocr_text("# 标题") == "标题"
        assert normalize_ocr_text("## 二级标题") == "二级标题"
        assert normalize_ocr_text("**粗体** *斜体* `代码`") == "粗体斜体代码"
        assert normalize_ocr_text("~~删除~~") == "删除"

    def test_quote_stripped_list_markers_kept(self):
        # 引用符是模型添加的装饰 → 剥
        assert normalize_ocr_text("> 引用") == "引用"
        # 列表符 / 有序列表号是**内容**（GT 是纯文本）→ 保留，只抹平空白差异
        assert normalize_ocr_text("- 列表项") == "-列表项"
        assert normalize_ocr_text("1. 有序项") == "1.有序项"
        # 内容里的负号 / 年度数字不被误剥（旧实现会篡改成 "5是负数" / "这是…"）
        assert normalize_ocr_text("-5 是负数") == "-5是负数"
        assert normalize_ocr_text("2024. 年度总结") == "2024.年度总结"
        # `*` 是强调符，恒删
        assert normalize_ocr_text("* 星号列表") == "星号列表"

    def test_underscore_is_content_by_default(self):
        """默认不删 `_`：保留 snake_case 辨识力（否则漏识下划线会被抹平）"""
        assert normalize_ocr_text("ingest_queue") == "ingest_queue"
        assert normalize_ocr_text("top_k 参数") == "top_k参数"
        # 开启后按强调符删除
        assert normalize_ocr_text("ingest_queue", strip_underscore=True) == "ingestqueue"

    def test_heading_marker_space_asymmetry_fixed(self):
        """GT 有空格 / 模型漏空格 → 两侧必须归一成同一结果（P1 回归锚点）"""
        assert normalize_ocr_text("# 标题") == normalize_ocr_text("#标题")
        assert normalize_ocr_text("## 二级标题") == normalize_ocr_text("##二级标题")

    def test_drop_markdown_table(self):
        assert normalize_ocr_text("| 列 |\n|---|---|\n| 值 |") == "列值"

    def test_whitespace_removed_by_default(self):
        assert normalize_ocr_text("a b\tc\nd") == "abcd"
        assert normalize_ocr_text("前\u3000后") == "前后"   # 全角空格

    def test_keep_whitespace(self):
        assert normalize_ocr_text("a b", keep_whitespace=True) == "a b"
        assert normalize_ocr_text("a\nb", keep_whitespace=True) == "a\nb"

    def test_flags_off(self):
        assert normalize_ocr_text("，", unify_punct=False) == "，"
        assert normalize_ocr_text("# x", drop_markdown=False) == "#x"

    @pytest.mark.parametrize("raw", [
        "# 标题\n\n正文 段落。",
        "**加粗**，全角（括号）—— 省略……",
        "- a\n- b\n\n1. c",
        "| h |\n|---|\n| v |",
        "> 引用\n\n正文",
        "IngestQueue 处理 snapshot_1 数据。",
        "",
    ])
    def test_idempotent(self, raw):
        once = normalize_ocr_text(raw)
        assert normalize_ocr_text(once) == once


# ================================================================
#  ocr_accuracy_report
# ================================================================

class TestOCRAccuracyReport:

    def test_success_fields(self):
        r = ocr_accuracy_report("abc", "abd")
        for key in ("ok", "ref_chars", "hyp_chars", "cer_raw", "cer_normalized",
                    "exact_raw", "exact_normalized"):
            assert key in r
        assert r["ok"] is True
        assert r["ref_chars"] == 3 and r["hyp_chars"] == 3
        assert r["cer_raw"] == pytest.approx(1 / 3)
        assert r["exact_raw"] is False

    def test_failure_semantics(self):
        """空输出 → ok=False，且 cer_* 为 None（不是 1.0）"""
        r = ocr_accuracy_report("abc", "")
        assert r["ok"] is False
        assert r["cer_raw"] is None
        assert r["cer_normalized"] is None
        assert r["hyp_chars"] == 0

    def test_normalized_ignores_format(self):
        """格式差异：原样 CER > 0，归一化后完全一致"""
        ref = "# 索引配置\n\n正文内容"
        hyp = "索引配置正文内容"
        r = ocr_accuracy_report(ref, hyp)
        assert r["ok"] is True
        # 漏了 "# " 与 "\n\n" 共 4 个字符 / 12 → 4/12
        assert r["cer_raw"] == pytest.approx(4 / 12)
        assert r["cer_normalized"] == 0.0
        assert r["exact_raw"] is False
        assert r["exact_normalized"] is True


# ================================================================
#  aggregate_ocr
# ================================================================

class TestAggregateOCR:

    def test_failed_samples_excluded_from_mean(self):
        """2 成功 + 1 失败：均值只由 2 条成功样本决定，失败单独计数"""
        samples = [
            {"ok": True, "cer_raw": 0.0, "cer_normalized": 0.0,
             "exact_normalized": True},
            {"ok": True, "cer_raw": 0.4, "cer_normalized": 0.2,
             "exact_normalized": False},
            {"ok": False, "cer_raw": None, "cer_normalized": None,
             "exact_normalized": False},
        ]
        agg = aggregate_ocr(samples)
        assert agg["n_total"] == 3
        assert agg["n_ok"] == 2
        assert agg["n_failed"] == 1
        assert agg["success_rate"] == pytest.approx(2 / 3)
        # (0.0 + 0.4) / 2，失败样本不参与
        assert agg["cer_raw_mean"] == pytest.approx(0.2)
        assert agg["cer_normalized_mean"] == pytest.approx(0.1)
        # 完全正确率分母是 ok 样本数：1/2
        assert agg["exact_match_rate"] == pytest.approx(0.5)

    def test_all_failed_means_none_not_zero(self):
        """全部失败 → 均值为 None（不是 0.0，也不是 1.0）"""
        samples = [
            {"ok": False, "cer_raw": None, "cer_normalized": None,
             "exact_normalized": False},
            {"ok": False, "cer_raw": None, "cer_normalized": None,
             "exact_normalized": False},
        ]
        agg = aggregate_ocr(samples)
        assert agg["n_ok"] == 0
        assert agg["n_failed"] == 2
        assert agg["cer_raw_mean"] is None
        assert agg["cer_normalized_mean"] is None
        assert agg["exact_match_rate"] is None
        assert agg["success_rate"] == 0.0

    def test_empty(self):
        agg = aggregate_ocr([])
        assert agg["n_total"] == 0
        assert agg["success_rate"] is None
        assert agg["cer_normalized_mean"] is None
        assert agg["exact_match_rate"] is None

    def test_report_then_aggregate_end_to_end(self):
        r1 = ocr_accuracy_report("# 索引配置\n\n正文内容", "# 索引配置\n\n正文内容")
        r2 = ocr_accuracy_report("# 索引配置\n\n正文内容", "索引配置正文内容")
        r3 = ocr_accuracy_report("# 索引配置\n\n正文内容", "")   # 失败
        agg = aggregate_ocr([r1, r2, r3])
        assert agg["n_failed"] == 1
        assert agg["success_rate"] == pytest.approx(2 / 3)
        # 原样：r2 漏了 "# " 与 "\n\n" 共 4 个字符 / 12 → 4/12
        assert r1["cer_raw"] == 0.0
        assert r2["cer_raw"] == pytest.approx(4 / 12)
        # 均值只由两条成功样本决定：(0 + 4/12) / 2 = 1/6
        assert agg["cer_raw_mean"] == pytest.approx(1 / 6)
        assert agg["cer_normalized_mean"] == pytest.approx(0.0)
        assert agg["exact_match_rate"] == pytest.approx(1.0)


# ================================================================
#  归一化 / CER 的**真正成立**的性质
#  （刻意不锁「norm ≤ raw」——归一化收缩分母，该式并非普适不变量，
#    反例 ref="a*b"/hyp="a*c" 即 raw=1/3 < norm=1/2）
# ================================================================

class TestOCRMetricProperties:

    def test_identical_implies_zero_cer(self):
        """hyp == ref ⇒ 两档 CER 均为 0，且 exact 双真"""
        r = ocr_accuracy_report("混合检索 top_k 权重", "混合检索 top_k 权重")
        assert r["cer_raw"] == 0.0
        assert r["cer_normalized"] == 0.0
        assert r["exact_raw"] is True
        assert r["exact_normalized"] is True

    def test_normalize_idempotent(self):
        raw = "**粗体**，全角（括号）—— 省略……\n| a |\n|---|\n| b |"
        once = normalize_ocr_text(raw)
        assert normalize_ocr_text(once) == once

    @pytest.mark.parametrize("ref,hyp", [
        ("# 索引配置", "#索引配置"),                       # 行首标题符空格
        ("分块大小 800，重叠 100。", "分块大小 800, 重叠 100."),  # 全角↔半角标点
        ("a b c", "a  b   c"),                             # 仅空白多寡
        ("-srv 端口 8000", "- srv 端口 8000"),              # 列表符后的空白
    ])
    def test_format_only_difference_flattens_cer(self, ref, hyp):
        """**限定在格式差异类输入**下 norm <= raw（不是普适不变量）"""
        r = ocr_accuracy_report(ref, hyp)
        assert r["ok"] is True
        assert r["cer_normalized"] <= r["cer_raw"]

    def test_underscore_not_erased_by_default(self):
        """默认保留 `_`：漏识下划线必须被计入（评测不得虚高）"""
        r = ocr_accuracy_report("ingest_queue", "ingestqueue")
        # 漏识 1 个下划线 / 12 字符
        assert r["cer_normalized"] == pytest.approx(1 / 12)
        assert r["exact_normalized"] is False

    def test_heading_marker_asymmetry_regression(self):
        """P1 回归锚点：修前归一化会把 '# 索引配置' 与 '#索引配置' 算成不同"""
        assert normalize_ocr_text("# 标题") == normalize_ocr_text("#标题")
        r = ocr_accuracy_report("# 索引配置", "#索引配置")
        assert r["cer_normalized"] == 0.0
        assert r["exact_normalized"] is True


# ================================================================
#  scripts/ocr_accuracy.py（纯逻辑，不调模型）
# ================================================================

def _load_script_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ocr_accuracy_script", ROOT / "scripts" / "ocr_accuracy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOCRAccuracyScript:

    def test_help_runs(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ocr_accuracy.py"), "--help"],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        assert "--scan-prompt" in proc.stdout
        assert "--scan-dpi" in proc.stdout
        assert "--scan-jpeg" in proc.stdout
        assert "--workers" in proc.stdout
        assert "--json-out" in proc.stdout

    def test_synthetic_samples_cover_content_types(self):
        mod = _load_script_module()
        specs = mod.synthetic_sample_specs()
        assert len(specs) >= 6
        kinds = [s["kind"] for s in specs]
        assert len(set(kinds)) == len(kinds), "内容类型不应重复"
        for s in specs:
            assert s["ground_truth"].strip()
            assert s["lines"]
        # 必须有「数字与表格」类，且其 ground truth 含数字
        digit = next(s for s in specs if "数字" in s["kind"])
        assert any(ch.isdigit() for ch in digit["ground_truth"])
        # 必须有「标题与正文」类，且 ground truth 含 Markdown 标题标记
        heading = next(s for s in specs if "标题" in s["kind"])
        assert heading["ground_truth"].lstrip().startswith("#")

    def test_default_matrix_is_baseline_only(self):
        mod = _load_script_module()
        combos = mod.build_config_matrix(base_prompt="OCR", base_dpi=150)
        assert len(combos) == 1
        c = combos[0]
        assert c["prompt"] == "OCR"
        assert c["render_dpi"] == 150
        assert c["jpeg_quality"] is None

    def test_matrix_single_axis_counts(self):
        mod = _load_script_module()
        # 字面量钉死：候选列表被误改小（如 JPEG 只剩 2 个）时测试必须失败
        assert len(mod.build_config_matrix(scan_jpeg=True)) == 3
        assert len(mod.build_config_matrix(scan_dpi=True)) == 3
        assert len(mod.build_config_matrix(scan_prompt=True)) == 3

    def test_matrix_full_product_and_unique_labels(self):
        mod = _load_script_module()
        combos = mod.build_config_matrix(scan_prompt=True, scan_dpi=True,
                                         scan_jpeg=True)
        # 3 prompt × 3 dpi × 3 压缩 = 27，字面量钉死
        assert len(combos) == 27
        labels = [c["label"] for c in combos]
        assert len(set(labels)) == 27
