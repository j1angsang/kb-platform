"""Knowledge Base Platform — FastAPI 入口：文件存储 + 文档审核流 + 向量检索。

启动: start.bat 或 uvicorn main:app --reload --port 8005
"""
import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from config.logging_conf import RequestLoggingMiddleware, setup_logging

# 日志基建必须早于 config.db_conf 和所有 router import
setup_logging()

from config.settings import validate_settings
from config.security_headers import SecurityHeadersMiddleware
from services.memory import close_memory_runtime, init_memory_runtime
from services.object_storage import object_storage

validate_settings()

from routers.chat import router as chat_router
from routers.knowledge import router as knowledge_router
from config.db_conf import AsyncSessionLocal, engine
from crud import knowledge as knowledge_crud
from services.storage import STORAGE_DIR
from routers.auth import router as auth_router
from routers.admin import router as admin_router
from routers.files import router as files_router
from routers.files import upload_cleanup_loop
from routers.health import router as health_router
from routers.metrics import router as metrics_router
from rag.indexer import index_document
from rag.store import store

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Knowledge Base Platform",
    # 未认证可读全部接口形状 = 给攻击者发地图，默认不暴露 /docs
    docs_url="/docs" if os.getenv("DOCS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on") else None,
    redoc_url=None,
    openapi_url="/openapi.json" if os.getenv("DOCS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on") else None,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(files_router)
app.include_router(knowledge_router)
app.include_router(chat_router)
app.include_router(health_router)
app.include_router(metrics_router)


app.mount("/", StaticFiles(directory="static", html=True), name="static")


# 统一错误体 {"error": {code, message, request_id}}；detail 是旧前端兼容别名
@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    from config.logging_conf import get_request_id

    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": f"http_{exc.status_code}", "message": message,
                      "request_id": get_request_id()},
            "detail": message,  # 兼容别名
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    from config.logging_conf import get_request_id

    message = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
        for e in exc.errors()
    )[:500]
    return JSONResponse(
        status_code=422,
        content={
            "error": {"code": "validation_error", "message": message,
                      "request_id": get_request_id()},
            "detail": message,  # 兼容别名
        },
    )


async def repair_stuck_indexing_on_startup() -> None:
    """把上次进程留下的 indexing 标成 failed，让卡住变可重试"""
    async with engine.begin() as conn:
        result = await conn.execute(text(
            "UPDATE knowledge_docs SET index_status='failed', error_message='进程重启中断' "
            "WHERE index_status='indexing'"
        ))
    if result.rowcount:
        logger.warning("startup.indexing_repaired count=%d reason=leftover_from_restart", result.rowcount)


async def index_job_loop() -> None:
    interval = knowledge_crud.INDEX_JOB_POLL_SECONDS
    if interval <= 0:
        logger.info("index_job_loop disabled poll_seconds=%s", interval)
        return
    interval = max(2, interval)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await knowledge_crud.requeue_stuck_index_jobs(db)
                await db.commit()
                due_doc_ids = await knowledge_crud.list_due_index_job_doc_ids(db)
            for doc_id in due_doc_ids:
                try:
                    await index_document(doc_id)
                except Exception:
                    logger.exception("index_job_loop doc=%s 执行失败", doc_id)
        except Exception:
            # 循环异常不静默，计数并入 /metrics
            from services.metrics import bg_loop_error_total
            bg_loop_error_total.labels(loop="index_jobs").inc()
            logger.exception("index_job_loop_failed")
        await asyncio.sleep(interval)


@app.on_event("startup")
async def on_startup():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    if object_storage.enabled:
        for attempt in range(1, 11):
            try:
                await asyncio.to_thread(object_storage.healthcheck)
                logger.info("object storage ready backend=%s bucket=%s", object_storage.backend, object_storage.bucket)
                break
            except Exception as exc:
                if attempt == 10:
                    logger.error(
                        "object storage unavailable endpoint=%s bucket=%s error=%s；"
                        "服务继续启动，上传会失败，/health/ready 返回 503",
                        object_storage.backend, object_storage.bucket, exc,
                    )
                else:
                    await asyncio.sleep(2)
    # 先把上次进程留下的 indexing 处理成可重试，再起恢复循环
    await repair_stuck_indexing_on_startup()
    # checkpointer/Store 启动装配：幂等建 LangGraph 表，失败即退出
    await init_memory_runtime()
    # BM25 索引启动时全量重建一次，检索路径只读现成索引
    await store.warmup_bm25()
    # rerank 冷启动预热：不预热的话第一个碰检索的请求要多等 2.8s 或进 fail-safe 拒答
    from rag.rerank import RERANK_ENABLED as _rerank_on

    if _rerank_on:
        async def _rerank_warmup() -> None:
            from rag.rerank import warmup_rerank

            try:
                await warmup_rerank()
            except Exception:
                logger.exception("rerank_warmup_failed，不影响启动，下个请求按 fail-safe 语义走")

        app.state.rerank_warmup_task = asyncio.create_task(_rerank_warmup())
    # 后台清理孤儿分片，默认每 3600s 跑一次
    app.state.upload_cleanup_task = asyncio.create_task(upload_cleanup_loop())
    app.state.index_job_task = asyncio.create_task(index_job_loop())
    logger.info("Knowledge Base Platform 启动完成，端口 8005")


@app.on_event("shutdown")
async def on_shutdown():
    for attr in ("upload_cleanup_task", "index_job_task"):
        task = getattr(app.state, attr, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    # 关 checkpointer/Store 连接池
    await close_memory_runtime()
