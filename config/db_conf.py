# 全仓唯一构造连接串的地方
import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# psycopg async 不吃 Windows 默认 ProactorEventLoop，建循环前切 selector policy
if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    def selector_loop_factory(use_subprocess: bool = False):
        return asyncio.SelectorEventLoop

    try:
        # uvicorn 在 Windows 默认硬编码 ProactorEventLoop，要替掉否则连不上
        import uvicorn.loops.asyncio as _uvicorn_asyncio_loop

        _uvicorn_asyncio_loop.asyncio_loop_factory = selector_loop_factory
    except Exception:
        pass
else:
    def selector_loop_factory(use_subprocess: bool = False):
        return asyncio.SelectorEventLoop

# .env 必须早于任何读 os.getenv 的模块加载
load_dotenv()

DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
if not DB_PASSWORD:
    raise RuntimeError("DB_PASSWORD 未配置:请检查 .env")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("FILE_STORAGE_DB_NAME") or os.getenv("DB_NAME", "file_storage_db")

DATABASE_URL = URL.create(
    "postgresql+psycopg",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME,
)
DATABASE_URL_STR = DATABASE_URL.render_as_string(hide_password=False)
SYNC_DATABASE_URL = DATABASE_URL_STR

# LangGraph 直连要用纯 postgresql://，+psycopg:// 连不上
LANGGRAPH_DB_URL = URL.create(
    "postgresql",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME,
).render_as_string(hide_password=False)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


def create_sync_engine():
    return create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)
