# 检索评测题集

这个目录只做检索层评测：不调用 LLM，直接跑 `store.search` + `select_relevant_hits`，因此成本低、结果可复现。

## 文件

| 文件 | 作用 |
|---|---|
| `eval_set.jsonl` | 21 道评测题：8 道知识问答、8 道字面查询、5 道无答案/超范围 |
| `metrics.py` | Recall@k、MRR、nDCG@k、关键词命中、噪声占比、P50/P95 |
| `run_retrieval.py` | 跑 `weighted` / `rrf` × `rerank on/off`，输出 JSON + Markdown |
| `results/latest.md` | 最近一次本机结果 |

## 指标口径

- **Recall@k**：期望文件是否出现在原始召回前 k 条；衡量“有没有召回”。
- **selected_recall**：经过门控后，期望文件是否还在入选结果里；衡量“门控有没有误杀”。
- **MRR**：第一个期望文件的名次倒数；衡量“排得靠不靠前”。
- **nDCG@k**：二值相关性下的排序质量。
- **keyword_rate**：入选文本是否覆盖期望关键词；衡量“内容对不对”。
- **avg_noise**：入选结果里非期望文件占比；衡量“有没有塞无关内容”。
- **refusal_rate**：无答案题是否返回空入选；这是检索层代理指标，不是最终回答正确率。

## 复跑

```bash
.venv\Scripts\python.exe eval\run_retrieval.py --fusion both --rerank both
```

只跑一种配置：

```bash
.venv\Scripts\python.exe eval\run_retrieval.py --fusion rrf --rerank on
```

## 当前结果（本机，21 题）

| 配置 | 知识题 Recall@5 | 字面题 Recall@5 | 知识题 MRR | 无答案拒绝率 |
|---|---|---|---|---|
| weighted + rerank off | 1.0 | 1.0 | 1.0 | 0/5 |
| rrf + rerank off | 1.0 | 1.0 | 1.0 | 0/5 |
| weighted + rerank on | 1.0 | 1.0 | 1.0 | 5/5 |
| rrf + rerank on | 1.0 | 1.0 | 1.0 | 5/5 |

结论要说清楚：

1. **存在明显的上限效应**：这套题集和语料太小，两种融合都拿满分，不能证明 RRF 提升了召回；
2. **RRF 没有降低现有指标**，但也没有可观测的增益；
3. **无答案题只有开 rerank 才能拒答**，关掉 rerank 时 weighted 和 RRF 都是 0/5——门控的关键是重排，不是融合；
4. 延迟包含 embedding / 数据库 / rerank，不能把 P95 差异归因于融合算法。复跑时注意缓存预热顺序。

更严谨的做法是扩大题集、加入难负例和更多部门/文档，再比较 Recall@k、MRR、nDCG；当前结果只作为回归基线。

## Text2SQL

| 文件 | 作用 |
|---|---|
| `text2sql_set.jsonl` | 20 道员工统计问题，含期望结果 |
| `run_text2sql.py` | 调 LLM 生成 SQL，再走 AST 校验、部门范围、只读执行，比对结果 |

```bash
.venv\Scripts\python.exe eval\run_text2sql.py
```
