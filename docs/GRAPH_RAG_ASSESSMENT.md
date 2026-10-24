# 知识图谱接入评估：改什么、怎么改、哪里会踩坑

> 评估对象：当前 kb-platform（pgvector + BM25 + RRF + rerank 门控 + LangGraph Agent）
> 评估结论：**值得做，但只能做"图作为候选池扩充器"，不能做"图作为内容源"**。
> 前者只碰 1 处主链路（`rag/store.py:search`）+ 4 处追加，回归风险接近零；后者会击穿部门权限隔离，方案不可行。

---

## 0. 推荐清单

### 要加的

| # | 加什么 | 落在哪 | 为什么 |
|---|---|---|---|
| **P0 骨架** | | | |
| 1 | 三张表 | `models/graph_entity.py`、`graph_relation.py`、`graph_job.py` | 实体 / 关系 / 任务。实体不存 department、不存原文 |
| 2 | 迁移 | `migrations/versions/xxxx_graph_tables.py` | 建表 + `knowledge_docs` 加 `kg_status` / `kg_error` / `kg_entity_count`，默认 `skipped` |
| 3 | 受限抽取 | `rag/graph/extract.py` | 实体类型白名单 + 谓词白名单 + `ALLOWED_DEPARTMENTS` 强约束 |
| 4 | 三层消歧 | `rag/graph/resolve.py` | 词典精确 → 名称向量 0.92 → 人工。图的成败在这一步 |
| 5 | 回填脚本 | `scripts/build_graph.py` | 限流 + 断点续跑（按 `kg_status`）+ `--gc` 清孤儿 |
| 6 | 任务解耦 | `crud/graph_job.py` + `rag/indexer.py` 挂载 | 先 `ready` 可检索再异步建图；`KG_ENABLED` 默认 false |
| **P1 检索接入** | | | |
| 7 | 实体链接 | `rag/graph/link.py` | query → 实体（词典 + 向量混合） |
| 8 | 图查询 | `rag/graph/store.py` | 多跳递归 CTE + `_permission_clause`，产出 chunk_id |
| 9 | 接入主链路 | `rag/store.py:search` + `config/settings.py` | 图只扩候选池；fail-fast 检查 `KG_ENABLED → RERANK_ENABLED` |
| 10 | 评测 | `eval/graph_set.jsonl` + `eval/run_graph.py` | 多跳题集 + **graph-only recall** + 消融 |
| **P2 治理与演示** | | | |
| 11 | 可视化 | `routers/graph.py` + `frontend/src/views/GraphView.vue` | 实体邻域浏览 + 人工合并。**"知识图谱"四个字唯一能被看见的地方** |
| 12 | 定时清理 | 挂 `--gc` | 孤儿实体会持续膨胀 |

### 时间有限时，最小可讲版本

`1 2 3 4 5` + `7 8` + `11` —— 能抽、能查、能看图。跳过 6（异步队列，先用脚本手动跑）、9（不接主链路）、10（先人工看效果）。

这条路能讲清"我建了图、它能多跳、我解决了消歧"三件事，且完全不碰主链路，零回归风险。

---

## 1. 先把三个词拆开

"知识图谱 + graph engineering"其实是三件独立的事，混在一起做会失焦：

| 层 | 是什么 | 解决什么问题 | 本项目判断 |
|---|---|---|---|
| **L1 实体关系层（KG）** | 从 chunk 抽 `(主体, 关系, 客体)` 三元组，落库，支持多跳遍历 | 跨文档关联推理（"技术部的年假比标准多几天"要两份文件） | **做**，收益真实且可测 |
| **L2 全局摘要层（GraphRAG）** | Leiden 社区检测 + 每个社区一次 LLM 摘要 | 总结类问题（"公司有哪些员工福利"） | **可选**，ROI 明显低于 L1，见 §8 |
| **L3 编排图层（graph engineering）** | 把 Agent 流程显式画成状态机节点 | 流程可控性 | **不做**，见 §9 |

**L3 特别说明：graph engineering 不是严格术语，本项目在编排层已经具备。**

这个词没有公认的技术边界，也没有对应的库，它只是描述性说法，落在两个层面：编排层（把 Agent 流程建模成图）和数据层（把知识建模成图）。本项目的情况是：

| 图工程的要素 | 本项目对应物 |
|---|---|
| 图节点 | `create_agent` 的 6 个 middleware（`rag_hook` / `exact_source_hook` / `collect_sources_hook` / `prune_knowledge_hook` / `tool_logging_hook` / `model_step_log_hook`） |
| 条件边 | `max_steps_guard` 的 `can_jump_to=["end"]` |
| 显式状态转移 | `Command(update={"jump_to": "end"})` |
| 图级硬顶 | `AGENT_RECURSION_LIMIT` + `ModelCallLimitMiddleware` |

也就是说，**"要不要加 graph engineering"这个问题在本项目里的答案是：编排层已经有了，缺的是数据层那张图。** 再往"图工程化"走只有一条路：把 middleware 拆成显式节点（retrieve → route → generate），这是纯重构、能力零增量，还会丢掉已经调好的东西（预检索注入时机、来源收集、`prune_knowledge_hook` 的清理逻辑、三层轮次上限）。**不建议动。**

**一个必须说清的陷阱**：正因为它是"思想"，容易让人以为"想通思路就够了"。事实相反 —— 图项目的失败几乎全部死在数据治理（抽取质量、实体消歧、增量腐化、权限穿透），不死在架构思路。这四件事没有一件是思路能解决的，全是工程量。

下面只讨论 L1，L2 在 §8 单独说。

---

## 2. 现状盘点：缺口到底在哪

现有检索链路（`rag/store.py:search`）：

```
query → embed_query ──┐
                       ├→ RRF(0.6 vector + 0.4 bm25) → rerank(top N) → 门控 → top3
query → BM25 ─────────┘
```

能答的：单点事实查询（"年假几天"→ 命中《请假与休假管理规定》）。
答不了的，按缺口严重程度排：

1. **跨文档关联**：答案分散在两份文件，`top_k=3` 且 RRF 只按单 chunk 相关性排序，第二份文件通常排在第 5~10 位被门控截掉。
2. **实体聚合**："技术部有哪些特殊规定" —— 需要先定位"技术部"这个实体，再找所有指向它的文档。语义检索对实体名不敏感（"技术部"在向量空间里和"研发"很近，但和《考勤补充规定》很远）。
3. **全局总结**："公司有哪些福利" —— `top_k=3` 天然答不了，这是 L2 的活。

缺口 1、2 是 L1 能补的，缺口 3 不属于 L1。

---

## 3. 三个必须先定的架构决策

### 决策一：用 PostgreSQL，不上 Neo4j

**先说准确版本：Neo4j 不是"上不了"，是"能上，但代价不对等"。**

其中确实有一层路径依赖：权限被做成 chunk 行上的三个冗余字段，这是为"行级权限下推 SQL WHERE"优化的设计，而图的核心动作是跨行遍历 —— 两者方向相反。如果第一版就做成策略引擎（权限不进数据行，进独立策略表、查询时 join），图和权限确实更容易共存。这一层要认。

但即使推倒重来，本项目仍不该上 Neo4j，以下三条与历史决策无关：

**1. 规模和形状不对。** 3~5 千实体 / 2~4 万条边。Neo4j 的价值区间是"亿级节点 + 4 跳以上深链遍历"，这个量级在 PG 邻接表上跑 2 跳递归 CTE 是毫秒级，性能收益为零，却要额外承担第 5 个容器、备份、监控、连接池、版本升级。

**2. 行级权限与图遍历，模型本身冲突。** Neo4j 没有 PG 那样的原生行级安全（RLS）。要在多跳里注入权限条件，Cypher 得写成：

```cypher
MATCH path = (a)-[*1..2]-(b)
WHERE ALL(n IN nodes(path) WHERE n.department = $dept OR n.is_shared)
```

但**这个条件根本写不出来** —— 实体节点是跨文档合并的，它上面没有单一 `department`。于是只剩两条路：权限字段冗余进图（实体在 N 个部门间分裂成 N 份，图退化为按部门切片的索引，跨文档价值归零），或者每跳跨库回 PG 查。而 PG 侧这一条就是普通 `WHERE`，与现有 `_permission_clause` 是同一个东西。

**3. 丢掉 `ON DELETE CASCADE` 这个免费的正确性。** PG 里 `graph_relations.chunk_id` 挂 FK CASCADE，`delete_by_file` 一执行边自动清。Neo4j 没有外键约束，双写后删文件要手写清理顺序，漏一条就是**幽灵召回**：图返回已删除的 chunk_id，回表查不到，静默丢弃。不致命，但会污染 `graph-only recall` 指标，让人误判图有没有用。

代价对比很直观：**递归 CTE 10 行，Cypher 3 行 —— 省 7 行代码换一个数据库。**

#### 什么时候才真的该上 Neo4j

留一套判据，便于以后自己判断而不是背结论：

| 判据 | 本项目 |
|---|---|
| 需要 **4 跳以上**路径查询（股权穿透、供应链溯源、反欺诈资金链路） | ❌ 查询深度 ≤2 |
| 需要**内建图算法**（PageRank / 社区检测 / 中心性）跑在**百万级**节点 | ❌ 实体数千级 |
| **图本身是主数据模型**，不是从文档抽出的附属索引 | ❌ 是附属索引，节点量被文档量约束 |
| 有独立运维能力，多一个数据库不影响交付 | ❌ 单实例部署 |

佐证：**微软 GraphRAG 官方实现的索引产物是 Parquet 文件，查询时在内存里建图**，Neo4j 只是可选导出目标之一。业界做 RAG 图检索的主流实现，恰恰不依赖图数据库。

若坚持要上：唯一可行路径是"图只存结构、不存内容"的地址簿路线（权限仍留在 PG），并接受双写一致性全靠自己维护。技术上行得通，但不划算。

进阶可选 Apache AGE（PG 的 Cypher 扩展），但 AGE 与 pgvector 的索引共存有坑，且 2 跳递归 CTE 手写也就 10 行。**不建议一开始就上。**

### 决策二：图只做"地址簿"，不做"内容库"

这是全文最重要的一条约束。**图的价值是"告诉我该看哪些 chunk"，不是"直接告诉我答案"。**

反例（GraphRAG 论文的常规做法，在本项目**不可用**）：实体页面 = 该实体的所有描述 + 关联社区摘要，整段注入 prompt。后果：技术部员工问"年假"，注入的实体页面里含人事部文档的原文 → **跨部门泄漏**。

正面设计：

| 存什么 | 存不存 |
|---|---|
| 实体标准名、类型、别名、名称 embedding | ✅ 存（名称本身不敏感） |
| 关系的证据 chunk_id、置信度 | ✅ 存（chunk_id 是权限锚点） |
| 实体的属性值（"年假=10天"） | ❌ **不存**，回 chunk 里读 |
| 实体描述、社区摘要（部门维度） | ❌ 不存进主图，见 §8 |

**唯一锚点原则**：图扩展产出的永远是 `chunk_id` 集合，然后必须回到 `knowledge_chunks` 表，走**原来那条** `_permission_clause` + `status='ready'` + 软删过滤的 SQL。图不产生任何新的数据出口。

这样权限实现只有一处，`services/permissions.py` 和 `store._permission_clause` 都不用改。

### 决策三：抽取管线必须与 `index_status` 解耦

现有状态机三层：

- `doc.status`：审核（pending → approved/rejected）
- `doc.index_status`：入库（pending → indexing → ready/failed）
- `IndexJob`：任务（pending/running/done/failed，带 lease + attempts + backoff）

**KG 抽取绝不能塞进 `_index_document_inner`。** 成本对比：

| 环节 | 单份 50 页 PDF 耗时 |
|---|---|
| 现状（解析 + embedding） | ~30 秒 |
| KG 抽取（~120 chunk × 1 次 LLM） | **2 分钟以上** |

塞进去 = 检索可用性从 30 秒退化到 2 分钟以上。这是倒退。

正确做法（照抄本项目 OCR 通道已经验证过的降级模式，`PDF_OCR_TEXT_ENABLED`）：

```python
# 顺序必须是：先 ready 可检索，再异步建图
doc.index_status = "ready"     # ← 先 commit，立刻可检索
await db.commit()
# 图抽取作为独立 job，失败只影响图通道
await enqueue_graph_job(db, doc.id)
```

- 新增 `doc.kg_status`（pending / extracting / ready / failed / skipped），**独立状态机**
- 新增 `graph_jobs` 表，**照抄 `IndexJob` 的 lease + attempts + backoff 结构，但独立成表**
  - 原因：`IndexJob.doc_id` 是 `unique=True`，一个 doc 只能有一行 job。加 `stage` 列会破坏现有 claim 逻辑（`crud/knowledge.py:claim_index_job` 按 `doc_id` 条件 UPDATE），且现有并发判定靠的是 `rowcount`，改约束风险大。独立表零风险。
- `KG_ENABLED=false` 时整条链路不存在，行为与改造前完全一致（对齐现有"可选能力默认关闭"的项目惯例）

---

## 4. 检索链路改造：图通道是候选池扩充器，不是第三路 RRF

### 4.1 为什么不能作为第三路 RRF

看起来最自然的接法是把图加进 `rrf_fuse` 的第三路（`rrf_fuse` 本身已经支持 N 路，`weights` 长度对齐即可，函数不用改）。但这条路有三个硬坑：

**坑 A：门控会把图通道全杀。**
`select_relevant_hits` 的 rrf 分支：

```python
def _relevance(hit):
    return float(hit.get("_relevance_score", hit.get("score", 0.0)))
top_relevance = _relevance(ordered[0])
floor = max(SEARCH_MIN_SCORE, top_relevance - SEARCH_MAX_DROP)   # ≥ 0.80
```

图通道召回的 chunk 没有向量分，`_relevance_score` 缺失 → 落到 `score`（RRF 归一化值，量级 0~1 但分布完全不同）→ 被 0.80 的地板全杀。要救就得回填向量分（一条 `SELECT 1 - (embedding <=> :qv)/2`，同库同表，这是选 PG 的红利），但——

**坑 B：多跳邻居的向量分天然很低。**
第二跳的 chunk 和 query 语义本来就远（这正是需要图的原因）。回填后依然过不了门控。放宽阈值又会击穿"无答案拒答"的现有能力（`eval/README.md` 记录：关掉 rerank 时无答案题拒绝率 0/5，门控全靠 rerank 撑着）。

**坑 C：图候选会挤掉原有候选。**
`RERANK_TOP_N` 是常量（README 明确写"是常量不是旋钮"）。图候选塞进同一个池子，原本排第 20 的向量候选被挤出去，可能反而降 Recall。

### 4.2 正确接法：图只扩候选池，判断权交给已有的 cross-encoder

```
query
 ├─ 向量召回 top N ───────────┐
 ├─ BM25 top N ───────────────┤
 │                            ├→ RRF 排序 → 候选池（RERANK_TOP_N + GRAPH_TOP_K）
 └─ 实体链接 → 图扩展(≤2跳) ──┘                    ↓
      → chunk_id → 权限 SQL              cross-encoder rerank（绝对分）
                                                    ↓
                                        RERANK_MIN_SCORE 门控（完全不变）
                                                    ↓
                                               top3 注入 prompt
```

**为什么这个接法是对的：**

- `rerank_score` 是 **cross-encoder 对 (query, chunk) 直接打分的绝对分**，不依赖候选来自哪一路。图找来的 chunk 只要真的相关，rerank 自然给高分；不相关，自然被门控拒掉。**图的唯一职责是"把原本进不了候选池的 chunk 送进来"，这恰好是图该干的事。**
- `select_relevant_hits` **一行不用改**。
- `SEARCH_MIN_SCORE` / `SEARCH_MAX_DROP` / `RERANK_MIN_SCORE` **全部不用重标定** —— 因为 `rerank_score` 是绝对分，扩池不改变它的分布。（对比：如果改 `RERANK_TOP_N` 本身，`_relevance_score` 是请求内相对分，扩池会抬高无关 query 的分数分布，必须重标定。这两个阈值性质不同，别混。）

### 4.3 由此推出一条硬依赖：`KG_ENABLED` 必须要求 `RERANK_ENABLED`

关掉 rerank 时，图候选没有任何绝对分可用，只能回落 `_relevance_score` → 回到坑 A/B，必然全杀或误放。

所以要在 `config/settings.py` 加 fail-fast（对齐现有 `LLM_SUPPORTS_VISION=false` 直接拒绝启动的先例）：

```python
if KG_ENABLED and not RERANK_ENABLED:
    raise RuntimeError(
        "KG_ENABLED=true 要求 RERANK_ENABLED=true：图通道只扩充候选池，"
        "相关性判定必须交给 cross-encoder 绝对分；否则图候选会绕过门控或被门控全杀。"
    )
```

这条检查本身就是设计文档 —— 它把"图不做独立门控"这个约束钉死在启动时。

---

## 5. 数据模型

### 5.1 新增两张表

```python
class GraphEntity(Base):
    __tablename__ = "graph_entities"
    id: int
    canonical_name: str(128)      # unique，归一后的标准名
    type: str(32)                 # 白名单类型，见 §6
    aliases: str                  # 逗号分隔别名
    name_embedding: Vector(DIM)   # 实体链接 + 自动合并用
    mention_count: int            # 支撑度；低频实体可归档
    # 刻意不存：department / 任何原文 / 任何属性值
    # 实体是跨文档合并的，带上部门字段会在 N 个部门间分裂成 N 份，图就退化成按部门切片的索引


class GraphRelation(Base):
    __tablename__ = "graph_relations"
    id: int
    subject_id: int               # FK graph_entities ON DELETE CASCADE
    predicate: str(64)            # 关系名，白名单
    object_id: int | None         # 实体型宾语；值型关系为 NULL
    value: str | None             # 字面值型宾语（金额/天数/比例），不建节点
    chunk_id: str(64)             # FK knowledge_chunks.id ON DELETE CASCADE ← 权限唯一锚点
    confidence: float
    # UNIQUE(subject_id, predicate, object_id, chunk_id) → 重入幂等
```

**两个设计点值得单独说：**

1. **`value` 字段砍掉一半实体爆炸**。如果"年假 --[标准为]--> 10 天"把"10 天"建成节点，图里会塞满 `500元`、`30天`、`80%` 这种一次性名词。让宾语是字面值时落 `value` 不建节点，实体数量能降一半以上。
2. **`chunk_id` 上的 FK CASCADE 是免费的正确性**。`store.delete_by_file` 是 `DELETE FROM knowledge_chunks WHERE file_id=...`，级联自动清边，不用在 indexer 里手写清理顺序 —— 手写一定会漏（这正是项目里 `KnowledgeDoc.file_id` 用 `ondelete="CASCADE"` 并注释"不靠手写清理顺序"的同一个理由）。

### 5.2 迁移与既有表改动

```
migrations/versions/xxxx_graph_tables.py
  ├─ create table graph_entities / graph_relations / graph_jobs
  ├─ alter knowledge_docs add kg_status varchar(20) default 'pending'
  ├─ alter knowledge_docs add kg_error varchar(500) null
  └─ alter knowledge_docs add kg_entity_count int default 0
```

`kg_status` 默认值用 `'skipped'` 更稳（存量文档不会因为部署就排队抽取），需要回填时跑 `scripts/build_graph.py` 显式改状态。**默认不动存量 = 部署零副作用。**

### 5.3 孤儿实体清理

删文件时边靠 CASCADE 自动清，但**实体节点不会删**（它可能还被其他文档引用，也可能变成 `mention_count` 归零的孤儿）。必须有运维脚本：

```sql
DELETE FROM graph_entities
WHERE id NOT IN (SELECT subject_id FROM graph_relations)
  AND id NOT IN (SELECT object_id FROM graph_relations WHERE object_id IS NOT NULL);
```

放进 `scripts/build_graph.py --gc`，挂到定时任务或手动跑。**不写这条，图会持续膨胀。**

---

## 6. 抽取与消歧：图的成败在消歧，不在抽取

### 6.1 抽取：受限 schema，不要开放域

开放域实体抽取在中文企业文档上噪声极大。三层约束：

1. **实体类型白名单**：`department / position / role / policy / process / system / form / org`。
2. **关系谓词白名单**：`适用 / 审批 / 归属 / 依据 / 包含 / 负责 / 替代 / 标准为`，不超过 10 个。
3. **部门用现有词典强约束**：`config/departments.py:ALLOWED_DEPARTMENTS` 的 5 个部门名直接喂给抽取器，命中即锁定 canonical。字典匹配的准确率远高于 LLM 抽取，这类确定性信息没理由交给模型猜。

另外**跳过的类型**：`_parse_xlsx` 产出的 `a | b | c` 表格行块（`sheet` 字段非空）。表格是结构化数据，该走 Text2SQL（项目里已有 `/analytics/query`），不该走图。硬抽只会得到一堆列名当实体。

### 6.2 消歧：三层，最后一层必须有人工

"年假" / "带薪年休假" / "annual leave" 是同一个；"技术部" / "技术部（研发中心）"可能是一个也可能是两个。不归一，图就是碎片，多跳根本走不通。

| 层 | 手段 | 阈值 |
|---|---|---|
| 1 | 精确匹配已有的 `canonical_name` / `aliases` | — |
| 2 | 名称 embedding 余弦近似自动合并 | `GRAPH_MERGE_MIN_SCORE=0.92` |
| 3 | 人工审核合并/拆分 | — |

**第 3 层不能省。** 自动合并阈值调高会漏合并（图碎片化），调低会错合并（"技术部"和"技术支持部"被并成一个，多跳走错）。复用现有 `ReviewView.vue` 的审核交互模式做一个实体合并页 —— 这也是最容易被面试官追问的地方，有界面比只有脚本强得多。

---

## 7. 代码落点清单

### 新增

| 文件 | 内容 |
|---|---|
| `models/graph_entity.py` | 实体表 |
| `models/graph_relation.py` | 关系表 |
| `models/graph_job.py` | 任务表（照抄 `IndexJob` 结构） |
| `migrations/versions/xxxx_graph_tables.py` | 建表 + `knowledge_docs` 加 3 列 |
| `rag/graph/extract.py` | LLM 抽取（schema 约束 + JSON 修复 + 失败降级） |
| `rag/graph/resolve.py` | 实体归一/合并（三层策略） |
| `rag/graph/store.py` | 图查询：多跳递归 CTE、按实体取 chunk、孤儿清理 |
| `rag/graph/link.py` | query → 实体链接（词典 + 向量混合，风格对齐现有 RRF） |
| `routers/graph.py` | `/graph/entities`、`/graph/neighbors`、`/graph/merge` |
| `scripts/build_graph.py` | 存量回填（限流 + 断点续跑 + `--gc`） |
| `frontend/src/views/GraphView.vue` | 实体浏览 + 合并 |
| `eval/graph_set.jsonl` | 多跳题集 |
| `eval/run_graph.py` | 图通道评测 |

### 改动

| 文件 | 改什么 | 风险 |
|---|---|---|
| `rag/store.py` | `search()` 内加图通道：`link()` → 图扩展 → chunk_id → 权限 SQL → 并入候选池；`RERANK_TOP_N` 池子加 `GRAPH_CANDIDATE_TOP_K` | **中**，唯一碰主链路的地方 |
| `rag/indexer.py` | `_index_document_inner` 末尾 commit 后 `enqueue_graph_job`；新增 `extract_doc_graph` 独立函数 | 低，纯追加 |
| `models/knowledge_doc.py` | 加 `kg_status` / `kg_error` / `kg_entity_count` | 低 |
| `crud/knowledge.py` | 加 `graph_jobs` 的 claim/finish/requeue（照抄现有 4 个函数） | 低 |
| `config/settings.py` | `KG_*` 开关 + §4.3 的 fail-fast 检查 | 低 |
| `tools/knowledge_tools.py` | **不改**。`search_knowledge` 内部已经走 `store.search`，图通道自动生效 | 无 |
| `agents/graph.py` | `rag_hook` **不改**。同上 | 无 |
| `rag/fusion.py` | **不改**。`rrf_fuse` 已支持 N 路 | 无 |
| `docker-compose.yml` | **不改**（选 PG 的直接收益） | 无 |

**值得注意**：`agents/graph.py` 和 `tools/knowledge_tools.py` 都**不用改**，因为图通道藏在 `store.search()` 里，两个调用点自动获得能力。这是把图放在 store 层而不是 hook 层的最大好处 —— 少两处改动，少两处不一致风险。

---

## 8. L2 全局摘要：能做，但先说清代价

### 成本

| 项 | 量级 |
|---|---|
| 社区检测（Leiden） | PG 里跑不了，要拉进 `networkx`/`igraph` 内存跑。3~5 千节点可接受 |
| 每社区一次 LLM 摘要 | 假设 200 社区 × 1 次 × 4 秒 ≈ **13 分钟/次全量** |
| 增量更新 | **死穴**。加一份文档要么全量重算，要么做受影响社区局部更新（复杂且容易不一致） |

### 建议

**离线批处理 + 手动触发，绝不进请求链路，绝不进 indexer。**

做成 `scripts/build_communities.py`，产出 `graph_communities` 表（社区 id、成员实体、摘要文本、覆盖的 chunk_id 范围）。检索侧只在判定为"全局类问题"时才查，且必须复用同一套权限 SQL 展开 chunk。

但说句实话：**对求职作品集项目，L2 的 ROI 明显低于 L1。** 同样投入放在多跳 + 评测上，能讲的东西更硬（有指标、有消融）；L2 讲起来漂亮，但很容易在"增量更新怎么办"上被问穿。**建议放在 L1 跑稳、题集能证明收益之后再考虑。**

---

## 9. 评测方案：这是唯一能证明"改得值"的东西

现有 `eval/` 只有 21 题检索层（知识 8 / 字面 8 / 无答案 5），且 `README.md` 已经诚实写明"存在明显上限效应，两种融合都拿满分"。**加图之后如果还跑这套题，大概率还是满分，证明不了任何事。**

必须新增三样：

### 9.1 多跳题集 `eval/graph_set.jsonl`

格式对齐现有 `eval_set.jsonl`，加一个 `hops` 字段：

```json
{"id":"g-01","category":"multihop","hops":2,"question":"技术部员工的年假比公司标准多几天？","department":"技术部","expected_files":["请假与休假管理规定.md","技术部考勤补充规定.md"],"expected_keywords":["5 天","7 天"],"expect_refusal":false}
```

**选题原则**：必须构造"单靠向量检索拿不到第二份文件"的题 —— 即拆开两份文件单独看都答不全。这样的题才有区分度。

### 9.2 图独占命中率（graph-only recall）—— 最重要的一个指标

```
graph_only = 图通道召回的 chunk 中，vector top-20 和 bm25 top-20 都没召回到的比例
```

**如果这个数是 0，图就是白做的。** 必须诚实报这个数，并且写进 `eval/results/`。

这是判断图有没有价值的唯一硬指标 —— 其他指标（Recall、MRR）都可能因为池子变大而"看起来变好"，只有 graph-only 能证明图真的带来了新东西。

### 9.3 消融实验

扩展现有 `run_retrieval.py` 的 `--fusion both --rerank both` 模式，加 `--graph both`：

| 配置 | 多跳题 Recall@5 | 单跳题 Recall@5 | avg_noise | 无答案拒绝率 | P95 |
|---|---|---|---|---|---|
| 两路 + rerank | 基线 | 基线 | 基线 | 5/5 | 基线 |
| 三路（含图）+ rerank | ？ | ？ | ？ | ？ | ↑ |

**必须同时看单跳题和无答案题有没有退化**。图通道扩池最典型的副作用是噪声上升、拒答率下降 —— 这两列不变差，才算改成功。

### 9.4 实体链接准确率

手标 30 个 query → 期望实体，算 Precision/Recall。不做这个，图召回错了根本不知道错在抽取、消歧还是链接。

---

## 10. 风险清单

| 风险 | 具体表现 | 应对 |
|---|---|---|
| **LLM 抽取成本与耗时** | 1 万 chunk × 1 次调用。钱不多（约 8 元/万 chunk），但并发 10 也要 15 分钟以上 | 回填脚本限流 + 断点续跑（按 `kg_status` 续）；不进主链路 |
| **抽取质量** | 中文长句、表格、OCR 低置信块（`source=ocr_lowconf`）抽出来全是噪声 | OCR 低置信块跳过；xlsx 整类跳过；只对 `type=text` 且 `source != 'ocr_lowconf'` 抽 |
| **实体爆炸** | 一次性名词占一半 | `HAVING mention_count >= 2` 才建正式实体；单次提及只留 relation 不建节点 |
| **错误合并** | "技术部" 与 "技术支持部" 被并 | 自动合并阈值 0.92 保守起步 + 人工合并页兜底 |
| **图与 chunk 不一致** | 删文件后边残留 | `chunk_id` FK CASCADE + `--gc` 清孤儿实体 |
| **噪声上升 / 拒答率下降** | 扩池的典型副作用 | 门控完全不动（靠 rerank 绝对分），消融实验盯住 `avg_noise` 和 `refusal_rate` |
| **部署副作用** | 存量文档被批量排队抽取，卡住后台 | `kg_status` 默认 `skipped`，回填必须显式触发 |

---

## 11. 分阶段落地

按"改动面 × 风险"排序，不按时间：

### P0：骨架（不改主链路）

1. 三张表 + 迁移
2. `rag/graph/extract.py` + `resolve.py`（先用 3~5 份文档验证抽取质量，**这一步不过关就别往下走**）
3. `scripts/build_graph.py` 回填 + 限流 + 断点续跑
4. `graph_jobs` + indexer 挂载（`KG_ENABLED` 默认 false）

**验收**：库里能查到实体和边，抽取准确率人工抽查 ≥ 70%，主链路行为零变化（跑一遍现有 `eval/run_retrieval.py`，指标必须一模一样）。

### P1：检索接入（唯一碰主链路的地方）

1. `rag/graph/link.py` 实体链接
2. `rag/graph/store.py` 多跳递归 CTE + 权限 SQL
3. `rag/store.py:search()` 接图通道 + `settings.py` fail-fast
4. `eval/graph_set.jsonl` + `run_graph.py` + 消融

**验收**：graph-only recall > 0；多跳题 Recall 提升；单跳题与无答案题指标不退化。

### P2：可视化与治理

1. `routers/graph.py` + `GraphView.vue`
2. 人工合并/拆分
3. 孤儿实体清理挂定时任务

**验收**：能点开看实体邻域、能手工合并错误实体。

### P3（可选）：L2 社区摘要

先看 P1 的消融结果再决定。若 P1 的 graph-only recall 一直很低（< 5%），说明这套语料本身关联性弱，L2 也不会有想象中好，**及时止损比硬做更专业**。

---

## 12. 一句话总结

**做 L1，用 PG 不引入新库，图只负责扩充候选池、判断权留给已有的 cross-encoder，抽取与入库状态解耦，权限永远在 chunk 上判。** 这条路线改 1 处主链路（`rag/store.py:search`）、5 处追加，回归风险接近零，且每一步都能被 `eval/` 验证。

L2 看 P1 数据再决定，L3 不做。
