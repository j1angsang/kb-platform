# 图通道消融评测

- 题集：`eval/graph_set.jsonl`（15 题）
- top_k=5，入选 top3，rerank 恒开（图通道的硬前提）
- **graph_only_rate**：图召回中，向量 top20 与 BM25 top20 都未命中的比例。这是图的净贡献；为 0 说明图白做

| 配置 | 多跳/聚合 Recall@k | 对照题 Recall@k | MRR | avg_noise | 拒答率 | graph_only_rate | 图独占进选中 | P95 ms |
|---|---|---|---|---|---|---|---|---|
| graph_off | 1.0 | None | 0.9444 | 0.0926 | None | 0.0 | 0 | 2619.75 |
| graph_on | 1.0 | None | 0.9444 | 0.0926 | None | 0.0 | 0 | 350.94 |

> 复跑：`.venv\Scripts\python.exe eval\run_graph.py --graph both`