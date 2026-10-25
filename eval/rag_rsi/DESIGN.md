# 设计说明

## 目标

验证 RAG 检索配置在固定题集上的变化，避免“改了一个参数、只看了几条问答”就判断效果。

评测只看检索层，不调用最终回答模型：题集有期望文件和关键词，适合低成本回归。

## 数据

- 语料：60 份制度文档
- 新增：20 份 synthetic 文档，覆盖人事、财务、技术、销售、行政
- 题集：134 题
  - knowledge：67
  - lexical：31
  - no_answer：18
  - permission：18
- dev 67 / test 67，test 只用于门控

标签口径和覆盖矩阵见 `WHY.md`。

## 指标

| 指标 | 含义 |
|---|---|
| Recall@5 | 期望文件是否进入前 5 |
| MRR | 第一个期望文件的名次 |
| refusal_accuracy | 无答案/越权题是否正确拒答 |
| quality | 答案题排名和拒答题的加权代理分，不是最终回答准确率 |

## 两套闭环

### `run_loop.py`

规则或模型选择查询扩展、`min_score`、`top_k`，在 dev 上比较，test 门控。

### `rsi_min_loop.py`

```text
诊断 Agent（失败归因）
  -> 编辑 Agent（生成白名单配置 diff）
  -> dev 回归（不提升就拒绝）
  -> test 门控（退化就回滚）
  -> 通过则写 config.json
  -> 失败则记录 history
```

编辑白名单只有：

- `synonyms`
- `min_score`
- `top_k`

不能改代码、评测集或业务规则。

### `meta_loop.py`

外层回答“哪种改进方式更稳”：

```text
3 折 meta cross-validation
  -> 每折：用 2/3 dev 拟合策略，1/3 dev 验证
  -> 比较策略均值，并惩罚更复杂的策略
  -> 选出策略后，在全量 dev 上拟合最终配置
  -> test 只做一次审计
```

当前策略空间是固定的三种：`threshold_only`、`generic_synonyms`、`domain_synonyms`。

## 目录

| 路径 | 作用 |
|---|---|
| `corpus.json` | 语料 |
| `eval_set.jsonl` | 题集 |
| `run_loop.py` | 检索配置闭环 |
| `rsi_min_loop.py` | 任务级 RSI 最小闭环 |
| `meta_loop.py` | 外层改进策略选择 |
| `meta_policy.json` | 外层选中的策略配置 |
| `rsi_workspace/config.json` | 当前配置 |
| `rsi_workspace/history/` | 每轮诊断、diff、指标、决策 |
| `results/report.md` | 检索配置结果 |
| `results/rsi_min_report.md` | RSI 逐轮结果 |
| `results/meta_report.md` | 外层策略选择结果 |

## 边界

- 离线 BM25，不是实际 pgvector；
- synthetic 文档是评测夹具，不是公司真实制度；
- 外层只做有限策略选择，不是开放式自我改写；
- 不自动上线，配置只写本地实验目录。
