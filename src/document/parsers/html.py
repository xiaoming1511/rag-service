"""
HTML 解析器
使用 BeautifulSoup 提取 <title> 与正文文本（去除脚本/样式/导航噪声）
"""

from typing import List, Optional

from bs4 import BeautifulSoup, Tag

from src.document.parsers.base import Parser, ParsedContent

# 需要提取的块级标签
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre", "blockquote", "td", "th"}
# 需要丢弃的标签
_SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "nav", "footer"}


class HTMLParser(Parser):
    """HTML 解析器（.html / .htm，也用于 URL 抓取）"""

    extensions: List[str] = [".html", ".htm"]

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        # 尝试按 HTML 编码声明解码，失败回退 UTF-8
        text = None
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = data.decode("utf-8", errors="replace")

        soup = BeautifulSoup(text, "html.parser")

        # 清理噪声标签
        for tag in soup.find_all(_SKIP_TAGS):
            tag.decompose()

        # 标题：优先 <title>，其次第一个 h1
        title: Optional[str] = None
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)

        # 按文档顺序提取块级文本，保留段落边界
        lines: List[str] = []
        seen = set()

        def walk(node):
            if isinstance(node, Tag):
                if node.name in _BLOCK_TAGS:
                    seg = node.get_text(" ", strip=True)
                    if seg and seg not in seen:
                        seen.add(seg)
                        lines.append(seg)
                        return  # 块级元素内部不再递归，避免重复
                for child in node.children:
                    walk(child)

        walk(soup)

        content = "\n\n".join(lines)
        return ParsedContent(content=content, title=title)