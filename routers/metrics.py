# /metrics 含内部数据，仅 admin 可读
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from config.db_conf import get_db
from models.user import User
from routers.auth import get_current_user
from services import metrics
from services.permissions import is_admin

router = APIRouter(tags=["可观测性"])
logger = logging.getLogger(__name__)


@router.get("/metrics")
async def prometheus_metrics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="无权读取指标")
    body, content_type = await metrics.render_metrics_async()
    return Response(content=body, media_type=content_type)
