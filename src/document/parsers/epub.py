"""
EPUB 电子书解析器
使用 ebooklib 读取章节（XHTML），委托 html_to_markdown 做结构感知转换；标题取书籍元数据

要点（与 HTML 解析器保持一致）：
    章节 XHTML 走 `HTMLToMarkdownConverter`，而不是 `soup.get_text(" ")`。
    后者会把章节内的换行与缩进统一压成空格——`<pre>`、ASCII 架构图、代码块
    全部塌陷成一行。这与 `parsers/html.py` 已经修过的「架构图丢失 / 排版错乱」
    是同一个问题，只是当时 epub 这条路径没有跟着改。

    对照：`to_markdown._epub_to_markdown`（转换接口路径）一直是结构感知的，
    于是同一本 EPUB 走索引路径与走 `/v1/convert` 会拿到不一致的正文——
    索引进去的那份是塌陷过的。现已统一。
"""

from typing import List

from src.document.html_to_markdown import HTMLToMarkdownConverter
from src.document.parsers.base import Parser, ParsedContent


class EPUBParser(Parser):
    """EPUB 解析器（.epub）"""

    extensions: List[str] = [".epub"]

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        import io

        import ebooklib
        from ebooklib import epub

        with io.BytesIO(data) as buf:
            book = epub.read_epub(buf)

        # 章节正文：逐章做结构感知转换（保留标题 / 代码块 / 表格 / ASCII 图）
        conv = HTMLToMarkdownConverter(keep_images=True)
        parts: List[str] = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            try:
                html = item.get_content().decode("utf-8", errors="replace")
            except Exception:
                continue  # 单章解码失败不影响整本
            md = conv.convert(html)
            if md.strip():
                parts.append(md)
        content = "\n\n".join(parts)

        # 标题：优先书籍元数据（DC title）
        title = None
        try:
            titles = book.get_metadata("DC", "title")
            if titles and titles[0]:
                title = str(titles[0][0]).strip()
        except Exception:
            pass

        return ParsedContent(content=content, title=title)
