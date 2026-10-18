# 受限 Text2SQL 代码讲解

## 1. 文件地图

| 文件 | 作用 |
|---|---|
| `migrations/versions/d4e5f6a7b8c0_text2sql_employee_view.py` | 建 `analytics.v_employee_directory` 只读视图 |
| `services/text2sql.py` | LLM 生成、AST 校验、部门 CTE、只读执行 |
| `tools/text2sql_tools.py` | Agent 工具 `query_employee_stats` |
| `routers/analytics.py` | `POST /analytics/query` |
| `eval/text2sql_set.jsonl` / `eval/run_text2sql.py` | 20 题评测集与脚本 |
| `scripts/create_text2sql_readonly.sql` | 可选只读角色 |

## 2. 视图

```sql
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE OR REPLACE VIEW analytics.v_employee_directory AS
SELECT department, position, role, created_at::date AS joined_at
FROM users
WHERE department IS NOT NULL;
```

只选四列，不把 username、hashed_password、token_version 暴露给分析层。`position` 可为空，统计时保留空值分组。

## 3. 生成 SQL

`generate_sql(question)`：

```python
response = await ChatOpenAI(...).ainvoke([
    ("system", SYSTEM_PROMPT),
    ("human", f"问题：{question}\nSQL："),
])
sql = _extract_sql(response.content)
```

Prompt 明确四列、role 枚举（admin/leader/employee）、允许的聚合、禁止的结构；模型只看到 schema，不接触连接，也不知道当前用户部门。

`_extract_sql` 去掉 ```sql fence 和尾部 fence；如果模型返回解释，后续 parse 会失败，工具返回“SQL 无法解析”，不会执行。

## 4. AST 校验

`validate_sql(sql, scope)`：

```python
statements = sqlglot.parse(raw, read="postgres")
if len(statements) != 1 or not isinstance(statements[0], exp.Select):
    raise Text2SQLError("unsafe_sql", "只允许单条 SELECT")
```

遍历 AST：

- `FORBIDDEN_NODES`：WITH、Subquery、Join、Union、Intersect、Except、Window、Lateral、Unnest、Offset、Fetch、Into、Lock；
- 只允许一张 `v_employee_directory`，拒绝 schema 限定和别的表；
- 列白名单 `department/position/role/joined_at`，别名也允许（ORDER BY 别名）；
- `Anonymous` 函数只允许 `date_trunc/extract/coalesce/round/lower/upper`；
- 其它结构化函数（count/sum/avg/min/max/cast 等）由 SQL AST 表达，不经过字符串拼接。

LIMIT：

```python
limit = tree.args.get("limit")
if limit is not None:
    # 必须是 1..TEXT2SQL_MAX_ROWS 的整数
else:
    tree = tree.limit(TEXT2SQL_MAX_ROWS)
```

生成最终 SQL：

```python
scope_sql = "" if scope.allow_all else " WHERE department = :scope_department"
cte = ("WITH v_employee_directory AS ("
       "SELECT department, position, role, joined_at "
       f"FROM analytics.v_employee_directory{scope_sql}) ")
safe = SafeQuery(sql=cte + tree.sql(dialect="postgres"), params=...)
```

CTE 名字与视图同名，模型生成的 `FROM v_employee_directory` 会命中 CTE，而不是基表。员工范围在 CTE 里过滤，模型写什么 `WHERE department=...` 都越不过去。

## 5. 只读执行

```python
async with factory() as db:
    await db.execute(text("SET TRANSACTION READ ONLY"))
    await db.execute(text("SET LOCAL statement_timeout = 3000"))
    result = await db.execute(text(query.sql), query.params)
    rows = [...]
    await db.rollback()
```

- 默认 `AsyncSessionLocal`；
- 配了 `TEXT2SQL_DATABASE_URL` 时用独立只读角色 engine；
- 只读事务是应用层兜底，角色 `default_transaction_read_only=on` 是数据库层兜底；
- 日期/Decimal 做 JSON 安全转换；
- 结果最多 50 行。

## 6. API 和工具

### API

`POST /analytics/query`：

```python
result = await answer_question(
    question=payload.question,
    department=current_user.department or "未分配",
    allow_all=can_view_all_departments(current_user),
)
```

`can_view_all_departments` 为真时是领导/管理员，CTE 不加部门条件；否则员工只看本部门。

错误映射：

- 问题/SQL 非法 -> 422；
- 生成/执行失败 -> 502；
- 超时 -> 504。

### Agent 工具

`tools/text2sql_tools.py`：

```python
@tool(parse_docstring=True)
async def query_employee_stats(question: str, state: Annotated[dict, InjectedState] = None):
    ...
```

- `question` 来自 LLM；
- 部门和 `allow_all` 来自 `InjectedState`，LLM 不可见；
- 工具内部走同一套生成/校验/执行；
- 失败返回可读消息，不把异常堆栈丢给模型。

`agents/graph.py` 里注册工具、补系统提示，并把数据源卡片标为“员工统计”。

## 7. 评测

`eval/text2sql_set.jsonl` 每行：

```json
{"id":"t2s-01","question":"技术部有多少人？","expected":[["8"]],"ordered":false}
```

`eval/run_text2sql.py`：

1. 读题；
2. `answer_question(..., allow_all=True)`；
3. 按列顺序把行转字符串；
4. `ordered=false` 时排序后比较（并列名次不要求顺序）；
5. 输出 PASS/FAIL 和总数。

本次 20/20 通过。

## 8. 只读角色

```bash
psql -U postgres -d file_storage_db \
  -v ro_password='<readonly-password>' \
  -f scripts/create_text2sql_readonly.sql
```

然后：

```dotenv
TEXT2SQL_DATABASE_URL=postgresql+asyncpg://kb_readonly:<password>@127.0.0.1:5432/file_storage_db
```

角色只授予 `analytics` schema usage 和视图 SELECT，并设置默认只读事务。

## 9. 复现

```bash
# 迁移
alembic upgrade head

# 接口
POST /analytics/query {"question": "各部门人数排名"}

# 评测
.venv\Scripts\python.exe eval\run_text2sql.py
```

## 10. 已知边界

- 只支持员工目录单表统计；
- 不支持 JOIN/子查询/窗口函数；
- 20 题依赖当前种子数据；
- 未做查询缓存；
- 模型生成有随机性，评测看结果集，不看 SQL 字符串。
