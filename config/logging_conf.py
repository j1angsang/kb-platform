from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv 可选，读不到不影响日志启动
    pass

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")

_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | "
    "rid=%(request_id)s | uid=%(user_id)s | %(message)s"
)
_ACCESS_LOGGER = logging.getLogger("request")
_STATIC_SUFFIXES = {
    ".html", ".css", ".js", ".mjs", ".map", ".ico", ".png", ".jpg", ".jpeg",
    ".gif", ".svg", ".webp", ".bmp", ".woff", ".woff2", ".ttf", ".otf",
}
_CONFIGURED = False

_SENSITIVE_KV = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|passwd|pwd|secret|token)\b(\s*[:=]\s*)(\S+)"
)
_SENSITIVE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_SENSITIVE_SK = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
_ID_LIKE = re.compile(r"(?<![0-9A-Za-z.])(\d{15}|\d{17}[0-9Xx])(?![0-9A-Za-z.])")


def _mask_text(value: str) -> str:
    def _mask_id(match: re.Match) -> str:
        raw = match.group(0)
        return raw[:6] + "*" * max(0, len(raw) - 10) + raw[-4:]
    value = _ID_LIKE.sub(_mask_id, value)
    value = _SENSITIVE_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
    value = _SENSITIVE_JWT.sub("***", value)
    value = _SENSITIVE_SK.sub("***", value)
    return value


class _MaskingFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        return _mask_text(super().format(record))


class _ContextFilter(logging.Filter):

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        return True


def get_request_id() -> str:
    return request_id_var.get()


def set_request_id(value: str | None) -> None:
    request_id_var.set((value or "").strip()[:64] or "-")


def bind_user_id(user_id) -> None:
    user_id_var.set(str(user_id) if user_id is not None else "-")


def _ensure_utf8_stream() -> None:
    """中文 Windows 控制台默认 GBK，避免 UnicodeEncodeError"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def setup_logging() -> None:
    """必须在其他业务模块 import / 打日志之前调用"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    _ensure_utf8_stream()
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # 清掉默认/重复 handler，避免一条日志打多份

    formatter = _MaskingFormatter(_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    context_filter = _ContextFilter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)
    root.addHandler(console_handler)

    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    log_file = log_dir / os.getenv("LOG_FILE", "app.log")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        root.addHandler(file_handler)
    except OSError as exc:  # 文件不可写就降级控制台，不因日志基建拖垮启动
        logging.getLogger(__name__).warning("文件日志初始化失败，已降级为控制台日志: %s", exc)

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "日志系统初始化完成 level=%s file=%s", level_name, log_file
    )


class RequestLoggingMiddleware:
    """不用 BaseHTTPMiddleware：它会把响应包一层，SSE 会被缓冲"""

    def __init__(self, app, skip_prefixes: tuple[str, ...] = ("/static", "/storage")):
        self.app = app
        self.skip_prefixes = skip_prefixes

    def _should_skip(self, path: str) -> bool:
        if path.startswith(self.skip_prefixes):
            return True
        if path == "/" or path == "/favicon.ico":
            return True
        return Path(path).suffix.lower() in _STATIC_SUFFIXES

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        if self._should_skip(path):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "-")
        raw_headers = dict(scope.get("headers") or [])
        incoming_rid = (raw_headers.get(b"x-request-id") or b"").decode("latin-1", "ignore").strip()
        request_id = incoming_rid[:64] or uuid.uuid4().hex
        request_token = request_id_var.set(request_id)
        user_token = user_id_var.set("-")
        start = time.perf_counter()
        state = {"status": 500, "response_started": False, "finished": False}

        def _log_access(status: int, *, level: int = logging.INFO, detail: str = "") -> None:
            if state["finished"]:
                return
            state["finished"] = True
            elapsed_ms = (time.perf_counter() - start) * 1000
            _ACCESS_LOGGER.log(
                level,
                "HTTP %s %s status=%s duration_ms=%.1f%s",
                method,
                path,
                status,
                elapsed_ms,
                f" {detail}" if detail else "",
            )

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = int(message.get("status", 500))
                state["response_started"] = True
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                _log_access(state["status"])

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            _log_access(499, level=logging.WARNING, detail="客户端断开/请求取消")
            raise
        except Exception:
            if state["finished"]:
                _ACCESS_LOGGER.exception(
                    "HTTP %s %s 响应已发送但后续任务异常，可能是 BackgroundTasks",
                    method,
                    path,
                )
            else:
                state["finished"] = True
                _ACCESS_LOGGER.exception(
                    "HTTP %s %s status=500 duration_ms=%.1f 未处理异常",
                    method,
                    path,
                    (time.perf_counter() - start) * 1000,
                )
            raise
        finally:
            request_id_var.reset(request_token)
            user_id_var.reset(user_token)