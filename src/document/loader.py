"""
文档加载器
从指定目录递归加载多种格式文档（Markdown/PDF/Word/HTML/纯文本），
支持远程网页 URL 抓取；解析逻辑由解析器注册表按扩展名自动路由。
"""

import os
from pathlib import Path
from typing import List, Optional, Dict, Any

from dataclasses import dataclass, field
from datetime import datetime

from src.document.parsers import DEFAULT_PARSERS, Parser

from src.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class Document:
    """文档数据模型"""
    id: str  # 文档唯一标识（文件路径/URL 的哈希）
    file_path: str  # 文件绝对路径或 URL
    file_name: str  # 文件名
    content: str  # 原始内容
    metadata: Dict[str, Any] = field(default_factory=dict)  # 元数据

    @property
    def source(self) -> str:
        """来源标识（用于引用）"""
        return self.file_name


class DocumentLoader:
    """文档加载器"""

    # watchdog 等需要忽略的目录（用户确认：继续排除 wiki 目录）
    EXCLUDE_DIRS = {"wiki", "node_modules"}

    def __init__(
            self,
            source_dirs: List[str],
            extensions: Optional[List[str]] = None,
            parsers: Optional[List[Parser]] = None,
            attachment_dir: Optional[str] = None,
    ):
        """
        初始化加载器

        Args:
            source_dirs: 源目录列表
            extensions: 支持的扩展名列表；None 表示使用全部可用解析器的扩展名
            parsers: 自定义解析器列表；None 使用默认解析器（md/txt/pdf/docx/html/epub/pptx）
            attachment_dir: 提取图片的附件目录（多模态）；默认 data/attachments
        """
        self.source_dirs = [Path(d).expanduser().resolve() for d in source_dirs]

        # 附件目录（多模态：从 PDF/DOCX/PPTX 提取的内嵌图片落盘于此）
        self.attachment_dir = Path(attachment_dir or "./data/attachments").expanduser().resolve()

        # 构建扩展名 → 解析器 注册表
        parser_list = parsers if parsers is not None else DEFAULT_PARSERS
        self._parsers: Dict[str, Parser] = {}
        for parser in parser_list:
            for ext in parser.extensions:
                self._parsers[ext.lower()] = parser

        # 支持扩展名（与注册表取交集）
        if extensions is None:
            self.extensions = sorted(self._parsers.keys())
        else:
            self.extensions = [
                e.lower() for e in extensions if e.lower() in self._parsers
            ]

    # ================================================================
    # 目录加载
    # ================================================================

    def load(self) -> List[Document]:
        """
        加载所有源目录下的支持文档

        Returns:
            List[Document]: 文档列表
        """
        documents = []

        for source_dir in self.source_dirs:
            if not source_dir.exists():
                logger.warning("目录不存在，已跳过: %s", source_dir)
                continue

            for file_path in self._walk_files(source_dir):
                doc = self._load_single_file(file_path)
                if doc:
                    documents.append(doc)

        return documents

    def load_single(self, file_path: str) -> Optional[Document]:
        """
        加载单个文件

        Args:
            file_path: 文件路径

        Returns:
            Optional[Document]: 文档对象，失败返回 None
        """
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            return None
        return self._load_single_file(path)

    # ================================================================
    # URL 加载
    # ================================================================

    def load_url(self, url: str, timeout: float = 30.0) -> Optional[Document]:
        """
        抓取远程网页并解析为文档

        Args:
            url: 网页地址
            timeout: 请求超时秒数

        Returns:
            Optional[Document]: 文档对象，失败返回 None
        """
        import httpx

        try:
            response = httpx.get(
                url,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "rag-service/1.0"},
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning("抓取 URL 失败: %s - %s", url, e)
            return None

        parser = self._parsers.get(".html")
        if parser is None:
            logger.warning("未注册 HTML 解析器，无法解析网页")
            return None

        try:
            parsed = parser.parse_bytes(response.content, url)
        except Exception as e:
            logger.warning("解析网页失败: %s - %s", url, e)
            return None

        content = parsed.content.strip()
        if not content:
            return None

        # URL 文档：file_path 记 URL，file_name 用标题或域名
        from urllib.parse import urlparse
        host = urlparse(url).netloc or url
        file_name = parsed.title or host

        # 构建元数据
        metadata = {
            'file_path': url,
            'file_name': file_name,
            'source': 'url',
            'url': url,
            'title': parsed.title or "",
            'fetch_time': datetime.now().isoformat(),
        }
        metadata.update(parsed.metadata)

        doc_id = self._generate_id(url)
        return Document(
            id=doc_id,
            file_path=url,
            file_name=file_name,
            content=content,
            metadata=metadata,
        )

    # ================================================================
    # 内部工具
    # ================================================================

    def _walk_files(self, source_dir: Path) -> List[Path]:
        """递归遍历目录，返回所有支持的文档文件"""
        files = []

        for root, dirs, filenames in os.walk(source_dir):
            # 跳过隐藏目录与用户确认忽略的目录
            dirs[:] = [
                d for d in dirs
                if not d.startswith('.') and d not in self.EXCLUDE_DIRS
            ]

            for filename in filenames:
                file_path = Path(root) / filename
                if file_path.suffix.lower() in self.extensions:
                    files.append(file_path)

        return files

    def _load_single_file(self, file_path: Path) -> Optional[Document]:
        """加载单个文件：按扩展名路由解析器，组装文档对象"""
        parser = self._parsers.get(file_path.suffix.lower())
        if parser is None:
            return None

        try:
            parsed = parser.parse_file(file_path)
        except Exception as e:
            logger.warning("解析文件失败: %s - %s", file_path, e)
            return None

        content = parsed.content.strip()
        if not content:
            return None

        # 生成文档 ID（附件目录等需要）
        doc_id = self._generate_id(str(file_path))

        # 构建元数据
        metadata = {
            'file_path': str(file_path),
            'file_name': file_path.name,
            'file_size': file_path.stat().st_size,
            'modified_time': datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
            'source_dir': str(file_path.parent),
            'format': file_path.suffix.lower().lstrip('.'),
        }
        if parsed.title:
            metadata['title'] = parsed.title
        metadata.update(parsed.metadata)

        # 多模态：保存提取的内嵌图片到附件目录，并记录到元数据
        if parsed.images:
            metadata['images'] = self._save_images(parsed.images, doc_id, file_path.name)

        return Document(
            id=doc_id,
            file_path=str(file_path),
            file_name=file_path.name,
            content=content,
            metadata=metadata,
        )

    def _save_images(self, images: List[Dict[str, Any]], doc_id: str, file_name: str) -> List[Dict[str, Any]]:
        """
        把解析器提取的图片字节落盘到附件目录（多模态：上下文字幕方案）

        Args:
            images: 解析器返回的图片列表 [{caption, bytes, ext}]
            doc_id: 文档 ID（用作附件子目录）
            file_name: 文档文件名（用于说明）

        Returns:
            List[Dict]: [{path, caption}]；单张失败跳过
        """
        saved = []
        if not images:
            return saved
        dest_dir = self.attachment_dir / doc_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        for i, img in enumerate(images, 1):
            try:
                ext = (img.get("ext") or "png").lower().strip(".")
                if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
                    ext = "png"
                path = dest_dir / f"img_{i}.{ext}"
                path.write_bytes(img["bytes"])
                saved.append({
                    "path": str(path),
                    "caption": img.get("caption", ""),
                    "source_file": file_name,
                })
            except Exception as e:
                logger.warning("附件图片保存失败: %s", e)
        return saved

    @staticmethod
    def _generate_id(file_path: str) -> str:
        """生成文档 ID（文件路径的 MD5 前 16 位）"""
        import hashlib
        return hashlib.md5(file_path.encode()).hexdigest()[:16]

    @classmethod
    def doc_id_for(cls, file_path: str) -> str:
        """为文件路径生成稳定的文档 ID（与 Document.id 保持一致，供增量索引使用）"""
        return cls._generate_id(file_path)