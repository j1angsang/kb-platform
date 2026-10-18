import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from config.db_conf import AsyncSessionLocal
from rag.embeddings import EMBEDDING_API_KEY
from rag.store import store
from services.object_storage import object_storage

router = APIRouter(tags=["健康检查"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    # 本端点无需认证，异常原文含表名/连接串片段：对外只返回枚举状态，完整异常只进日志
    checks: dict[str, str] = {}

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        logger.exception("health.ready db check failed")
        checks["db"] = "error: db_unavailable"

    try:
        await store.count()
        checks["vector"] = "ok"
    except Exception:
        logger.exception("health.ready vector check failed")
        checks["vector"] = "error: vector_unavailable"

    # 连接池水位，取自 SQLAlchemy 自带计数器
    try:
        from config.db_conf import engine as _engine
        checks["db_pool"] = f"ok(inuse={_engine.pool.checkedout()})"
    except Exception:
        logger.exception("health.ready db_pool check failed")
        checks["db_pool"] = "error: db_pool_unavailable"

    # 卡住的入库任务：indexing 超 10 分钟，不代表服务不可用，不参与 503 判定
    try:
        async with AsyncSessionLocal() as db:
            stuck = (await db.execute(text(
                "SELECT count(*) FROM knowledge_docs "
                "WHERE index_status='indexing' AND created_at < now() - interval '10 minutes'"
            ))).scalar()
        checks["index_stuck"] = f"ok(count={stuck or 0})"
    except Exception:
        logger.exception("health.ready index_stuck check failed")
        checks["index_stuck"] = "ok(count=unknown)"

    if EMBEDDING_API_KEY and EMBEDDING_API_KEY.strip():
        checks["embedding_key"] = "ok"
    else:
        checks["embedding_key"] = "error: embedding_key_missing"

    try:
        storage_status = await asyncio.to_thread(object_storage.healthcheck)
        checks["object_storage"] = (
            storage_status if storage_status.startswith("ok") else f"ok({storage_status})"
        )
    except Exception:
        logger.exception("health.ready object_storage check failed")
        checks["object_storage"] = "error: object_storage_unavailable"

    healthy = all(value.startswith("ok") for value in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "unhealthy", "checks": checks},
    )
