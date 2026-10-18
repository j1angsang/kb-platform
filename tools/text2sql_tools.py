"""员工统计的受限 Text2SQL 工具。"""
from typing import Annotated

from langchain.tools import InjectedState, tool

from services.text2sql import Text2SQLError, answer_question, format_result


@tool(parse_docstring=True)
async def query_employee_stats(
    question: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """按自然语言查询员工统计（人数、岗位、角色、入职时间分布）。

    Args:
        question: 用中文描述要统计的内容，例如“技术部有多少人”“各部门人数排名”。
    """
    try:
        result = await answer_question(
            question=question,
            department=state["user_department"],
            allow_all=state.get("allow_all_departments", False),
        )
    except Text2SQLError as exc:
        return f"查询失败：{exc.public_message}"
    return format_result(result)


TOOLS = [query_employee_stats]
