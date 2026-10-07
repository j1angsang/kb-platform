from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


class InMemoryRateLimiter:

    def __init__(self, max_requests: int, window_seconds: int):
        if max_requests <= 0:
            raise ValueError("max_requests must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._records: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._checks_since_purge = 0
        self._purge_every = 256

    async def check(self, key: str) -> int | None:
        now = time.monotonic()
        key = str(key)
        with self._lock:
            records = self._records.get(key)
            if records is None:
                records = deque()
                self._records[key] = records
            else:
                while records and now - records[0] >= self.window_seconds:
                    records.popleft()
                if not records:
                    del self._records[key]
                    records = deque()
                    self._records[key] = records

            if len(records) >= self.max_requests:
                retry_after = int(math.ceil(self.window_seconds - (now - records[0])))
                return max(1, retry_after)
            records.append(now)

            self._checks_since_purge += 1
            if self._checks_since_purge >= self._purge_every:
                self._purge_expired_locked(now)
                self._checks_since_purge = 0
            return None

    def purge_expired(self, now: float | None = None) -> int:
        with self._lock:
            return self._purge_expired_locked(now if now is not None else time.monotonic())

    def _purge_expired_locked(self, now: float) -> int:
        removed = 0
        for key in list(self._records):
            records = self._records[key]
            while records and now - records[0] >= self.window_seconds:
                records.popleft()
            if not records:
                del self._records[key]
                removed += 1
        return removed

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._records.clear()
            else:
                self._records.pop(str(key), None)


_REDIS_KEY_PREFIX = "kb:rl:"
_REDIS_COOLDOWN_SECONDS = 30.0
# redis-py 在 Windows 上偶发不守 socket_connect_timeout，wait_for 兜底
_REDIS_CHECK_DEADLINE_SECONDS = 0.5

# 时间由客户端传入：Redis 3.0 禁止"非确定性命令之后再写"，TIME→ZADD 报错
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local max_req = tonumber(ARGV[2])
local member = ARGV[3]
local now = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= max_req then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window
    if oldest[2] then
        retry_after = math.ceil(window - (now - tonumber(oldest[2])))
    end
    if retry_after < 1 then
        retry_after = 1
    end
    return {tostring(retry_after), tostring(count)}
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000)
return {'0', tostring(count + 1)}
"""


class RedisRateLimiter:

    def __init__(self, max_requests: int, window_seconds: int):
        if max_requests <= 0:
            raise ValueError("max_requests must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._host = os.getenv("REDIS_HOST", "127.0.0.1")
        self._port = int(os.getenv("REDIS_PORT", "6379"))
        self._db = int(os.getenv("REDIS_DB", "0"))
        self._client = None  # 惰性创建，避免 Redis 变硬依赖
        self._loop = None    # client 绑定的事件循环，换循环必须重建
        self._failed_until = 0.0  # monotonic，冷却期内走内存实现
        self._fallback = InMemoryRateLimiter(max_requests, window_seconds)

    def _full_key(self, key: str) -> str:
        return f"{_REDIS_KEY_PREFIX}{key}:w{self.window_seconds}"

    async def _get_client(self):
        import redis.asyncio as redis_async  # 延迟导入：memory 后端不碰 Redis

        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            if self._client is not None:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
            self._client = redis_async.Redis(
                host=self._host,
                port=self._port,
                db=self._db,
                protocol=2,  # Redis 3.0 无 HELLO/RESP3，必须 RESP2
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                decode_responses=True,
            )
            self._loop = loop
        return self._client

    async def check(self, key: str) -> int | None:
        key = str(key)
        if time.monotonic() < self._failed_until:
            return await self._fallback.check(key)
        full_key = self._full_key(key)
        member = f"{time.time_ns()}-{os.urandom(4).hex()}"
        try:
            client = await self._get_client()
            result = await asyncio.wait_for(
                client.eval(
                    _RATE_LIMIT_LUA, 1, full_key,
                    str(self.window_seconds), str(self.max_requests), member,
                    repr(time.time()),
                ),
                timeout=_REDIS_CHECK_DEADLINE_SECONDS,
            )
            retry_after = _lua_int(result[0])
            return None if retry_after == 0 else retry_after
        except Exception as exc:
            logger.error(
                "rate_limit.redis_failed key=%s backend=redis error=%s action=fallback_memory",
                full_key, exc,
            )
            self._failed_until = time.monotonic() + _REDIS_COOLDOWN_SECONDS
            self._client = None
            self._loop = None
            return await self._fallback.check(key)

    def purge_expired(self) -> int:
        return 0

    def reset(self, key: str | None = None) -> None:
        self._fallback.reset(key)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._areset(key))
            return
        asyncio.get_running_loop().create_task(self._areset(key))

    async def _areset(self, key: str | None) -> None:
        import redis.asyncio as redis_async

        try:
            client = redis_async.Redis(
                host=self._host, port=self._port, db=self._db, protocol=2,
                socket_connect_timeout=0.5, socket_timeout=0.5, decode_responses=True,
            )
            try:
                if key is not None:
                    await client.delete(self._full_key(str(key)))
                    return
                cursor = 0
                while True:
                    cursor, keys = await client.scan(cursor=cursor, match=f"{_REDIS_KEY_PREFIX}*", count=200)
                    if keys:
                        await client.delete(*keys)
                    if int(cursor) == 0:
                        break
            finally:
                await client.aclose()
        except Exception as exc:
            logger.warning("rate_limit.redis_reset_failed error=%s", exc)


def _lua_int(value) -> int:
    if isinstance(value, bytes):
        value = value.decode()
    return int(float(value))


def _backend_from_env() -> str:
    raw = (os.getenv("RATE_LIMIT_BACKEND") or "memory").strip().lower()
    if raw not in ("memory", "redis"):
        raise RuntimeError(
            f"RATE_LIMIT_BACKEND 非法：{raw!r}，只允许 memory|redis"
        )
    return raw


_LIMITERS: list[InMemoryRateLimiter | RedisRateLimiter] = []


def make_limiter(max_requests: int, window_seconds: int) -> InMemoryRateLimiter | RedisRateLimiter:
    backend = _backend_from_env()
    limiter = RedisRateLimiter(max_requests, window_seconds) if backend == "redis" else InMemoryRateLimiter(max_requests, window_seconds)
    _LIMITERS.append(limiter)
    return limiter


def reset_all_limiters() -> None:
    for limiter in _LIMITERS:
        limiter.reset()


def _trusted_proxy() -> bool:
    return (os.getenv("TRUSTED_PROXY") or "false").strip().lower() in ("1", "true", "yes", "on")


def client_ip(request: Request) -> str:
    if _trusted_proxy():
        hops = [h.strip() for h in (request.headers.get("x-forwarded-for") or "").split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


async def check_or_429(
    limiter: InMemoryRateLimiter | RedisRateLimiter,
    key: str,
    *,
    action: str,
    dimension: str,
    **context,
) -> None:
    retry_after = await limiter.check(key)
    if retry_after is not None:
        from services.metrics import rate_limit_rejected_total
        rate_limit_rejected_total.inc()
        logger.warning(
            "rate_limited action=%s dimension=%s retry_after=%s context=%s",
            action, dimension, retry_after, context,
        )
        raise HTTPException(
            status_code=429,
            detail=f"{action}过于频繁，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )
