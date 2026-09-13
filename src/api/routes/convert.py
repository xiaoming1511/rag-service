"""
文档 → Markdown 转换路由（统一入口）

供 Obsidian 插件（及任何客户端）调用：把任意支持的文档格式（html/pdf/docx/
pptx/epub/txt/md/图片）转换成排版正确、标题层级保留的 Markdown。转换逻辑委托
`src/document/to_markdown`（统一分发到各格式转换器）。

端点：
    POST /v1/convert/to-md
        请求体 { format: "html"|"pdf"|..., content: <原文或 base64>, path?: <可选文件路径> }
        返回 { ok, markdown, title, format, stats, images }
        images（R10-3）：pptx/epub 内嵌图片的元数据 + base64 数据（聚合超上限
        时去 data 保元数据），供调用方另存附件或改写引用

    POST /v1/convert/html2md   （向后兼容：等价于 format=html）

OCR（可选，默认关闭；配置 ocr.enabled=true 后生效）：
    - 图片格式（png/jpg/webp/gif/bmp/tiff）需要 OCR 才能转出文字，
      未启用时返回 400 并说明原因（而不是给一份空 Markdown）；
    - 扫描版 PDF 的页面会自动回落 OCR（文本层字符数低于 ocr.min_text_chars）；
    - pptx/epub 的内嵌图片逐张识别，文字并入 Markdown。

口径：不落盘到 vault —— 写 .md 由插件负责，避免服务端越权写库 +
      与 Obsidian 文件监听冲突。
"""

import base64
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.document.to_markdown import (
    convert_to_markdown,
    prefix_title,
    supported_formats,
)
from src.logging_setup import get_logger

logger = get_logger("rag.api.convert")

router = APIRouter(prefix="/v1", tags=["convert"])

# 单篇转换的字节上限（防滥用/防误传二进制）
_MAX_BYTES = 64 * 1024 * 1024  # 64MB


class ToMdRequest(BaseModel):
    """统一转换请求：format + content（文本原文，或 base64）"""
    format: str
    content: str
    content_is_base64: bool = False      # content 是否为 base64（二进制格式必须置 true）
    title: Optional[str] = None          # 可选：覆盖标题


class ToMdResponse(BaseModel):
    """统一转换响应"""
    ok: bool
    markdown: str
    title: Optional[str] = None
    format: str = ""
    stats: dict = {}
    images: list = []          # R10-3：内嵌图片元数据（pptx/epub），其余格式为空


# 图片 base64 数据的聚合回传上限：超限时去 data（置 None）保元数据——
# 响应不无限膨胀，调用方仍知道每张图的存在与说明
_MAX_IMAGES_PAYLOAD = 8 * 1024 * 1024  # 8 MiB（base64 字符数）


def _shape_images(images: list) -> tuple:
    """按聚合上限裁剪图片载荷；返回 (shaped_images, truncated)"""
    total = 0
    truncated = False
    out = []
    for im in images or []:
        data = im.get("data_base64")
        if data is not None:
            if total + len(data) > _MAX_IMAGES_PAYLOAD:
                im = {**im, "data_base64": None}
                truncated = True
            else:
                total += len(data)
        out.append(im)
    return out, truncated


class HtmlToMdRequest(BaseModel):
    """HTML → Markdown 兼容请求"""
    html: str
    title: Optional[str] = None


def _to_bytes(content: str, is_base64: bool) -> bytes:
    """把 content 转成字节（文本编码 or base64 解码），并做大小护栏

    先做一次「廉价下界预检」再解码/编码：等到字节化之后再判断大小，
    超大请求体（最大 1/3 来自 base64 膨胀）早已进入内存。这里用下界
    （base64 为 len*3/4，UTF-8 每字符至少 1 字节）预先挡掉明显超限的请求，
    解码后再用精确长度兜底。
    """
    if is_base64:
        # base64 解码后的字节数 ≈ len*3/4（去 padding），取其下界
        lower_bound = max(0, (len(content) // 4) * 3 - 2)
    else:
        lower_bound = len(content)

    if lower_bound > _MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"文件过大（>{_MAX_BYTES} 字节）")

    if is_base64:
        try:
            decoded = base64.b64decode(content, validate=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"base64 解码失败: {e}")
    else:
        decoded = content.encode("utf-8")

    if len(decoded) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"文件过大（>{_MAX_BYTES} 字节）")
    return decoded


@router.post("/convert/to-md", response_model=ToMdResponse)
def convert_document_to_markdown(req: ToMdRequest):
    """
    统一转换入口：任意支持格式 → Markdown

    - format: html / htm / pdf / docx / pptx / epub / txt / md / markdown
      / png / jpg / jpeg / webp / gif / bmp / tiff / tif
    - 二进制格式（pdf/docx/pptx/epub/图片）用 content_is_base64=true + base64(content)
    - 文本格式（html/txt/md）可直接传原文
    - 图片格式需要 ocr.enabled=true，否则返回 400

    声明为 def（非 async def）：pdf/docx/pptx/epub 解析是同步 CPU 密集工作，
    在 async def 里会冻结事件循环；def 端点由 Starlette 放入线程池执行。
    """
    try:
        if not req.content or not req.content.strip():
            raise HTTPException(status_code=400, detail="content 不能为空")
        data = _to_bytes(req.content, req.content_is_base64)
        result = convert_to_markdown(data, req.format, source_name=req.format)

        markdown = result.markdown
        if req.title:
            # 标题覆盖：replace_existing=True 才会替换解析阶段已写入的首行 H1，
            # 否则（首行已是 `# `）会静默不生效，用户的 title 形同虚设
            markdown = prefix_title(markdown, req.title, replace_existing=True)

        stats = {
            "markdown_chars": len(markdown),
            "markdown_lines": len(markdown.splitlines()),
            "fence_pairs": markdown.count("```") // 2,
            "source_format": result.source_format,
        }
        images, img_truncated = _shape_images(result.images)
        stats["images_count"] = len(images)
        if img_truncated:
            stats["images_payload_truncated"] = True

        return ToMdResponse(
            ok=True,
            markdown=markdown,
            title=req.title or result.title,
            format=result.source_format or req.format,
            stats=stats,
            images=images,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("文档转 Markdown 失败: %s", e)
        raise HTTPException(status_code=422, detail="转换失败（文档格式或内容无效）")


@router.post("/convert/html2md", response_model=ToMdResponse)
def convert_html_to_markdown(req: HtmlToMdRequest):
    """
    兼容端点：HTML → Markdown（等价于 /convert/to-md format=html）

    保留以兼容旧版本插件；新调用请使用 /convert/to-md。
    """
    if not req.html or not req.html.strip():
        raise HTTPException(status_code=400, detail="HTML 内容为空")

    try:
        result = convert_to_markdown(req.html.encode("utf-8"), "html", source_name="html")
        markdown = result.markdown
        if req.title:
            # 与 /convert/to-md 同口径：显式 title 必须能覆盖解析出的 H1
            markdown = prefix_title(markdown, req.title, replace_existing=True)

        return ToMdResponse(
            ok=True,
            markdown=markdown,
            title=req.title or result.title,
            format="html",
            stats={
                "html_chars": len(req.html),
                "markdown_chars": len(markdown),
                "markdown_lines": len(markdown.splitlines()),
                "fence_pairs": markdown.count("```") // 2,
            },
        )
    except Exception as e:
        logger.exception("HTML 转 Markdown 失败: %s", e)
        raise HTTPException(status_code=422, detail="转换失败（HTML 内容无效）")


@router.get("/convert/formats")
async def list_supported_formats():
    """列出支持的转换格式"""
    return {"formats": supported_formats()}
