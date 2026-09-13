"""
Deep Research 简版（决策：一层展开）

流程：
1. 用 LLM 把研究问题拆解为 N 个子查询；
2. 并行检索各子查询并合并去重；
3. 把合并结果作为上下文，用 LLM 汇总为带引用的研究报告。
"""

import concurrent.futures
import re
from typing import Any, Dict, List, Optional

from src.retrieval.retriever import Retriever
from src.retrieval.context_builder import build_context
from src.generation.generator import Generator

from src.logging_setup import get_logger

logger = get_logger(__name__)


# 列表前缀：仅剥离「编号 + 分隔符」或「项目符号」。
# 旧实现用 str.lstrip("0123456789.、）)]-·*# ") 无差别剥离行首字符集，
# 会把合法子问题开头的数字一起吃掉（"3D 渲染管线如何工作？" → "D 渲染管线如何工作？"，
# "2025 年的新变化" → "年的新变化"），静默污染后续检索查询。
# 因此数字必须**紧跟分隔符**才算编号前缀。
_LIST_PREFIX_RE = re.compile(r"^\s*(?:\d+\s*[.、)）\]】:：]|[-–—·•*#]+\s*)\s*")


def parse_sub_queries(text: str, count: Optional[int] = None) -> List[str]:
    """
    解析 LLM 输出的子问题列表（每行一个，容忍编号/符号前缀）

    Args:
        text: LLM 原始输出
        count: 最多取前 N 个

    Returns:
        List[str]: 去重后的子问题列表
    """
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # 去掉 "1."、"1、"、"1）"、"· "、"* " 等前缀（保留行首数字本身）
        cleaned = _LIST_PREFIX_RE.sub("", stripped)
        cleaned = cleaned.strip("\"'「」【】").strip()
        if cleaned and cleaned not in lines:
            lines.append(cleaned)
        if count and len(lines) >= count:
            break
    return lines


class DeepResearch:
    """Deep Research（递归多轮：配置最大轮次 + 新信息增益停止）"""

    SUB_QUERY_PROMPT = (
        "你是研究助手。请把用户的研究问题拆解为 {n} 个独立、可检索的子问题，"
        "覆盖概念定义、原理机制、应用场景与对比等角度。\n"
        "每行一个子问题，不要编号外的多余文字。"
    )

    FOLLOWUP_PROMPT = (
        "你是研究助手。基于当前的研究报告，提出 {n} 个需要进一步查证的子问题，"
        "用于补全缺失角度（事实、机制、对比、反例）。\n"
        "每行一个子问题，不要多余文字。"
    )

    def __init__(
            self,
            retriever: Retriever,
            generator: Generator,
            sub_query_count: int = 4,
            top_k_per_query: int = 3,
            max_results: int = 8,
            max_context_tokens: Optional[int] = None,
            max_rounds: int = 2,
    ):
        self.retriever = retriever
        self.generator = generator
        self.sub_query_count = max(1, min(sub_query_count, 8))
        self.top_k_per_query = top_k_per_query
        self.max_results = max_results
        # R9-2：上下文预算统一走 token 口径，None 回落 RetrievalConfig 字段默认值
        # （与 Retriever 的单一真源约定一致）；旧参数 max_context_length 为字符
        # 硬截口径，已废弃且无外部调用方。
        if max_context_tokens is None:
            from src.config import RetrievalConfig
            max_context_tokens = RetrievalConfig().context_token_budget
        self.max_context_tokens = max_context_tokens
        # 递归最大轮次（决策：配置最大轮次 + 新信息增益停止）
        self.max_rounds = max(1, min(max_rounds, 5))

    # ================================================================
    # 子查询生成
    # ================================================================

    def _generate_sub_queries(self, question: str) -> List[str]:
        """调用 LLM 生成子查询；失败时退化为 [问题本身]"""
        client = getattr(self.generator, "client", None)
        if client is None:
            return [question]

        messages = [
            {"role": "system", "content": self.SUB_QUERY_PROMPT.format(n=self.sub_query_count)},
            {"role": "user", "content": f"研究问题：{question}"},
        ]
        try:
            # 模型路由：research_subqueries 任务；兼容无 model_for 的桩生成器
            if hasattr(self.generator, "model_for"):
                model = self.generator.model_for("research_subqueries")
            else:
                model = getattr(self.generator, "model", None)
            raw = client.chat_sync(
                model=model,
                messages=messages,
                max_tokens=200,
                temperature=0.3,
            )
            subs = parse_sub_queries(raw, count=self.sub_query_count)
            return subs or [question]
        except Exception as e:
            logger.warning("⚠️ 子查询生成失败，退化为原问题: %s", e)
            return [question]

    # ================================================================
    # 研究主流程
    # ================================================================

    def research(
            self,
            question: str,
            sub_queries: Optional[List[str]] = None,
            max_rounds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        执行一次深度研究（递归多轮：配置最大轮次 + 新信息增益停止）

        Args:
            question: 研究问题
            sub_queries: 自定义子查询列表（None 则用 LLM 生成）
            max_rounds: 本轮最大轮次（None 使用构造配置值；每轮=拆解→检索→汇总→生成追问）

        Returns:
            Dict: report（研究报告）/ sub_queries / sources / total_results / rounds
        """
        rounds = self.max_rounds if max_rounds is None else max(1, min(max_rounds, 5))
        merged: Dict[str, Any] = {}
        all_subs = list(sub_queries or [])
        current_subs = sub_queries or self._generate_sub_queries(question)
        report = ""

        for round_idx in range(rounds):
            # 1. 并行检索本轮子查询
            subs = current_subs or [question]
            all_subs.extend(s for s in subs if s not in all_subs)
            results_list = self._retrieve_all(subs)

            # 2. 合并去重（按结果 id）
            before = len(merged)
            for res in results_list:
                for r in res:
                    if r.id not in merged:
                        merged[r.id] = r

            # 3. 新信息增益停止：本轮没有新结果则提前结束
            if round_idx > 0 and len(merged) == before:
                logger.info("⏹️ Deep Research 提前停止（本轮无新信息）")
                break

            ranked = sorted(merged.values(), key=lambda r: r.score, reverse=True)
            ranked = ranked[: self.max_results]

            # 4. 生成（阶段性）报告
            context = self._build_context(ranked)
            report = self.generator.generate(query=question, context=context)

            # 5. 生成追问，进入下一轮
            if round_idx + 1 < rounds:
                followups = self._generate_followups(question, report, count=3)
                current_subs = followups
                if not current_subs:
                    break

        sources = [
            {
                "file_name": r.metadata.get("file_name", "unknown"),
                "file_path": r.metadata.get("file_path", ""),
                "heading": r.metadata.get("heading_path", ""),
                "line_start": r.metadata.get("start_line") or None,
                "line_end": r.metadata.get("end_line") or None,
                "images": r.metadata.get("images") or None,
                "score": r.score,
            }
            for r in ranked
        ]

        return {
            "report": report,
            "sub_queries": all_subs,
            "sources": sources,
            "total_results": len(ranked),
            "rounds": round_idx + 1,
        }

    # ================================================================
    # 内部工具
    # ================================================================

    def _retrieve_all(self, subs: List[str]):
        """并行检索一组子查询

        单个子查询检索失败不中断整体研究（与评测 runner 的"单条失败不中断"
        约定一致）：该路返回空列表（融合阶段过滤空列表），其余子查询照常合并。
        否则 ex.map 会把第一个异常在迭代时抛出，一个坏子查询就让整次研究 500。
        """
        def _one(q: str) -> list:
            try:
                return self.retriever.retrieve(
                    q, top_k=self.top_k_per_query, use_rerank=True
                )
            except Exception as e:
                logger.warning("⚠️ 子查询检索失败，已跳过 [%s]: %s", q, e)
                return []

        workers = min(len(subs), 4)
        if workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                return list(ex.map(_one, subs))
        return [_one(q) for q in subs]

    def _build_context(self, ranked) -> str:
        """构建上下文：复用检索层的 token 感知装配（R9-2）

        旧实现把拼接后的整串按字符数硬截——会拦腰截断最后一条来源，且字符口径
        与主问答链路的 token 预算不一致。build_context 保留整块、仅在预算不足时
        于行边界截断，格式（[来源 > 标题] + \n\n---\n\n）与本处原实现一致。
        """
        return build_context(list(ranked), self.max_context_tokens)

    def _generate_followups(self, question: str, report: str, count: int = 3) -> List[str]:
        """基于当前报告生成追问；失败返回空列表"""
        client = getattr(self.generator, "client", None)
        if client is None:
            return []
        messages = [
            {"role": "system", "content": self.FOLLOWUP_PROMPT.format(n=count)},
            {"role": "user", "content": f"研究问题：{question}\n\n当前报告：\n{report[:3000]}"},
        ]
        try:
            if hasattr(self.generator, "model_for"):
                model = self.generator.model_for("research_subqueries")
            else:
                model = getattr(self.generator, "model", None)
            raw = client.chat_sync(model=model, messages=messages, max_tokens=200, temperature=0.3)
            return parse_sub_queries(raw, count=count)
        except Exception as e:
            logger.warning("⚠️ 追问生成失败: %s", e)
            return []