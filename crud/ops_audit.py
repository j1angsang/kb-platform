import logging

from sqlalchemy.ext.asyncio import AsyncSession

from models.ops_audit_log import OpsAuditLog

logger = logging.getLogger(__name__)


async def add_ops_log(db: AsyncSession, *, action: str, target_type: str,
                      actor_id: int | None, target_id: str | None = None,
                      detail: dict | None = None) -> OpsAuditLog:
    row = OpsAuditLog(
        action=action, target_type=target_type, actor_id=actor_id,
        target_id=str(target_id) if target_id is not None else None,
        detail=detail,
    )
    db.add(row)
    await db.flush()
    logger.info(
        "ops.audit action=%s target_type=%s target_id=%s actor_id=%s",
        action, target_type, target_id, actor_id,
    )
    return row
