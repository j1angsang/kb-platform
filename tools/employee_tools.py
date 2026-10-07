"""精确数据走 SQL 等值查询，不走向量检索"""
from typing import Annotated

from langchain.tools import InjectedState, tool
from sqlalchemy import select

from config.db_conf import AsyncSessionLocal
from models import Employee


@tool(parse_docstring=True)
async def query_personnel(
    name: str | None = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """精确查询员工信息(按姓名或本部门全量)。name 不填则查本部门全部。"""
    user_department = state["user_department"]
    allow_all = state.get("allow_all_departments", False)
    # name 来自 LLM；department 绝不来自 LLM，由 InjectedState 注入并在工具内决定权限
    async with AsyncSessionLocal() as db:
        stmt = select(Employee)
        if not allow_all:
            stmt = stmt.where(Employee.department == user_department)
        if name:
            stmt = stmt.where(Employee.name == name)
        rows = (await db.execute(stmt)).scalars().all()

    if not rows:
        if name:
            return "未找到该员工。请确认姓名准确性,或咨询人事部获取最新名单。"
        return "本部门暂无员工信息。"
    if name:
        # 等值命中，重名允许都列上，不隐瞒
        e = rows[0]
        return (f"{e.name}（{e.department}）：{e.position},"
                f"入职 {e.hire_date},当前状态 {e.status},直属上级 {e.manager}。")
    body = "\n".join(
        f"- {e.name}（{e.department}）：{e.position}(入职 {e.hire_date},状态 {e.status})" for e in rows
    )
    return f"本部门共 {len(rows)} 人:\n{body}"


TOOLS = [query_personnel]
