# 检索评测结果

- 评测题集：`eval/eval_set.jsonl`（21 题）
- top_k：5；入选 top3；rerank 阈值取 `.env` 的 `RERANK_MIN_SCORE`
- 延迟包含 embedding / 数据库 / rerank，**不要把它当成融合算法的纯耗时**

| 配置 | 知识题 Recall@k | 知识题 MRR | 字面题 Recall@k | 字面题 MRR | 无答案拒绝率 | P95 ms |
|---|---|---|---|---|---|---|
| weighted_rerank_off | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 | 203.26 |
| weighted_rerank_on | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1252.63 |
| rrf_rerank_off | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 | 18.44 |
| rrf_rerank_on | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 376.63 |

> 复跑：`.venv\Scripts\python.exe eval\run_retrieval.py --fusion both --rerank both`