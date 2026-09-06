"""
问答沉淀（syntheses）

把每次问答（问题 + 回答 + 来源）写为 vault 根 syntheses/ 下的 .md 文件。
该目录位于 vault 根（非排除目录），会被加载器索引，
从而使回答可被后续问答再次检索 —— 形成"知识记忆循环"（复利）。

每篇沉淀文件包含：
- YAML frontmatter（日期、问题、来源列表）
- 问题、回答正文、来源清单（含文件路径与标题锚点）
"""

import hashlib
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logging_setup import get_logger

logger = get_logger(__name__)


def resolve_syntheses_dir(
    source_dirs: List[str],
    configured: str = "",
) -> Path:
    """
    解析沉淀目录

    Args:
        source_dirs: 加载器源目录列表
        configured: 配置中显式指定的目录；空则自动取 source_dirs[0]/syntheses

    Returns:
        Path: 沉淀目录的绝对路径
    """
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(source_dirs[0]).expanduser().resolve() if source_dirs else Path(".")
    return root / "syntheses"


def _slug(text: str, max_len: int = 40) -> str:
    """生成文件名片段：去特殊字符、截断"""
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", text.strip()).strip("-")
    return slug[:max_len] or "qa"


def _short_hash(question: str, answer: str) -> str:
    """问题+答案的短哈希（供日志/调试使用）"""
    raw = f"{question}|{answer}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:8]


def save_syntheses(
    question: str,
    answer: str,
    sources: List[Dict[str, Any]],
    out_dir: Path,
) -> Optional[Path]:
    """
    保存一次问答沉淀

    Args:
        question: 用户问题
        answer: 回答文本
        sources: 来源列表 [{file_name, file_path, heading, score, content}, ...]
        out_dir: 沉淀目录

    Returns:
        Optional[Path]: 写入的文件路径；失败返回 None
    """
    if not question.strip() or not answer.strip():
        return None

    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d")
        # uuid 短片段保证同名问题多次沉淀也不覆盖
        filename = f"{date_str}-{_slug(question)}-{uuid.uuid4().hex[:8]}.md"
        file_path = out_dir / filename

        # 构建正文
        source_lines = []
        for s in sources or []:
            heading = f" ({s.get('heading')})" if s.get("heading") else ""
            score = f"{s.get('score', 0):.3f}" if isinstance(s.get("score"), float) else ""
            source_lines.append(
                f"- [[{s.get('file_name', 'unknown')}]]{heading} · 分数 {score} · `{s.get('file_path', '')}`"
            )

        frontmatter = (
            "---\n"
            f"type: synthesis\n"
            f"date: {datetime.now().isoformat(timespec='seconds')}\n"
            f"question: {question}\n"
            f"source_count: {len(sources or [])}\n"
            "---\n"
        )
        content_parts = [
            frontmatter,
            f"# {question}",
            "",
            answer.strip(),
            "",
            f"## 来源 ({len(sources or [])})",
            "",
            *source_lines,
            "",
        ]
        content = "\n".join(content_parts)

        file_path.write_text(content, encoding="utf-8")
        return file_path
    except Exception as e:
        logger.warning("问答沉淀写入失败: %s", e)
        return None