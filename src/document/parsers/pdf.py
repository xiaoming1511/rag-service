"""
PDF 解析器
使用 PyMuPDF（fitz）逐页提取文本，中文/CJK 支持好、速度快

OCR 回落（可选，默认关闭）：
    扫描件没有文本层，`page.get_text()` 返回空——旧实现下这类文件会被
    加载器当作「空文档」静默跳过。开启 `ocr.enabled` 后，文本层字符数低于
    `ocr.min_text_chars` 的页面会整页渲染成图送视觉模型识别。

    注意：扫描页里的「内嵌图片」就是页面本身，若同时按内嵌图片再 OCR 一次
    会把同一段文字重复入库，因此**扫描页不做内嵌图片提取**。
    每篇文档的 OCR 页数受 `ocr.max_pages_per_doc` 限制。
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
            return self._parse_document(doc, source_name)
        finally:
            # 确保文档句柄一定释放（资源泄漏保护）
            doc.close()

    def _parse_document(self, doc, source_name: str = "") -> ParsedContent:
        from src.logging_setup import get_logger
        logger = get_logger(__name__)

        ocr = self._resolve_ocr()
        ocr_pages_budget = max(0, ocr.max_pages_per_doc) if ocr is not None else 0
        ocr_attempted = 0   # 预算计数：含失败页（否则失败页会让预算形同虚设）
        ocr_ok = 0          # 真正识别出文字的页数（写进元数据的就是它）

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

            # —— 扫描页回落：文本层为空/极少 → 整页 OCR ——
            scanned = ocr is not None and ocr.looks_scanned(text)
            if scanned:
                if ocr_attempted >= ocr_pages_budget:
                    logger.info("PDF 扫描页 OCR 已达单文档上限（%d 页），其余页面跳过: %s",
                                ocr_pages_budget, source_name)
                else:
                    ocr_attempted += 1
                    try:
                        ocr_text = ocr.ocr_pdf_page(page, source=source_name)
                    except Exception as e:
                        # 结构性保证：OCR 异常不冒出——该页退化为"无文本"，不阻断整篇
                        logger.warning("PDF 扫描页 OCR 失败（%s）: %s: %s",
                                       source_name, type(e).__name__, e)
                        ocr_text = ""
                    if ocr_text:
                        ocr_ok += 1
                        # 文本层若有零星字符（如页眉页脚页码），保留在 OCR 正文之前，
                        # 避免因「用 OCR 结果整体覆盖」而丢掉这些真实文字
                        text = f"{text}\n\n{ocr_text}".strip() if text else ocr_text

            if text:
                pages.append(text)

            # 内嵌图片（多模态：上下文字幕方案，说明文字取本页文本）
            # 扫描页跳过：那张"内嵌图"就是整页扫描图，OCR 已在上面做过一次
            if scanned:
                continue
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

        # 只把"真正识别出文字的页数"写进 ocr_pages（成功口径，便于下游判断覆盖度）；
        # 失败页单独记一个键——OCR 静默失败是运维最需要看见的信号
        if ocr_ok:
            metadata["ocr_pages"] = ocr_ok
        if ocr_attempted > ocr_ok:
            metadata["ocr_pages_failed"] = ocr_attempted - ocr_ok

        return ParsedContent(content=content, title=title, metadata=metadata, images=images)