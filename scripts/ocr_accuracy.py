#!/usr/bin/env python3
"""
OCR 识别率评测脚本（视觉模型，默认 OvisOCR2）

用途：`scripts/ocr_smoke.py` 用子串断言**验接线**（"出没出字"），
`test/test_ocr_integration.py` 全部打桩；两者都不度量**认得多准**。
本脚本用**合成测试集**（PIL 渲染已知文本，ground truth 即渲染前字符串，
零标注成本）给出字符级识别率：原样 CER 与归一化 CER 两档、完全正确率、
成功率，并支持对提示词 / 渲染分辨率 / 压缩方式的参数矩阵扫描。
一句话：冒烟验接线，本脚本验精度。

覆盖内容类型（每类一张样本）：纯中文、纯英文、中英混排、数字与表格、
标题+正文（结构保真）、小字号密排。

用法：
    # 基线（当前配置）：6 张样本，默认 2 并发
    python scripts/ocr_accuracy.py

    # 扫描提示词 × 分辨率（每种组合跑完整测试集，调真实模型较慢）
    python scripts/ocr_accuracy.py --scan-prompt --scan-dpi --workers 3

    # 只扫压缩：PNG / JPEG q85 / q60
    python scripts/ocr_accuracy.py --scan-jpeg

    # 落盘 JSON 供跨版本对比，并覆盖服务地址
    python scripts/ocr_accuracy.py --json-out out/ocr_baseline.json \
        --base-url http://127.0.0.1:8000/v1

    # 放开阈值（探索性扫描时）
    python scripts/ocr_accuracy.py --scan-dpi --max-cer-normalized 1.0

注意：
    - 走**真实 OCRClient**（`OCRClient(**kwargs)` 显式构造），不写回
      config/settings.yaml；base_url / model 等默认值读自 config_manager。
    - 冷启动首次调用需加载模型（实测约 60s），热态单张约 4s；脚本默认
      2 并发（oMLX 同时在服务问答，别打爆）。
    - 退出码非零表示未达阈值（--min-success-rate / --max-cer-normalized）；
      扫描模式下任一组合未达标即报错，可用阈值开关放宽。
"""

import argparse
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.metrics import aggregate_ocr, ocr_accuracy_report  # noqa: E402

# 中文字体候选（照抄 scripts/ocr_smoke.py，已验证可用）
CN_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

# dpi 轴以 BASE_DPI 为 1.0 基准（与 ocr.render_dpi 默认值一致）
BASE_DPI = 150

# 提示词扫描候选（对应 OCRClient.prompt）
PROMPT_CANDIDATES = [
    "OCR",
    "请识别图中的所有文字，保持原有排版与层级",
    "OCR the image and output markdown",
]
# 渲染分辨率扫描候选（对应 ocr.render_dpi；本脚本以图像缩放实现）
DPI_CANDIDATES = [150, 200, 300]
# 压缩扫描候选：None = PNG 无损；数字 = JPEG 质量
JPEG_CANDIDATES: List[Optional[int]] = [None, 85, 60]

# 一行文本的「文本 + 字号」；字号可省略（用默认字号）
Line = Union[str, Tuple[str, int]]


# ================================================================
#  素材渲染
# ================================================================

def _cn_font(size: int = 30, font_path: Optional[str] = None):
    """加载中文字体。优先用 font_path，否则走 CN_FONT_CANDIDATES。"""
    from PIL import ImageFont

    candidates: List[str] = []
    if font_path:
        candidates.append(font_path)
    candidates.extend(CN_FONT_CANDIDATES)
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    raise RuntimeError(f"找不到可用的中文字体，试过: {candidates}")


def render_text_image(lines: Sequence[Line], font_size: int = 28,
                      size: Tuple[int, int] = (900, 520),
                      dpi: int = BASE_DPI,
                      font_path: Optional[str] = None) -> bytes:
    """把若干行文字渲染成一张白底黑字 PNG（模拟截图 / 扫描页）。

    Args:
        lines: 文本行；每项可为 str（用 font_size），或 (str, size) 元组以
            单独指定该行字号（用于标题 / 正文字号混排）
        font_size: 默认字号（像素 @ BASE_DPI）
        size: 基准画布尺寸（像素 @ BASE_DPI）
        dpi: 渲染分辨率；相对 BASE_DPI 等比放大字号与画布，dpi=300 即字号
            与画布都 ×2（像素密度翻倍），用于扫描分辨率维度
        font_path: 指定字体文件；None = 走 CN_FONT_CANDIDATES

    Returns:
        PNG 字节
    """
    from PIL import Image, ImageDraw

    scale = dpi / BASE_DPI
    canvas = (max(1, int(round(size[0] * scale))),
              max(1, int(round(size[1] * scale))))
    img = Image.new("RGB", canvas, "white")
    draw = ImageDraw.Draw(img)
    margin = int(round(30 * scale))
    y = margin
    for item in lines:
        text, fs = item if isinstance(item, tuple) else (item, font_size)
        px_font_size = max(8, int(round(fs * scale)))
        draw.text((margin, y), text, font=_cn_font(px_font_size, font_path),
                  fill="black")
        # 行高按字号推导：小字号密排不重叠、大字号有呼吸
        y += int(round(px_font_size * 1.8))
        if y > canvas[1] - margin:
            break
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def encode_jpeg(png_bytes: bytes, quality: int) -> bytes:
    """把 PNG 字节转成 JPEG（quality 1~95），模拟压缩传输对识别的影响。"""
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=int(quality))
        return buf.getvalue()


# ================================================================
#  合成测试集（纯描述，可离线单测）
# ================================================================

def synthetic_sample_specs() -> List[Dict[str, Any]]:
    """合成测试集的**纯描述**（不含像素，供渲染与离线单测）。

    每项键：
        id / kind / lines / ground_truth / font_size / size
    `ground_truth` 恒等于渲染前文本（lines 以 \\n 连接；标题样本显式给出，
    以便把标题层级的 Markdown 标记纳入 ground truth）。
    """
    specs: List[Dict[str, Any]] = []

    def add(sample_id: str, kind: str, lines: Sequence[Line], *,
            font_size: int = 28, size: Tuple[int, int] = (900, 520),
            ground_truth: Optional[str] = None) -> None:
        specs.append({
            "id": sample_id,
            "kind": kind,
            "lines": list(lines),
            "font_size": font_size,
            "size": size,
            "ground_truth": ground_truth if ground_truth is not None else
                            "\n".join(l[0] if isinstance(l, tuple) else l for l in lines),
        })

    # 1. 纯中文段落（含标点）
    add("zh-paragraph", "纯中文段落", [
        "知识库索引配置说明",
        "分块大小设置为 800 个字符，重叠 100 个字符。",
        "检索时先做混合召回，再用重排模型精排候选。",
    ])

    # 2. 纯英文段落
    add("en-paragraph", "纯英文段落", [
        "Configuration Guide",
        "The chunk size is 800 characters with 100 characters overlap.",
        "Retrieval combines BM25 and dense vectors before reranking.",
    ])

    # 3. 中英混排（技术文档风格，含 IngestQueue 这类标识符）
    add("mixed-tech", "中英混排", [
        "IngestQueue 负责文档入库，支持增量索引。",
        "每篇文档 chunk 后写入 VectorStore 并落库。",
        "watchdog 检测到文件变化时重新入队。",
    ])

    # 4. 数字与表格（数字是 OCR 高频失分点）
    add("numbers-table", "数字与表格", [
        "服务端口 8000，请求超时 120 秒。",
        "召回率 0.85，精确率 0.72。",
        "备份日期 2024-03-15，保留 7 天。",
    ])

    # 5. 标题 + 正文（结构保真：验证是否保留标题层级 → 影响原样 CER）
    add("heading-body", "标题与正文", [
        ("# 索引配置", 40),
        ("分块大小 800，重叠 100。", 26),
        ("若标题层级丢失，原样 CER 升高而归一化 CER 不变。", 22),
    ], ground_truth="# 索引配置\n分块大小 800，重叠 100。\n"
                    "若标题层级丢失，原样 CER 升高而归一化 CER 不变。")

    # 6. 小字号密排文本
    add("dense-small", "小字号密排", [
        "本章介绍混合检索的参数调优方法，包括 BM25 权重与向量权重的",
        "取值建议，以及重排模型在候选集上的截断策略。实践中需要根据",
        "语料规模动态调整 top_k 与重排条数，兼顾召回率与响应延迟。",
    ], font_size=18, size=(1000, 460))

    return specs


# ================================================================
#  配置矩阵（纯逻辑，可离线单测）
# ================================================================

def build_config_matrix(*, scan_prompt: bool = False, scan_dpi: bool = False,
                        scan_jpeg: bool = False,
                        base_prompt: str = "OCR",
                        base_dpi: int = BASE_DPI) -> List[Dict[str, Any]]:
    """生成参数矩阵：每个组合是一个 dict。

    Args:
        scan_prompt / scan_dpi / scan_jpeg: 是否展开对应轴；全关 → 只跑基线
        base_prompt: 基线提示词；非扫描时作为该轴唯一取值
        base_dpi: 基线渲染分辨率；非扫描时作为该轴唯一取值

    Returns:
        list[dict]，每项键：label / prompt / render_dpi / jpeg_quality
    """
    prompts = PROMPT_CANDIDATES if scan_prompt else [base_prompt]
    dpis = DPI_CANDIDATES if scan_dpi else [base_dpi]
    jpegs = JPEG_CANDIDATES if scan_jpeg else [None]

    combos: List[Dict[str, Any]] = []
    for pi, prompt in enumerate(prompts):
        for dpi in dpis:
            for jpeg in jpegs:
                tag = "png" if jpeg is None else f"jpeg{jpeg}"
                combos.append({
                    "label": f"prompt[{pi}] dpi{dpi} {tag}",
                    "prompt": prompt,
                    "render_dpi": dpi,
                    "jpeg_quality": jpeg,
                })
    return combos


# ================================================================
#  执行
# ================================================================

def resolve_defaults(args: argparse.Namespace) -> Dict[str, Any]:
    """解析连接默认值：命令行 > 配置文件 > 内置兜底。绝不写回配置文件。"""
    from src.config import AppConfig, config_manager

    try:
        cfg = config_manager.config
    except Exception:
        try:
            cfg = config_manager.load()
        except Exception:
            cfg = AppConfig()

    ocr = cfg.ocr
    base_url = (args.base_url or ocr.base_url or cfg.omlx.base_url or "").strip()
    prompt = args.prompt or ocr.prompt
    timeout = args.timeout if args.timeout is not None else ocr.timeout
    return {
        "base_url": base_url,
        "model": args.model or ocr.model,
        "api_key": ocr.api_key or cfg.omlx.api_key or "",
        "timeout": float(timeout),
        "prompt": prompt,
        "render_dpi": int(ocr.render_dpi),
    }


def _build_client(defaults: Dict[str, Any], combo: Dict[str, Any]):
    """按组合覆盖项显式构造 OCRClient（不用 get_ocr_client，便于临时改参数）。

    Note:
        `render_dpi` 仅对 PDF 整页渲染（ocr_pdf_page）生效；本脚本走
        `ocr_image_bytes`，分辨率扫描因此以**图像缩放**实现（见
        `render_text_image` 的 dpi 参数），此处同时把 render_dpi 透传给
        客户端以保持口径一致（对图片路径无副作用）。
    """
    from src.document.ocr import OCRClient

    return OCRClient(
        model=defaults["model"],
        base_url=defaults["base_url"],
        api_key=defaults["api_key"],
        timeout=defaults["timeout"],
        prompt=combo["prompt"],
        render_dpi=combo["render_dpi"],
    )


def _evaluate_sample(client, spec: Dict[str, Any], dpi: int,
                     jpeg_quality: Optional[int]) -> Dict[str, Any]:
    """识别单张样本并产出度量 dict（含耗时与原文）。"""
    png = render_text_image(spec["lines"], font_size=spec["font_size"],
                            size=spec["size"], dpi=dpi)
    if jpeg_quality is None:
        data, ext = png, "png"
    else:
        data, ext = encode_jpeg(png, jpeg_quality), "jpg"

    t0 = time.time()
    text = client.ocr_image_bytes(data, ext=ext, source=spec["id"])
    elapsed = time.time() - t0

    report = ocr_accuracy_report(spec["ground_truth"], text)
    report.update({
        "id": spec["id"],
        "kind": spec["kind"],
        "ground_truth": spec["ground_truth"],
        "hypothesis": text,
        "elapsed_s": elapsed,
        "render_dpi": dpi,
        "jpeg_quality": jpeg_quality,
    })
    return report


def evaluate_combo(client, specs: Sequence[Dict[str, Any]], dpi: int,
                   jpeg_quality: Optional[int],
                   workers: int) -> List[Dict[str, Any]]:
    """并发跑完一整个测试集，逐张打印进度行，返回按样本顺序排列的结果。"""
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_evaluate_sample, client, spec, dpi, jpeg_quality): spec
                   for spec in specs}
        for fut in as_completed(futures):
            spec = futures[fut]
            try:
                report = fut.result()
            except Exception as e:   # 结构性兜底：单张异常不拖垮整轮
                report = ocr_accuracy_report(spec["ground_truth"], "")
                report.update({
                    "id": spec["id"], "kind": spec["kind"],
                    "ground_truth": spec["ground_truth"], "hypothesis": "",
                    "elapsed_s": 0.0, "render_dpi": dpi,
                    "jpeg_quality": jpeg_quality,
                    "error": f"{type(e).__name__}: {e}",
                })
            results.append(report)
            cer = report["cer_normalized"]
            cer_s = "n/a" if cer is None else f"{cer:.3f}"
            flag = "OK  " if report["ok"] else "FAIL"
            print(f"    [{flag}] {report['id']:<14} {report['elapsed_s']:6.2f}s  "
                  f"cer_norm={cer_s:<7} {report['kind']}")

    order = {spec["id"]: i for i, spec in enumerate(specs)}
    results.sort(key=lambda r: order.get(r["id"], 1 << 30))
    return results


# ================================================================
#  报告输出
# ================================================================

def _display_width(text: str) -> int:
    """按东亚宽字符占 2 列估算显示宽度（对齐用）。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    pad = max(0, width - _display_width(text))
    return text + " " * pad if align == "left" else " " * pad + text


def _fmt(value: Optional[float], digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def print_combo_table(rows: Sequence[Tuple[str, Dict[str, Any]]]) -> None:
    """打印各配置组合的对比表（行 = 组合，列 = 成功率/两档 CER/完全正确率）。"""
    cols = [("配置组合", 26, "left"), ("成功率", 8, "right"),
            ("原样CER", 9, "right"), ("归一化CER", 11, "right"),
            ("完全正确率", 11, "right"), ("失败", 6, "right")]
    header = " ".join(_pad(name, w, a) for name, w, a in cols)
    print(header)
    print("-" * _display_width(header))
    for label, agg in rows:
        cells = [
            _pad(label, 26, "left"),
            _pad(_fmt(agg["success_rate"], 2), 8, "right"),
            _pad(_fmt(agg["cer_raw_mean"]), 9, "right"),
            _pad(_fmt(agg["cer_normalized_mean"]), 11, "right"),
            _pad(_fmt(agg["exact_match_rate"], 2), 11, "right"),
            _pad(str(agg["n_failed"]), 6, "right"),
        ]
        print(" ".join(cells))


def print_combo_detail(combo: Dict[str, Any],
                       results: Sequence[Dict[str, Any]]) -> None:
    """打印单组配置下的逐样本明细。"""
    jpeg = "png" if combo["jpeg_quality"] is None else f"jpeg{combo['jpeg_quality']}"
    print(f"  · label={combo['label']}  prompt={combo['prompt']!r}  "
          f"dpi={combo['render_dpi']}  {jpeg}")
    for r in results:
        line = (f"      {r['id']:<14} ref={r['ref_chars']:>4} hyp={r['hyp_chars']:>4}  "
                f"raw={_fmt(r['cer_raw']):>7} norm={_fmt(r['cer_normalized']):>7}  "
                f"exact={'Y' if r['exact_normalized'] else 'n'}  "
                f"ok={'Y' if r['ok'] else 'n'}")
        if r.get("error"):
            line += f"  error={r['error']}"
        print(line)


def _print_aggregate(agg: Dict[str, Any]) -> None:
    print(f"    成功率={_fmt(agg['success_rate'], 2)}  "
          f"原样CER={_fmt(agg['cer_raw_mean'])}  "
          f"归一化CER={_fmt(agg['cer_normalized_mean'])}  "
          f"完全正确率={_fmt(agg['exact_match_rate'], 2)}  "
          f"(ok={agg['n_ok']}/{agg['n_total']}, 失败={agg['n_failed']})")


# ================================================================
#  入口
# ================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="OCR 识别率评测（合成测试集 + 参数矩阵扫描）")
    ap.add_argument("--base-url", default=None,
                    help="视觉服务地址；默认用配置 ocr.base_url 或 omlx.base_url")
    ap.add_argument("--model", default=None, help="模型名；默认用配置 ocr.model")
    ap.add_argument("--prompt", default=None, help="基线提示词；默认用配置 ocr.prompt")
    ap.add_argument("--timeout", type=float, default=None,
                    help="单张请求超时（秒）；默认用配置 ocr.timeout")
    ap.add_argument("--workers", type=int, default=2,
                    help="并发度（默认 2，保守，避免打爆 oMLX）")
    ap.add_argument("--scan-prompt", action="store_true",
                    help="扫描提示词候选（对比指令风格）")
    ap.add_argument("--scan-dpi", action="store_true",
                    help="扫描渲染分辨率 150/200/300")
    ap.add_argument("--scan-jpeg", action="store_true",
                    help="扫描压缩：PNG / JPEG q85 / q60")
    ap.add_argument("--json-out", default=None,
                    help="把完整结果写入该 JSON 路径（含每条样本 ground truth/识别结果/指标/耗时）")
    ap.add_argument("--min-success-rate", type=float, default=1.0,
                    help="成功率下限（默认 1.0）")
    ap.add_argument("--max-cer-normalized", type=float, default=0.2,
                    help="归一化 CER 上限（默认 0.2）")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()

    defaults = resolve_defaults(args)
    if not defaults["base_url"]:
        print("错误：未配置 base_url（--base-url / ocr.base_url / omlx.base_url 均空）")
        return 2

    specs = synthetic_sample_specs()
    combos = build_config_matrix(
        scan_prompt=args.scan_prompt, scan_dpi=args.scan_dpi,
        scan_jpeg=args.scan_jpeg, base_prompt=defaults["prompt"],
        base_dpi=defaults["render_dpi"],
    )

    print(f"合成测试集：{len(specs)} 张；配置组合：{len(combos)} 组；"
          f"并发：{max(1, args.workers)}")
    print(f"服务：{defaults['base_url']}  model={defaults['model']}")
    print("（冷启动首次调用需加载模型，可能约 60s；热态单张约 4s）\n")

    failures: List[str] = []
    table_rows: List[Tuple[str, Dict[str, Any]]] = []
    combos_out: List[Dict[str, Any]] = []

    for combo in combos:
        print(f"=== {combo['label']} ===")
        client = _build_client(defaults, combo)
        results = evaluate_combo(client, specs, combo["render_dpi"],
                                 combo["jpeg_quality"], args.workers)
        agg = aggregate_ocr(results)
        _print_aggregate(agg)
        print_combo_detail(combo, results)
        table_rows.append((combo["label"], agg))

        if agg["success_rate"] is None or agg["success_rate"] < args.min_success_rate:
            failures.append(
                f"{combo['label']}: 成功率 {_fmt(agg['success_rate'], 2)} "
                f"< {args.min_success_rate}")
        if (agg["cer_normalized_mean"] is not None
                and agg["cer_normalized_mean"] > args.max_cer_normalized):
            failures.append(
                f"{combo['label']}: 归一化 CER "
                f"{agg['cer_normalized_mean']:.3f} > {args.max_cer_normalized}")

        combos_out.append({"label": combo["label"], "params": combo,
                           "aggregate": agg, "samples": results})
        print()

    print("=" * 64)
    print("对比表")
    print_combo_table(table_rows)

    if args.json_out:
        payload = {
            "meta": {
                "base_url": defaults["base_url"],
                "model": defaults["model"],
                "default_prompt": defaults["prompt"],
                "default_render_dpi": defaults["render_dpi"],
                "workers": max(1, args.workers),
                "thresholds": {
                    "min_success_rate": args.min_success_rate,
                    "max_cer_normalized": args.max_cer_normalized,
                },
                "n_samples": len(specs),
                "n_combos": len(combos),
            },
            "combos": combos_out,
        }
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"\n完整结果已写入: {out}")

    if failures:
        print(f"\n结论：{len(failures)} 项未达标")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n结论：全部达标 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
