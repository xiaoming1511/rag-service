"""
Word 解析器
使用 python-docx 提取段落与表格文本
"""

from typing import List, Optional

from src.document.parsers.base import Parser, ParsedContent


class DocxParser(Parser):
    """Word 解析器（.docx）"""

    extensions: List[str] = [".docx"]

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        import io

        from docx import Document as DocxDocument

        doc = DocxDocument(io.BytesIO(data))

        parts: List[str] = []

        # 段落
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                parts.append(text)

        # 表格（逐行拼成文本行，便于检索）
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                line = " | ".join(c for c in cells if c)
                if line:
                    parts.append(line)

        content = "\n".join(parts)

        # 提取内嵌图片（多模态：附件存储，说明文字用固定标注）
        images = []
        for shape in doc.inline_shapes:
            try:
                if getattr(shape, "image", None) is not None:
                    images.append({
                        "caption": "Word 文档内嵌图片",
                        "bytes": shape.image.blob,
                        "ext": getattr(shape.image, "ext", "png"),
                    })
            except Exception:
                continue  # 单张图片提取失败不影响整体

        # 标题：首个非空段落（通常为首个标题/第一行）
        title: Optional[str] = None
        for para in doc.paragraphs:
            t = para.text.strip()
            if t:
                title = t if len(t) <= 80 else t[:80]
                break

        return ParsedContent(content=content, title=title, images=images)