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
        # 失败时绝不返回空串或半截结果：模型会在没有数据的情况下"顺手"给出一个数字。
        # 实测评测抓到过：工具超时返回失败，模型照样答"技术部共有 8 名员工"（真实是 10）。
        return (
            f"查询失败：{exc.public_message}。"
            "本轮没有拿到任何数据，请如实告知用户查询失败、建议稍后重试；"
            "禁止根据常识、上文或历史对话推测具体人数、比例或排名。"
        )
    return format_result(result)


TOOLS = [query_employee_stats]
