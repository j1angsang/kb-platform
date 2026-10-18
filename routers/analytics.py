from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from models import User
from routers.auth import get_current_user
from services.permissions import can_view_all_departments
from services.text2sql import Text2SQLError, answer_question

router = APIRouter(prefix="/analytics", tags=["analytics"])


class Text2SQLIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class Text2SQLOut(BaseModel):
    sql: str
    columns: list[str]
    rows: list[dict]
    row_count: int


@router.post("/query", response_model=Text2SQLOut)
async def query_employee_stats(
    payload: Text2SQLIn,
    current_user: User = Depends(get_current_user),
):
    try:
        result = await answer_question(
            question=payload.question,
            department=current_user.department or "未分配",
            allow_all=can_view_all_departments(current_user),
        )
    except Text2SQLError as exc:
        status = 422 if exc.code in {"invalid_question", "invalid_sql", "unsafe_sql"} else 502
        if exc.code == "timeout":
            status = 504
        raise HTTPException(status_code=status, detail=exc.public_message) from exc
    return Text2SQLOut(
        sql=result.sql,
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
    )
