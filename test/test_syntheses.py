"""
问答沉淀（syntheses）测试
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pipeline.syntheses import resolve_syntheses_dir, save_syntheses


def test_resolve_syntheses_dir(tmp_path):
    """目录解析：配置优先，其次 source_dirs[0]/syntheses"""
    # 显式配置
    assert resolve_syntheses_dir([], str(tmp_path / "custom")) == tmp_path / "custom"
    # 自动推导
    auto = resolve_syntheses_dir([str(tmp_path / "vault")])
    assert auto == tmp_path / "vault" / "syntheses"


def test_save_syntheses(tmp_path):
    """写入文件：frontmatter、问题、回答、来源"""
    out = tmp_path / "syntheses"
    sources = [
        {"file_name": "redis.md", "file_path": "/vault/redis.md",
         "heading": "数据类型", "score": 0.98, "content": "..."},
        {"file_name": "python.md", "file_path": "/vault/python.md",
         "heading": "", "score": 0.81, "content": "..."},
    ]
    path = save_syntheses(
        "Redis 支持哪些数据结构？",
        "字符串、列表、哈希等。",
        sources,
        out,
    )

    assert path is not None
    assert path.exists() and path.suffix == ".md"
    content = path.read_text(encoding="utf-8")

    assert content.startswith("---")
    assert "type: synthesis" in content
    assert "Redis 支持哪些数据结构？" in content
    assert "字符串、列表、哈希等。" in content
    assert "redis.md" in content          # 来源引用
    assert "数据类型" in content           # 标题锚点
    assert "0.980" in content
    assert "/vault/redis.md" in content   # 文件路径


def test_save_syntheses_skips_empty(tmp_path):
    """空问题或空回答不写入"""
    assert save_syntheses("", "答案", [], tmp_path) is None
    assert save_syntheses("问题", "", [], tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_save_syntheses_unique(tmp_path):
    """相同问题+回答不覆盖（文件名带哈希唯一）"""
    p1 = save_syntheses("问题", "回答", [], tmp_path)
    p2 = save_syntheses("问题", "回答", [], tmp_path)
    assert p1 is not None and p2 is not None
    assert p1 != p2
    assert len(list(tmp_path.iterdir())) == 2


def test_save_syntheses_creates_dir(tmp_path):
    """目录不存在时自动创建（含多级）"""
    out = tmp_path / "a" / "b" / "syntheses"
    path = save_syntheses("Q", "A", [{"file_name": "x.md"}], out)
    assert path is not None and path.exists()