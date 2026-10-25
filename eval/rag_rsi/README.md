# 企业知识库 RAG 评测与 RSI 最小闭环

一套离线、可复现的 RAG 评测与策略自改进实验，覆盖知识问答、字面查询、无答案拒答和权限边界。

它不依赖 PostgreSQL、embedding 或云 API；用于在改检索配置前先跑固定回归集。

## 能做什么

- 60 份制度语料、134 条评测题，dev/test 各 67
- 固定题集回归：Recall@5、MRR、拒答率和分类质量
- 两套闭环：
  - `run_loop.py`：规则/模型编辑检索配置
  - `rsi_min_loop.py`：诊断 Agent → 编辑 Agent → dev 回归 → test 门控 → 写入/回滚
- 可选本地 Ollama 编辑器；不接模型也能跑规则基线

## 快速开始

环境：Python 3.10+，只使用标准库。

```bash
cd rsi_demo
python run_loop.py
```

跑任务级 RSI 最小闭环：

```bash
python rsi_min_loop.py --model qwen3.5:4b-q4_K_M --rounds 3 --reset
```

本地 Ollama 需先启动，模型用 `qwen3.5:4b-q4_K_M`。Qwen3.5 必须关闭 thinking，脚本已经设置 `think: false` 和 `format: json`。

## 结果

- baseline：dev 80.0%，test 82.1%
- 检索配置闭环：相关性下限方案 dev 86.0%，test 88.1%，Recall@5 保持 100%
- RSI 最小闭环：第 1 轮接受 dev 85.97%，test 88.06%；第 2/3 轮因 dev 无提升被拒绝

完整逐轮记录见：

- [results/report.md](results/report.md)
- [results/rsi_min_report.md](results/rsi_min_report.md)

## 目录

| 路径 | 内容 |
|---|---|
| `corpus.json` | 60 份制度语料 |
| `corpus_extra.json` | 新增的 20 份语料 |
| `eval_set.jsonl` | 134 条评测题 |
| `run_loop.py` | 检索配置闭环 |
| `rsi_min_loop.py` | 诊断/编辑/门控最小闭环 |
| `rsi_workspace/` | 当前配置和历史轮次 |
| `results/` | 报告和机器可读结果 |

## 边界

- 当前离线评测使用 BM25，不是实际 pgvector 检索；
- 语料中有 20 份是 synthetic 评测夹具，不冒充公司真实制度；
- 编辑对象只有检索配置，不改代码、不上线；
- 不是完整 RSI，没有递归改进“改进方法本身”。

## 相关文档

- [WHY.md](WHY.md)：为什么扩语料、为什么加这些题
- [DESIGN.md](DESIGN.md)：数据口径、指标和闭环细节
