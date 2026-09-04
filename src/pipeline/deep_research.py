"""
Deep Research 简版（决策：一层展开）

流程：
1. 用 LLM 把研究问题拆解为 N 个子查询；
2. 并行检索各子查询并合并去重；
3. 把合并结果作为上下文，用 LLM 汇总为带引用的研究报告。
"""

import concurrent.futures
from typing import Any, Dict, List, Optional

from src.retrieval.retriever import Retriever
from src.generation.generator import Generator


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
        # 去掉 "1."、"1、"、"1）"、"· "、"* " 等前缀
        cleaned = stripped.lstrip("0123456789.、）)]-·*# ")
        cleaned = cleaned.strip("\"'「」【】").strip()
        if cleaned and cleaned not in lines:
            lines.append(cleaned)
        if count and len(lines) >= count:
            break
    return lines


class DeepResearch:
    """Deep Research（一层展开）"""

    SUB_QUERY_PROMPT = (
        "你是研究助手。请把用户的研究问题拆解为 {n} 个独立、可检索的子问题，"
        "覆盖概念定义、原理机制、应用场景与对比等角度。\n"
        "每行一个子问题，不要编号外的多余文字。"
    )

    def __init__(
            self,
            retriever: Retriever,
            generator: Generator,
            sub_query_count: int = 4,
            top_k_per_query: int = 3,
            max_results: int = 8,
            max_context_length: int = 3000,
    ):
        self.retriever = retriever
        self.generator = generator
        self.sub_query_count = max(1, min(sub_query_count, 8))
        self.top_k_per_query = top_k_per_query
        self.max_results = max_results
        self.max_context_length = max_context_length

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
            print(f"⚠️ 子查询生成失败，退化为原问题: {e}")
            return [question]

    # ================================================================
    # 研究主流程
    # ================================================================

    def research(
            self,
            question: str,
            sub_queries: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        执行一次深度研究（一层展开）

        Args:
            question: 研究问题
            sub_queries: 自定义子查询列表（None 则用 LLM 生成）

        Returns:
            Dict: report（研究报告）/ sub_queries / sources / total_results
        """
        subs = sub_queries or self._generate_sub_queries(question)

        # 1. 并行检索各个子查询
        workers = min(len(subs), 4)
        if workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                results_list = list(ex.map(
                    lambda q: self.retriever.retrieve(
                        q, top_k=self.top_k_per_query, use_rerank=True
                    ),
                    subs,
                ))
        else:
            results_list = [
                self.retriever.retrieve(q, top_k=self.top_k_per_query, use_rerank=True)
                for q in subs
            ]

        # 2. 合并去重（按结果 id）
        merged: Dict[str, Any] = {}
        for res in results_list:
            for r in res:
                if r.id not in merged:
                    merged[r.id] = r
        ranked = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        ranked = ranked[: self.max_results]

        # 3. 构建上下文（与检索器格式一致：[来源 > 标题] 内容）
        context_parts = []
        for r in ranked:
            source = r.metadata.get("file_name", "unknown")
            heading = r.metadata.get("heading_path", "")
            header = f"[{source}]{f' > {heading}' if heading else ''}"
            context_parts.append(f"{header}\n{r.content}")
        context = "\n\n---\n\n".join(context_parts)
        if len(context) > self.max_context_length:
            context = context[: self.max_context_length] + "\n\n...(内容过长，已截断)"

        # 4. 生成研究报告
        report = self.generator.generate(query=question, context=context)

        sources = [
            {
                "file_name": r.metadata.get("file_name", "unknown"),
                "file_path": r.metadata.get("file_path", ""),
                "heading": r.metadata.get("heading_path", ""),
                "score": r.score,
            }
            for r in ranked
        ]

        return {
            "report": report,
            "sub_queries": subs,
            "sources": sources,
            "total_results": len(ranked),
        }