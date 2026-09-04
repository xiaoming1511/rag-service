"""
解析器抽象基类

统一的解析接口，使 DocumentLoader 能够按扩展名自动路由到对应解析器。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, List, Optional


@dataclass
class ParsedContent:
    """解析结果"""
    content: str  # 提取出的纯文本内容
    title: Optional[str] = None  # 文档标题（有则提取，无则 None）
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据


class Parser(ABC):
    """文档解析器抽象基类"""

    # 支持的扩展名列表（含点，小写），如 [".md", ".markdown"]
    extensions: List[str] = []

    @abstractmethod
    def parse_bytes(self, data: bytes, source_name: str) -> ParsedContent:
        """
        从字节数据解析文档（统一入口，文件与 URL 抓取共用）

        Args:
            data: 文件/网页字节内容
            source_name: 来源名称（文件名或 URL，用于错误提示）

        Returns:
            ParsedContent: 解析出的内容与元数据
        """
        raise NotImplementedError

    def parse_file(self, file_path: Path) -> ParsedContent:
        """从文件解析文档"""
        return self.parse_bytes(file_path.read_bytes(), file_path.name)

    @staticmethod
    def _decode_text(data: bytes) -> str:
        """解码文本：优先 UTF-8，失败回退 GB18030（兼容中文 Windows 文档）"""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("gb18030", errors="replace")