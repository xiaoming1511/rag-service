"""
LLM-as-judge 生成质量评测（决策 B1）

用本地聊天模型对「问题 + 检索上下文 + 生成的回答」打分：
- faithfulness（忠实度）: 回答是否严格由上下文支撑、无编造
- answer_relevance（答案相关性）: 回答是否切题、完整回答了问题

设计约束：
- 只依赖 OMLXClient（本项目唯一 LLM 通道），无新依赖
- judge 输出约定 "SCORE: <0~1>"，解析失败该项记为无效不计入均值
- 失败（服务不可达等）抛给调用方由 runner 汇总为 judge_error
"""

from typing import Dict, List

from src.evaluation.metrics import parse_judge_score
from src.logging_setup import get_logger

logger = get_logger(__name__)

FAITHFULNESS_PROMPT = """你是一个严格的评测裁判。请根据给定的上下文判断「回答」的忠实度：
- 回答中的每个关键论断是否都能在上下文中找到依据？
- 是否存在上下文不支持的编造内容？
- 允许回答对上下文进行合理概括，但必须无外部事实编造。

上下文：
{context}

回答：
{answer}

请只输出一行：SCORE: <0到1之间的小数>
1 = 完全忠实，0 = 完全编造。不要输出其他内容。"""

RELEVANCE_PROMPT = """你是一个严格的评测裁判。请判断「回答」对「问题」的相关性与完整度：
- 回答是否切题（答的是所问）？
- 回答是否覆盖了问题的核心要点（可依据参考关键词）？

问题：{question}
参考关键词：{keywords}

回答：
{answer}

请只输出一行：SCORE: <0到1之间的小数>
1 = 完全切题且完整，0 = 答非所问。不要输出其他内容。"""


class LLMJudge:
    """基于本地聊天模型的裁判"""

    def __init__(self, client, model: str, max_tokens: int = 32, temperature: float = 0.0):
        """
        Args:
            client: OMLXClient 实例（或提供 chat_sync 的对象，便于测试打桩）
            model: 裁判用聊天模型（建议与主模型一致或更强的本地模型）
        """
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _score(self, prompt: str) -> float:
        raw = self.client.chat_sync(
            self.model,
            [{"role": "user", "content": prompt}],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        return parse_judge_score(raw)

    def faithfulness(self, question: str, context: str, answer: str) -> float:
        """忠实度评分（0~1；解析失败返回 -1.0）"""
        prompt = FAITHFULNESS_PROMPT.format(context=context, answer=answer)
        return self._score(prompt)

    def answer_relevance(self, question: str, answer: str,
                         gold_keywords: List[str]) -> float:
        """答案相关性评分（0~1；解析失败返回 -1.0）"""
        prompt = RELEVANCE_PROMPT.format(
            question=question,
            keywords="、".join(gold_keywords) if gold_keywords else "（无）",
            answer=answer,
        )
        return self._score(prompt)

    def evaluate_generation(self, question: str, context: str, answer: str,
                            gold_keywords: List[str]) -> Dict[str, float]:
        """一次性评两条生成指标，异常抛给调用方"""
        return {
            "faithfulness": self.faithfulness(question, context, answer),
            "answer_relevance": self.answer_relevance(question, answer, gold_keywords),
        }
