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

## 图通道（三路消融）

| 文件 | 作用 |
|---|---|
| `graph_set.jsonl` | 15 题：8 道图优势题（实体聚合/间接表述/多跳）+ 4 道对照题 + 3 道拒答题（含 1 道跨部门越权） |
| `run_graph.py` | 跑 graph on/off 两档，输出逐题明细与对比表 |
| `results/graph_latest.md` | 最近一次结果 |

```bash
.venv\Scripts\python.exe eval\run_graph.py --graph both
```

### 最关键的一个指标：graph_only_rate

```
graph_only = 图通道召回的 chunk 中，向量 top20 与 BM25 top20 都未命中的部分
graph_only_rate = graph_only / 图召回总量
```

**这个数是 0，就说明图白做了** —— 候选池里本来就有的东西，加个通道没有任何增量。

只看 Recall 会被扩池骗：池子变大后 Recall 通常会涨，但那可能只是 cross-encoder 多看了
几十个候选，跟图没关系。`graph_only` 才是图的净贡献。

### 当前结果（本机，15 题）

| 配置 | 多跳/聚合 Recall@5 | 对照题 Recall@5 | avg_noise | 拒答率 | graph_only_rate |
|---|---|---|---|---|---|
| graph_off | 1.0 | 1.0 | 0.2917 | 3/3 | — |
| graph_on | 1.0 | 1.0 | 0.2917 | 3/3 | **0.0** |

结论要说清楚：

1. **两档指标完全一致，图通道净贡献为 0。** 图在 11/15 题上召回了候选（最多一题 13 个），
   但这些 chunk 全部已被向量或 BM25 覆盖。
2. **原因在语料，不在实现。** 当前是"1 文档 1 chunk / 短文档单主题"，被实体连到的 chunk
   正文必然写着实体名，所以 BM25 必能召回。要造出 graph_only，需要"关系存在但字面不重叠"
   的结构（某文档只写"用车申请"不写"OA"，而图里有 `用车申请 --提交至--> OA`）。
3. **权限隔离通过对照实验。** 非共享的技术部文档在 `department='人事部'` 下被挡住，
   `department='技术部'` 下可见，`None` 下可见 —— 图没有绕过 `_permission_clause`。
4. **零回归。** `KG_ENABLED=false` 时原有 21 题指标与改造前逐项一致。

一个不敢报 0 的评测，报出来的正数也不可信。详见 `docs/GRAPH_IMPLEMENTATION.md` §9。

## Agent 七维评测

单测检索、单测答案都不够 —— Agent 最危险的失败是**过程错了但答案碰巧对**。
这套评测同时管结果和过程。

| 文件 | 作用 |
|---|---|
| `agent_set.jsonl` | 15 题：知识/部分相关/无答案/越权/注入/结构化查询 |
| `agent_eval.py` | 七维打分 + 逐题明细，输出 `results/agent_latest.md` |

```bash
.venv\Scripts\python.exe eval\agent_eval.py               # 七维全量
.venv\Scripts\python.exe eval\agent_eval.py --repeat 3    # 加稳定性（成本×3）
.venv\Scripts\python.exe eval\agent_eval.py --only a-04   # 单题
.venv\Scripts\python.exe eval\agent_eval.py --no-judge    # 跳过 LLM 判定
```

七个维度：过程管控 / 召回完整性 / 忠实度 / 工具真实性 / 稳定性 / 成本效率 / 安全权限。

### 出题时最容易踩的两个坑

1. **`must_not_leak` 只放「答案里才有的词」，不能放问题主题词。**
   越权题「技术部的数据库变更怎么审批」，若是禁止「数据库变更审批流程」，
   模型拒答时必然复述这个短语 → 误判成泄漏。要放具体审批人、步骤名这类答案才有的内容。

2. **Text2SQL 题要按 `analytics.v_employee_directory` 的口径出**，
   不是 `employees` 表。两者数据不一致（技术部 8 人 vs 10 人），
   按错的那套出题会把正确回答判成幻觉。

详见 `docs/AGENT_EVAL.md`。
