import asyncio
import hashlib
import json
import logging
import os
import threading
import time

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = (os.getenv("EMBEDDING_MODEL") or "").strip()
if not EMBEDDING_MODEL:
    raise RuntimeError("EMBEDDING_MODEL 未配置:请检查 .env")

DASHSCOPE_BATCH_LIMIT = 20

EMBEDDING_CACHE_ENABLED = (os.getenv("EMBEDDING_CACHE_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on")
EMBEDDING_CACHE_TTL = int(os.getenv("EMBEDDING_CACHE_TTL", "604800"))  # 默认 7 天
_EMB_CACHE_PREFIX = "kb:emb:query:"
_EMB_CACHE_IO_TIMEOUT = 2.0
_EMB_REDIS_DEADLINE = 0.5
_EMB_CACHE_COOLDOWN_SECONDS = 30.0


def _call_dashscope(items:list[dict]) -> list[list[float]]:
    import dashscope  # 延迟导入：不碰向量库就不加载 SDK

    vectors: list = []
    for start in range(0, len(items), DASHSCOPE_BATCH_LIMIT):
        batch = items[start : start + DASHSCOPE_BATCH_LIMIT]
        resp = dashscope.MultiModalEmbedding.call(
            model=EMBEDDING_MODEL,
            input = batch,
            api_key=EMBEDDING_API_KEY,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"embedding 调用失败: {resp.code} {resp.message}")
        for item in resp.output["embeddings"]:
            vectors.append(item["embedding"])
    return vectors

def embed_texts(texts: list[str]) -> list[list[float]]:
    return _call_dashscope([{"text": t} for t in texts])


class _BackgroundLoop:

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="kb-emb-cache", daemon=True
        )
        self._thread.start()

    def run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)


_emb_runner: _BackgroundLoop | None = None
_emb_runner_lock = threading.Lock()
_emb_client = None  # redis.asyncio.Redis，绑在后台 loop 上，进程内复用
_emb_failed_until = 0.0  # monotonic；Redis 不可用冷却期内直接调真实 API


def _get_runner() -> _BackgroundLoop:
    global _emb_runner
    if _emb_runner is None:
        with _emb_runner_lock:
            if _emb_runner is None:
                _emb_runner = _BackgroundLoop()
    return _emb_runner


async def _get_cache_client():
    global _emb_client
    import redis.asyncio as redis_async  # 延迟导入：缓存关闭时 Redis 不是依赖

    if _emb_client is None:
        _emb_client = redis_async.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            protocol=2,  # Redis 3.0 无 HELLO/RESP3，必须 RESP2
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            decode_responses=True,
        )
    return _emb_client


def _embedding_cache_key(query: str) -> str:
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()
    return f"{_EMB_CACHE_PREFIX}{EMBEDDING_MODEL}:{digest}"


async def _cache_get(key: str) -> list[float] | None:
    client = await _get_cache_client()
    raw = await asyncio.wait_for(client.get(key), timeout=_EMB_REDIS_DEADLINE)
    if raw is None:
        return None
    try:
        vector = json.loads(raw)
    except Exception:
        return None  # JSON 损坏 = miss，宁可重调 API
    return vector if isinstance(vector, list) else None


async def _cache_set(key: str, vector: list[float]) -> None:
    client = await _get_cache_client()
    await asyncio.wait_for(
        client.set(key, json.dumps(vector), ex=EMBEDDING_CACHE_TTL),
        timeout=_EMB_REDIS_DEADLINE,
    )


def _cached_query_vector(query: str) -> list[float]:
    global _emb_failed_until
    key = _embedding_cache_key(query)
    if time.monotonic() < _emb_failed_until:
        return _call_dashscope([{"text": query}])[0]
    runner = _get_runner()
    try:
        cached = runner.run(_cache_get(key), timeout=_EMB_CACHE_IO_TIMEOUT)
    except Exception as exc:
        _emb_failed_until = time.monotonic() + _EMB_CACHE_COOLDOWN_SECONDS
        _drop_cache_client()
        logger.warning(
            "emb.cache get_failed model=%s error=%s action=real_api cooldown_seconds=%d",
            EMBEDDING_MODEL, exc, _EMB_CACHE_COOLDOWN_SECONDS,
        )
        return _call_dashscope([{"text": query}])[0]

    if cached is not None:
        from services.metrics import embedding_cache_hit_total
        embedding_cache_hit_total.inc()
        logger.info(
            "emb.cache hit model=%s len=%d", EMBEDDING_MODEL, len(cached)
        )
        return cached

    logger.info("emb.cache query model=%s query_sha1=%s action=lookup", EMBEDDING_MODEL, key.rsplit(":", 1)[-1])
    from services.metrics import embedding_cache_miss_total
    embedding_cache_miss_total.inc()
    vector = _call_dashscope([{"text": query}])[0]
    logger.info(
        "emb.cache miss model=%s len=%d query_sha1=%s",
        EMBEDDING_MODEL, len(vector), key.rsplit(":", 1)[-1],
    )
    try:
        runner.run(_cache_set(key, vector), timeout=_EMB_CACHE_IO_TIMEOUT)
    except Exception as exc:
        logger.warning(
            "emb.cache set_failed model=%s error=%s action=ignore", EMBEDDING_MODEL, exc
        )
    return vector


def embed_query(query: str) -> list[float]:
    if not EMBEDDING_CACHE_ENABLED:
        return _call_dashscope([{"text": query}])[0]
    return _cached_query_vector(query)

def embed_image(image_path: str) -> list[float]:
    return _call_dashscope([{"image": image_path}])[0]


def _reset_cache_runtime_for_tests() -> None:
    global _emb_runner, _emb_client, _emb_failed_until
    with _emb_runner_lock:
        if _emb_runner is not None:
            try:
                asyncio.run_coroutine_threadsafe(_close_cache_client(), _emb_runner._loop).result(2.0)
            except Exception:
                pass
        _emb_client = None
        _emb_runner = None
        _emb_failed_until = 0.0


async def _close_cache_client() -> None:
    global _emb_client
    if _emb_client is not None:
        try:
            await _emb_client.aclose()
        except Exception:
            pass
    _emb_client = None


def _drop_cache_client() -> None:
    global _emb_client
    client, _emb_client = _emb_client, None
    if client is not None and _emb_runner is not None:
        try:
            asyncio.run_coroutine_threadsafe(_close_cache_client_quietly(client), _emb_runner._loop).result(1.0)
        except Exception:
            pass


async def _close_cache_client_quietly(client) -> None:
    try:
        await client.aclose()
    except Exception:
        pass
