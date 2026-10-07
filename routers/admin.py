# 部门归属是授权数据，只能 admin 写：注册接口不接受部门，可见性由检索 SQL 实时拼 WHERE
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.departments import ALLOWED_DEPARTMENTS
from config.db_conf import get_db
from models.user import User
from routers.auth import get_current_user
from services.permissions import is_admin

router = APIRouter(prefix="/admin", tags=["管理"])

logger = logging.getLogger(__name__)


class DepartmentAssignIn(BaseModel):
    department: str


@router.patch("/users/{user_id}/department")
async def assign_department(
    user_id: int,
    payload: DepartmentAssignIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 仅 admin；部门名必须在白名单内，非法 400 不静默改成未分配
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="无权分配部门")
    if payload.department not in ALLOWED_DEPARTMENTS:
        raise HTTPException(
            status_code=400,
            detail="非法部门，合法取值：" + "、".join(ALLOWED_DEPARTMENTS),
        )
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    old_department = user.department
    user.department = payload.department
    # commit 后再返回：客户端拿到 200 立即能读到新部门
    await db.commit()
    logger.info(
        "admin.department_assigned operator_id=%s operator=%s target_id=%s "
        "target=%s from=%s to=%s",
        current_user.id, current_user.username, user.id, user.username,
        old_department or "-", payload.department,
    )
    return {"id": user.id, "username": user.username, "department": user.department, "role": user.role}
