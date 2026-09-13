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
    images: List[Dict[str, Any]] = field(default_factory=list)
    # 内嵌图片（多模态）：[{caption: 周边文本说明, bytes: 原始字节, ext: 扩展名(png/jpg/...)}]


class Parser(ABC):
    """文档解析器抽象基类"""

    # 支持的扩展名列表（含点，小写），如 [".md", ".markdown"]
    extensions: List[str] = []

    def __init__(self, ocr_client=None):
        """
        Args:
            ocr_client: 显式注入的 OCR 客户端（测试用桩）；
                None（默认）= 解析时按当前配置解析——配置未启用即无 OCR，
                因此「不传」与「OCR 关闭」在全链路上等价。
        """
        self._ocr_client = ocr_client

    def _resolve_ocr(self):
        """取本次解析应使用的 OCR 客户端；未启用 → None

        每次解析都重新解析（而不是在 __init__ 里缓存）：解析器实例是
        模块级单例、会被多轮索引复用，配置热改后必须能立即生效。
        """
        if self._ocr_client is not None:
            return self._ocr_client
        from src.document import ocr

        return ocr.get_ocr_client()

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
        """解码文本。

        编码回退链统一在 `md_common.decode_bytes`（utf-8 → gb18030 →
        latin-1）。此前这里自成一链（utf-8 → gb18030 + errors=replace），
        与 HTML / 转换接口的链不一致，同一份 Big5 文本经不同入口会得到
        不同结果（Big5 局限见 md_common 注释）。
        """
        from src.document.md_common import decode_bytes

        return decode_bytes(data)