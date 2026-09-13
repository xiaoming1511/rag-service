#!/usr/bin/env python3
"""
OCR 真机冒烟脚本（视觉模型，默认 OvisOCR2）

用途：`test/test_ocr_integration.py` 全部用桩，验证的是**接线**而非模型效果。
换模型、升级 oMLX、或改了 OCR 提示词之后，跑这个脚本确认「真机确实认得出字」。

覆盖四条链路：
    1. 独立图片 → `convert_to_markdown`（/v1/convert 同一条转换器）
    2. 多页扫描 PDF → 索引路径（`PDFParser` 扫描页回落）
    3. docx 内嵌图片 → 索引路径（loader 把 OCR 文字并入正文）
    4. 对照：OCR 关闭时，图片转换应报错、扫描件应被当空文档跳过

用法：
    # 前置：oMLX 已加载 OvisOCR2（curl -s $BASE/v1/models 可确认）
    python scripts/ocr_smoke.py

    # 指定服务地址 / 保留中间产物
    python scripts/ocr_smoke.py --base-url http://127.0.0.1:8000/v1 --keep

注意：
    - 只在**内存里**改配置，不写回 config/settings.yaml；
    - 冷启动首次调用需要加载模型（实测约 56s），热态约 4s/页，请耐心等待。
"""

import argparse
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CN_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _cn_font(size=30):
    from PIL import ImageFont

    for path in CN_FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise RuntimeError(f"找不到可用的中文字体，试过: {CN_FONT_CANDIDATES}")


def make_page_png(lines, size=(900, 400)) -> bytes:
    """把若干行文字渲染成一张图片（模拟截图 / 扫描页）"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = _cn_font()
    y = 30
    for line in lines:
        draw.text((30, y), line, font=font, fill="black")
        y += 50
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_scanned_pdf(pages) -> bytes:
    """每页放一张文字图片、且不写文本层 —— 即扫描件"""
    import pymupdf

    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page(width=900, height=400)
        page.insert_image(pymupdf.Rect(0, 0, 900, 400), stream=make_page_png(lines))
    data = doc.tobytes()
    doc.close()
    return data


def enable_ocr(enabled=True, base_url=None, **overrides):
    """只在内存里改全局配置（不落盘）"""
    from src.config import AppConfig, config_manager

    base = (config_manager._config or AppConfig()).model_dump()
    ocr = {**base.get("ocr", {}), "enabled": enabled, **overrides}
    if base_url:
        ocr["base_url"] = base_url
    base["ocr"] = ocr
    config_manager._config = AppConfig(**base)
    return config_manager.config


def main() -> int:
    ap = argparse.ArgumentParser(description="OCR 真机冒烟（OvisOCR2）")
    ap.add_argument("--base-url", default=None,
                    help="视觉服务地址；默认用配置里的 omlx.base_url")
    ap.add_argument("--model", default=None, help="模型名；默认用配置里的 ocr.model")
    ap.add_argument("--work-dir", default=None, help="中间产物目录；默认临时目录")
    ap.add_argument("--keep", action="store_true", help="保留中间产物")
    args = ap.parse_args()

    import tempfile

    from src.document.loader import DocumentLoader
    from src.document.to_markdown import convert_to_markdown

    overrides = {}
    if args.model:
        overrides["model"] = args.model
    work = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="ocr_smoke_"))
    work.mkdir(parents=True, exist_ok=True)
    failures = []

    def check(label, ok, note=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + note) if note else ''}")
        if not ok:
            failures.append(label)

    png = make_page_png(["知识库索引配置说明", "分块大小 800，重叠 100。", "端口号 8000。"])
    pdf_bytes = make_scanned_pdf([
        ["第一页：索引配置", "分块大小 800，重叠 100。"],
        ["第二页：检索参数", "top_k 等于 5，重排保留 5 条。"],
    ])
    scan_path = work / "scan.pdf"
    scan_path.write_bytes(pdf_bytes)

    # ---------------- 对照：OCR 关闭 ----------------
    print("\n=== 对照：OCR 关闭 ===")
    enable_ocr(False)
    try:
        convert_to_markdown(png, "png", "配置截图.png")
        check("图片转换应报错", False, "未报错")
    except ValueError as e:
        check("图片转换报错且提示 OCR", "OCR" in str(e), str(e))

    loader_off = DocumentLoader(source_dirs=[str(work)], extensions=[".pdf"],
                                attachment_dir=str(work / "att_off"))
    check("扫描件被当空文档跳过（与接入前一致）",
          loader_off.load_single(str(scan_path)) is None)

    # ---------------- 开启 OCR ----------------
    print("\n=== OCR 已启用（首次调用含模型加载，请耐心等待）===")
    cfg = enable_ocr(True, base_url=args.base_url, **overrides)
    print(f"  model={cfg.ocr.model}  base_url={cfg.ocr.base_url or cfg.omlx.base_url}")

    t0 = time.time()
    try:
        result = convert_to_markdown(png, "png", "配置截图.png")
        print(f"  [1] 独立图片转换  {time.time() - t0:.1f}s  title={result.title!r}")
        print("      " + result.markdown.replace("\n", "\n      ")[:300])
        check("独立图片 OCR 出字", "800" in result.markdown or "端口" in result.markdown)
    except ValueError as e:
        check("独立图片 OCR 出字", False, str(e))

    t0 = time.time()
    loader_on = DocumentLoader(source_dirs=[str(work)], extensions=[".pdf"],
                               attachment_dir=str(work / "att_on"))
    doc = loader_on.load_single(str(scan_path))
    print(f"  [2] 扫描版 PDF  {time.time() - t0:.1f}s  ocr_pages={getattr(doc, 'metadata', {}).get('ocr_pages') if doc else None}")
    if doc:
        print("      " + doc.content.replace("\n", "\n      ")[:300])
    check("扫描页 OCR 出字", bool(doc and doc.content.strip()))

    from docx import Document as DocxDocument

    d = DocxDocument()
    d.add_paragraph("这是一段普通 Word 正文，用于验证 OCR 文字是追加而非替换。")
    d.add_picture(io.BytesIO(make_page_png(["图 1：备份策略", "每日 02:00 全量，保留 7 天。"])))
    docx_path = work / "with_fig.docx"
    d.save(str(docx_path))

    t0 = time.time()
    loader_docx = DocumentLoader(source_dirs=[str(work)], extensions=[".docx"],
                                 attachment_dir=str(work / "att_docx"))
    d_doc = loader_docx.load_single(str(docx_path))
    print(f"  [3] docx 内嵌图片  {time.time() - t0:.1f}s  "
          f"附件={len(d_doc.metadata.get('images', []))}  ocr_images={d_doc.metadata.get('ocr_images')}")
    print("      " + d_doc.content.replace("\n", "\n      ")[:300])
    check("docx 内嵌图片 OCR 并入正文", "备份策略" in d_doc.content)
    check("正文原本内容未被覆盖", "普通 Word 正文" in d_doc.content)

    print(f"\n中间产物目录: {work}{'（--keep 保留）' if args.keep else '（可删除）'}")
    if failures:
        print(f"\n结论：{len(failures)} 项未通过 -> {failures}")
        return 1
    print("\n结论：全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
