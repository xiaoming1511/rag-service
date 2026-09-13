"""
HTML 解析器
使用 BeautifulSoup 提取 <title> 与正文内容（去除脚本/样式/导航噪声）

要点（修复「架构图丢失」问题）：
    早期实现只挑白名单块级标签（p/h1-6/li/pre/td...）的文本，导致两处损坏：
    1. 非白名单容器被静默丢弃 —— 如 `<div class="architecture">` 里的 ASCII
       架构图不属于任何白名单标签，整张图既不报错也不提取，直接从索引消失；
    2. 结构化内容塌陷 —— `get_text(" ")` 把内部换行统一替换成空格，
       ASCII 图/代码块被压成一行，写回 Markdown 后排版错乱。

    现改为委托 `html_to_markdown` 做结构感知转换：<pre>/ASCII 图容器用 ``` 围栏
    逐字保留（含空白与换行），表格/列表/标题转为对应 Markdown 语法。
"""

from typing import List, Optional

from src.document.html_to_markdown import HTMLToMarkdownConverter
from src.document.parsers.base import Parser, ParsedContent


class HTMLParser(Parser):
    """HTML 解析器（.html / .htm，也用于 URL 抓取）"""

    extensions: List[str] = [".html", ".htm"]

    def __init__(self, keep_images: bool = True):
        """
        Args:
            keep_images: 是否保留图片为 Markdown 图片语法（默认保留）
        """
        self.keep_images = keep_images

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        converter = HTMLToMarkdownConverter(keep_images=self.keep_images)

        # 先探测标题（转换过程会顺带记录），再取正文
        content = converter.convert_bytes(data, source_name)
        title: Optional[str] = converter.last_title

        # 兜底：若 <title> 与 <h1> 都缺失，用首个 Markdown 标题
        if not title:
            for line in content.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip() or None
                    break

        return ParsedContent(content=content, title=title)