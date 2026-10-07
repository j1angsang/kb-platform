import asyncio, logging, time

logger = logging.getLogger(__name__)

RETRY_DELAYS = (0.5, 1.0)


def status_code(exc) -> int | None:
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable(exc: BaseException) -> bool:
    code = status_code(exc)
    if code is not None:
        return code >= 500
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ("timeout", "timed out", "connection", "connect",
                                  "500", "502", "503", "504", "temporarily", "unavailable"))


def reason_of(exc: BaseException) -> str:
    code = status_code(exc)
    if code is not None:
        return f"http_{code}"
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "connect" in msg or "connection" in msg:
        return "connection"
    return type(exc).__name__


async def retry_async(tool: str, func, *args, retries: int = 2, **kwargs):
    started = time.perf_counter()
    last_exc = None
    for attempt in range(retries + 1):
        try:
            result = await func(*args, **kwargs)
            if attempt > 0:
                logger.info("tool_retry_success tool=%s attempt=%d/%d elapsed_ms=%.1f",
                            tool, attempt + 1, retries, (time.perf_counter() - started) * 1000)
            return result
        except Exception as exc:
            last_exc = exc
            if attempt >= retries or not is_retryable(exc):
                break
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            logger.warning(
                "tool_retry tool=%s attempt=%d/%d reason=%s delay_s=%.1f error=%s",
                tool, attempt + 1, retries, reason_of(exc), delay, exc,
            )
            await asyncio.sleep(delay)
    logger.error(
        "tool_retry_final tool=%s attempts=%d retryable=%s elapsed_ms=%.1f error=%s",
        tool, retries + 1, is_retryable(last_exc), (time.perf_counter() - started) * 1000, last_exc,
    )
    raise last_exc


def user_readable_error(exc: BaseException) -> str:
    code = status_code(exc)
    if code == 404:
        return "未找到相关内容，请换关键词或联系管理员补充知识。"
    if code is not None and 400 <= code < 500:
        return "请求参数有误或无权限，请调整后重试。"
    return "知识检索暂时不可用，请稍后再试。"
