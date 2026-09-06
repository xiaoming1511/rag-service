"""
评测执行器 CLI（决策 B1）

用法（项目根目录）：
    # 仅检索指标（Recall@5 / MRR@5 / Hit@5 / Precision@5），rerank 开
    .venv/bin/python -m src.evaluation.run_eval

    # 对比 rerank 开关
    .venv/bin/python -m src.evaluation.run_eval --no-rerank

    # 含生成质量（faithfulness / answer_relevance，走本地 LLM-as-judge）
    .venv/bin/python -m src.evaluation.run_eval --with-generation

    # 保存基线（供后续优化对比）
    .venv/bin/python -m src.evaluation.run_eval --save-baseline

输出：
- 终端摘要表
- JSON 报告 data/eval/results/eval-<时间戳>.json
- 基线（--save-baseline 时）data/eval/baselines/baseline.json
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from src.config import config_manager
from src.evaluation.dataset import EvalDataset, normalize_doc_id
from src.evaluation.metrics import aggregate, parse_judge_score
from src.logging_setup import get_logger
from src.retrieval.context_builder import estimate_tokens

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_DATASET = PROJECT_ROOT / "data" / "eval" / "eval_qa.json"
RESULTS_DIR = PROJECT_ROOT / "data" / "eval" / "results"
BASELINE_PATH = PROJECT_ROOT / "data" / "eval" / "baselines" / "baseline.json"


def build_components(use_rerank: bool, use_hybrid: bool = True,
                     use_parent: bool = True):
    """按 settings.yaml 组装评测所需组件（与 run_api.py 同源接线）"""
    config = config_manager.load()

    from src.embedding.client import OMLXClient
    from src.embedding.embedder import Embedder
    from src.vector_store.chroma_store import ChromaStore
    from src.retrieval.reranker import Reranker
    from src.retrieval.retriever import Retriever

    client = OMLXClient(
        base_url=config.omlx.base_url,
        api_key=config.omlx.api_key,
        timeout=config.omlx.timeout,
    )
    embedder = Embedder(
        client=client,
        model=config.omlx.embedding_model,
        cache_enabled=True,
        mem_cache_capacity=config.performance.embed_cache_capacity,
    )
    vector_store = ChromaStore(
        collection_name=config.vector_store.collection_name,
        persist_directory=config.vector_store.persist_directory,
    )
    reranker = Reranker(
        client=client,
        model=config.omlx.reranker_model,
        enabled=config.retrieval.enable_rerank and use_rerank,
    )
    from src.retrieval.bm25_index import BM25Index

    retriever = Retriever(
        vector_store=vector_store,
        embedder=embedder,
        reranker=reranker,
        top_k=config.retrieval.top_k,
        rerank_top_k=config.retrieval.rerank_top_k,
        similarity_threshold=config.retrieval.similarity_threshold,
        rerank_threshold=config.retrieval.rerank_threshold,
        bm25_index=BM25Index(vector_store),
        hybrid=config.retrieval.hybrid and use_hybrid,
        hybrid_candidates=config.retrieval.hybrid_candidates,
        rrf_k=config.retrieval.rrf_k,
        parent_expansion=config.retrieval.parent_expansion and use_parent,
        parent_max_tokens=config.retrieval.parent_max_tokens,
    )
    return config, client, retriever


def run_retrieval_eval(dataset: EvalDataset, retriever, top_k: int,
                       use_rerank: bool) -> Dict[str, Any]:
    """逐条评测检索，返回指标与逐项明细"""
    ranked_lists: List[List[str]] = []
    gold_sets = [dataset.gold_set(it) for it in dataset.items]
    details: List[Dict[str, Any]] = []

    for item in dataset.items:
        t0 = time.perf_counter()
        try:
            # 用 retrieve_with_context：同时拿到结果列表与装配后的上下文
            #（context_tokens 统计用于量化 B2 token 预算 / B4 父块的完整性收益）
            context, results = retriever.retrieve_with_context(
                query=item["question"],
                top_k=top_k,
                use_rerank=use_rerank,
            )
            error = None
        except Exception as e:  # 单条失败不中断整体评测
            logger.warning("检索失败 [%s]: %s", item["id"], e)
            results, context, error = [], "", str(e)

        ranked = [normalize_doc_id(r.metadata.get("file_name", "")) for r in results]
        ranked_lists.append(ranked)

        gold = dataset.gold_set(item)
        first_hit = next((i + 1 for i, d in enumerate(ranked) if d in gold), None)
        details.append({
            "id": item["id"],
            "question": item["question"],
            "gold_docs": item["gold_docs"],
            "ranked_docs": ranked,
            "first_hit_rank": first_hit,
            "scores": [round(r.score, 4) for r in results],
            "context_tokens": estimate_tokens(context),
            "error": error,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        })

    metrics = aggregate(ranked_lists, gold_sets, k=top_k)
    ctx_list = [d["context_tokens"] for d in details if not d["error"]]
    if ctx_list:
        metrics["context_tokens_mean"] = round(sum(ctx_list) / len(ctx_list), 1)
    return {"metrics": metrics, "details": details}


def run_generation_eval(dataset: EvalDataset, client, retriever,
                        config, judge, top_k: int, use_rerank: bool) -> Dict[str, Any]:
    """含生成链路的评测：检索 → 生成 → LLM-as-judge 打分"""
    from src.generation.generator import Generator

    generator = Generator(
        client=client,
        model=config.omlx.chat_model,
        max_tokens=config.generation.max_tokens,
        temperature=config.generation.temperature,
        stream=False,
    )

    faith_scores, rel_scores, details, errors = [], [], [], 0
    for item in dataset.items:
        question = item["question"]
        context, results = retriever.retrieve_with_context(
            query=question, top_k=top_k, use_rerank=use_rerank,
            max_context_tokens=4000,
        )
        try:
            answer = generator.generate(query=question, context=context)
        except Exception as e:
            logger.warning("生成失败 [%s]: %s", item["id"], e)
            details.append({"id": item["id"], "error": str(e)})
            errors += 1
            continue

        try:
            scores = judge.evaluate_generation(
                question=question, context=context, answer=answer,
                gold_keywords=item.get("gold_keywords", []),
            )
        except Exception as e:
            logger.warning("judge 失败 [%s]: %s", item["id"], e)
            details.append({"id": item["id"], "error": str(e)})
            errors += 1
            continue

        for key, bucket in (("faithfulness", faith_scores),
                            ("answer_relevance", rel_scores)):
            v = scores[key]
            if v >= 0:
                bucket.append(v)
            else:
                errors += 1

        details.append({
            "id": item["id"],
            "answer": answer,
            "scores": {k: round(v, 3) for k, v in scores.items()},
        })

    metrics = {
        "faithfulness": round(sum(faith_scores) / len(faith_scores), 4) if faith_scores else None,
        "answer_relevance": round(sum(rel_scores) / len(rel_scores), 4) if rel_scores else None,
        "judge_valid_count": len(faith_scores),
        "judge_error_count": errors,
    }
    return {"metrics": metrics, "details": details}


def compare_with_baseline(current: Dict[str, float]) -> Dict[str, Any]:
    """与已保存基线对比，返回增量"""
    if not BASELINE_PATH.exists():
        return {"baseline": None}
    with open(BASELINE_PATH, "r", encoding="utf-8") as f:
        baseline = json.load(f)
    base_metrics = baseline.get("metrics", {})
    diff = {
        k: round(current[k] - base_metrics[k], 4)
        for k in current if k in base_metrics and base_metrics[k] is not None
        and current[k] is not None
    }
    return {"baseline_meta": baseline.get("meta"), "diff": diff}


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 评测执行器")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="评测集路径")
    parser.add_argument("--top-k", type=int, default=5, help="检索截断 k")
    parser.add_argument("--no-rerank", action="store_true", help="关闭重排序（对比用）")
    parser.add_argument("--no-hybrid", action="store_true", help="关闭混合检索（对比用）")
    parser.add_argument("--no-parent", action="store_true", help="关闭父子块召回（对比用）")
    parser.add_argument("--with-generation", action="store_true",
                        help="启用生成链路 + LLM-as-judge（较慢）")
    parser.add_argument("--save-baseline", action="store_true", help="保存为基线")
    parser.add_argument("--label", default="", help="本次评测标签（写入报告）")
    args = parser.parse_args()

    dataset = EvalDataset.load(args.dataset)
    print(f"评测集: {args.dataset}（{len(dataset)} 条）")
    print(f"参数: top_k={args.top_k} rerank={not args.no_rerank} hybrid={not args.no_hybrid} parent={not args.no_parent} generation={args.with_generation}")

    use_rerank = not args.no_rerank
    config, client, retriever = build_components(
        use_rerank, use_hybrid=not args.no_hybrid, use_parent=not args.no_parent,
    )

    report: Dict[str, Any] = {
        "meta": {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "label": args.label,
            "dataset": str(args.dataset),
            "dataset_size": len(dataset),
            "top_k": args.top_k,
            "use_rerank": use_rerank,
            "use_hybrid": not args.no_hybrid,
            "use_parent": not args.no_parent,
            "embedding_model": config.omlx.embedding_model,
            "chat_model": config.omlx.chat_model,
        },
    }

    # 检索指标
    retrieval = run_retrieval_eval(dataset, retriever, args.top_k, use_rerank)
    report["retrieval"] = retrieval
    print("\n=== 检索指标 ===")
    for k, v in retrieval["metrics"].items():
        print(f"  {k:<16} {v:.4f}")

    # 生成指标（可选）
    if args.with_generation:
        from src.evaluation.judge import LLMJudge
        judge = LLMJudge(client, model=config.omlx.chat_model)
        gen = run_generation_eval(dataset, client, retriever, config,
                                  judge, args.top_k, use_rerank)
        report["generation"] = gen
        print("\n=== 生成指标（LLM-as-judge） ===")
        for k, v in gen["metrics"].items():
            print(f"  {k:<20} {v}")

    # 与基线对比
    comparison = compare_with_baseline(retrieval["metrics"])
    if comparison.get("diff"):
        print("\n=== 与基线对比 ===")
        print(f"  基线时间: {comparison['baseline_meta'].get('timestamp')}")
        for k, d in comparison["diff"].items():
            sign = "+" if d >= 0 else ""
            print(f"  {k:<16} {sign}{d:.4f}")
    report["comparison"] = comparison

    # 落盘报告
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"eval-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存: {out_path}")

    if args.save_baseline:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump({"meta": report["meta"],
                       "metrics": retrieval["metrics"]}, f, ensure_ascii=False, indent=2)
        print(f"基线已保存: {BASELINE_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
