"""
纯文本解析器
"""

from typing import List

from src.document.parsers.base import Parser, ParsedContent


class TextParser(Parser):
    """纯文本解析器（.txt）"""

    extensions: List[str] = [".txt"]

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        content = self._decode_text(data).strip()
        # 取首行作为标题（非空行）
        title = None
        first_line = content.split("\n", 1)[0].strip()
        if first_line and len(first_line) <= 60:
            title = first_line
        return ParsedContent(content=content, title=title)