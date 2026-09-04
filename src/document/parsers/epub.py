"""
EPUB 电子书解析器
使用 ebooklib 读取章节（XHTML），BeautifulSoup 提取正文；标题取书籍元数据
"""

from typing import List

from bs4 import BeautifulSoup

from src.document.parsers.base import Parser, ParsedContent


class EPUBParser(Parser):
    """EPUB 解析器（.epub）"""

    extensions: List[str] = [".epub"]

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        import io

        import ebooklib
        from ebooklib import epub

        book = epub.read_epub(io.BytesIO(data))

        # 章节文本
        parts = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            text = soup.get_text(" ", strip=True)
            if text:
                parts.append(text)
        content = "\n\n".join(parts)

        # 标题：优先书籍元数据
        title = None
        try:
            titles = book.get_metadata("DC", "title")
            if titles and titles[0]:
                title = str(titles[0][0]).strip()
        except Exception:
            pass

        return ParsedContent(content=content, title=title)