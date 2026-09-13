#!/usr/bin/env python3
"""
HTML → Markdown 转换 CLI

用途：把导出的 HTML 文档（如 AI 对话导出、网页存档）转换成排版正确的
Markdown 笔记，可直接放入 Obsidian vault。

核心保障：ASCII 架构图 / 代码块会套 ```` ``` ```` 围栏逐字保留，
不会像纯文本抽取那样被压成一行、进而在 Obsidian 里塌成乱码。

用法：
    # 单个文件（默认输出到同目录同名 .md）
    python scripts/html_to_md.py docs/xxx.html

    # 指定输出路径
    python scripts/html_to_md.py docs/xxx.html -o out/xxx.md

    # 批量转换目录
    python scripts/html_to_md.py docs/ --outdir out/ --recursive

    # 预览到终端（不落盘）
    python scripts/html_to_md.py docs/xxx.html --stdout

    # 覆盖已存在文件 / 不保留图片
    python scripts/html_to_md.py docs/xxx.html --force
    python scripts/html_to_md.py docs/xxx.html --no-images
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# 允许直接以脚本方式运行（无需先安装包）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.document.html_to_markdown import HTMLToMarkdownConverter  # noqa: E402


def _convert_one(
    src: Path,
    dst: Optional[Path],
    keep_images: bool,
    force: bool,
    to_stdout: bool,
) -> bool:
    """
    转换单个文件

    Returns:
        bool: 成功 True，跳过/失败 False
    """
    if not src.is_file():
        print(f"❌ 文件不存在: {src}", file=sys.stderr)
        return False

    try:
        data = src.read_bytes()
    except OSError as e:
        print(f"❌ 读取失败 {src}: {e}", file=sys.stderr)
        return False

    converter = HTMLToMarkdownConverter(keep_images=keep_images)
    try:
        md = converter.convert_bytes(data, src.name)
    except Exception as e:  # noqa: BLE001 — CLI 需容错，单文件失败不影响批次
        print(f"❌ 解析失败 {src}: {e}", file=sys.stderr)
        return False

    if not md.strip():
        print(f"⚠️  未提取到内容，已跳过: {src}", file=sys.stderr)
        return False

    if to_stdout:
        print(md, end="")
        return True

    assert dst is not None
    if dst.exists() and not force:
        print(f"⏭️  已存在，跳过（加 --force 覆盖）: {dst}", file=sys.stderr)
        return False

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(md, encoding="utf-8")

    lines = md.splitlines()
    longest = max((len(line) for line in lines), default=0)
    print(f"✅ {src.name} → {dst}  （{len(lines)} 行，{len(md)} 字符，最长行 {longest}）")
    return True


def _iter_inputs(path: Path, recursive: bool) -> List[Path]:
    """收集待转换的 HTML 文件"""
    if path.is_file():
        return [path]
    pattern = "**/*" if recursive else "*"
    files = sorted(
        p for p in path.glob(pattern)
        if p.is_file() and p.suffix.lower() in (".html", ".htm")
    )
    return files


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 HTML 文档转换为排版正确的 Markdown（ASCII 图/代码块用围栏保留）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", help="输入 HTML 文件或目录")
    parser.add_argument("-o", "--output", help="输出 .md 路径（单文件模式；默认同目录同名 .md）")
    parser.add_argument("--outdir", help="输出目录（目录批量模式；默认与源文件同目录）")
    parser.add_argument("-r", "--recursive", action="store_true", help="目录模式下递归子目录")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("--no-images", action="store_true", help="不把 <img> 转成 Markdown 图片")
    parser.add_argument("--stdout", action="store_true", help="输出到终端而不落盘（仅单文件）")
    parser.add_argument("--suffix", default=".md", help="输出扩展名（默认 .md）")

    args = parser.parse_args(argv)

    src_path = Path(args.input).expanduser()
    if not src_path.exists():
        print(f"❌ 输入不存在: {src_path}", file=sys.stderr)
        return 1

    if args.stdout and not src_path.is_file():
        print("❌ --stdout 仅支持单个文件", file=sys.stderr)
        return 1

    targets = _iter_inputs(src_path, args.recursive)
    if not targets:
        print(f"⚠️  未找到 HTML 文件: {src_path}", file=sys.stderr)
        return 1

    if args.output and len(targets) > 1:
        print("❌ -o/--output 仅用于单文件；目录批量请用 --outdir", file=sys.stderr)
        return 1

    ok = 0
    for src in targets:
        if args.stdout:
            dst = None
        elif args.output:
            dst = Path(args.output).expanduser()
        elif args.outdir:
            dst = Path(args.outdir).expanduser() / (src.stem + args.suffix)
        else:
            dst = src.with_suffix(args.suffix)

        if _convert_one(
            src=src,
            dst=dst,
            keep_images=not args.no_images,
            force=args.force,
            to_stdout=args.stdout,
        ):
            ok += 1

    if len(targets) > 1:
        print(f"\n完成：成功 {ok}/{len(targets)}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
