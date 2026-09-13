"""
HTML → Markdown 转换器

背景（本次问题的根因）：
    原 `parsers/html.py` 走的是「提取纯文本」路线：只挑出白名单块级标签
    （p/h1-6/li/pre/td...）的文本，其余结构一律丢弃。这带来两个后果：

    1. 非白名单容器被静默丢弃
       `<div class="architecture">` 里的 ASCII 架构图不是任何一个白名单标签，
       于是整张图既不报错、也不提取，直接从索引里消失。

    2. 结构化内容塌陷成一行
       即使内容被提取，`get_text(" ")` 也会把内部换行统一替换成空格。
       一旦这段文本被写回 .md 而不套代码块，Obsidian 渲染时会再次折叠连续空格，
       最终塌成一坨无法阅读的乱码——即用户遇到的「排版错乱」。

本模块的职责：
    把 HTML **当作带结构的文档**来转换（而不是当纯文本抽取），重点保住：
    - `<pre>` / `<code>`：按原文逐字保留（含空白与换行），用 ``` 围栏包裹，
      避免 Markdown 折叠空格、也避免内部符号被当 Markdown 语法解析
    - 预格式化容器（如 `<div class="architecture">`）：识别其内部的
      box-drawing 字符（─│┌┐└┘├┤┬┴┼ 等），按 pre 同样处理
    - 表格 → Markdown 表格；列表 → Markdown 列表；标题 → # 级标题
    - 行内 code / 链接 → Markdown 行内语法

设计原则：宁可保守，不可损坏。识别不确定时退回普通段落，绝不臆造结构。
"""

import re
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from src.document.md_common import decode_bytes, escape_pipe, render_md_table
from src.logging_setup import get_logger

logger = get_logger(__name__)

# 表格/列表等需要特殊处理，不作为普通块
_SKIP_TAGS = {
    "script", "style", "noscript", "iframe", "svg", "nav", "footer",
    "head", "meta", "link", "template",
}

# 这些标签整体按「预格式化」处理：内容逐字保留
_PREFORMATTED_TAGS = {"pre", "textarea"}

# box-drawing / 制表字符：用于判定一个容器是否为 ASCII 图表
_BOX_CHARS = set("─│┌┐└┘├┤┬┴┼━┃┏┓┗┛┣┫┳┻╋═║╔╗╚╝╠╣╦╩╬▁▔▏▕█▌▐░▒▓→←↑↓▼▲◀▶")

# 判定为 ASCII 图所需的 box 字符数量下限（避免把偶然出现的 │ 误判）
_BOX_CHAR_THRESHOLD = 4

# 需要包成代码块的语言标记（这里统一用空标记，纯文本更安全）
_FENCE = "```"

# 显式换行占位符（<br> 用；普通源码缩进换行会被折叠成空格）
_BR_MARK = "\x00BR\x00"


def _looks_like_ascii_art(text: str) -> bool:
    """
    判断一段文本是否为 ASCII 图表（架构图/流程框线图）

    判定依据：包含换行，且 box-drawing 字符数量达到阈值。
    """
    if not text or "\n" not in text:
        return False
    if not _BOX_CHARS.intersection(text):
        return False
    count = sum(1 for ch in text if ch in _BOX_CHARS)
    return count >= _BOX_CHAR_THRESHOLD


def _dedent_block(text: str) -> str:
    """
    去掉预格式化文本的统一公共缩进，同时保留相对缩进

    例如 HTML 里常见的每行 12 空格缩进，去掉后图仍在 Markdown 中正确对齐。
    """
    lines = text.split("\n")

    # 去掉首尾空行（不动中间的空行）
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return ""

    # 计算非空行的最小缩进
    indents = [
        len(line) - len(line.lstrip())
        for line in lines
        if line.strip()
    ]
    if not indents:
        return ""
    common = min(indents)

    if common == 0:
        return "\n".join(line.rstrip() for line in lines)

    return "\n".join(
        (line[common:] if len(line) >= common else line).rstrip()
        for line in lines
    )


def _fence_block(text: str, lang: str = "") -> str:
    """
    把文本包进 Markdown 代码围栏

    关键点：若文本内部本身含 ``` ，则加长围栏长度避免提前闭合。
    """
    text = text.rstrip("\n")
    if not text.strip():
        return ""

    fence = _FENCE
    while fence in text:
        fence += "`"

    return f"{fence}{lang}\n{text}\n{fence}"


class HTMLToMarkdownConverter:
    """
    HTML → Markdown 转换器

    用法：
        conv = HTMLToMarkdownConverter()
        md = conv.convert(html_text)            # 全文档 → Markdown
        title = conv.last_title                 # 顺带取到的文档标题
    """

    def __init__(self, keep_images: bool = True):
        """
        Args:
            keep_images: 是否把 <img> 转成 Markdown 图片语法（默认保留）
        """
        self.keep_images = keep_images
        self.last_title: Optional[str] = None

    # ================================================================
    # 对外入口
    # ================================================================

    def convert(self, html: str) -> str:
        """把 HTML 字符串转换为 Markdown 文本"""
        soup = BeautifulSoup(html, "html.parser")

        # 标题必须在清理前提取：<title> 位于 <head>，而 head 属于待丢弃噪音
        self.last_title = self._extract_title(soup)

        for tag in soup.find_all(_SKIP_TAGS):
            tag.decompose()

        # 优先取正文容器，避免把导航/页脚等噪音转进来
        root = self._pick_root(soup) or soup
        blocks = self._render_children(root)
        return self._join_blocks(blocks)

    def convert_bytes(self, data: bytes, source_name: str = "") -> str:
        """从字节转换（自动处理常见中文编码）"""
        return self.convert(self._decode(data, source_name))

    # ================================================================
    # 内部：编码与标题
    # ================================================================

    @staticmethod
    def _decode(data: bytes, source_name: str = "") -> str:
        """解码 HTML 字节（编码回退链统一在 `md_common.decode_bytes`）"""
        return decode_bytes(data, source_name)

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        if soup.title and soup.title.string:
            t = soup.title.string.strip()
            if t:
                return t
        h1 = soup.find("h1")
        if h1:
            return self._inline_text(h1).strip() or None
        return None

    @staticmethod
    def _pick_root(soup: BeautifulSoup) -> Optional[Tag]:
        """挑选正文根节点：优先语义化标签，其次 body"""
        for selector in ("article", "main", '[role="main"]'):
            node = soup.select_one(selector)
            if node:
                return node
        return soup.body or None

    # ================================================================
    # 内部：块级渲染
    # ================================================================

    def _render_children(self, node: Tag) -> List[str]:
        """渲染一个节点的所有子节点为块级 Markdown 片段列表"""
        out: List[str] = []
        for child in node.children:
            out.extend(self._render_node(child))
        return out

    def _render_node(self, node) -> List[str]:
        """渲染单个节点，返回 0..n 个块级片段"""
        if isinstance(node, NavigableString):
            # HTML 注释不作为正文（BeautifulSoup 把 Comment 归为 NavigableString）
            if isinstance(node, Comment):
                return []
            text = str(node)
            # 块级之间的裸文本：仅保留有实际内容的
            if text.strip():
                return [self._escape_paragraph(text.strip())]
            return []

        if not isinstance(node, Tag):
            return []

        name = node.name.lower()

        # ---- 预格式化：逐字保留 ----
        if name in _PREFORMATTED_TAGS:
            return self._render_pre(node)

        # ---- 标题 ----
        if re.fullmatch(r"h[1-6]", name):
            level = int(name[1])
            text = self._inline_text(node).strip()
            return [f"{'#' * level} {text}"] if text else []

        # ---- 段落 ----
        if name == "p":
            text = self._inline_text(node).strip()
            return [self._escape_paragraph(text)] if text else []

        # ---- 分隔线 ----
        if name == "hr":
            return ["---"]

        # ---- 图片 ----
        if name == "img":
            src = node.get("src", "")
            alt = node.get("alt", "")
            if self.keep_images and src:
                return [f"![{alt}]({src})"]
            return []

        # ---- 表格 ----
        if name == "table":
            md = self._render_table(node)
            return [md] if md else []

        # ---- 列表 ----
        if name in ("ul", "ol"):
            md = self._render_list(node)
            return [md] if md else []

        # ---- 引用 ----
        if name == "blockquote":
            inner = self._join_blocks(self._render_children(node))
            if not inner:
                return []
            quoted = "\n".join(
                ("> " + line) if line.strip() else ">"
                for line in inner.strip("\n").split("\n")
            )
            return [quoted]

        # ---- 换行符 ----
        if name == "br":
            return ["\n"]

        # ---- 其它容器：判断是否为 ASCII 图，否则递归 ----
        if self._is_ascii_art_container(node):
            return self._render_pre(node)

        return self._render_children(node)

    def _render_pre(self, node: Tag) -> List[str]:
        """渲染预格式化内容（<pre> 或 ASCII 图容器）"""
        raw = node.get_text()
        text = _dedent_block(raw)
        if not text.strip():
            return []

        # 已知语言可标注；这里不猜语言，纯文本围栏最稳
        lang = ""
        classes = node.get("class") or []
        if isinstance(classes, str):
            classes = [classes]
        for cls in classes:
            m = re.match(r"(?:language|lang|highlight)-([\w+#-]+)", str(cls))
            if m:
                lang = m.group(1)
                break

        return [_fence_block(text, lang)]

    def _is_ascii_art_container(self, node: Tag) -> bool:
        """
        判断容器是否为「预格式化图表容器」

        典型例子：`<div class="architecture">` 里直接放 ASCII 架构图。
        这类 div 内部没有块级标签，文本靠换行与空格对齐。
        """
        if node.name in ("div", "section", "figure", "span"):
            # 内部若已有块级子元素，说明是正常结构，不按图处理
            if node.find(["p", "div", "table", "ul", "ol", "pre", "h1", "h2",
                          "h3", "h4", "h5", "h6", "section"]):
                return False
            text = node.get_text()
            return _looks_like_ascii_art(text)
        return False

    # ================================================================
    # 内部：表格 / 列表
    # ================================================================

    def _render_table(self, table: Tag) -> str:
        """表格 → Markdown 表格（首行作表头）

        拼装统一交给 `md_common.render_md_table`（与 docx/pptx 共用同一份规则，
        避免同一张表在不同格式下渲染不一致）。
        """
        rows: List[List[str]] = []
        # 只取直接子级 tr，避免嵌套表格的 tr 混入
        trs = table.find_all("tr", recursive=False)
        if not trs:
            trs = table.find_all("tr")
        for tr in trs:
            cells = tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"])
            if not cells:
                continue
            row = [escape_pipe(self._inline_text(c).strip()) for c in cells]
            if any(row):
                rows.append(row)

        return render_md_table(rows)

    def _render_list(self, node: Tag, depth: int = 0) -> str:
        """列表 → Markdown 列表（支持嵌套）"""
        ordered = node.name.lower() == "ol"
        lines: List[str] = []
        indent = "  " * depth

        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            # 分离直接文本与嵌套列表
            marker = f"{i}. " if ordered else "- "
            parts = self._inline_text(li, skip_lists=True).strip()

            if parts:
                lines.append(f"{indent}{marker}{parts}")

            for sub in li.find_all(["ul", "ol"], recursive=False):
                sub_md = self._render_list(sub, depth + 1)
                if sub_md:
                    lines.append(sub_md)

        return "\n".join(lines)

    # ================================================================
    # 内部：行内渲染
    # ================================================================

    def _inline_text(self, node: Tag, skip_lists: bool = False) -> str:
        """
        渲染行内内容（保留 code / 链接 / 加粗等 Markdown 语法）

        Args:
            skip_lists: 是否跳过嵌套列表（列表渲染时避免重复）
        """
        parts: List[str] = []

        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                parts.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue

            name = child.name.lower()

            if skip_lists and name in ("ul", "ol"):
                continue
            if name in _SKIP_TAGS:
                continue

            if name in ("code", "tt"):
                code = child.get_text()
                # 行内 code：用反引号包裹，内部反引号做转义
                if "`" in code:
                    fence = "``"
                    parts.append(f"{fence}{code}{fence}")
                else:
                    parts.append(f"`{code}`")
                continue

            if name == "a":
                text = self._inline_text(child).strip()
                href = child.get("href", "")
                if href and text:
                    parts.append(f"[{text}]({href})")
                else:
                    parts.append(text)
                continue

            if name in ("strong", "b"):
                inner = self._inline_text(child).strip()
                parts.append(f"**{inner}**" if inner else "")
                continue

            if name in ("em", "i"):
                inner = self._inline_text(child).strip()
                parts.append(f"*{inner}*" if inner else "")
                continue

            if name == "br":
                # 显式换行：用占位符标记，稍后还原（源码缩进换行不算换行）
                parts.append(_BR_MARK)
                continue

            if name == "img":
                src = child.get("src", "")
                alt = child.get("alt", "")
                if self.keep_images and src:
                    parts.append(f"![{alt}]({src})")
                continue

            parts.append(self._inline_text(child, skip_lists=skip_lists))

        # 折叠多余空白（含源码缩进换行）：HTML 里 <p> 内的换行不表示换行，
        # 只有显式 <br> 才换行，这里用占位符精确保留后者
        text = "".join(parts)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r" *" + _BR_MARK + r" *", "\n", text)
        return text.strip()

    # ================================================================
    # 内部：拼接与转义
    # ================================================================

    @staticmethod
    def _escape_paragraph(text: str) -> str:
        """
        段落文本转义

        只做最小必要处理：避免正文里的裸 HTML/字符被误当语法。
        """
        return text.strip()

    @staticmethod
    def _join_blocks(blocks: List[str]) -> str:
        """用空行连接块级片段，并压缩连续空行"""
        cleaned: List[str] = []
        for b in blocks:
            if b is None:
                continue
            b = b.strip("\n")
            if not b.strip():
                continue
            cleaned.append(b)

        text = "\n\n".join(cleaned)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n" if text.strip() else ""


# ================================================================
# 便捷函数
# ================================================================

def html_to_markdown(html: str, keep_images: bool = True) -> str:
    """把 HTML 字符串转换为 Markdown（便捷函数）"""
    return HTMLToMarkdownConverter(keep_images=keep_images).convert(html)


def html_bytes_to_markdown(data: bytes, source_name: str = "", keep_images: bool = True) -> str:
    """把 HTML 字节转换为 Markdown（便捷函数）"""
    return HTMLToMarkdownConverter(keep_images=keep_images).convert_bytes(data, source_name)
