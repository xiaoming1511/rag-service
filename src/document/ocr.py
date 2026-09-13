"""
OCR 支持（视觉模型：OvisOCR2）

用途：给「扫描版 PDF / 独立图片 / 文档内嵌图片」补上文字，让它们能被检索到。
接入点：
    - `parsers/pdf.py`   整页 OCR（扫描页回落）+ 内嵌图片
    - `parsers/image.py` 独立图片文件（.png/.jpg/...）
    - `loader.py`        内嵌图片 → 并入正文（索引路径）
    - `to_markdown.py`   转换接口（/v1/convert）同口径

接口形态
--------
走 **OpenAI 兼容**的 `/v1/chat/completions`，图片以 `image_url` data URL 传入
（与项目既有 OMLXClient 同源，不引入新协议）。实测 OvisOCR2 对指令 `OCR`
直接返回保持排版的 Markdown（标题、段落都会带格式）。

设计取舍
--------
1. **默认关闭**（`ocr.enabled=false`）：实测单页约 8s（冷加载 4.9s + 出字 2.4s），
   开启后索引明显变慢，且与问答争抢同一台 oMLX 服务。
2. **失败即降级**：OCR 的任何异常（超时 / 非 200 / 图片解不开 / 响应格式异常）
   只记 warning 并返回空串——绝不让文档因此入库失败。宁可少一段文字，
   不可少一篇文档；扫描件在 OCR 失败时的表现与未接入前一致（被跳过）。
3. **配置现读、客户端无状态**：`get_ocr_client()` 每次按当前 config 构造，
   配置改了下次调用即生效。因此 `/v1/config` 改完不需要重启、也不需要
   在 config 路由里加热更新钩子。客户端不持有长连接（每次请求自建 httpx
   连接），代价是 localhost 上约 1ms 的连接开销，换来的是「配置切换时
   不会因为关掉正在被其他线程使用的连接池而误伤并发请求」。
4. **护栏前置**：页数 / 图片数上限、最小边长、最大像素都在发起请求**之前**
   判断，避免一次索引把模型服务打满，或踩到图片解压炸弹。
5. **归一化用 Pillow**（已声明依赖）：png/jpeg 原样透传（零重编码损失），
   其余（webp/gif/bmp/tiff/RGBA/调色板）统一转 PNG。不用 PyMuPDF 是因为
   MuPDF 不支持 webp 解码（实测 FileDataError），而 webp 在笔记库里很常见。
"""

import base64
import io
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.config import config_manager
from src.logging_setup import get_logger

logger = get_logger(__name__)

# OCR 输出上限：一页密排中文约 1~1.5k 字，4096 token 足够且能兜住异常长输出
_MAX_OUTPUT_TOKENS = 4096

_DEFAULT_EXT = "png"

# 可直接透传给模型的格式（无需重编码，避免二次压缩损失）
_PASSTHROUGH_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


# ================================================================
#  客户端
# ================================================================

class OCRClient:
    """视觉模型 OCR 客户端（无状态，可安全地在多线程间共享）"""

    def __init__(
            self,
            *,
            model: str,
            base_url: str,
            api_key: str = "",
            timeout: float = 120.0,
            prompt: str = "OCR",
            render_dpi: int = 150,
            min_text_chars: int = 16,
            max_pages_per_doc: int = 30,
            max_images_per_doc: int = 20,
            min_image_side: int = 64,
            max_image_pixels: int = 40_000_000,
    ):
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = float(timeout)
        self.prompt = prompt or "OCR"
        self.render_dpi = int(render_dpi)
        self.min_text_chars = int(min_text_chars)
        self.max_pages_per_doc = int(max_pages_per_doc)
        self.max_images_per_doc = int(max_images_per_doc)
        self.min_image_side = int(min_image_side)
        self.max_image_pixels = int(max_image_pixels)

    # ---------------- 判定 ----------------

    def looks_scanned(self, page_text: str) -> bool:
        """页面文本层字符数低于阈值 → 视为扫描页（需要整页 OCR）"""
        return len((page_text or "").strip()) < self.min_text_chars

    # ---------------- 图片归一化 ----------------

    def _prepare(self, data: bytes, ext: str) -> Optional[Tuple[bytes, str, int, int]]:
        """归一化为模型可接受的图片。

        Returns:
            (payload, mime, width, height)；解不开 / 超像素上限 → None

        Note:
            `Image.open` 只读文件头，不解码像素，因此像素上限判断发生在
            真正解码（convert）之前——这正是防解压炸弹需要的位置。
        """
        if not data:
            return None
        from PIL import Image

        ext = (ext or _DEFAULT_EXT).lower().lstrip(".")
        try:
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
                fmt = (im.format or "").lower()
                if width <= 0 or height <= 0:
                    return None
                if width * height > self.max_image_pixels:
                    logger.warning("OCR 跳过超大图片 %dx%d（上限 %d 像素）",
                                   width, height, self.max_image_pixels)
                    return None
                if min(width, height) < self.min_image_side:
                    logger.debug("OCR 跳过过小图片 %dx%d（下限 %d 像素边长）",
                                 width, height, self.min_image_side)
                    return None
                mime = _PASSTHROUGH_MIME.get(ext)
                if mime and fmt in ("png", "jpeg") and im.mode in ("RGB", "L"):
                    return data, mime, width, height
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="PNG")
                return buf.getvalue(), "image/png", width, height
        except Exception as e:
            logger.warning("OCR 图片解析失败（ext=%s）: %s: %s", ext, type(e).__name__, e)
            return None

    # ---------------- 调用 ----------------

    def ocr_image_bytes(self, data: bytes, *, ext: str = _DEFAULT_EXT,
                        source: str = "") -> str:
        """识别单张图片字节 → 文本；任何失败返回空串"""
        prepared = self._prepare(data, ext)
        if prepared is None:
            return ""
        payload, mime, width, height = prepared
        return self._chat_ocr(payload, mime, source=f"{source} [{width}x{height}]")

    def ocr_pdf_page(self, page, *, source: str = "") -> str:
        """识别 PDF 单页（整页按 render_dpi 渲染成图再 OCR）；失败返回空串"""
        try:
            pix = page.get_pixmap(dpi=self.render_dpi)
        except Exception as e:
            logger.warning("OCR 页面渲染失败 [%s]: %s: %s", source, type(e).__name__, e)
            return ""
        if pix.width * pix.height > self.max_image_pixels:
            logger.warning("OCR 跳过超大页面 %dx%d [%s]", pix.width, pix.height, source)
            return ""
        try:
            png = pix.tobytes("png")
        except Exception as e:
            logger.warning("OCR 页面编码失败 [%s]: %s", source, e)
            return ""
        try:
            page_no = int(getattr(page, "number", 0)) + 1
        except Exception:
            page_no = 0
        return self._chat_ocr(png, "image/png", source=f"{source} p{page_no}")

    def _chat_ocr(self, image_bytes: bytes, mime: str, *, source: str = "") -> str:
        """单次视觉请求（OpenAI 兼容 chat/completions）"""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self.prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "temperature": 0.0,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            with httpx.Client(timeout=self.timeout) as http:
                resp = http.post(f"{self.base_url}/chat/completions",
                                 json=body, headers=headers)
        except Exception as e:
            logger.warning("OCR 请求失败 [%s]: %s: %s", source, type(e).__name__, e)
            return ""

        if resp.status_code != 200:
            logger.warning("OCR 返回非 200 [%s]: %s %s",
                           source, resp.status_code, (resp.text or "")[:200])
            return ""

        try:
            content = resp.json()["choices"][0]["message"]["content"] or ""
        except Exception as e:
            logger.warning("OCR 响应解析失败 [%s]: %s: %s", source, type(e).__name__, e)
            return ""
        return _strip_code_fence(content).strip()


def _strip_code_fence(text: str) -> str:
    """模型偶尔会把整段结果包进 ``` 围栏，剥掉外层"""
    t = (text or "").strip()
    if not (t.startswith("```") and t.endswith("```")):
        return t
    lines = t.split("\n")
    if len(lines) < 2:
        return t
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


# ================================================================
#  配置解析
# ================================================================

def get_ocr_client() -> Optional[OCRClient]:
    """按当前配置返回 OCR 客户端；未启用 / 配置未加载 → None

    每次调用都按最新配置构造（对象极轻，无长连接），所以：
      - `/v1/config` 改完立刻生效，无需热更新钩子；
      - 配置未加载（例如测试里直接 import 解析器）时安全地返回 None，
        全链路退化成「无 OCR」的既有行为。
    """
    try:
        cfg = config_manager.config
    except Exception:
        return None
    try:
        ocr_cfg = cfg.ocr
    except AttributeError:
        return None
    if not ocr_cfg.enabled:
        return None

    base_url = (ocr_cfg.base_url or cfg.omlx.base_url or "").strip()
    if not base_url:
        logger.warning("OCR 已启用但 base_url 为空（ocr.base_url 与 omlx.base_url 均未配置），跳过 OCR")
        return None

    return OCRClient(
        model=ocr_cfg.model,
        base_url=base_url,
        api_key=ocr_cfg.api_key or cfg.omlx.api_key or "",
        timeout=ocr_cfg.timeout,
        prompt=ocr_cfg.prompt,
        render_dpi=ocr_cfg.render_dpi,
        min_text_chars=ocr_cfg.min_text_chars,
        max_pages_per_doc=ocr_cfg.max_pages_per_doc,
        max_images_per_doc=ocr_cfg.max_images_per_doc,
        min_image_side=ocr_cfg.min_image_side,
        max_image_pixels=ocr_cfg.max_image_pixels,
    )


# ================================================================
#  内嵌图片批量 OCR（索引路径与转换接口共用）
# ================================================================

def ocr_block(
        img: Dict[str, Any],
        client: Optional[OCRClient],
        *,
        label: str = "图片文字",
) -> str:
    """单张内嵌图片 → 可并入正文的文本块；未启用 / 失败 / 无文字 → 空串

    调用方自行决定把块放在哪里（转换接口需要「紧随图片所在幻灯片」，
    加载器统一拼到正文末尾），因此这里只负责识别 + 加标记，不负责排版位置。
    """
    if client is None or not img:
        return ""
    data = img.get("bytes")
    if not data:
        return ""
    caption = (img.get("caption") or "").strip()
    source = str(caption or "embedded")[:60]
    try:
        text = client.ocr_image_bytes(data, ext=img.get("ext", _DEFAULT_EXT), source=source)
    except Exception as e:
        # 结构性保证：OCR 的任何异常都不允许冒出到调用方（宁可少一段文字，
        # 不可少一篇文档）。真实客户端内部已自行兜底，这里防的是注入的实现。
        logger.warning("OCR 内嵌图片失败 [%s]: %s: %s", source, type(e).__name__, e)
        return ""
    if not text:
        return ""
    head = f"[{label}]" + (f" {caption}" if caption else "")
    return f"{head}\n\n{text}"


def ocr_embedded_images(
        images: List[Dict[str, Any]],
        client: Optional[OCRClient],
        *,
        label: str = "图片文字",
) -> List[str]:
    """逐张 OCR 内嵌图片，返回可并入正文的文本块列表。

    Args:
        images: 解析器/转换器收集的图片列表，每项需含 `bytes`（可选 `ext`/`caption`）
        client: OCR 客户端；None（未启用）→ 直接返回空列表
        label: 正文里给 OCR 段落打的前缀标记

    Returns:
        List[str]: 每个成功识别的图片一个文本块；失败/超限的图片不出现在结果里

    Note:
        与原列表**不保证等长**——调用方只用它拼正文，图片本身的落盘
        （附件存储）仍由各自的既有逻辑负责，互不影响。
    """
    if client is None or not images:
        return []

    limit = max(0, client.max_images_per_doc)
    if len(images) > limit:
        logger.info("OCR 内嵌图片超出单文档上限：%d 张中只处理前 %d 张", len(images), limit)

    blocks: List[str] = []
    for img in images[:limit]:
        block = ocr_block(img, client, label=label)
        if block:
            blocks.append(block)
    return blocks


# 供解析器/加载器使用的最小依赖注入点：测试可替换为桩实现
__all__ = [
    "OCRClient",
    "get_ocr_client",
    "ocr_block",
    "ocr_embedded_images",
]
