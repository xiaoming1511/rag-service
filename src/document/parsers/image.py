"""
独立图片解析器（OCR）

把图片文件本身当作一篇文档：交给视觉模型识别其中的文字，识别结果作为正文入库。

启用条件（两个都要满足，缺一不可）：
  1. `ocr.enabled: true`  —— 否则本解析器返回空内容，加载器按「空文档」跳过；
  2. `documents.supported_extensions` 里包含图片扩展名（如 `.png`），
     否则加载器根本不会扫描到这些文件。

为什么 title 取文件名而不是 OCR 首行：
    图片没有可靠的标题语义（截图首行常常是正文断句），而文件名是稳定的、
    用户在 Obsidian 里能直接对照的标识；引用展示用的是 file_name，
    因此取 stem 与其它格式的观感一致。
"""

from pathlib import Path
from typing import List

from src.document.parsers.base import Parser, ParsedContent


class ImageParser(Parser):
    """图片解析器（.png / .jpg / .jpeg / .webp / .gif / .bmp / .tiff / .tif）"""

    extensions: List[str] = [
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif",
    ]

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        from src.logging_setup import get_logger

        logger = get_logger(__name__)

        client = self._resolve_ocr()
        if client is None:
            # OCR 未启用：图片不作为文档产出内容（与接入前行为一致）
            logger.debug("OCR 未启用，跳过图片文件: %s", source_name)
            return ParsedContent(content="", title=None, metadata={})

        path = Path(source_name or "")
        ext = path.suffix.lower().lstrip(".") or "png"

        text = ""
        try:
            text = client.ocr_image_bytes(data, ext=ext, source=source_name or "image")
        except Exception as e:
            # 结构性保证：OCR 异常不冒出——图片按空文档跳过，而不是让入库失败
            logger.warning("图片 OCR 失败 [%s]: %s: %s",
                           source_name, type(e).__name__, e)
        if not text:
            logger.info("图片 OCR 未得到文本，按空文档跳过: %s", source_name)
            return ParsedContent(content="", title=None, metadata={})

        return ParsedContent(
            content=text,
            title=(path.stem or None),
            metadata={"ocr": "standalone_image", "ocr_model": client.model},
        )
