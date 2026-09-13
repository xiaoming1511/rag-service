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
from typing import Any, Dict, List, Optional

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


def _fmt_metric(v: Any) -> str:
    """指标格式化：None 显示 n/a（分母不可得），不伪装成 0"""
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def build_components(use_rerank: bool, use_hybrid: bool = True,
                     use_parent: bool = True):
    """按 settings.yaml 组装评测所需组件（与 run_api.py 同源接线）

    Returns:
        (config, client, retriever, vector_store) —— vector_store 用于统计
        gold 文档的块数（块级 Recall@k 的分母）
    """
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
        recall_candidates=config.retrieval.recall_candidates,
        rerank_candidates=config.retrieval.rerank_candidates,
        synthesis_weight=config.retrieval.synthesis_weight,
        rrf_k=config.retrieval.rrf_k,
        parent_expansion=config.retrieval.parent_expansion and use_parent,
        parent_max_tokens=config.retrieval.parent_max_tokens,
    )
    return config, client, retriever, vector_store


def build_gold_chunk_totals(dataset: EvalDataset, vector_store) -> Dict[str, int]:
    """统计每个文档在向量库中的块数（块级 Recall@k 的分母）

    块级 Recall 的分母必须是「gold 文档一共有多少块」，只有向量库知道。
    取不到时返回空 dict —— 上层会把它翻译成 `chunk_recall@k = None`，
    而不是伪装成 0.0。
    """
    from collections import Counter

    totals: Counter = Counter()
    try:
        for raw in vector_store.get_all():
            name = normalize_doc_id((raw.get("metadata") or {}).get("file_name", ""))
            if name:
                totals[name] += 1
    except Exception as e:
        logger.warning("无法统计文档块数，块级 Recall 将不可用: %s", e)
        return {}
    return dict(totals)


def run_retrieval_eval(dataset: EvalDataset, retriever, top_k: int,
                       use_rerank: bool,
                       doc_chunk_totals: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """逐条评测检索，返回指标与逐项明细

    Args:
        doc_chunk_totals: {文档名: 该文档块数}，用于块级 Recall 的分母；
            None / 空 dict 时 `chunk_recall@k` 记为 None
    """
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

    # 每条 query 的块级 Recall 分母 = 其 gold 文档的块数之和
    totals = doc_chunk_totals or {}
    chunk_totals: Optional[List[int]] = None
    if totals:
        chunk_totals = [
            sum(totals.get(d, 0) for d in gold_sets[i])
            for i in range(len(dataset.items))
        ]

    metrics = aggregate(ranked_lists, gold_sets, k=top_k, chunk_totals=chunk_totals)
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
        try:
            context, results = retriever.retrieve_with_context(
                query=question, top_k=top_k, use_rerank=use_rerank,
                # 取配置预算而非写死 4000：否则 settings.yaml 调大了预算，
                # 评测仍按 4000 测，指标与线上实际上下文不符
                max_context_tokens=config.retrieval.context_token_budget,
            )
        except Exception as e:  # 与 run_retrieval_eval 一致：单条失败不中断整体评测
            logger.warning("检索失败 [%s]: %s", item["id"], e)
            details.append({"id": item["id"], "error": str(e)})
            errors += 1
            continue
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
    """与已保存基线对比，返回增量

    基线文件是外部产物（可能被手改/被旧版本写过），因此对结构做防御性校验，
    并把 baseline_meta 归一为 dict——否则调用方 `comparison["baseline_meta"].get(...)`
    会在基线缺 meta 时抛 AttributeError。
    """
    if not BASELINE_PATH.exists():
        return {"baseline": None}
    with open(BASELINE_PATH, "r", encoding="utf-8") as f:
        baseline = json.load(f)
    if not isinstance(baseline, dict):
        return {"baseline": None, "error": "基线文件格式不可识别（应为 JSON 对象）"}
    meta = baseline.get("meta")
    base_metrics = baseline.get("metrics")
    if not isinstance(meta, dict) or not isinstance(base_metrics, dict):
        return {"baseline": None, "error": "基线文件缺少有效的 meta / metrics"}
    comparable = [k for k in current if k in base_metrics]
    if not comparable:
        # 指标口径改过（如 D5-1 的 doc_*/chunk_* 重命名）→ 旧基线键全部失配。
        # 此时「没有差异」会误导成「指标持平」，必须显式说明。
        return {
            "baseline": None,
            "baseline_meta": meta,
            "error": "基线指标口径与当前不一致（键不重叠），请用 --save-baseline 重建基线",
        }
    diff = {
        k: round(current[k] - base_metrics[k], 4)
        for k in comparable
        if base_metrics[k] is not None and current[k] is not None
    }
    return {"baseline_meta": meta, "diff": diff}


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
    config, client, retriever, vector_store = build_components(
        use_rerank, use_hybrid=not args.no_hybrid, use_parent=not args.no_parent,
    )

    # gold 文档块数（块级 Recall 的分母）；向量库不可用时退化为 None
    doc_chunk_totals = build_gold_chunk_totals(dataset, vector_store)
    if not doc_chunk_totals:
        print("提示: 未能统计文档块数，chunk_recall@k 将显示为 n/a")

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
            "metric_schema": "doc/chunk-v2",
        },
    }

    # 检索指标（文档级 + 块级两套）
    retrieval = run_retrieval_eval(
        dataset, retriever, args.top_k, use_rerank,
        doc_chunk_totals=doc_chunk_totals,
    )
    report["retrieval"] = retrieval
    print("\n=== 检索指标 ===")
    print("  [文档级]")
    for k, v in retrieval["metrics"].items():
        if k.startswith("doc_"):
            print(f"    {k:<20} {_fmt_metric(v)}")
    print("  [块级]")
    for k, v in retrieval["metrics"].items():
        if k.startswith("chunk_"):
            print(f"    {k:<20} {_fmt_metric(v)}")
    print("  [其他]")
    for k, v in retrieval["metrics"].items():
        if not k.startswith(("doc_", "chunk_")):
            print(f"    {k:<20} {_fmt_metric(v)}")

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
    elif comparison.get("error"):
        print(f"\n基线对比跳过: {comparison['error']}")
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
