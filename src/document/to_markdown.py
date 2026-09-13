"""
统一「任意格式 → Markdown」转换

把入库/归档的一整族文档格式，转成排版正确、标题结构保留的 Markdown：

    .md / .markdown  → 原样（本身就是 Markdown）
    .txt             → 原样，首行作为标题
    .html / .htm     → HTMLToMarkdownConverter（结构感知，含 ASCII 图/代码块围栏）
    .epub            → 逐章节走 HTMLToMarkdownConverter（XHTML 章节）
    .pdf             → PyMuPDF 抽文本，按字号识别标题层级
    .docx            → python-docx，按 style 识别 Heading 1/2/... 层级
    .pptx            → python-pptx，每页一个 ## 幻灯片 N + 文本框/表格
    .png/.jpg/...    → 视觉模型 OCR（需 ocr.enabled=true）

OCR（可选，默认关闭；实现见 src/document/ocr.py）：
    - 扫描版 PDF：文本层字符数低于 ocr.min_text_chars 的页面整页渲染后识别
    - 独立图片  ：图片本体送模型识别，识别结果即正文
    - 内嵌图片  ：pptx/epub 已提取出的图片逐张识别，文字并入正文

设计原则（与 html_to_markdown 一致）：
    - 标题统一成 Markdown `#`/`##`/`###` 层级（而非扁平文本）
    - 文档级标题写入正文开头 `# <title>`，内部结构层级顺延
    - 优先级链：正文结构 > 元数据标题；统一「# H1」为各文件产出 `.md` 的第一行
"""

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.document.html_to_markdown import HTMLToMarkdownConverter
from src.document.md_common import decode_bytes, escape_pipe, render_md_table
from src.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class ConvertResult:
    """转换结果"""
    markdown: str
    title: Optional[str] = None
    source_format: str = ""
    # 文档内嵌图片（R10-3：转换接口回传图片元数据，口径对齐索引路径——
    # 索引侧解析器返回 images 列表、loader 落盘为附件；转换服务无状态不落盘，
    # 故携带 data_base64 由调用方自行处置）。
    # pptx 项：{slide, caption, ext, size_bytes, data_base64}
    # epub  项：{src, ext, size_bytes, data_base64}（src 对应正文 ![](src) 引用）
    images: List[Dict[str, Any]] = field(default_factory=list)


# ================================================================
#  工具：标题拼装
# ================================================================
# 注：表格单元格转义（escape_pipe）与表格拼装（render_md_table）已收敛到
# `src.document.md_common`，与 html_to_markdown / parsers 共用同一份规则。


def prefix_title(
    markdown: str,
    title: Optional[str],
    replace_existing: bool = False,
) -> str:
    """在 Markdown 文本开头插入 `# <title>`

    Args:
        markdown: 正文
        title: 标题；空则不处理
        replace_existing: True 时若正文首行已是一级标题，则**替换**它
            （供「用户显式指定标题」覆盖文档自带标题）；
            False（默认）时保留原有首行标题不动，避免重复前缀。

    Note:
        convert_to_markdown() 内部已按解析出的标题前缀过一次，
        因此调用方若要覆盖标题，必须用 replace_existing=True，
        否则会因为「首行已是 # 」而静默不生效。
    """
    markdown = (markdown or "").strip()
    if not title or not title.strip():
        return markdown

    title = title.strip()

    # 正文首行若已是任意一级标题，说明原文档自带标题结构
    first_line = markdown.split("\n", 1)[0].strip() if markdown else ""
    if first_line.startswith("# "):
        if not replace_existing:
            return markdown
        # 替换首行标题，保留其余正文
        rest = markdown.split("\n", 1)[1].lstrip("\n") if "\n" in markdown else ""
        return f"# {title}\n\n{rest}".strip()

    return f"# {title}\n\n{markdown}".strip()


# ================================================================
#  各格式转换实现
# ================================================================

def _md_to_markdown(content: str, _title: Optional[str]) -> ConvertResult:
    """Markdown 原样返回（已含标题结构）"""
    c = content.strip()
    return ConvertResult(markdown=c, title=_title, source_format="md")


def _txt_to_markdown(content: str, _title: Optional[str]) -> ConvertResult:
    """纯文本：首行作标题

    首行被采纳为标题时，会从正文中移除该行——否则 prefix_title 会在
    正文之前再插一次同名 H1，导致标题在文档里出现两遍。
    """
    c = content.strip()
    title = _title
    if not title:
        first_line, _, rest = c.partition("\n")
        first_line = first_line.strip()
        if first_line and len(first_line) <= 60:
            title = first_line
            c = rest.strip()
    return ConvertResult(markdown=c, title=title, source_format="txt")


def _html_to_markdown(content: str, _title: Optional[str]) -> ConvertResult:
    """HTML → Markdown（结构感知）"""
    conv = HTMLToMarkdownConverter(keep_images=True)
    md = conv.convert(content)
    title = _title or conv.last_title
    return ConvertResult(markdown=md, title=title, source_format="html")


def _epub_to_markdown(data: bytes, source_name: str, ocr=None) -> ConvertResult:
    """EPUB → Markdown：逐章节转 XHTML，书籍标题作 H1、章节 H1 降为 H2；内嵌图片可选 OCR"""
    import io

    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(io.BytesIO(data))

    parts: List[str] = []
    conv = HTMLToMarkdownConverter(keep_images=True)
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        try:
            html = item.get_content().decode("utf-8", errors="replace")
            md = conv.convert(html)
            if md.strip():
                # 章节内的 h1 降级为 h2，让书籍标题独占 h1
                md = _demote_heading_levels(md, base_offset=1)
                parts.append(md)
        except Exception as e:
            logger.warning("EPUB 章节转换失败: %s - %s", source_name, e)
            continue

    # 书籍标题（DC title）
    title = None
    try:
        titles = book.get_metadata("DC", "title")
        if titles and titles[0]:
            title = str(titles[0][0]).strip()
    except Exception:
        pass

    # 图片资源枚举（R10-3）：正文里的 ![](src) 引用指向 epub 内部路径，
    # 落盘后并不解析；这里把图片内容带出，供调用方改写引用或另存附件
    images: List[Dict[str, Any]] = []
    ocr_candidates: List[Dict[str, Any]] = []  # 仅本地用于 OCR（需原始字节）
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        try:
            blob = item.get_content()
            name = item.get_name() or ""
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "bin"
            images.append({
                "src": name,
                "ext": ext,
                "size_bytes": len(blob),
                "data_base64": base64.b64encode(blob).decode("ascii"),
            })
            # 响应载荷只放 base64 字符串（原始 bytes 无法 JSON 序列化），
            # 因此 OCR 另用一份带字节的清单
            ocr_candidates.append({"bytes": blob, "ext": ext, "caption": name})
        except Exception as e:
            logger.warning("EPUB 图片提取失败 [%s]: %s", source_name, e)
            continue

    markdown = "\n\n".join(parts)
    if ocr is not None and ocr_candidates:
        from src.document.ocr import ocr_embedded_images

        blocks = ocr_embedded_images(ocr_candidates, ocr)
        if blocks:
            # epub 正文里图片以 ![](path) 引用，位置不便内联注入 → 统一附在文末
            # （每段 caption 带内部路径，可与正文引用对照）
            markdown = "\n\n".join(([markdown] if markdown else []) + blocks)

    return ConvertResult(markdown=markdown, title=title,
                         source_format="epub", images=images)


def _demote_heading_levels(markdown: str, base_offset: int = 1) -> str:
    """把 Markdown 中所有 # 级标题整体下移 base_offset 级（h1→h2, h2→h3...），封顶 6 级"""
    import re

    def repl(m: "re.Match[str]") -> str:
        n = len(m.group(1))
        return "#" * min(6, n + base_offset) + m.group(2)

    return re.sub(r"^(#{1,6})(\s)", repl, markdown, flags=re.MULTILINE)


def _pdf_to_markdown(data: bytes, source_name: str, ocr=None) -> ConvertResult:
    """PDF → Markdown：按字号识别标题，保留段落；扫描页可选 OCR 回落"""
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        pdf_meta = doc.metadata or {}
        doc_title = (pdf_meta.get("title") or "").strip() or None

        # 收集全文 span 字号，判断正文基准字号与标题阈值
        all_spans: List[Tuple[float, str]] = []
        pages_text: List[List[Tuple[float, str]]] = []
        first_ocr_line: Optional[str] = None
        ocr_budget = max(0, ocr.max_pages_per_doc) if ocr is not None else 0
        ocr_done = 0

        for page in doc:
            d = page.get_text("dict")
            page_parts: List[Tuple[float, str]] = []
            for block in d.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        if text:
                            size = float(span.get("size", 0))
                            all_spans.append((size, text))
                            page_parts.append((size, text))

            # —— 扫描页回落：整页无文本层时渲染成图送 OCR ——
            page_text = " ".join(t for _, t in page_parts).strip()
            if ocr is not None and ocr.looks_scanned(page_text) and ocr_done < ocr_budget:
                ocr_text = ocr.ocr_pdf_page(page, source=source_name)
                ocr_done += 1
                if ocr_text.strip():
                    # 字号记 0：OCR 文本没有排版信息，既不参与正文字号统计，
                    # 也不会被误判成标题（0 < body_size * 1.25 恒成立）
                    page_parts = [(0.0, t) for t in (page_text, ocr_text.strip()) if t]
                    if first_ocr_line is None:
                        for ln in ocr_text.split("\n"):
                            if ln.strip():
                                first_ocr_line = ln.strip()
                                break

            pages_text.append(page_parts)

        # 正文基准字号：全文出现最多的字号
        body_size = 0.0
        if all_spans:
            from collections import Counter
            size_counter = Counter(round(s, 1) for s, _ in all_spans)
            body_size = size_counter.most_common(1)[0][0]

        # 按字号映射标题层级：大于正文基准 1.2x 视为标题
        pages_md: List[str] = []
        for idx, page in enumerate(pages_text, 1):
            lines: List[str] = []
            for size, text in page:
                if body_size > 0 and size >= body_size * 1.25 and len(text) <= 120:
                    lines.append(f"### {text}")
                else:
                    lines.append(text)
            if lines:
                pages_md.append(f"## 第 {idx} 页\n\n" + "\n\n".join(lines))
    finally:
        # 必须保证关闭：解析中途异常时若不关，句柄泄漏（Windows 上还会锁文件）
        doc.close()

    markdown = "\n\n".join(pages_md)
    title = doc_title or None
    if not title and all_spans:
        # 兜底：首个大字号文本作为标题
        max_size = max((s for s, _ in all_spans), default=0)
        for s, t in all_spans:
            if s == max_size and len(t) <= 80:
                title = t
                break
    if not title and first_ocr_line and len(first_ocr_line) <= 80:
        # 全篇扫描件没有任何字号信息（all_spans 为空），退用首段 OCR 文字作标题
        title = first_ocr_line

    return ConvertResult(markdown=markdown, title=title, source_format="pdf")


def _image_to_markdown(data: bytes, source_name: str, ocr=None) -> ConvertResult:
    """图片 → Markdown：视觉模型 OCR（需 ocr.enabled=true）

    未启用 OCR 时**显式报错**，而不是返回空 Markdown：图片转文本没有第二条
    路径，静默返回空串会让调用方以为"转换成功了、只是内容为空"。
    """
    if ocr is None:
        raise ValueError("图片转 Markdown 需要启用 OCR（config: ocr.enabled=true）")

    ext = Path(source_name or "").suffix.lower().lstrip(".") or "png"
    try:
        text = ocr.ocr_image_bytes(data, ext=ext, source=source_name or "image")
    except Exception as e:
        # 与索引路径同构：OCR 异常不冒出（转换接口把它转成明确的 400 提示，
        # 而不是让路由落到笼统的 422「文档格式或内容无效」）
        raise ValueError(f"图片 OCR 失败: {type(e).__name__}: {e}")
    if not text.strip():
        raise ValueError("图片 OCR 未识别出文字（或图片过小/超大被护栏跳过）")

    # 标题取文件名 stem；经 HTTP 路由进来时 source_name 是格式字符串（如 "png"），
    # 那不是标题 → 留空，由调用方显式传 title
    stem = Path(source_name or "").stem
    title = stem if stem and stem.lower() not in _SUPPORTED_FORMATS else None
    return ConvertResult(markdown=text.strip(), title=title, source_format="image")


def _docx_to_markdown(data: bytes, source_name: str, ocr=None) -> ConvertResult:
    """DOCX → Markdown：按 Heading style 识别标题层级

    注：本路径不提取 docx 内嵌图片（索引路径的 DocxParser 才提取），
    因此 ocr 参数在此不接受使用，仅为保持转换器签名统一。
    """
    del ocr  # 本路径无内嵌图片可 OCR
    import io

    from docx import Document

    doc = Document(io.BytesIO(data))

    parts: List[str] = []
    title: Optional[str] = None

    def emit_heading(text: str, level: int) -> str:
        # docx heading 级别 1..6 → Markdown #..######
        return f"{'#' * max(1, min(6, level))} {text}"

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        style_name = para.style.name or ""
        style_id = para.style.style_id or ""

        # 标题样式识别：Heading N / 标题 N / Title
        import re
        m = re.match(r"(?:Heading|标题)\s*([1-6])", style_name, re.IGNORECASE)
        if m:
            level = int(m.group(1))
            parts.append(emit_heading(text, level))
            if title is None:
                title = text
            continue

        if style_id.lower() == "title" or style_name.lower() == "title":
            parts.append(f"# {text}")
            if title is None:
                title = text
            continue

        # 普通段落
        parts.append(text)

    # 表格（逐行收集 → 统一交给 render_md_table 渲染）
    for table in doc.tables:
        rows: List[List[str]] = []
        for row in table.rows:
            # 合并单元格会让 row.cells 返回重复引用（同一 tc 出现多次），
            # 按底层 tc 去重，避免列错位
            seen_cells = set()
            cells: List[str] = []
            for c in row.cells:
                tc_id = id(c._tc)
                if tc_id in seen_cells:
                    continue
                seen_cells.add(tc_id)
                cells.append(escape_pipe(c.text.strip()))
            rows.append(cells)
        md = render_md_table(rows)
        if md:
            parts.append(md)

    markdown = "\n\n".join(parts)
    return ConvertResult(markdown=markdown, title=title, source_format="docx")


def _pptx_to_markdown(data: bytes, source_name: str, ocr=None) -> ConvertResult:
    """PPTX → Markdown：每页 ## 幻灯片 N + 文本框/表格（内嵌图片可选 OCR）"""
    import io

    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))

    slides_md: List[str] = []
    first_text: Optional[str] = None

    images: List[Dict[str, Any]] = []
    ocr_done = 0

    for idx, slide in enumerate(prs.slides, 1):
        # blocks 而非 lines：一块表格是多行 Markdown，必须整体作为一个块，
        # 若按行塞进列表再用 "\n\n" 连接，表格会被空行切断、渲染不成表格
        blocks: List[str] = []
        slide_texts: List[str] = []  # 本页已见文字（图片 caption 语义与索引路径一致）
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs).strip()
                    if text:
                        blocks.append(text)
                        slide_texts.append(text)
                        if first_text is None and len(text) <= 100:
                            first_text = text
            if getattr(shape, "has_table", False):
                table = shape.table
                # 全表单元格收集后统一渲染：既补齐列宽（避免塌陷错位），
                # 也补上 `| --- |` 分隔行（此前缺失 → Obsidian 不认这是表格）
                raw_rows = [
                    [escape_pipe(c.text.strip()) for c in row.cells]
                    for row in table.rows
                ]
                table_md = render_md_table(raw_rows)
                if table_md:
                    blocks.append(table_md)
            # 图片（R10-3）：旧实现静默丢弃图片形状；与 PptxParser 同款
            # 判定与 caption 语义（本页已见文字拼接，截 200 字）
            if "PICTURE" in str(getattr(shape, "shape_type", "")):
                try:
                    img = shape.image
                    images.append({
                        "slide": idx,
                        "caption": ("；".join(slide_texts) or "（本页无文字说明）")[:200],
                        "ext": getattr(img, "ext", "png"),
                        "size_bytes": len(img.blob),
                        "data_base64": base64.b64encode(img.blob).decode("ascii"),
                    })
                    # OCR 文字就插在本页文本框之后（比统一堆到文末更贴合阅读位置）；
                    # 单文档上限与索引路径共用 ocr.max_images_per_doc
                    if ocr is not None and ocr_done < ocr.max_images_per_doc:
                        from src.document.ocr import ocr_block

                        ocr_done += 1
                        block = ocr_block(
                            {"bytes": img.blob, "ext": getattr(img, "ext", "png"),
                             "caption": f"幻灯片 {idx}"},
                            ocr,
                        )
                        if block:
                            blocks.append(block)
                except Exception:
                    continue
        if blocks:
            slides_md.append(f"## 幻灯片 {idx}\n\n" + "\n\n".join(blocks))

    markdown = "\n\n".join(slides_md)
    title = first_text if first_text else None
    return ConvertResult(markdown=markdown, title=title,
                         source_format="pptx", images=images)


# ================================================================
#  分发：按格式字符串或扩展名路由
# ================================================================

# 格式标识（小写，无点）→ 处理函数
# 二进制格式（pdf/docx/pptx/epub/图片）函数签名为 (bytes, source_name, ocr_client)
# 文本格式（md/txt/html）函数签名为 (str, title)

_TEXT_CONVERTERS: Dict[str, Callable[[str, Optional[str]], ConvertResult]] = {
    "md": _md_to_markdown,
    "markdown": _md_to_markdown,
    "txt": _txt_to_markdown,
    "text": _txt_to_markdown,
    "html": _html_to_markdown,
    "htm": _html_to_markdown,
}

_BINARY_CONVERTERS: Dict[str, Callable[..., ConvertResult]] = {
    "pdf": _pdf_to_markdown,
    "docx": _docx_to_markdown,
    "pptx": _pptx_to_markdown,
    "epub": _epub_to_markdown,
    # 图片格式：需 ocr.enabled=true，否则显式报错（见 _image_to_markdown）
    "png": _image_to_markdown,
    "jpg": _image_to_markdown,
    "jpeg": _image_to_markdown,
    "webp": _image_to_markdown,
    "gif": _image_to_markdown,
    "bmp": _image_to_markdown,
    "tiff": _image_to_markdown,
    "tif": _image_to_markdown,
}

_SUPPORTED_FORMATS = set(_TEXT_CONVERTERS) | set(_BINARY_CONVERTERS)


def _normalize_format(fmt: str) -> str:
    """把扩展名/格式名（可带点）归一化为小写无点格式标识"""
    return (fmt or "").strip().lstrip(".").lower()


def _resolve_ocr_client():
    """按当前配置解析 OCR 客户端（配置未加载 / 未启用 → None）"""
    from src.document import ocr

    return ocr.get_ocr_client()


def convert_to_markdown(data: bytes, fmt: str, source_name: str = "",
                        ocr_client=None) -> ConvertResult:
    """
    统一入口：任意格式字节 → Markdown

    Args:
        data: 文件字节 / 文本编码后的字节
        fmt: 格式标识（支持带点：.pdf / pdf 均可）
        source_name: 来源文件名（用于错误提示与部分标题兜底）
        ocr_client: 显式注入的 OCR 客户端（测试用桩）；
            None = 按当前配置解析（ocr.enabled=false 时即无 OCR）

    Returns:
        ConvertResult

    Raises:
        ValueError: 不支持的格式 / 图片格式未启用 OCR
    """
    fmt = _normalize_format(fmt)
    if fmt not in _SUPPORTED_FORMATS:
        raise ValueError(f"不支持的格式: {fmt}（支持: {', '.join(sorted(_SUPPORTED_FORMATS))}）")

    # 文本类：解码后调用（编码回退链统一在 md_common.decode_bytes）
    if fmt in _TEXT_CONVERTERS:
        text = decode_bytes(data, source_name)
        result = _TEXT_CONVERTERS[fmt](text, None)
    else:
        ocr = ocr_client if ocr_client is not None else _resolve_ocr_client()
        result = _BINARY_CONVERTERS[fmt](data, source_name, ocr)

    # 统一：开头加 # 标题（若转换结果有 title）
    if result.title:
        result.markdown = prefix_title(result.markdown, result.title)

    return result


def convert_file_to_markdown(path: str) -> ConvertResult:
    """
    从文件路径转换（按扩展名自动路由）

    Args:
        path: 文件路径

    Returns:
        ConvertResult
    """
    p = Path(path)
    data = p.read_bytes()
    fmt = p.suffix.lstrip(".").lower()
    return convert_to_markdown(data, fmt, source_name=p.name)


def supported_formats() -> List[str]:
    """返回支持的格式列表（小写无点，排序）"""
    return sorted(_SUPPORTED_FORMATS)
