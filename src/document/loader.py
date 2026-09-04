"""
文档加载器
从指定目录递归加载 Markdown 文件
"""

import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime

import yaml


@dataclass
class Document:
    """文档数据模型"""
    id: str  # 文档唯一标识（文件路径的哈希）
    file_path: str  # 文件绝对路径
    file_name: str  # 文件名
    content: str  # 原始内容
    metadata: Dict[str, Any] = field(default_factory=dict)  # 元数据

    @property
    def source(self) -> str:
        """来源标识（用于引用）"""
        return self.file_name


class DocumentLoader:
    """文档加载器"""

    def __init__(self, source_dirs: List[str], extensions: List[str] = None):
        """
        初始化加载器

        Args:
            source_dirs: 源目录列表
            extensions: 支持的文件扩展名，默认 ['.md', '.markdown']
        """
        self.source_dirs = [Path(d).expanduser().resolve() for d in source_dirs]
        self.extensions = extensions or ['.md', '.markdown']

    def load(self) -> List[Document]:
        """
        加载所有源目录下的 Markdown 文件

        Returns:
            List[Document]: 文档列表
        """
        documents = []

        for source_dir in self.source_dirs:
            if not source_dir.exists():
                print(f"⚠️ 目录不存在，已跳过: {source_dir}")
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

    def _walk_files(self, source_dir: Path) -> List[Path]:
        """递归遍历目录，返回所有支持的 Markdown 文件"""
        files = []

        for root, dirs, filenames in os.walk(source_dir):
            # 跳过隐藏目录和常见非内容目录
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'wiki' and d != 'node_modules']

            for filename in filenames:
                file_path = Path(root) / filename
                if file_path.suffix.lower() in self.extensions:
                    files.append(file_path)

        return files

    def _load_single_file(self, file_path: Path) -> Optional[Document]:
        """加载单个文件，提取内容和元数据"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if not content.strip():
                return None

            # 构建元数据
            metadata = {
                'file_path': str(file_path),
                'file_name': file_path.name,
                'file_size': file_path.stat().st_size,
                'modified_time': datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                'source_dir': str(file_path.parent),
            }

            # 提取 Frontmatter（如果存在）
            if content.startswith('---'):
                try:
                    parts = content.split('---', 2)
                    if len(parts) >= 3:
                        frontmatter = yaml.safe_load(parts[1])
                        if frontmatter:
                            metadata.update(frontmatter)
                        content = parts[2].strip()
                except:
                    pass  # Frontmatter 解析失败，忽略

            # 生成文档 ID
            doc_id = self._generate_id(str(file_path))

            return Document(
                id=doc_id,
                file_path=str(file_path),
                file_name=file_path.name,
                content=content,
                metadata=metadata
            )

        except Exception as e:
            print(f"⚠️ 加载文件失败: {file_path} - {e}")
            return None

    @staticmethod
    def _generate_id(file_path: str) -> str:
        """生成文档 ID（文件路径的 MD5 前 16 位）"""
        import hashlib
        return hashlib.md5(file_path.encode()).hexdigest()[:16]

    @classmethod
    def doc_id_for(cls, file_path: str) -> str:
        """为文件路径生成稳定的文档 ID（与 Document.id 保持一致，供增量索引使用）"""
        return cls._generate_id(file_path)