import hashlib
import logging
import os
import re
import time
from pathlib import Path

from fastapi import Form, HTTPException, Query, UploadFile

from services.storage import STORAGE_DIR, generate_stored_name, resolve_storage_path

logger = logging.getLogger(__name__)


make_stored_name = generate_stored_name

CHUNK_SIZE = 1024 * 1024

# 定义放 service 层，路由与清理任务共用，避免两份 CHUNK_DIR 漂移
CHUNK_DIR = STORAGE_DIR / "chunks"

# upload_id 是 uuid4().hex，格式白名单，不是"猜不到"的安全保证
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class UploadIdFormatError(ValueError):
    pass


class UploadPathError(ValueError):
    pass


def normalize_upload_id(raw: str) -> str:
    # 只做纯格式匹配，路径边界由 resolve_chunk_dir 兜
    if not isinstance(raw, str) or not UPLOAD_ID_RE.fullmatch(raw):
        safe_value = raw if isinstance(raw, str) else ""
        logger.warning(
            "upload.upload_id_rejected reason=bad_format value=%r",
            safe_value[:64],
        )
        raise UploadIdFormatError("传入的 upload_id 格式非法")
    return raw


def validate_upload_id_http(raw: str) -> str:
    try:
        return normalize_upload_id(raw)
    except UploadIdFormatError as exc:
        raise HTTPException(
            status_code=400,
            detail="upload_id 格式非法：必须是服务端下发的 32 位十六进制字符串",
        ) from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_chunk_dir(upload_id: str) -> Path:
    # 只做纯判断，不 mkdir/写盘/rmtree；调用方拿到合法路径再操作
    safe_id = normalize_upload_id(upload_id)
    root = CHUNK_DIR.resolve()
    candidate = (root / safe_id).resolve()
    if candidate == root or not _is_relative_to(candidate, root):
        logger.warning(
            "upload.upload_id_rejected reason=path_escape upload_id=%s root=%s candidate=%s",
            safe_id, root, candidate,
        )
        raise UploadPathError("传入的 upload_id 路径越界")
    return candidate


def safe_chunk_dir(upload_id: str) -> Path:
    try:
        return resolve_chunk_dir(upload_id)
    except (UploadIdFormatError, UploadPathError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def upload_id_form(upload_id: str = Form(...)) -> str:
    return validate_upload_id_http(upload_id)


async def upload_id_query(upload_id: str = Query(...)) -> str:
    return validate_upload_id_http(upload_id)


# 上传上限集中定义，可 .env 覆盖
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(50 * 1024 * 1024)))
MAX_CHUNK_SIZE = int(os.getenv("MAX_CHUNK_SIZE", str(5 * 1024 * 1024)))
MAX_TOTAL_CHUNKS = int(os.getenv("MAX_TOTAL_CHUNKS", "10000"))


class UploadTooLargeError(Exception):
    pass


async def save_upload_file(
    file: UploadFile,
    dest: Path,
    chunk_size: int = CHUNK_SIZE,
    max_size: int = MAX_FILE_SIZE,
) -> tuple[int, str]:
    # 超限或写盘失败都删半截文件
    hasher = hashlib.md5()
    size = 0
    chunks = 0
    started = time.perf_counter()
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                if size + len(chunk) > max_size:
                    raise UploadTooLargeError(
                        f"文件超过上限 {max_size} 字节，已收到 {size + len(chunk)} 字节"
                    )
                out.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
                chunks += 1
                # 分片级累计大小，便于排查第几片写坏了
                logger.debug(
                    "upload.write dest=%s chunk_no=%d chunk_bytes=%d accumulated_bytes=%d",
                    dest.name, chunks, len(chunk), size,
                )
    except UploadTooLargeError:
        dest.unlink(missing_ok=True)
        logger.warning(
            "upload.too_large dest=%s accumulated_bytes=%d max_size=%d",
            dest.name, size, max_size,
        )
        raise
    except Exception:
        logger.exception(
            "upload.write_failed dest=%s chunks=%d accumulated_bytes=%d elapsed_ms=%.1f",
            dest.name, chunks, size, (time.perf_counter() - started) * 1000,
        )
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    md5_hex = hasher.hexdigest()
    logger.info(
        "upload.write_done dest=%s bytes=%d md5=%s chunks=%d elapsed_ms=%.1f",
        dest.name, size, md5_hex, chunks, (time.perf_counter() - started) * 1000,
    )
    return size, md5_hex
