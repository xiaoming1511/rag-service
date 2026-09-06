"""
评测与质量基线（决策 B1）

模块组成：
- dataset:  评测集加载与校验（data/eval/eval_qa.json）
- metrics:  检索指标（Recall@k / MRR@k / HitRate@k / Precision@k）纯函数
- judge:    LLM-as-judge 生成质量指标（faithfulness / answer_relevance）
- run_eval: 评测执行器 CLI（python -m src.evaluation.run_eval）

指标基线保存于 data/eval/baselines/，用于量化后续优化（B2/B3/B4）的收益。
"""
