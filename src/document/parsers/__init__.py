"""
文档解析器包

多格式支持（决策 D3/D4 + 评审补全）：
- MarkdownParser     .md / .markdown（含 frontmatter 解析）
- TextParser         .txt（UTF-8，中文回退 GB18030）
- PDFParser          .pdf（PyMuPDF，中文/CJK 支持好；可选扫描页 OCR 回落）
- DocxParser         .docx（python-docx，段落 + 表格）
- HTMLParser         .html / .htm（BeautifulSoup 提取正文）
- EPUBParser         .epub（ebooklib 章节解析）
- PptxParser         .pptx（python-pptx 文本框 + 表格）
- ImageParser        .png/.jpg/...（视觉模型 OCR，需 ocr.enabled=true 才有产出）

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
from src.document.parsers.image import ImageParser

# 默认解析器列表（加载器按扩展名自动路由）
#
# 注意：这是**模块级单例**，不持有 OCR 客户端（客户端由各解析器在解析时按
# 当前配置解析）。需要显式注入客户端（测试桩、或明确指定实例）时用
# `build_default_parsers(ocr_client=...)` 另建一组，不要改这份单例。
_PARSER_CLASSES = [
    MarkdownParser,
    TextParser,
    PDFParser,
    DocxParser,
    HTMLParser,
    EPUBParser,
    PptxParser,
    ImageParser,
]


def build_default_parsers(ocr_client=None) -> list:
    """构造一组默认解析器实例，并把 ocr_client 注入给每个解析器"""
    return [cls(ocr_client=ocr_client) for cls in _PARSER_CLASSES]


DEFAULT_PARSERS = build_default_parsers()

__all__ = [
    "Parser",
    "ParsedContent",
    "DEFAULT_PARSERS",
    "build_default_parsers",
    "MarkdownParser",
    "TextParser",
    "PDFParser",
    "DocxParser",
    "HTMLParser",
    "EPUBParser",
    "PptxParser",
    "ImageParser",
]