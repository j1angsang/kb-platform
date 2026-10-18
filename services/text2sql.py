"""受限 Text2SQL：LLM 生成 SQL，AST 校验后只读执行。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from sqlalchemy import text
from sqlglot import exp
from sqlglot.errors import ParseError

from config.db_conf import AsyncSessionLocal
from config.llm import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

VIEW = "v_employee_directory"
ALLOWED_COLUMNS = {"department", "position", "role", "joined_at"}
ALLOWED_ANONYMOUS = {"date_trunc", "extract", "coalesce", "round", "lower", "upper"}
FORBIDDEN_NODES = (
    exp.With, exp.Subquery, exp.Join, exp.Union, exp.Intersect,
    exp.Except, exp.Window, exp.Lateral, exp.Unnest, exp.Offset,
    exp.Fetch, exp.Into, exp.Lock,
)

TEXT2SQL_MAX_ROWS = max(1, min(int(os.getenv("TEXT2SQL_MAX_ROWS", "50")), 200))
TEXT2SQL_TIMEOUT_MS = max(500, min(int(os.getenv("TEXT2SQL_TIMEOUT_MS", "3000")), 10000))
TEXT2SQL_DATABASE_URL = (os.getenv("TEXT2SQL_DATABASE_URL") or "").strip()

SYSTEM_PROMPT = """你是 PostgreSQL 员工统计查询生成器。
只输出一条 SELECT，不要解释，不要 markdown。

可用表：
v_employee_directory(department text, position text, role text, joined_at date)

role 取值固定为 admin / leader / employee：
管理员=admin，领导=leader，普通员工=employee。

规则：
1. 只能查询 v_employee_directory，不能查其他表。
2. 不要写 WITH、JOIN、子查询、UNION、注释或分号。
3. 只能使用上面的列；聚合可用 count/sum/avg/min/max。
4. 日期可用 date_trunc('year', joined_at) 或 extract(year from joined_at)。
5. 不要写 LIMIT，服务端会加；结果按需 ORDER BY。
6. 只返回 SQL。"""

_readonly_factory = None


class Text2SQLError(Exception):
    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


@dataclass(frozen=True)
class Scope:
    department: str
    allow_all: bool


@dataclass(frozen=True)
class SafeQuery:
    sql: str
    params: dict[str, Any]


@dataclass
class Text2SQLResult:
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


def _get_llm():
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        temperature=0,
        timeout=15,
        max_retries=0,
        max_tokens=300,
    )


def _extract_sql(content: Any) -> str:
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    value = str(content or "").strip()
    value = re.sub(r"^```(?:sql)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


async def generate_sql(question: str) -> str:
    response = await _get_llm().ainvoke(
        [
            ("system", SYSTEM_PROMPT),
            ("human", f"问题：{question}\nSQL："),
        ]
    )
    sql = _extract_sql(response.content)
    if not sql:
        raise Text2SQLError("generation_failed", "没有生成 SQL，请换一种问法")
    return sql


def _clean_sql(sql: str) -> str:
    raw = (sql or "").strip()
    if len(raw) > 4000:
        raise Text2SQLError("invalid_sql", "SQL 过长")
    if "--" in raw or "/*" in raw or ";" in raw.rstrip(";"):
        raise Text2SQLError("unsafe_sql", "SQL 含注释或多条语句")
    return raw.rstrip(";").strip()


def validate_sql(sql: str, scope: Scope) -> SafeQuery:
    raw = _clean_sql(sql)
    try:
        statements = sqlglot.parse(raw, read="postgres")
    except ParseError as exc:
        raise Text2SQLError("invalid_sql", "SQL 无法解析") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise Text2SQLError("unsafe_sql", "只允许单条 SELECT")

    tree = statements[0]
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1:
        raise Text2SQLError("unsafe_sql", "只能查询一张员工目录视图")
    aliases = {alias.alias.lower() for alias in tree.find_all(exp.Alias) if alias.alias}

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise Text2SQLError("unsafe_sql", "不允许的 SQL 结构")
        if isinstance(node, exp.Table):
            if node.name.lower() != VIEW or node.db or node.catalog:
                raise Text2SQLError("unsafe_sql", "只能查询员工目录视图")
        if isinstance(node, exp.Column):
            name = node.name.lower()
            if name != "*" and name not in ALLOWED_COLUMNS | aliases:
                raise Text2SQLError("unsafe_sql", f"不允许的列：{node.name}")
        if isinstance(node, exp.Func) and isinstance(node, exp.Anonymous):
            if node.name.lower() not in ALLOWED_ANONYMOUS:
                raise Text2SQLError("unsafe_sql", f"不允许的函数：{node.name}")

    limit = tree.args.get("limit")
    if limit is not None:
        expression = limit.expression
        if not isinstance(expression, exp.Literal) or not expression.is_int:
            raise Text2SQLError("unsafe_sql", "LIMIT 必须是整数")
        if not 1 <= int(expression.this) <= TEXT2SQL_MAX_ROWS:
            raise Text2SQLError("unsafe_sql", f"LIMIT 必须在 1~{TEXT2SQL_MAX_ROWS}")
    else:
        tree = tree.limit(TEXT2SQL_MAX_ROWS)

    scope_sql = "" if scope.allow_all else " WHERE department = :scope_department"
    cte = (
        "WITH v_employee_directory AS ("
        "SELECT department, position, role, joined_at "
        f"FROM analytics.v_employee_directory{scope_sql}) "
    )
    params = {} if scope.allow_all else {"scope_department": scope.department}
    return SafeQuery(sql=cte + tree.sql(dialect="postgres"), params=params)


def _session_factory():
    global _readonly_factory
    if not TEXT2SQL_DATABASE_URL:
        return AsyncSessionLocal
    if _readonly_factory is None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(TEXT2SQL_DATABASE_URL, pool_pre_ping=True)
        _readonly_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _readonly_factory


def _json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


async def execute_sql(query: SafeQuery) -> tuple[list[str], list[dict[str, Any]]]:
    factory = _session_factory()
    try:
        async with factory() as db:
            try:
                await db.execute(text("SET TRANSACTION READ ONLY"))
                await db.execute(text(f"SET LOCAL statement_timeout = {TEXT2SQL_TIMEOUT_MS}"))
                result = await db.execute(text(query.sql), query.params)
                columns = list(result.keys())
                rows = [
                    {key: _json_safe(value) for key, value in row.items()}
                    for row in result.mappings().all()
                ]
                return columns, rows
            finally:
                await db.rollback()
    except asyncio.TimeoutError as exc:
        raise Text2SQLError("timeout", "查询超时，请缩小范围") from exc
    except Text2SQLError:
        raise
    except Exception as exc:
        logger.exception("text2sql execute failed")
        raise Text2SQLError("execution_failed", "查询执行失败") from exc


async def answer_question(question: str, department: str, allow_all: bool) -> Text2SQLResult:
    question = (question or "").strip()
    if not question or len(question) > 500:
        raise Text2SQLError("invalid_question", "问题为空或过长")
    scope = Scope(department=department or "未分配", allow_all=allow_all)
    sql = await generate_sql(question)
    safe = validate_sql(sql, scope)
    columns, rows = await execute_sql(safe)
    return Text2SQLResult(sql=safe.sql, columns=columns, rows=rows, row_count=len(rows))


def format_result(result: Text2SQLResult) -> str:
    if not result.rows:
        return "没有符合条件的员工数据（也可能超出你的部门权限）。"
    headers = result.columns
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in result.rows:
        values = [str(row.get(col, "")).replace("|", "\\|") for col in headers]
        lines.append("| " + " | ".join(values) + " |")
    return f"共 {result.row_count} 行：\n" + "\n".join(lines)
