"""
PDF 解析器
使用 PyMuPDF（fitz）逐页提取文本，中文/CJK 支持好、速度快
"""

from typing import List, Optional

from src.document.parsers.base import Parser, ParsedContent


class PDFParser(Parser):
    """PDF 解析器（.pdf）"""

    extensions: List[str] = [".pdf"]

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        import pymupdf  # PyMuPDF（新版模块名；旧版本兼容 import fitz）

        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            return self._parse_document(doc)
        finally:
            # 确保文档句柄一定释放（资源泄漏保护）
            doc.close()

    def _parse_document(self, doc) -> ParsedContent:
        from src.logging_setup import get_logger
        logger = get_logger(__name__)

        # 文档元数据（PDF 内置元信息，惰性属性可能因文档损坏抛异常）
        metadata = {}
        pdf_meta = {}
        try:
            pdf_meta = doc.metadata or {}
        except Exception as e:
            logger.warning("PDF 元数据读取失败: %s", e)
        if pdf_meta.get("title"):
            metadata["pdf_title"] = pdf_meta["title"]
        if pdf_meta.get("author"):
            metadata["pdf_author"] = pdf_meta["author"]

        # 逐页提取文本
        pages = []
        images = []

        for page in doc:
            text = page.get_text("text").strip()
            if text:
                pages.append(text)
            # 提取本页内嵌图片（多模态：上下文字幕方案，说明文字取本页文本）
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    extracted = doc.extract_image(xref)
                    if extracted and extracted.get("image"):
                        caption = (text or "（本页无文字说明）")[:200]
                        images.append({
                            "caption": caption,
                            "bytes": extracted["image"],
                            "ext": extracted.get("ext", "png"),
                        })
                except Exception:
                    continue  # 单张图片提取失败不影响整体

        content = "\n\n".join(pages)

        # 标题：优先文档元数据，其次首页首行
        title: Optional[str] = pdf_meta.get("title") or None
        if not title and content:
            first_line = content.split("\n", 1)[0].strip()
            if first_line and len(first_line) <= 80:
                title = first_line

        return ParsedContent(content=content, title=title, metadata=metadata, images=images)