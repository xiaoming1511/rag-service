"""
文档分块器
按 Markdown 标题层级切分文档，保持语义完整性；超长章节按固定大小二次切分
"""

import re
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

from src.document.loader import Document


@dataclass
class Chunk:
    """文档块数据模型"""
    id: str  # 块唯一标识
    content: str  # 块内容
    metadata: Dict[str, Any] = field(default_factory=dict)  # 元数据

    @property
    def source(self) -> str:
        """来源文件名"""
        return self.metadata.get('file_name', 'unknown')

    @property
    def heading_path(self) -> str:
        """标题路径（如：基础语法 > 变量）"""
        return self.metadata.get('heading_path', '')


class Chunker:
    """文档分块器 - 按 Markdown 标题切分"""

    def __init__(self, chunk_size: int = 800, overlap: int = 100, strategy: str = "heading"):
        """
        初始化分块器

        Args:
            chunk_size: 每块最大字符数
            overlap: 块间重叠字符数
            strategy: 分块策略 (heading | fixed)
        """
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.strategy = strategy

    def chunk_document(self, document: Document) -> List[Chunk]:
        """
        将单个文档切分为多个块

        Args:
            document: 文档对象

        Returns:
            List[Chunk]: 文档块列表
        """
        if self.strategy == "heading":
            return self._chunk_by_heading(document)
        return self._chunk_by_fixed(document)

    def chunk_documents(self, documents: List[Document]) -> List[Chunk]:
        """
        批量切分文档

        Args:
            documents: 文档列表

        Returns:
            List[Chunk]: 所有文档块列表
        """
        all_chunks = []
        for doc in documents:
            chunks = self.chunk_document(doc)
            all_chunks.extend(chunks)
        return all_chunks

    # ================================================================
    # 策略一：按标题层级切分
    # ================================================================

    def _chunk_by_heading(self, document: Document) -> List[Chunk]:
        """
        按 Markdown 标题层级切分

        规则：
        1. 按 #, ##, ### 等标题分割
        2. 每个块包含一个标题及其下的内容
        3. 如果某个块超过 chunk_size，进一步按段落切分
        """
        content = document.content
        lines = content.split('\n')

        # 解析标题结构
        sections = []
        current_heading = "root"
        current_heading_level = 0
        current_content = []
        heading_stack = ["root"]

        # 正则匹配标题行（1-6 级标题）
        heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$')

        i = 0
        while i < len(lines):
            line = lines[i]
            match = heading_pattern.match(line)

            if match:
                # 保存之前的段落
                if current_content:
                    sections.append({
                        'heading': current_heading,
                        'heading_level': current_heading_level,
                        'heading_path': ' > '.join(heading_stack[1:]) if len(heading_stack) > 1 else '',
                        'content': '\n'.join(current_content).strip()
                    })
                    current_content = []

                # 维护标题层级栈（同级或更高级标题弹出栈顶）
                level = len(match.group(1))
                title = match.group(2).strip()

                while len(heading_stack) > level:
                    heading_stack.pop()
                heading_stack.append(title)

                current_heading = title
                current_heading_level = level

            else:
                # 非标题行，累积内容
                current_content.append(line)

            i += 1

        # 保存最后一个段落
        if current_content:
            sections.append({
                'heading': current_heading,
                'heading_level': current_heading_level,
                'heading_path': ' > '.join(heading_stack[1:]) if len(heading_stack) > 1 else '',
                'content': '\n'.join(current_content).strip()
            })

        # 过滤空块，构建 Chunk 对象
        chunks = []
        for idx, section in enumerate(sections):
            content = section['content']
            if not content:
                continue

            # 如果内容过长，按固定大小再切分（子块共享切片索引）
            if len(content) > self.chunk_size:
                sub_chunks = self._split_long_content(
                    content=content,
                    heading=section['heading'],
                    heading_level=section['heading_level'],
                    heading_path=section['heading_path'],
                    section_index=idx,
                    document=document,
                )
                chunks.extend(sub_chunks)
            else:
                chunk = Chunk(
                    id=f"{document.id}_{idx}",
                    content=content,
                    metadata=self._build_metadata(
                        document=document,
                        heading=section['heading'],
                        heading_level=section['heading_level'],
                        heading_path=section['heading_path'],
                        chunk_index=idx,
                    )
                )
                chunks.append(chunk)

        # 如果没有分块（文档可能只有标题没有内容），返回空
        if not chunks:
            return []

        return chunks

    # ================================================================
    # 策略二：按固定大小切分
    # ================================================================

    def _chunk_by_fixed(self, document: Document) -> List[Chunk]:
        """
        按固定大小切分（备用策略）
        """
        content = document.content
        chunks = []

        # 按段落分割
        paragraphs = content.split('\n\n')

        current_chunk = ""
        chunk_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current_chunk) + len(para) > self.chunk_size and current_chunk:
                chunks.append(Chunk(
                    id=f"{document.id}_{chunk_idx}",
                    content=current_chunk.strip(),
                    metadata=self._build_metadata(
                        document=document,
                        heading="",
                        heading_level=0,
                        heading_path="",
                        chunk_index=chunk_idx,
                    )
                ))
                chunk_idx += 1
                # 保留重叠部分
                current_chunk = current_chunk[-self.overlap:] if self.overlap else ""

            current_chunk += para + "\n\n"

        # 最后一个块
        if current_chunk.strip():
            chunks.append(Chunk(
                id=f"{document.id}_{chunk_idx}",
                content=current_chunk.strip(),
                metadata=self._build_metadata(
                    document=document,
                    heading="",
                    heading_level=0,
                    heading_path="",
                    chunk_index=chunk_idx,
                )
            ))

        return chunks

    # ================================================================
    # 内部工具
    # ================================================================

    def _build_metadata(
            self,
            document: Document,
            heading: str,
            heading_level: int,
            heading_path: str,
            chunk_index: int,
    ) -> Dict[str, Any]:
        """构建统一的块元数据（同步了 doc_id / file_name / file_path 等）"""
        return {
            'doc_id': document.id,
            'file_name': document.file_name,
            'file_path': document.file_path,
            'heading': heading,
            'heading_level': heading_level,
            'heading_path': heading_path,
            'chunk_index': chunk_index,
        }

    def _split_long_content(
            self,
            content: str,
            heading: str,
            heading_level: int,
            heading_path: str,
            section_index: int,
            document: Document,
    ) -> List[Chunk]:
        """
        切分过长的章节内容（按段落切分）

        子块 ID 形如 "{doc_id}_{章节序号}_{子块序号}"，
        保证跨文档全局唯一，避免向量库 ID 冲突。

        Args:
            content: 章节内容
            heading: 章节标题
            heading_level: 标题级别
            heading_path: 标题路径
            section_index: 章节在文档中的序号
            document: 所属文档

        Returns:
            List[Chunk]: 子块列表
        """
        chunks = []
        paragraphs = content.split('\n\n')

        current = ""
        chunk_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if len(current) + len(para) > self.chunk_size and current:
                chunks.append(Chunk(
                    id=f"{document.id}_{section_index}_{chunk_idx}",
                    content=current.strip(),
                    metadata=self._build_metadata(
                        document=document,
                        heading=heading,
                        heading_level=heading_level,
                        heading_path=heading_path,
                        chunk_index=section_index,
                    ) | {'sub_index': chunk_idx},
                ))
                chunk_idx += 1
                current = current[-self.overlap:] if self.overlap else ""

            current += para + "\n\n"

        if current.strip():
            chunks.append(Chunk(
                id=f"{document.id}_{section_index}_{chunk_idx}",
                content=current.strip(),
                metadata=self._build_metadata(
                    document=document,
                    heading=heading,
                    heading_level=heading_level,
                    heading_path=heading_path,
                    chunk_index=section_index,
                ) | {'sub_index': chunk_idx},
            ))

        return chunks

    def get_chunk_info(self, chunks: List[Chunk]) -> Dict[str, Any]:
        """获取分块统计信息"""
        if not chunks:
            return {'total': 0, 'avg_size': 0, 'max_size': 0, 'min_size': 0}

        sizes = [len(c.content) for c in chunks]
        return {
            'total': len(chunks),
            'avg_size': sum(sizes) / len(sizes),
            'max_size': max(sizes),
            'min_size': min(sizes),
        }