import asyncio
import hashlib
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from routers.auth import get_current_user
from models.file import FileInfo
from models.user import User
from config.db_conf import AsyncSessionLocal, get_db
from config.rate_limit import check_or_429, client_ip, make_limiter
from rag.store import store  # 软删/恢复/物理清理后通知 BM25 快照失效
from services.storage import STORAGE_DIR, delete_storage_artifacts
from services.upload import (
    CHUNK_DIR,
    MAX_CHUNK_SIZE,
    MAX_FILE_SIZE,
    MAX_TOTAL_CHUNKS,
    UploadIdFormatError,
    UploadPathError,
    UploadTooLargeError,
    make_stored_name,
    resolve_chunk_dir,
    safe_chunk_dir,
    save_upload_file,
    upload_id_form,
    upload_id_query,
)
from services.permissions import can_use_shared_upload, can_manage_file, check_department_access, is_admin, is_employee
from crud import file as file_crud
from crud import ops_audit as crud_ops_audit
from schemas.common import Page
from schemas.file import (
    CONTENT_TYPE_MAX_LENGTH,
    FILENAME_MAX_LENGTH,
    FileOut,
    FilePage,
    Md5Hex,
    ShareIn,
    UploadCompleteIn,
    UploadInitIn,
    UploadInitOut,
    DownloadStatsOut,
    normalize_shared_departments,
)

router = APIRouter(prefix="/files", tags=["文件"])
logger = logging.getLogger(__name__)

DOWNLOAD_USER_LIMITER = make_limiter(30, 60)
DOWNLOAD_IP_LIMITER = make_limiter(60, 60)

CHUNK_SIZE = 1024 * 1024

UPLOAD_STALE_HOURS = int(os.getenv("UPLOAD_STALE_HOURS", "24"))
UPLOAD_CLEANUP_INTERVAL_SECONDS = int(os.getenv("UPLOAD_CLEANUP_INTERVAL_SECONDS", "3600"))


async def _require_upload_owner(
    db: AsyncSession,
    upload_id: str,
    current_user: User,
    endpoint: str,
) -> "UploadTask | None":
    """不存在或不属于本人统一 404，防状态码探测 upload_id"""
    task = await file_crud.get_upload_task(db, upload_id)
    if task is None:
        logger.warning(
            "upload.owner_check upload_id=%s user_id=%s endpoint=%s reason=task_not_found",
            upload_id, current_user.id, endpoint,
        )
        raise HTTPException(status_code=404, detail="上传任务不存在")
    if task.user_id != current_user.id:
        logger.warning(
            "upload.owner_check upload_id=%s user_id=%s endpoint=%s reason=not_owner",
            upload_id, current_user.id, endpoint,
        )
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return task


DOWNLOAD_ALLOWED_ORIGINS_ENV = "DOWNLOAD_ALLOWED_ORIGINS"
REFERER_LOG_MAX_LEN = 255


def _configured_download_origins() -> list[str]:
    raw = os.getenv(DOWNLOAD_ALLOWED_ORIGINS_ENV, "")
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


def _url_origin_parts(value: str):
    try:
        parsed = urlsplit(value)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not scheme or not host:
        return None
    authority = host.lower()
    if port is not None:
        authority = f"{authority}:{port}"
    return scheme, authority


def _allowed_item_parts(item: str):
    if "://" in item:
        return _url_origin_parts(item)
    try:
        parsed = urlsplit(f"//{item}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    authority = host.lower()
    if port is not None:
        authority = f"{authority}:{port}"
    return None, authority


def _is_allowed_download_referer(
    referer: str, request_origin: tuple[str, str] | None = None
) -> bool:
    """逐项相等，禁止子串匹配，否则 evil-example.com 会命中 example.com"""
    referer_parts = _url_origin_parts(referer)
    if referer_parts is None:
        logger.warning("download.referer_blocked reason=unparsable")
        return False
    if request_origin is not None and referer_parts == request_origin:
        return True

    allowed = _configured_download_origins()
    if not allowed:
        logger.warning(
            "download.referer_blocked reason=allowlist_not_configured env=%s",
            DOWNLOAD_ALLOWED_ORIGINS_ENV,
        )
        return False

    referer_scheme, referer_authority = referer_parts

    for item in allowed:
        allowed_parts = _allowed_item_parts(item)
        if allowed_parts is None:
            logger.warning(
                "download.allowed_origin_invalid value=%r reason=unparsable", item
            )
            continue
        allowed_scheme, allowed_authority = allowed_parts
        if allowed_scheme is None:
            if referer_authority == allowed_authority:
                return True
        elif referer_scheme == allowed_scheme and referer_authority == allowed_authority:
            return True

    logger.warning(
        "download.referer_blocked reason=not_in_allowlist referer=%s",
        _url_origin_parts(referer),
    )
    return False




@router.post("/upload", response_model=FileOut, status_code=201)
async def upload_file(
    file: UploadFile = File(...),  # multipart 表单里的文件字段；前端 <input type="file" name="file">
    shared: bool = Form(False),   # 管理员/领导可上传到共享空间
    shared_departments: str = Form(""),   # 逗号分隔部门，空=共享给全部部门
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    filename = file.filename or "未命名"
    if len(filename) > FILENAME_MAX_LENGTH:
        raise HTTPException(status_code=422, detail=f"文件名超过 {FILENAME_MAX_LENGTH} 字符")
    from rag.parser import is_supported_upload
    if not is_supported_upload(filename):
        raise HTTPException(status_code=415, detail="暂不支持该文件类型")
    content_type = file.content_type or "application/octet-stream"
    if len(content_type) > CONTENT_TYPE_MAX_LENGTH:
        raise HTTPException(status_code=422, detail=f"content_type 超过 {CONTENT_TYPE_MAX_LENGTH} 字符")
    try:
        share_depts = normalize_shared_departments(
            (shared_departments or "").strip() or None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    can_shared = can_use_shared_upload(current_user) and shared
    stored_name = make_stored_name(filename)
    dest = STORAGE_DIR / stored_name
    logger.info(
        "upload.file_start filename=%s user_id=%s stored_name=%s shared=%s",
        file.filename, current_user.id, stored_name, can_shared,
    )

    started = time.perf_counter()
    try:
        size, md5_hex = await save_upload_file(file, dest, max_size=MAX_FILE_SIZE)
    except UploadTooLargeError as exc:
        logger.warning("upload.file_too_large filename=%s reason=%s", file.filename, exc)
        raise HTTPException(status_code=413, detail=str(exc))
    except Exception:
        logger.exception(
            "upload.file_failed filename=%s stored_name=%s elapsed_ms=%.1f",
            file.filename, stored_name, (time.perf_counter() - started) * 1000,
        )
        raise HTTPException(status_code=500, detail="文件写入失败")

    record, inserted = await file_crud.create_file_atomic(
        db=db,
        filename=filename,
        size_bytes=size,
        content_type=content_type,
        md5=md5_hex,
        stored_path=stored_name,
        user_id=current_user.id,
        department=current_user.department or "未分配",
        is_shared=can_shared,
        shared_departments=share_depts,
    )
    if inserted:
        await db.commit()
        logger.info(
            "upload.file_done filename=%s user_id=%s file_id=%s size=%d md5=%s elapsed_ms=%.1f",
            record.filename, current_user.id, record.id, record.size_bytes, record.md5,
            (time.perf_counter() - started) * 1000,
        )
        return record

    existing = record
    restored = False
    if existing.deleted_at is not None:
        existing.deleted_at = None
        restored = True
        logger.info("upload.file_fast_upload_restored file_id=%s user_id=%s", existing.id, current_user.id)
    existing_path = STORAGE_DIR / existing.stored_path
    if existing_path.exists():
        dest.unlink(missing_ok=True)
    else:
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(dest, existing_path)
        logger.warning(
            "upload.file_fast_upload_repaired filename=%s user_id=%s existing_file_id=%s md5=%s",
            file.filename, current_user.id, existing.id, md5_hex,
        )
    if can_shared:
        existing.is_shared = True
        existing.shared_departments = share_depts
    await db.commit()
    logger.info(
        "upload.file_fast_upload_hit filename=%s user_id=%s existing_file_id=%s size=%d md5=%s",
        file.filename, current_user.id, existing.id, size, md5_hex,
    )
    if restored:
        store.notify_bm25_stale()  # 恢复可见 → BM25 快照重建
    return JSONResponse(status_code=200, content=FileOut.model_validate(existing).model_dump(mode="json"))


@router.get("/check")
async def check_md5(md5: Md5Hex, db: AsyncSession = Depends(get_db),current_user: User = Depends(get_current_user)):
    """看不到的文件等价于不存在，不泄露系统里有没有该内容"""
    record = await file_crud.get_file_by_md5(db, md5)
    if record is not None and is_employee(current_user) and not check_department_access(
        current_user, record.department, record.is_shared, record.shared_departments,
    ):
        record = None
    if record is None:
        logger.info("upload.check_md5 md5=%s exists=False", md5)
        return {"exists": False, "file": None}
    logger.info(
        "upload.check_md5 md5=%s exists=True file_id=%s size=%d",
        md5, record.id, record.size_bytes,
    )
    return {"exists": True, "file": FileOut.model_validate(record)}


@router.get("", response_model=FilePage)
async def list_files(
    department: str | None = None,   # 领导/老板筛选用：GET /files?department=技术部
    page: int = 1,
    page_size: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page = max(1, page)
    page_size = max(1, min(page_size, 50))
    if is_employee(current_user):
        dept = current_user.department
        include_shared = True
    else:
        dept = department
        include_shared = False
    total = await file_crud.count_files(db, department=dept, include_shared=include_shared)
    rows = await file_crud.list_files(
        db,
        department=dept,
        include_shared=include_shared,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    items = [FileOut.model_validate(f).model_copy(update={"uploader": uname}) for f, uname in rows]
    return FilePage(items=items, total=total, page=page, page_size=page_size)


@router.delete("/{file_id}")
async def delete_file(file_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    record = await file_crud.get_file(db, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not can_manage_file(current_user, record):
        raise HTTPException(status_code=403, detail="无权删除该部门的文件")
    if record.deleted_at is not None:
        raise HTTPException(status_code=404, detail="文件不存在")

    record.deleted_at = datetime.now(timezone.utc)
    await crud_ops_audit.add_ops_log(
        db, action="delete_file", target_type="file", actor_id=current_user.id,
        target_id=file_id, detail={"filename": record.filename, "mode": "soft"},
    )
    await db.commit()
    logger.info("file.soft_deleted file_id=%s operator=%s", file_id, current_user.id)
    store.notify_bm25_stale()
    return {"ok": True, "deleted": record.filename}


async def _set_file_share_state(
    file_id: int,
    target_is_shared: bool,
    target_shared_departments: str | None,
    db: AsyncSession,
    current_user: User,
) -> FileOut:
    record = await file_crud.get_file(db, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not can_manage_file(current_user, record):
        raise HTTPException(status_code=403, detail="无权设置该部门文件的共享")

    record.is_shared = target_is_shared
    record.shared_departments = target_shared_departments
    await db.flush()
    await crud_ops_audit.add_ops_log(
        db, action="share" if target_is_shared else "unshare",
        target_type="file", actor_id=current_user.id, target_id=file_id,
        detail={"shared_departments": target_shared_departments},
    )

    from models import KnowledgeDoc
    from sqlalchemy import select as _select

    doc = (
        await db.execute(
            _select(KnowledgeDoc).where(KnowledgeDoc.file_id == record.id)
        )
    ).scalar_one_or_none()
    await db.commit()

    if doc is not None and doc.status == "approved":
        from rag.indexer import reindex_document

        await reindex_document(doc.id)
    return FileOut.model_validate(record)


@router.post("/{file_id}/share", response_model=FileOut)
async def share_file(
    file_id: int,
    payload: ShareIn | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload is None or payload.all:
        target_is_shared = True
        target_shared_departments = None
    else:
        target_is_shared = True
        target_shared_departments = (
            ",".join(d.strip() for d in payload.departments if d.strip()) or None
        )
    return await _set_file_share_state(
        file_id,
        target_is_shared,
        target_shared_departments,
        db,
        current_user,
    )


@router.post("/{file_id}/unshare", response_model=FileOut)
async def unshare_file(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _set_file_share_state(file_id, False, None, db, current_user)


@router.post("/upload/init", response_model=UploadInitOut)
async def init_upload(
    payload: UploadInitIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.total_chunks > MAX_TOTAL_CHUNKS:
        raise HTTPException(status_code=413, detail=f"分片数超过上限 {MAX_TOTAL_CHUNKS}")
    if payload.chunk_size > MAX_CHUNK_SIZE:
        raise HTTPException(status_code=413, detail=f"单分片大小超过上限 {MAX_CHUNK_SIZE} 字节")
    if payload.total_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"文件大小超过上限 {MAX_FILE_SIZE} 字节")
    from rag.parser import is_supported_upload
    if not is_supported_upload(payload.filename):
        raise HTTPException(status_code=415, detail="暂不支持该文件类型")
    upload_id = uuid4().hex
    await file_crud.create_upload_task(
        db, upload_id, current_user.id,
        total_size=payload.total_size, total_chunks=payload.total_chunks,
        expected_md5=payload.md5,
    )
    await db.commit()
    logger.info(
        "upload.chunk_init upload_id=%s user_id=%s filename=%s total_chunks=%d chunk_size=%d",
        upload_id, current_user.id, payload.filename, payload.total_chunks, payload.chunk_size,
    )
    return UploadInitOut(
        upload_id=upload_id,
        chunk_size=payload.chunk_size,
        total_chunks=payload.total_chunks,
    )


@router.post("/upload/chunk")
async def upload_chunk(
    upload_id: str = Depends(upload_id_form),
    chunk_index: int = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    await _require_upload_owner(db, upload_id, current_user, endpoint="/upload/chunk")
    task = await file_crud.get_upload_task(db, upload_id)
    if task is not None and task.total_chunks is not None and not (
        0 <= chunk_index < task.total_chunks
    ):
        raise HTTPException(
            status_code=400,
            detail=f"chunk_index 越界：应在 [0, {task.total_chunks}) 内",
        )
    existing = await file_crud.get_chunk(db, upload_id, chunk_index)
    if existing is not None:
        await file.close()
        logger.info(
            "upload.chunk_skip upload_id=%s chunk_index=%d user_id=%s reason=already_received",
            upload_id, chunk_index, current_user.id,
        )
        return {"ok": True, "skipped": True, "chunk_index": chunk_index}

    chunk_dir = safe_chunk_dir(upload_id)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    dest = chunk_dir / f"{chunk_index}.part"
    tmp_dest = chunk_dir / f"{chunk_index}.{uuid4().hex}.tmp"

    started = time.perf_counter()
    try:
        size, chunk_md5 = await save_upload_file(file, tmp_dest, max_size=MAX_CHUNK_SIZE)
    except UploadTooLargeError as exc:
        logger.warning(
            "upload.chunk_too_large upload_id=%s chunk_index=%d user_id=%s reason=%s",
            upload_id, chunk_index, current_user.id, exc,
        )
        raise HTTPException(status_code=413, detail=str(exc))
    except Exception:
        logger.exception(
            "upload.chunk_failed upload_id=%s chunk_index=%d user_id=%s dest=%s",
            upload_id, chunk_index, current_user.id, tmp_dest.name,
        )
        raise HTTPException(status_code=500, detail="分片写入失败")

    claimed = await file_crud.create_chunk(
        db, upload_id, chunk_index, size, chunk_md5, user_id=current_user.id
    )
    await db.commit()
    if claimed:
        os.replace(tmp_dest, dest)
    else:
        tmp_dest.unlink(missing_ok=True)
        logger.info(
            "upload.chunk_skip upload_id=%s chunk_index=%d user_id=%s reason=lost_claim",
            upload_id, chunk_index, current_user.id,
        )
        return {"ok": True, "skipped": True, "chunk_index": chunk_index}
    logger.info(
        "upload.chunk_done upload_id=%s chunk_index=%d user_id=%s size_bytes=%d md5=%s elapsed_ms=%.1f",
        upload_id, chunk_index, current_user.id, size, chunk_md5,
        (time.perf_counter() - started) * 1000,
    )
    return {"ok": True, "skipped": False, "chunk_index": chunk_index, "size_bytes": size}


@router.get("/upload/status")
async def chunk_status(
    upload_id: str = Depends(upload_id_query),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _require_upload_owner(db, upload_id, current_user, endpoint="/upload/status")
    chunks = await file_crud.list_chunks(db, upload_id)
    logger.debug(
        "upload.chunk_status upload_id=%s user_id=%s count=%d received=%s",
        upload_id, current_user.id, len(chunks), [c.chunk_index for c in chunks],
    )
    return {
        "upload_id": upload_id,
        "received": [c.chunk_index for c in chunks],
        "count": len(chunks),
    }


@router.post("/upload/complete")
async def complete_upload(payload: UploadCompleteIn, db: AsyncSession = Depends(get_db),current_user: User = Depends(get_current_user)):
    # 任务不存在 → 400，重试是常态；不属于本人 → 404 防探测
    task = await file_crud.get_upload_task(db, payload.upload_id)
    if task is None:
        raise HTTPException(status_code=400, detail="上传任务不存在或已完成合并")
    if task.user_id != current_user.id:
        logger.warning(
            "upload.owner_check upload_id=%s user_id=%s endpoint=/upload/complete reason=not_owner",
            payload.upload_id, current_user.id,
        )
        raise HTTPException(status_code=404, detail="上传任务不存在")
    chunk_dir = safe_chunk_dir(payload.upload_id)
    if payload.total_chunks > MAX_TOTAL_CHUNKS:
        raise HTTPException(status_code=413, detail=f"分片数超过上限 {MAX_TOTAL_CHUNKS}")
    can_shared = can_use_shared_upload(current_user) and payload.shared
    share_depts = (payload.shared_departments or "").strip() or None
    chunks = await file_crud.list_chunks(db, payload.upload_id)
    if task.total_chunks is not None:
        expected_chunks = task.total_chunks
        expected_total = task.total_size
    else:
        expected_chunks = payload.total_chunks
        expected_total = None
        logger.warning(
            "upload.complete_legacy_task upload_id=%s reason=declared_values_missing",
            payload.upload_id,
        )
    logger.info(
        "upload.complete_start upload_id=%s user_id=%s filename=%s received_chunks=%d expected_chunks=%d",
        payload.upload_id, current_user.id, payload.filename, len(chunks), expected_chunks,
    )
    if len(chunks) != expected_chunks:
        logger.warning(
            "upload.complete_incomplete upload_id=%s user_id=%s received_chunks=%d expected_chunks=%d",
            payload.upload_id, current_user.id, len(chunks), expected_chunks,
        )
        raise HTTPException(
            status_code=400,
            detail=f"分片不齐：已收 {len(chunks)}/{expected_chunks}，请补传后再合并",
        )
    if sorted(c.chunk_index for c in chunks) != list(range(expected_chunks)):
        raise HTTPException(status_code=400, detail="分片序号不连续：请重新校验后补传")

    stored_name = make_stored_name(payload.filename)
    dest = STORAGE_DIR / stored_name
    started = time.perf_counter()

    hasher = hashlib.md5()
    total = 0
    try:
        with open(dest, "wb") as out:
            for c in chunks:
                part = chunk_dir / f"{c.chunk_index}.part"
                if not part.exists():
                    logger.error(
                        "upload.complete_part_missing upload_id=%s chunk_index=%d path=%s",
                        payload.upload_id, c.chunk_index, part,
                    )
                    raise HTTPException(status_code=400, detail=f"第 {c.chunk_index} 片实体缺失")
                if part.stat().st_size != c.size_bytes:
                    logger.warning(
                        "upload.complete_part_size_mismatch upload_id=%s chunk_index=%d "
                        "registered=%d actual=%d",
                        payload.upload_id, c.chunk_index, c.size_bytes, part.stat().st_size,
                    )
                    raise HTTPException(
                        status_code=400, detail=f"第 {c.chunk_index} 片与登记大小不一致"
                    )
                part_hasher = hashlib.md5()
                with open(part, "rb") as f:
                    while True:
                        data = f.read(CHUNK_SIZE)
                        if not data:
                            break
                        if total + len(data) > MAX_FILE_SIZE:
                            raise UploadTooLargeError(
                                f"合并结果超过上限 {MAX_FILE_SIZE} 字节，已累积 {total + len(data)}"
                            )
                        out.write(data)
                        hasher.update(data)
                        part_hasher.update(data)
                        total += len(data)
                part_md5 = part_hasher.hexdigest()
                if part_md5 != c.md5:
                    logger.warning(
                        "upload.complete_part_md5_mismatch upload_id=%s chunk_index=%d "
                        "registered=%s actual=%s",
                        payload.upload_id, c.chunk_index, c.md5, part_md5,
                    )
                    raise HTTPException(
                        status_code=400, detail=f"第 {c.chunk_index} 片与登记指纹不一致"
                    )
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except UploadTooLargeError as exc:
        logger.warning(
            "upload.complete_too_large upload_id=%s user_id=%s accumulated_bytes=%d reason=%s",
            payload.upload_id, current_user.id, total, exc,
        )
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=str(exc))
    except Exception:
        logger.exception(
            "upload.complete_merge_failed upload_id=%s user_id=%s dest=%s accumulated_bytes=%d",
            payload.upload_id, current_user.id, dest.name, total,
        )
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="合并失败")

    md5_hex = hasher.hexdigest()
    expected_md5 = task.expected_md5 or payload.md5
    if md5_hex != expected_md5:
        logger.warning(
            "upload.complete_md5_mismatch upload_id=%s user_id=%s expected_md5=%s actual_md5=%s "
            "size_bytes=%d",
            payload.upload_id, current_user.id, expected_md5, md5_hex, total,
        )
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件校验失败：分片与原始文件不一致")
    if expected_total is not None and total != expected_total:
        logger.warning(
            "upload.complete_total_mismatch upload_id=%s declared=%d actual=%d",
            payload.upload_id, expected_total, total,
        )
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件校验失败：合并大小与声明不一致")
    record, inserted = await file_crud.create_file_atomic(
        db=db,
        filename=payload.filename,
        size_bytes=total,
        content_type=payload.content_type,
        md5=md5_hex,
        stored_path=stored_name,
        user_id=current_user.id,
        department=current_user.department or "未分配",
        is_shared=can_shared,
        shared_departments=share_depts,
    )

    shutil.rmtree(chunk_dir, ignore_errors=True)
    await file_crud.delete_chunks(db, payload.upload_id)
    await file_crud.delete_upload_task(db, payload.upload_id)

    if not inserted:
        existing = record
        restored = False
        if existing.deleted_at is not None:
            existing.deleted_at = None  # 同内容重传恢复软删除行
            restored = True
            logger.info("upload.complete_fast_upload_restored file_id=%s", existing.id)
        existing_path = STORAGE_DIR / existing.stored_path
        if existing_path.exists():
            dest.unlink(missing_ok=True)
        else:
            existing_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dest, existing_path)
            logger.warning(
                "upload.complete_fast_upload_repaired upload_id=%s existing_file_id=%s md5=%s",
                payload.upload_id, existing.id, md5_hex,
            )
        if can_shared:
            existing.is_shared = True
            existing.shared_departments = share_depts
        await db.commit()
        logger.info(
            "upload.complete_fast_upload_hit upload_id=%s user_id=%s existing_file_id=%s "
            "total_bytes=%d md5=%s",
            payload.upload_id, current_user.id, existing.id, total, md5_hex,
        )
        if restored:
            store.notify_bm25_stale()  # 恢复可见 → BM25 快照重建
        return JSONResponse(status_code=200, content=FileOut.model_validate(existing).model_dump(mode="json"))

    await db.commit()
    logger.info(
        "upload.complete_done upload_id=%s user_id=%s file_id=%s filename=%s "
        "chunks=%d total_bytes=%d md5=%s elapsed_ms=%.1f",
        payload.upload_id, current_user.id, record.id, record.filename,
        expected_chunks, total, md5_hex, (time.perf_counter() - started) * 1000,
    )
    return record


@router.get("/{file_id}/download")
async def download_file(file_id: int, request: Request, db: AsyncSession = Depends(get_db),current_user: User = Depends(get_current_user)):
    await check_or_429(DOWNLOAD_USER_LIMITER, f"download:user:{current_user.id}", action="下载请求", dimension="user", user_id=current_user.id)
    await check_or_429(DOWNLOAD_IP_LIMITER, f"download:ip:{client_ip(request)}", action="下载请求", dimension="ip", user_id=current_user.id)
    record = await file_crud.get_file(db, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    raw_referer = request.headers.get("referer") or ""
    request_origin = _url_origin_parts(str(request.base_url))
    if raw_referer and not _is_allowed_download_referer(raw_referer, request_origin):
        raise HTTPException(status_code=403, detail="禁止盗链：请从本站页面下载")
    if record.deleted_at is not None:
        raise HTTPException(status_code=404, detail="文件不存在")
    path = STORAGE_DIR / record.stored_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已丢失，元数据与实体不一致")
    if is_employee(current_user) and not check_department_access(
        current_user,
        record.department,
        record.is_shared,
        record.shared_departments,
    ):
        raise HTTPException(status_code=404, detail="文件不存在")

    await file_crud.create_download_log(
        db=db,
        file_id=file_id,
        ip=request.client.host if request.client else "unknown",
        referer=raw_referer[:REFERER_LOG_MAX_LEN] if raw_referer else None,
    )

    media_type = record.content_type
    if media_type.startswith("text/"):
        media_type = f"{media_type}; charset=utf-8"
    return FileResponse(path=path, filename=record.filename, media_type=media_type)


@router.get("/stats/downloads", response_model=Page[DownloadStatsOut],)
async def download_stats(db: AsyncSession = Depends(get_db),current_user: User = Depends(get_current_user)):
    """部门过滤下推 SQL，否则会跨部门泄漏文件名"""
    dept = current_user.department if is_employee(current_user) else None
    counts = await file_crud.count_downloads_by_file(db, department=dept, limit=50)
    items = [
        DownloadStatsOut(file_id=file_id, filename=filename, download_count=cnt)
        for file_id, filename, cnt in counts
    ]
    return Page(items=items, total=len(items), page=1, page_size=max(1, len(items)))


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for file in path.rglob("*"):
        try:
            if file.is_file():
                total += file.stat().st_size
        except OSError:
            continue
    return total


async def cleanup_stale_uploads(older_than_hours: int | None = None, db: AsyncSession | None = None) -> dict:
    """以最后一片活动时间判定，否则会误删正在续传的任务"""
    hours = int(older_than_hours if older_than_hours is not None else UPLOAD_STALE_HOURS)
    if hours <= 0:
        raise ValueError("older_than_hours 必须大于 0")
    # 必须用 aware UTC：naive 本地时间会被 psycopg 按 UTC 解释，cutoff 早 8 小时
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    async def _run(session: AsyncSession) -> dict:
        rows = await file_crud.list_stale_upload_ids(session, cutoff)
        deleted_uploads = 0
        deleted_chunks = 0
        freed_bytes = 0
        skipped_invalid = 0
        upload_ids: list[str] = []
        for row in rows:
            upload_id = row.upload_id
            # 该 DB 值来源于客户端可控字段，不能当可信输入
            try:
                chunk_dir = resolve_chunk_dir(upload_id)
            except (UploadIdFormatError, UploadPathError) as exc:
                skipped_invalid += 1
                logger.warning(
                    "upload.cleanup_skip_invalid upload_id=%r reason=%s",
                    upload_id, type(exc).__name__,
                )
                await file_crud.delete_chunks(session, upload_id)
                await file_crud.delete_upload_task(session, upload_id)
                continue
            freed = _dir_size_bytes(chunk_dir)
            if chunk_dir.exists():
                shutil.rmtree(chunk_dir, ignore_errors=True)
            await file_crud.delete_chunks(session, upload_id)
            await file_crud.delete_upload_task(session, upload_id)
            deleted_uploads += 1
            deleted_chunks += int(row.chunk_count or 0)
            freed_bytes += freed
            upload_ids.append(upload_id)
        await session.commit()
        logger.info(
            "upload.cleanup_stale cutoff=%s older_than_hours=%d deleted_uploads=%d "
            "deleted_chunks=%d freed_bytes=%d skipped_invalid=%d upload_ids=%s",
            cutoff.isoformat(), hours, deleted_uploads, deleted_chunks, freed_bytes,
            skipped_invalid, upload_ids,
        )
        return {
            "older_than_hours": hours,
            "deleted_uploads": deleted_uploads,
            "deleted_chunks": deleted_chunks,
            "freed_bytes": freed_bytes,
            "upload_ids": upload_ids,
        }

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as session:
        return await _run(session)


@router.post("/upload/cleanup-stale")
async def cleanup_stale_uploads_endpoint(
    older_than_hours: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="无权执行清理")
    return await cleanup_stale_uploads(older_than_hours=older_than_hours, db=db)


SOFT_DELETE_RETENTION_DAYS = int(os.getenv("SOFT_DELETE_RETENTION_DAYS", "30"))


async def cleanup_soft_deleted_files(db: AsyncSession | None = None) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SOFT_DELETE_RETENTION_DAYS)

    async def _run(session: AsyncSession) -> dict:
        from sqlalchemy import select as _select
        rows = (await session.execute(
            _select(FileInfo).where(FileInfo.deleted_at.is_not(None), FileInfo.deleted_at < cutoff)
        )).scalars().all()
        freed_bytes = 0
        removed: list[int] = []
        for record in rows:
            path = STORAGE_DIR / record.stored_path
            if path.exists():
                freed_bytes += path.stat().st_size
            try:
                delete_storage_artifacts(record.stored_path)
            except Exception:
                logger.exception("file.purge_disk_failed file_id=%s stored_path=%s",
                                 record.id, record.stored_path)
                continue  # 盘删失败不删行，下一轮重试
            await file_crud.delete_file(session, record.id)  # 先删下载流水再删行，外键约束
            removed.append(record.id)
        await session.commit()
        if removed:
            logger.info("file.purged count=%d freed_bytes=%d file_ids=%s",
                        len(removed), freed_bytes, removed)
            from rag.store import store as _store
            _store.notify_bm25_stale()
        return {"removed": len(removed), "freed_bytes": freed_bytes, "file_ids": removed}

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as session:
        return await _run(session)


async def upload_cleanup_loop() -> None:
    interval = max(60, UPLOAD_CLEANUP_INTERVAL_SECONDS)
    while True:
        try:
            await cleanup_stale_uploads()
        except Exception:
            from services.metrics import bg_loop_error_total
            bg_loop_error_total.labels(loop="upload_cleanup").inc()
            logger.exception("upload.cleanup_stale_loop_failed")
        try:
            await cleanup_soft_deleted_files()
        except Exception:
            logger.exception("file.soft_delete_cleanup_loop_failed")
        await asyncio.sleep(interval)
