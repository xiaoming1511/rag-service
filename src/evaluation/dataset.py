"""
评测数据集加载与校验

数据集格式（JSON 数组，每项）：
{
    "id": "redis-001",
    "question": "Redis 支持哪两种持久化方式？",
    "gold_docs": ["Redis 详细指南.md"],      # gold 文档（file_name，含扩展名）
    "gold_keywords": ["RDB", "AOF"],         # 期望出现在正确答案中的关键词（供人工/judge 参考）
    "note": "可选备注"
}

gold_docs 匹配规则：检索结果 metadata.file_name 与 gold 精确相等即命中；
也允许写文件路径后缀（以 / 结尾或包含路径时按 basename 归一）。
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Set

REQUIRED_FIELDS = {"id", "question", "gold_docs"}
OPTIONAL_FIELDS = {"gold_keywords", "note"}


def normalize_doc_id(name: str) -> str:
    """归一化文档标识：取 basename，便于 gold_docs 写路径或文件名均可"""
    return Path(name).name


class EvalDataset:
    """评测数据集"""

    def __init__(self, items: List[Dict[str, Any]]):
        self.items = items

    def __len__(self) -> int:
        return len(self.items)

    @classmethod
    def load(cls, path: str) -> "EvalDataset":
        """加载并校验数据集，格式错误时抛 ValueError（fail-fast）"""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"评测集不存在: {p}")
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list) or not raw:
            raise ValueError("评测集必须是非空 JSON 数组")

        seen_ids: Set[str] = set()
        for i, item in enumerate(raw):
            # fail-fast 的报错必须指向数据本身：非 dict 项先明说，
            # 否则会在 item.keys() 上抛 AttributeError，报错与真实原因无关
            if not isinstance(item, dict):
                raise ValueError(
                    f"评测集第 {i} 项必须是 JSON 对象，实际为 {type(item).__name__}"
                )
            missing = REQUIRED_FIELDS - set(item.keys())
            if missing:
                raise ValueError(f"评测集第 {i} 项缺少必填字段: {sorted(missing)}")
            if not isinstance(item["gold_docs"], list) or not item["gold_docs"]:
                raise ValueError(f"评测集第 {i} 项（{item['id']}）gold_docs 必须是非空数组")
            if any(not isinstance(d, str) or not d.strip() for d in item["gold_docs"]):
                raise ValueError(f"评测集第 {i} 项（{item['id']}）gold_docs 元素必须是非空字符串")
            if item["id"] in seen_ids:
                raise ValueError(f"评测集 id 重复: {item['id']}")
            seen_ids.add(item["id"])

        return cls(raw)

    def gold_set(self, item: Dict[str, Any]) -> Set[str]:
        """单条评测项的 gold 文档集合（归一化为 basename）"""
        return {normalize_doc_id(d) for d in item["gold_docs"]}
