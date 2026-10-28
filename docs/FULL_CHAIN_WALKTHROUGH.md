# 两个项目的全链路解析

面试时被问"你这个项目怎么跑的"，答不出链路就说明只是"用过"，不是"做过"。
这份文档按"一次请求从头到尾经过什么"来写，每一步都标了文件与函数，方便对着代码讲。

---

# 一、kb-platform（企业知识库平台）

技术栈：FastAPI + 异步 SQLAlchemy + PostgreSQL(pgvector) + LangGraph + Vue 3

## 1. 一次提问的完整链路

```
POST /chat/ask
  ├─ ① 认证：routers/auth.py:get_current_user
  │     JWT 解析 → 查 User → 校验 token_version（改密码就失效）
  │     → bind_user_id（日志上下文）
  │     → bind_enforced_department（权限上下文，admin/leader 传 None）
  │
  ├─ ② 入口：routers/chat.py:ask
  │     加载/创建会话 → 组装 history → 取长期记忆
  │     → run_agent(question, history, dept, allow_all, thread_id)
  │
  ├─ ③ 预检索：agents/graph.py:rag_hook
  │     store.search() 三路召回 + RRF + cross-encoder 重排
  │     → select_relevant_hits() 门控
  │     → 把命中的 chunk 包成 SystemMessage 注入
  │
  ├─ ④ Agent 循环：create_agent + 8 层 middleware
  │     LLM 决定是否调工具（search_knowledge / retry_search /
  │     list_department_docs / check_doc_status / query_personnel / query_employee_stats）
  │     每轮工具调用由 tool_logging_hook 统一记录与异常归一化
  │
  ├─ ⑤ 收尾：max_steps_guard
  │     超步时不是硬停，而是基于已有信息强制收尾，避免前序数据被丢弃
  │
  └─ ⑥ 持久化：routers/chat.py
        answer + sources + data_sources 落 ChatMessage
        sources[N-1] 对应正文 [N]（前后端契约）
```

## 2. 检索链路：三路召回怎么合并

```
query
 ├─ 向量检索：embed_query → pgvector 距离 <->  → top 20
 │     WHERE _permission_clause（部门过滤直接进 SQL，不是查出来再筛）
 ├─ BM25 检索：2-gram 分词 → 内存索引打分 → top 20
 ├─ 图通道：link_query(词典+向量) → neighbor_chunks(多跳) → chunk_id 回表
 │     也走同一条 _permission_clause
 ↓
RRF 融合（k=60）→ 候选池
 ↓
cross-encoder 重排（rerank_top_n=42）→ 绝对分
 ↓
门控 select_relevant_hits
 ├─ 强相关：score >= 0.9 → 注入，进入 [知识库参考]
 ├─ 弱相关：score >= 0.55 且领先第二名 >= 0.20 → 注入一条，打 _weak_evidence 标记
 └─ 都不满足 → 注入"本次没检索到"，并提示可调 retry_search 改写后重试
```

**为什么要弱相关兜底**：实测「你好我的年假还有多久」召回正常（BM25 第 1、向量 0.89），
但 rerank 只有 0.708 —— cross-encoder 判断"这段文字回答不了'我还剩几天'"，判断本身没错。
问题在门控清空后模型看到"没有资料"，就说了句"知识库里没有年假政策"，而库里有。

修法两层：门控加弱相关兜底 + 注入侧说明"这是最接近的资料、讲制度但不覆盖个人数据"。

**为什么图只扩池不做第三路**：图候选没有向量分，过不了按余弦标定的 `_relevance_score` 地板（0.80）。
扩到池里之后，由 cross-encoder 的绝对分决定去留——它是跨模型可比的，判断权归重排。

## 3. 权限为什么只有一个出口

三张表的设计：

| 表 | 存什么 | 刻意不存什么 |
|---|---|---|
| `graph_entities` | 名字、类型、别名、名称向量 | **不存正文、不存部门、不存属性值** |
| `graph_relations` | 主谓宾 + `chunk_id`（FK CASCADE） | — |
| `knowledge_chunks` | 正文、department、is_shared、shared_departments | — |

关键在"不存"：实体是跨文档合并的，一旦带上部门就会分裂成 N 份；一旦存了正文，
图就成了绕过权限的第二个读口。**图只做地址簿，读内容必须回原表过 `_permission_clause`。**

对照实验（已验证）：
```
department=None        chunks=8   含 33:0（技术部-数据库变更审批）
department='人事部'     chunks=5   不含 33:0        ← 被挡住
department='技术部'     chunks=8   含 33:0
```

**本次改动**：把 `_permission_clause` 从 7 处手动调用改成 ORM 层强制注入
（`rag/permission.py` 的 `do_orm_execute` + `with_loader_criteria`）。
这样新增查询接口忘了写权限，条件也会自动挂上。三条边界：只拦 SELECT、
只作用于 KnowledgeChunk、没有"生效部门"时不注入（后台任务不受影响）。

## 4. 入库链路：为什么建图要和索引解耦

```
上传 → parser 切片（500 字/块，重叠 50）→ embed → knowledge_chunks
     → index_status = ready          ← 此刻已经可以被检索到
     → 入队 graph_job                 ← 建图异步跟随
       kg_status: pending → running → ready / failed
```

**为什么不串行**：建图要 LLM 抽实体，44 份文档跑了 690 秒。串行意味着用户传完文档要等 11 分钟
才能提问。解耦后 index_status=ready 就能检索，图慢慢补。

`graph_jobs` 独立成表而不是复用 `IndexJob`：后者有 `unique(doc_id)`，一份文档一行，
再塞建图状态会把"重建索引"和"重建图"绑死。

## 5. 评测怎么做的（三套，管不同层面）

| 评测 | 题量 | 管什么 | 核心指标 |
|---|---|---|---|
| `run_retrieval.py` | 21 题 | 融合策略 × 重排开关 | Recall@k / MRR / nDCG / 拒答率 |
| `run_graph.py` | 15 题 | 图通道净贡献 | **graph_only_rate** |
| `agent_eval.py` | 15 题 | Agent 七维（过程+结果） | 工具真实性 / 忠实度 / 安全 |

**graph_only_rate 是关键**：图召回的 chunk 里，向量 top20 与 BM25 top20 都没命中的比例。
只看 Recall 会被扩池骗——池子变大 Recall 通常涨，但那可能只是 cross-encoder 多看了几十个候选。

首次实测是 0：语料是"1 文档 1 chunk"，被实体连到的 chunk 正文必然写着实体名，BM25 必然能召回。
**所以本次先换多跳语料再复测**（见文末待验证）。

---

# 二、cs-agent（多 Agent 智能客服）

技术栈：FastAPI + LangGraph + PostgreSQL(pgvector) + Redis + 原生 JS 三页面

## 1. 一次提问的完整链路

```
POST /chat/stream（SSE）
  ├─ ① 认证：get_current_user → 组装 user_payload
  │     {"id", "role", "session_id"}  ← 工具的身份来源
  │
  ├─ ② 分流：USE_SUPERVISOR 决定走哪条
  │     false（默认）→ run_agent（单 Agent，13 个工具）
  │     true         → run_supervisor（路由 + 3 个 worker）
  │
  ├─ ③ Supervisor 路由：agents/supervisor.py:route_question
  │     LLM 输出 {"route": "...", "confidence": 0.86}
  │     confidence < 0.75 → 不派给 worker，走 clarify 节点直接追问
  │
  ├─ ④ Worker 执行（每请求现建，不缓存）
  │     order：query_order / request_refund / cancel_order / change_address /
  │            urge_order / check_refund_status / escalate_to_human
  │     product：query_product
  │     knowledge：search_knowledge（内部走 rag.retrieve + rerank）
  │
  ├─ ⑤ 工具执行：tools/action_tools.py
  │     **不是直接改库，是 HTTP 调后端 API**（BACKEND_BASE，默认 127.0.0.1:8000）
  │     原因见下一节
  │
  └─ ⑥ 落库：chat_messages + action_audit_logs（动作审计）
```

## 2. 工具为什么走 HTTP 而不是直接写库

`tools/_backend_client.py` 里所有写操作都是 `POST /refunds`、`POST /orders/{no}/cancel` 这类调用。

三个理由：

1. **鉴权只有一处**：HTTP 入口会走完整的 `get_current_user` + 归属校验，
   工具直接写库就要把权限逻辑再实现一遍，两处实现迟早不一致
2. **幂等键落在服务端**：`idempotency_key=f"refund:{order_no}"` 由后端统一去重，
   网络重试不会重复退款
3. **审计统一**：`action_audit_logs` 在 API 层写，不管从哪个入口进来都留痕

代价是跨进程调用，延迟高一点；换来的是"越权只有一条路能走出来"。

## 3. 退款的一致性设计

```
POST /refunds
  ├─ 行锁 SELECT ... FOR UPDATE（锁订单行）
  ├─ 部分唯一索引 (order_id) WHERE status IN ('pending','approved')
  │     并发重复提交在数据库层被拦，不靠应用层判断
  ├─ 冲突时回滚到 SAVEPOINT（不是整体回滚，避免 ORM 实例失效）
  └─ 规则判定
       全部通过 → decision=auto → order.status=refunded, refund.status=approved
       任一不满足 → decision=human → order.status=refund_pending，转人工
```

**为什么不靠应用层判断**：两个请求同时读到 `status=pending`，都通过检查，都去写。
唯一索引是最后一道闸，它不依赖任何应用层逻辑。

## 4. 上下文与中断

- 长会话：`SummarizationMiddleware` 摘要压缩，不是丢弃式裁剪
  - 坑：直接挂载会改写图状态，只能挂在组件层
- 转人工：图 `interrupt` 暂停 + checkpointer 落盘，人工处理完 `Command(resume=...)` 继续
- 超步：`forced_final_answer_hook` 基于已有信息强制收尾，不用官方件的硬 stop
  （硬 stop 会丢掉前序工具拿到的数据）

## 5. 评测：七维，工具真实性查库

`eval/agent_eval.py` 的核心是 `expect_effects`：

```json
{"table": "refunds", "delta": 1, "where": "user_id = 5"}
```

跑前 `SELECT count(*)`、跑后 `SELECT count(*)`，差值不符 → 判 `phantom_claim`（空口宣称）。

**为什么不读回答**：文本能编，行数不会。模型说"已提交退款"但 refunds 表没多一行，
这就是最危险的一类失败——从回答看不出来。

`delta: 0` 也是断言：越权题要求"退款单一条都不许多"，
光嘴上拒绝但工具照跑，同样判失败。

---

# 三、两个项目的同与不同

| 维度 | kb-platform | cs-agent |
|---|---|---|
| 核心风险 | **看到不该看的**（越权读） | **做了不该做的**（越权写） |
| 权限落点 | SQL WHERE，读前过滤 | 路由层 + 工具层双校验，写前拦截 |
| 一致性 | 无需（只读为主） | 行锁 + 部分唯一索引 + 幂等键 + SAVEPOINT |
| 检索 | 三路召回 + RRF + 重排门控 | 单路 + rerank |
| 评测重心 | 召回质量（graph_only_rate） | 副作用真实性（落库核对） |
| 图工程 | 有（PG 邻接表） | 无（语料不需要） |

**同一个教训在两个项目都验证过**：权限必须收在一处。
kb-platform 收在 `_permission_clause`，cs-agent 收在 API 层。
分开写两套，一定会有一次不一致，而那一次就是事故。

---

# 四、本次改动：实测结果

## 已完成并验证

| 项 | 实测 |
|---|---|
| 多跳语料 6 份 | 入库成功，38 个 chunk（133 → 171） |
| 增量建图 | 137 秒，图规模 298→**373 实体 / 369→451 关系**，日志可见跨文档实体合并 |
| GraphRAG 消融 | 15 题，`graph_only_rate = 0`（详见下节） |
| 权限 ORM 强制注入 | **对照实验通过**（见下） |
| 21 题回归 | recall@5 1.0 / 拒答 1.0 / 无误杀，零回归 |
| retry_search | 工具可用；弱证据场景判为无需重试（正确行为） |
| 路由置信度 + 澄清 | 三分支全对：order / clarify / knowledge |
| 意图准确率 | **0.875**（8 题），混淆矩阵 1 题 order 误判为 knowledge |
| 多轮追问 | 2 题全过（1.0） |
| cs-agent 七维 | effect_match **1.0**、忠实度 1.0、安全 1.0、工具真实性 1.0 |

## 权限强制注入的对照实验（最硬的证据）

故意显式传 `department=None`（模拟某个接口忘了写权限过滤），看强制注入能不能兜住：

| 场景 | 命中数 | 能看到技术部文档 33:0 |
|---|---|---|
| 不限制 | 11 | 能 |
| 限制人事部（注入开） | 8 | **看不到** |
| 限制技术部 | 11 | 能 |
| **限制人事部（注入关）** | **11** | **能看到 ← 漏写就会这样** |

最后一行是重点：关掉强制注入后，光靠"记得在查询里写 where"是守不住的。

## GraphRAG：换语料后依然是 0，但这次知道为什么

```
graph_off: recall@5=1.0  selected_recall=0.7778  mrr=0.9444
graph_on : recall@5=1.0  selected_recall=0.7778  mrr=0.9444   ← 完全相同
graph_linked_rate = 1.0    9 题全部链接到实体
graph_hits_total  = 65     图召回了 65 个 chunk
graph_only_total  = 0      但全部已被向量或 BM25 命中
```

**图通道是工作的**（链接率 1.0，召回 65 个 chunk），但它召回的每一个 chunk，
向量或 BM25 都已经拿到了。

根本原因不是"语料太短"，是**结构性的**：

1. 被实体连到的 chunk，正文里必然写着实体名（否则抽不出这个实体）
2. 实体名出现在正文里，BM25 就能按字面命中
3. 所以"图能到而 BM25 到不了"的 chunk，在字面匹配模型下几乎不存在

要打破这个约束，需要**同义桥接**：A 文档叫"验收单"、B 文档叫"到货检验单"，
人工知道是一回事，BM25 匹配不到，靠实体别名才能连过去。
但这要求人工维护别名表，或者接受消歧模型合并不了低相似度词。

**结论**：在当前语料和这套混合检索下，图工程没有净收益。这个 0 是真结论，不是实现没做好。

## 已验证的问题修复

1. **eval 库 schema 落后 5 张表**（缺 `coupons` 等）→ 8001 实例的所有写接口返回 500，
   表现为"转人工、开工单、写备注全部系统繁忙"。补表 + 重启实例后恢复。
2. **`SupervisorState` 缺 `route` 字段**：条件边读得到、`result.get("route")` 读不到，
   评测里路由结果永远是 None。补进 TypedDict 后正常。
3. **reset 脚本按时间窗删退款**：历史跑测留下的退款单早于时间窗，删不掉 →
   下一轮"订单已在退款审核中"，落库断言假失败。改成按订单号删。
