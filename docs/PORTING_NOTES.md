# 图工程 + 七维评测：改动说明与迁移指南

> 这份文档有两个用途：① 复盘本仓库这次改了什么；② 迁移到另一个项目时按顺序照做。
> 第二部分才是重点，第一部分只是索引。

---

## 一、这次改了什么（清单）

### 新增（图工程）

| 文件 | 作用 | 关键设计 |
|---|---|---|
| `models/graph_entity.py` | 实体表 | **不存 department / 正文 / 属性值**，只存名字+类型+别名+名称向量 |
| `models/graph_relation.py` | 关系表 | `chunk_id` 挂 FK CASCADE，是权限唯一锚点；`value` 字段收字面值宾语 |
| `models/graph_job.py` | 抽取任务表 | 独立成表，不碰 `IndexJob` 的 `unique(doc_id)` |
| `migrations/versions/e5f6a7b8c9d1_graph_tables.py` | 迁移 | 三表 + `knowledge_docs` 加 `kg_status/kg_error/kg_entity_count` |
| `rag/graph/config.py` | 配置 | 阈值集中，越界即 raise |
| `rag/graph/extract.py` | 受限 LLM 抽取 | 实体类型/谓词白名单、部门词典强约束 |
| `rag/graph/resolve.py` | 实体消歧 | 精确 → 向量(0.98) → 子串(0.9, 短词≥3) → 人工 |
| `rag/graph/link.py` | 查询期链接 | 词典 + 向量双路，泛实体停用词过滤 |
| `rag/graph/store.py` | 图查询 | 多跳递归 CTE、邻域、合并/拆分、GC |
| `rag/graph/pipeline.py` | 单文档编排 | LLM 调用期间不持 DB 连接 |
| `crud/graph_job.py` | 任务队列 | 条件 UPDATE + rowcount 判并发 |
| `routers/graph.py` / `schemas/graph.py` | 管理 API | 9 个端点，仅管理者 |
| `scripts/build_graph.py` | 回填脚本 | dry-run / limit / gc / 断点续跑 |
| `frontend/src/views/GraphView.vue` | 图谱界面 | 实体浏览、邻域图、合并拆分、链接预览 |

### 新增（评测）

| 文件 | 作用 |
|---|---|
| `eval/graph_set.jsonl` + `eval/run_graph.py` | 图通道三路消融，核心指标 `graph_only_rate` |
| `eval/agent_set.jsonl` + `eval/agent_eval.py` | 七维评测 |
| `docs/GRAPH_IMPLEMENTATION.md` | 图工程实现报告（含实测与坑） |
| `docs/AGENT_EVAL.md` | 七维评测说明 |

### 改动（主链路，共 5 处）

| 文件 | 改了什么 | 为什么必须改 |
|---|---|---|
| `rag/store.py` | `search()` 加图通道扩候选池；`select_relevant_hits()` 加弱相关兜底 | 图要能进 rerank 池；弱证据要能兜住"部分相关" |
| `rag/indexer.py` | 入库 ready 后再排队建图；加独立 `_graph_locks` 和 `extract_doc_graph` | 建图是分钟级，不能绑住检索可用性 |
| `agents/graph.py` | 引用编号按文件去重；弱证据注入提示；`run_agent` 返回 `tool_trace` | 编号契约；防"知识库没有"幻觉；评测要过程数据 |
| `config/settings.py` | `KG_ENABLED` 必须 `RERANK_ENABLED`，启动即校验 | 关掉 rerank 时图候选没有绝对分，会被门控全杀 |
| `services/text2sql.py` / `tools/text2sql_tools.py` | 超时 15→30s；失败返回加"禁止推测" | 实测超时后模型编数字 |

---

## 二、迁移顺序（别跳步骤）

### 第 0 步：先判断要不要做

| 判据 | 满足才做 |
|---|---|
| 语料里有跨文档关联（一份文档讲流程、另一份讲审批权限） | ✅ |
| 文档是长文档或多主题，一份文档里能抽出几十个实体 | ✅ |
| 现有检索指标还有提升空间（Recall 已 1.0 就别做了） | ✅ |

**本仓库的教训**：语料是"1 文档 1 chunk / 短文档单主题"，做完 `graph_only_rate = 0` ——
图召回的 chunk 全在向量/BM25 的 top20 里。原因很硬：被实体连到的 chunk 正文必然写着实体名，
BM25 必然能召回。**没有多跳语料，图工程就是白做。**

### 第 1 步：先建表和权限

按 `models/graph_*.py` 抄。三个硬约束，破一个就出问题：

1. **实体表不加 department** —— 实体是跨文档合并的，带部门字段会分裂成 N 份
2. **实体表不存正文** —— 存了就是绕过权限的新出口
3. **`relation.chunk_id` 挂 FK CASCADE** —— 删文件时边自动清，不用手写清理顺序

### 第 2 步：写抽取，先跑 3~5 份看质量

```bash
python scripts/build_graph.py --limit 3
python scripts/build_graph.py --dry-run
```

**必须逐条看抽出来的边，不只看数量。** 我第一次只看"19 条边里有没有错的"，
没看"实体有没有被错误合并"，结果 0.92 的合并阈值把"销售经理"并进了"销售总监"，
全量跑完才发现，只能清空重来。

Go/No-Go 闸门：可接受率 ≥ 70% 才继续。本仓库实测 19 条里 1 条明显错，94%。

### 第 3 步：消歧阈值必须标定，不能拍

本仓库返工两次的教训：0.92 → 0.96 → 0.98+子串规则。原因是

```
销售经理 → 销售主管   0.9744   ✗ 不该并
直属上级 → 直属领导   0.9719   ✓ 该并
```

**两个分数几乎一样，余弦分不出来。** 所以结论是：自动合并只保留"几乎肯定相同"的情形
（字面全同 / 长词包含短词且短词≥3字），其余全交人工。

判断标准是**代价不对称**：错并会让下游推理走错且无法事后拆；漏并只是图碎片化。

### 第 4 步：接检索，只做扩池不做第三路

```
query
 ├─ 向量 top20 ──┐
 ├─ BM25 top20 ──┤→ RRF → 候选池 → 图候选追加 → cross-encoder → 门控
 └─ 链接→多跳→chunk_id→权限SQL ─┘
```

**为什么不能做第三路 RRF**：`_relevance_score` 的门控地板是 `0.80`（按余弦标定），
图候选没有向量分，RRF 归一化后的量级完全不同，会被全杀。

**为什么扩池可行**：`rerank_score` 是 cross-encoder 的绝对分，不依赖候选来自哪一路。
图找来真相关内容，rerank 自然给高分；不相关就被门控拒掉。**判断权归 rerank。**

代价：`RERANK_TOP_N` 从 30 变成 42，多 40% 重排成本。可接受。

**硬依赖**：`KG_ENABLED` 必须要求 `RERANK_ENABLED`，启动即校验（对齐
`LLM_SUPPORTS_VISION=false` 的先例）。

### 第 5 步：加评测，先测净贡献

核心指标是 **graph_only_rate**：

```
图召回的 chunk 中，向量 top20 与 BM25 top20 都未命中的比例
```

**这个数是 0，就说明图白做了。** 只看 Recall 会被扩池"骗"—— 池子变大后 Recall 通常涨，
但那可能只是 cross-encoder 多看了几十个候选，跟图没关系。

---

## 三、七个坑（可直接复用）

### 1. 批量 `INSERT ... ON CONFLICT` 的 rowcount 是 -1

```python
# 错
result = await db.execute(stmt); return result.rowcount or 0
# 对
stmt = stmt.returning(GraphRelation.id)
return len((await db.execute(stmt)).all())
```

psycopg 对批量 upsert 返回 `-1`，日志和指标全失真。

### 2. 确定性 id + upsert 时，FK CASCADE 不触发

`chunk_id = f"{file_id}:{seq}"`，重入库走 `on_conflict_do_update` 而不是 delete+insert，
行没被删过，级联就不会触发 —— 旧边混在新 chunk 上。
**必须显式 `DELETE FROM graph_relations WHERE chunk_id IN (...)`。**

### 3. 泛实体污染：一个"员工"毁掉整条图通道

实测「员工生病要请长假」先命中「员工」(dict 0.99)，图于是扩出「员工」的全部证据 →
《报销管理制度》。**泛实体对定位零区分度，必须在链接期过滤**（`KG_LINK_STOPWORDS`）。

### 4. 链接阈值别照抄，要用实测分布标定

0.86 看着合理，实测把「病假」0.8497 和「转正」0.8530 全挡在门外，图通道等于没开。
降到 0.82 后两类都能命中。**敢降的前提是 rerank 绝对分兜底。**

### 5. `entity_neighbors` 用 inner join 会丢掉字面值边

`value` 型边的 `object_id` 是 NULL，inner join 直接整条丢。
用 `outerjoin` + 对 `other is None` 兜底。**这是"省掉节点"这个设计的配套代价。**

### 6. 拆分实体时，查回来的可能还是那条 merged 行

合并时源实体标 `status='merged'` 保留（为了可追溯），拆分时若只做
`INSERT ... ON CONFLICT DO NOTHING` 再按名字查，查回来的是那条 merged 行 ——
接口返回了 id，实际什么都没改。**必须显式恢复 `status='active'` 并清 `merged_into`。**

### 7. 不能用 `ToolMessage.status` 判断工具失败

工具内部 `try/except` 后仍返回字符串，`status` 是 `success`，content 却是
`工具执行失败：Request timed out.`。评测信了 status，就会把"工具超时 + 模型编数字"
判成完全正常。**要复用 `_tool_ok()` 看内容。**

---

## 四、门控与幻觉：一条独立经验

这次最有价值的发现，和图没关系，是**门控话术导致幻觉**：

- 用户问「你好我的年假还有多久」，资料是制度，cross-encoder 判 0.708，被 0.9 门槛丢弃
- 门控清空后注入的提示是「知识库中没有与用户问题相关的内容」
- 模型据此输出「**知识库里没有年假政策**」—— 它并不知道这件事

两层修法：

1. **弱相关兜底**：强相关全无时，若 `top1 ≥ 0.55` 且 `margin ≥ 0.20`，注入一条并打标。
   标定依据：无答案题 top1 0.23/0.33 且 margin <0.1；部分相关题 0.71/0.88 且 margin >0.35。
2. **注入侧说明资料性质**：「这是最接近的资料，但不一定能直接回答；讲制度 + 指出个人数据另查；
   **禁止断言知识库里没有相关政策**」。

**拒答话术必须留余地**：写"没有相关内容"，模型会加工成"没有 X 政策"。
改成"本次检索没有找到直接相关的材料……不要断言该制度不存在"。

---

## 五、验收清单

上线前逐项确认：

- [ ] `alembic upgrade head` 通过，表结构含 FK CASCADE 和 CHECK 约束
- [ ] 3~5 份文档抽取质量抽检 ≥ 70%，**合并日志逐条看过**
- [ ] `KG_ENABLED=false` 时原有评测指标**逐项不变**（零回归）
- [ ] 权限对照实验：同一 chunk 在 `department=A` 可见、`department=B` 不可见、`None` 可见
- [ ] `graph_only_rate` 有数（为 0 也要报出来）
- [ ] 七维评测：拒答率、越权泄漏、工具真实性三项必须全绿
- [ ] 前端入口页 `Cache-Control: no-cache`（否则重新构建后用户还在吃旧产物）
