"""
Markdown 解析器
支持 frontmatter（--- 分隔的 YAML 元数据）提取，抽取首个一级标题作为文档标题
"""

import re
from pathlib import Path
from typing import List

import yaml

from src.document.parsers.base import Parser, ParsedContent


class MarkdownParser(Parser):
    """Markdown 解析器"""

    extensions: List[str] = [".md", ".markdown"]

    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        content = self._decode_text(data)
        metadata = {}

        # 提取 frontmatter（--- 开头，三连横线分隔）
        if content.startswith("---"):
            try:
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = yaml.safe_load(parts[1])
                    if isinstance(frontmatter, dict):
                        metadata.update(frontmatter)
                    content = parts[2]
            except Exception:
                pass  # frontmatter 解析失败则忽略

        content = content.strip()

        # 提取标题：首个一级标题（# xxx），无则取首个任意级标题
        title = None
        match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
        if match:
            title = match.group(1).strip()
        else:
            match = re.search(r"^#{1,6}\s+(.+)$", content, flags=re.MULTILINE)
            if match:
                title = match.group(1).strip()

        if not metadata.get("title") and title:
            metadata["title"] = title

        return ParsedContent(content=content, title=title, metadata=metadata)