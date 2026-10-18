"""本地磁盘 / MinIO(S3) 对象存储适配。"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

from minio import Minio
from minio.error import S3Error

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: str = "false") -> bool:
    return (os.getenv(name) or default).strip().lower() in ("1", "true", "yes", "on")


STORAGE_BACKEND = (os.getenv("STORAGE_BACKEND") or "local").strip().lower()
if STORAGE_BACKEND not in ("local", "s3"):
    raise RuntimeError(f"STORAGE_BACKEND 非法：{STORAGE_BACKEND!r}，只允许 local|s3")

S3_ENDPOINT = (os.getenv("S3_ENDPOINT") or "127.0.0.1:9000").strip()
S3_ACCESS_KEY = (os.getenv("S3_ACCESS_KEY") or "").strip()
S3_SECRET_KEY = (os.getenv("S3_SECRET_KEY") or "").strip()
S3_BUCKET = (os.getenv("S3_BUCKET") or "kb-files").strip()
S3_SECURE = _env_bool("S3_SECURE", "false") or S3_ENDPOINT.startswith("https://")
S3_KEY_PREFIX = (os.getenv("S3_KEY_PREFIX") or "kb").strip().strip("/")
S3_AUTO_CREATE_BUCKET = _env_bool("S3_AUTO_CREATE_BUCKET", "true")
S3_REGION = (os.getenv("S3_REGION") or "").strip() or None

S3_ENDPOINT = S3_ENDPOINT.removeprefix("http://").removeprefix("https://").rstrip("/")

if STORAGE_BACKEND == "s3":
    _missing = [
        name for name, value in (
            ("S3_ENDPOINT", S3_ENDPOINT),
            ("S3_ACCESS_KEY", S3_ACCESS_KEY),
            ("S3_SECRET_KEY", S3_SECRET_KEY),
            ("S3_BUCKET", S3_BUCKET),
        ) if not value
    ]
    if _missing:
        raise RuntimeError(
            "STORAGE_BACKEND=s3 但缺少配置：" + ", ".join(_missing)
            + "；请参考 .env.example"
        )
    if not S3_BUCKET.replace("-", "").replace(".", "").isalnum():
        raise RuntimeError(f"S3_BUCKET 非法：{S3_BUCKET!r}")


def _local_path(stored_path: str) -> Path:
    from services.storage import STORAGE_DIR

    name = Path(stored_path).name
    if not name or name in (".", "..") or name != stored_path:
        raise ValueError(f"非法 stored_path：{stored_path!r}")
    return STORAGE_DIR / name


class ObjectStorage:
    def __init__(self) -> None:
        self.backend = STORAGE_BACKEND
        self.bucket = S3_BUCKET
        self._client: Minio | None = None
        self._bucket_ready = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.backend == "s3"

    def _key(self, stored_path: str) -> str:
        name = Path(stored_path).name
        if not name or name in (".", "..") or name != stored_path:
            raise ValueError(f"非法 stored_path：{stored_path!r}")
        return f"{S3_KEY_PREFIX}/{name}" if S3_KEY_PREFIX else name

    def _get_client(self) -> Minio:
        if not self.enabled:
            raise RuntimeError("对象存储未启用")
        if self._client is None:
            self._client = Minio(
                S3_ENDPOINT,
                access_key=S3_ACCESS_KEY,
                secret_key=S3_SECRET_KEY,
                secure=S3_SECURE,
                region=S3_REGION,
            )
        return self._client

    def _ensure_bucket(self) -> None:
        if not self.enabled or self._bucket_ready:
            return
        with self._lock:
            if self._bucket_ready:
                return
            client = self._get_client()
            if not client.bucket_exists(S3_BUCKET):
                if not S3_AUTO_CREATE_BUCKET:
                    raise RuntimeError(
                        f"MinIO/S3 bucket 不存在：{S3_BUCKET}（S3_AUTO_CREATE_BUCKET=false）"
                    )
                client.make_bucket(S3_BUCKET, location=S3_REGION)
                logger.info("object_storage.bucket_created bucket=%s", S3_BUCKET)
            self._bucket_ready = True

    def upload(self, stored_path: str, local_path: str | Path) -> None:
        """把原始文件同步到对象存储；local 后端下是 no-op。"""
        if not self.enabled:
            return
        source = Path(local_path)
        if not source.exists():
            raise FileNotFoundError(f"待上传文件不存在：{source}")
        self._ensure_bucket()
        self._get_client().fput_object(
            S3_BUCKET, self._key(stored_path), str(source),
            content_type="application/octet-stream",
        )
        logger.info(
            "object_storage.uploaded bucket=%s key=%s bytes=%d",
            S3_BUCKET, self._key(stored_path), source.stat().st_size,
        )

    async def upload_async(self, stored_path: str, local_path: str | Path) -> None:
        await asyncio.to_thread(self.upload, stored_path, local_path)

    def ensure_local(self, stored_path: str) -> Path:
        """返回本地工作副本；本地缺失时从对象存储回源。"""
        path = _local_path(stored_path)
        if path.exists() and path.stat().st_size > 0:
            return path
        if not self.enabled:
            raise FileNotFoundError(f"本地文件不存在：{path}")
        self._ensure_bucket()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._get_client().fget_object(
                S3_BUCKET, self._key(stored_path), str(path),
            )
        except S3Error as exc:
            raise FileNotFoundError(
                f"对象存储回源失败：bucket={S3_BUCKET} key={self._key(stored_path)} code={exc.code}"
            ) from exc
        logger.info(
            "object_storage.downloaded bucket=%s key=%s bytes=%d",
            S3_BUCKET, self._key(stored_path), path.stat().st_size,
        )
        return path

    async def ensure_local_async(self, stored_path: str) -> Path:
        return await asyncio.to_thread(self.ensure_local, stored_path)

    def delete(self, stored_path: str) -> None:
        if not self.enabled:
            return
        try:
            self._get_client().remove_object(S3_BUCKET, self._key(stored_path))
            logger.info("object_storage.deleted bucket=%s key=%s", S3_BUCKET, self._key(stored_path))
        except S3Error as exc:
            if exc.code not in ("NoSuchKey", "NoSuchBucket"):
                raise
            logger.info(
                "object_storage.delete_missing bucket=%s key=%s code=%s",
                S3_BUCKET, self._key(stored_path), exc.code,
            )

    async def delete_async(self, stored_path: str) -> None:
        await asyncio.to_thread(self.delete, stored_path)

    def healthcheck(self) -> str:
        if not self.enabled:
            return "local"
        self._ensure_bucket()
        if not self._get_client().bucket_exists(S3_BUCKET):
            raise RuntimeError(f"bucket 不可用：{S3_BUCKET}")
        return "ok"


object_storage = ObjectStorage()

if object_storage.enabled:
    logger.info(
        "object_storage.ready backend=s3 endpoint=%s bucket=%s secure=%s prefix=%s",
        S3_ENDPOINT, S3_BUCKET, S3_SECURE, S3_KEY_PREFIX,
    )
