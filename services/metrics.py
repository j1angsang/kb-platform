# 进程重启计数器归零，单机场景下可接受，不做持久化
import logging

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logger = logging.getLogger(__name__)

# 已存在的埋点，暴露成可聚合指标
rate_limit_rejected_total = Counter(
    "rate_limit_rejected_total", "被限流拒绝的请求总数",
)
embedding_cache_hit_total = Counter(
    "embedding_cache_hit_total", "查询向量 Redis 缓存命中次数",
)
embedding_cache_miss_total = Counter(
    "embedding_cache_miss_total", "查询向量 Redis 缓存未命中次数",
)
retrieval_duration_ms = Histogram(
    "retrieval_duration_ms", "知识检索耗时，单位毫秒",
    buckets=(10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
)
bg_loop_error_total = Counter(
    "bg_loop_error_total", "进程内后台循环异常退出又被 try 住的总次数",
    ["loop"],  # loop=upload_cleanup / index_jobs
)
# 随抓取实时查库的 Gauge，是数据库计数而非累加器
db_pool_inuse = Gauge("db_pool_inuse", "SQLAlchemy 连接池当前借出连接数")
knowledge_docs_total = Gauge(
    "knowledge_docs_total", "知识文档数，按审核状态", ["status"],
)
knowledge_index_failed_total = Gauge(
    "knowledge_index_failed_total", "index_status=failed 的文档数",
)
knowledge_index_stuck = Gauge(
    "knowledge_index_stuck", "停在 indexing 超过 10 分钟的文档数",
)


async def render_metrics_async() -> tuple[bytes, str]:
    from sqlalchemy import text as sa_text

    from config.db_conf import AsyncSessionLocal, engine

    try:
        db_pool_inuse.set(engine.pool.checkedout())
    except Exception:
        logger.exception("metrics.db_pool_inuse_failed")
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(sa_text(
                "SELECT status, count(*) FROM knowledge_docs GROUP BY status"
            ))).all()
            for status, count in rows:
                knowledge_docs_total.labels(status=status).set(count)
            knowledge_index_failed_total.set((await db.execute(sa_text(
                "SELECT count(*) FROM knowledge_docs WHERE index_status='failed'"
            ))).scalar() or 0)
            knowledge_index_stuck.set((await db.execute(sa_text(
                "SELECT count(*) FROM knowledge_docs "
                "WHERE index_status='indexing' AND created_at < now() - interval '10 minutes'"
            ))).scalar() or 0)
    except Exception:
        logger.exception("metrics.refresh_doc_gauges_failed")
    return generate_latest(), CONTENT_TYPE_LATEST
