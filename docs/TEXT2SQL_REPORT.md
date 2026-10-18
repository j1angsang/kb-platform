# 受限 Text2SQL 改造报告

## 1. 为什么做

“技术部有多少人”“各部门人数排名”这类问题用向量检索答不准：知识库里没有实时聚合结果。直接把 SQL 交给模型执行又不安全：

- 模型可能写 `DELETE` / `DROP` / 多语句；
- 可能查 `users` 拿走密码哈希；
- 员工可能借 SQL 跨部门查数据；
- 大表或笛卡尔积可能把数据库拖死。

目标不是做一个通用 SQL 执行器，而是做一个**只读、单表、带部门范围、可评测**的员工统计工具。

## 2. 方案

```text
自然语言
  -> LLM 生成单条 SELECT
  -> sqlglot AST 校验
  -> 注入部门范围 CTE
  -> 只读事务 + statement_timeout + LIMIT
  -> 返回列和行
```

### 2.1 只读视图

`analytics.v_employee_directory` 只暴露四列：

| 列 | 说明 |
|---|---|
| department | 部门 |
| position | 岗位，可能为空 |
| role | admin / leader / employee |
| joined_at | 账号加入日期 |

视图基于 `users`，不包含 username、密码哈希、token_version 等敏感字段。姓名/直属上级也不暴露。

### 2.2 生成

LLM 只负责“问题 -> SQL”，不接触数据库连接，也不知道当前用户的部门。Prompt 固定：

- 只能查 `v_employee_directory`；
- 不写 WITH / JOIN / 子查询 / UNION / 注释 / 分号；
- 只允许四列和 count/sum/avg/min/max 等聚合；
- 不写 LIMIT，服务端统一加；
- 只返回 SQL，不解释。

### 2.3 AST 校验

用 sqlglot 解析 PostgreSQL 方言，拒绝：

- 非单条 SELECT；
- 多表、JOIN、子查询、WITH、UNION/INTERSECT/EXCEPT；
- 非白名单表/列/函数；
- `SELECT INTO`、`FOR UPDATE`、`FETCH`、`OFFSET`；
- 注释、分号、多语句；
- `LIMIT > 50`、非整数 LIMIT。

合法查询自动补 `LIMIT 50`。

### 2.4 部门范围

服务端生成 CTE：

```sql
WITH v_employee_directory AS (
  SELECT department, position, role, joined_at
  FROM analytics.v_employee_directory
  WHERE department = :scope_department
)
<模型生成的 SELECT>
```

- 员工：`scope_department` 是本人部门；
- 领导/管理员：`allow_all=True`，CTE 不加部门条件；
- 部门条件来自服务端用户状态，不来自 LLM 参数。

即使模型写了 `WHERE department='销售部'`，员工也只能在本人部门的 CTE 里查，结果为空，不会越权。

### 2.5 只读执行

- 默认：主库连接 + `SET TRANSACTION READ ONLY`；
- 可选：`TEXT2SQL_DATABASE_URL` 指向只读角色，角色默认 `default_transaction_read_only=on`；
- `SET LOCAL statement_timeout = 3000`；
- 结果最多 50 行，日期/Decimal 转 JSON 安全类型。

### 2.6 接入方式

- `POST /analytics/query`：JWT 鉴权，直接返回 SQL/列/行；
- Agent 工具 `query_employee_stats(question)`：身份从 `InjectedState` 注入，工具内部生成+校验+执行；
- 数据源卡片标记为“员工统计”，不伪造知识库 `[N]` 引用。

## 3. 评测

`eval/text2sql_set.jsonl`：20 道员工统计问题，覆盖部门人数、总人数、角色分布、岗位列表、入职日期分布。当前种子数据下期望结果固定；`eval/run_text2sql.py` 调 LLM 生成 SQL，走完整校验/执行后比对结果。

本次结果：**20/20 通过**。

安全验证（直接调用校验器，均被拒绝）：

- DELETE / DROP / 多语句 / 注释；
- 查 `users` / `password`；
- `pg_sleep` / `current_setting`；
- JOIN / 子查询 / WITH / UNION；
- `SELECT INTO` / `FOR UPDATE` / `FETCH`；
- `LIMIT 0/100/1+1`；
- schema 限定表名。

接口验证：

- 管理员问“各部门人数排名”：5 行，技术部 8、人事部 5、销售部 2、财务部 1、行政部 1；
- 人事部员工问“技术部有多少人”：0；
- 人事部员工问“人事部有多少人”：5；
- Agent 问“技术部有多少人”：回答 8，并展示“员工统计”数据源卡片。

只读角色验证：

- `kb_ro_test` 只能 SELECT 视图，查询返回 17 行；
- 同角色 INSERT `users` 被 PostgreSQL 拒绝：`cannot execute INSERT in a read-only transaction`；
- 验证后已删除测试角色。

## 4. 边界

- 只覆盖员工目录统计，不替代知识检索；
- 不开放写操作、跨表 JOIN、自由 SQL；
- 领导/管理员可跨部门，员工只看本部门；
- `position` 允许为空，岗位统计会包含空值；
- 20 题结果依赖当前种子数据，换数据集需要重跑并更新期望；
- 模型生成有随机性，评测是结果集比对，不是 SQL 字符串比对；
- 当前没有做查询缓存和更细的列级权限，属于后续工作。

## 5. 简历写法

> 参与员工统计的受限 Text2SQL：自然语言生成 SQL 后做 AST 白名单校验，服务端注入部门范围，只读事务执行并限制 50 行/3 秒；20 题评测集全部通过，越权、写操作和危险函数在 SQL 层被拒绝。

## 6. 面试追问

**Q1：为什么不让模型直接执行 SQL？**
模型输出不可信。必须先 AST 校验，再用只读事务/角色兜底；部门范围由服务端 CTE 注入，不能相信模型写的 WHERE。

**Q2：为什么用 CTE 而不是在结果上过滤？**
结果上过滤可能已经把数据读出来，也可能把聚合算错。CTE 在数据源处限制可见行，模型后续怎么聚合都越不过去。

**Q3：sqlglot 校验能防住所有注入吗？**
不能只靠它。还有单表/列白名单、禁多语句/注释、参数绑定、只读事务、statement_timeout 和 LIMIT。AST 是第一道，数据库权限是最后一道。

**Q4：为什么只读角色还要 READ ONLY 事务？**
双保险。角色配置可能被改，应用层事务再限制一次；即使拿到连接，也写不进去。

**Q5：20 题 20/20 能说明什么？**
说明当前种子数据下生成、校验、执行、结果比对闭环可用；不说明能覆盖任意企业 schema。换数据/问题要重跑，且要补负例。

**Q6：为什么不用向量检索回答人数？**
向量检索返回文本片段，不做聚合；人数/排名必须走结构化查询。两类问题分流，而不是让一个通道硬答。
