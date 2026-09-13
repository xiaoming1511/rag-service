"""
模型路由（Model Routing）

作用：让不同任务使用不同模型，避免单一模型成为瓶颈——
- 简单任务（追问改写、子查询拆解）可配置轻量/快速模型，提速省钱；
- 复杂任务（问答生成）可配置更强模型，保证质量；
- 未配置的任务回退到默认聊天模型（omlx.chat_model）。

任务清单：chat（问答生成）、rewrite（追问改写）、research_subqueries（Deep Research 拆解）
"""

from typing import Dict, Optional


class ModelRouter:
    """任务 → 模型 的路由表"""

    SUPPORTED_TASKS = ("chat", "rewrite", "research_subqueries")

    def __init__(
            self,
            default_model: str,
            tasks: Optional[Dict[str, str]] = None,
    ):
        """
        初始化路由

        Args:
            default_model: 默认聊天模型（回退目标）
            tasks: 任务 → 模型 映射（空值表示跟随默认）
        """
        self.default_model = default_model
        self._tasks: Dict[str, str] = {t: "" for t in self.SUPPORTED_TASKS}
        if tasks:
            self.update(tasks)

    def update(self, tasks: Dict[str, str]):
        """批量更新任务映射（服务运行时可热更新）

        先构造完整新字典再一次原子替换，避免并发 resolve/tasks 读到
        「部分任务已切换、部分仍旧值」的中间态。
        """
        if not tasks:
            return
        new_tasks = dict(self._tasks)
        for key, value in tasks.items():
            if key in self.SUPPORTED_TASKS:
                new_tasks[key] = (value or "").strip()
        self._tasks = new_tasks  # 单次引用赋值，原子替换

    def resolve(self, task: str) -> str:
        """
        解析任务应使用的模型

        Args:
            task: 任务名（chat / rewrite / research_subqueries）

        Returns:
            str: 模型名；未配置或任务未知时返回默认模型
        """
        if task in self.SUPPORTED_TASKS and self._tasks.get(task):
            return self._tasks[task]
        return self.default_model

    @property
    def tasks(self) -> Dict[str, str]:
        """当前任务映射（副本）"""
        return dict(self._tasks)