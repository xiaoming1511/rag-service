"""
文档解析器包

多格式支持（决策 D3/D4 + 评审补全）：
- MarkdownParser     .md / .markdown（含 frontmatter 解析）
- TextParser         .txt（UTF-8，中文回退 GB18030）
- PDFParser          .pdf（PyMuPDF，中文/CJK 支持好）
- DocxParser         .docx（python-docx，段落 + 表格）
- HTMLParser         .html / .htm（BeautifulSoup 提取正文）
- EPUBParser         .epub（ebooklib 章节解析）
- PptxParser         .pptx（python-pptx 文本框 + 表格）

统一接口：
- 每个解析器声明支持的扩展名列表 extensions
- parse_file(path) / parse_bytes(data, source_name) 返回 ParsedContent
"""

from src.document.parsers.base import Parser, ParsedContent
from src.document.parsers.markdown import MarkdownParser
from src.document.parsers.text import TextParser
from src.document.parsers.pdf import PDFParser
from src.document.parsers.docx import DocxParser
from src.document.parsers.html import HTMLParser
from src.document.parsers.epub import EPUBParser
from src.document.parsers.pptx import PptxParser

# 默认解析器列表（加载器按扩展名自动路由）
DEFAULT_PARSERS = [
    MarkdownParser(),
    TextParser(),
    PDFParser(),
    DocxParser(),
    HTMLParser(),
    EPUBParser(),
    PptxParser(),
]

__all__ = [
    "Parser",
    "ParsedContent",
    "DEFAULT_PARSERS",
    "MarkdownParser",
    "TextParser",
    "PDFParser",
    "DocxParser",
    "HTMLParser",
    "EPUBParser",
    "PptxParser",
]