from sqlalchemy import and_, delete, select, func, or_
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import DownloadLog
from models.chunk import UploadChunk, UploadTask
from models.file import FileInfo
from models.user import User


def shared_departments_clause(department: str):
    # 数组精确匹配，不能用 like 子串，否则可见性泄漏
    return and_(
        FileInfo.is_shared.is_(True),
        or_(
            FileInfo.shared_departments.is_(None),
            FileInfo.shared_departments == "",
            sa_text(":dept = ANY(string_to_array(shared_departments, ','))").bindparams(
                dept=department
            ),
        ),
    )


async def create_file(db: AsyncSession, filename: str, size_bytes: int,
                      content_type: str, md5: str, stored_path: str,
                      user_id: int | None = None, department: str | None = None,
                      is_shared: bool = False,
                      shared_departments: str | None = None) -> FileInfo:
    record = FileInfo(
        filename=filename,
        size_bytes=size_bytes,
        content_type=content_type,
        md5=md5,
        stored_path=stored_path,
        user_id=user_id,
        department=department,
        is_shared=is_shared,
        shared_departments=shared_departments,
    )
    db.add(record)
    await db.flush()  # 只取自增 id，不提交
    # created_at 是 server_default，不 refresh 序列化时报 MissingGreenlet
    await db.refresh(record)
    return record


async def get_file(db: AsyncSession, file_id: int) -> FileInfo | None:
    stmt = select(FileInfo).where(FileInfo.id == file_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_file_by_md5(db: AsyncSession, md5: str) -> FileInfo | None:
    stmt = select(FileInfo).where(FileInfo.md5 == md5)
    result = await db.execute(stmt)
    return result.scalars().first()


async def create_file_atomic(db: AsyncSession, *, filename: str, size_bytes: int,
                             content_type: str, md5: str, stored_path: str,
                             user_id: int | None, department: str | None,
                             is_shared: bool, shared_departments: str | None,
                             ) -> tuple[FileInfo | None, bool]:
    # 并发首传靠唯一索引 + ON CONFLICT，只有 rowcount>0 的一方写成功
    stmt = (
        pg_insert(FileInfo)
        .values(
            filename=filename, size_bytes=size_bytes, content_type=content_type,
            md5=md5, stored_path=stored_path, user_id=user_id,
            department=department, is_shared=is_shared,
            shared_departments=shared_departments,
        )
        .on_conflict_do_nothing(index_elements=["md5"])
        .returning(FileInfo.id)
    )
    result = await db.execute(stmt)
    row = result.first()
    if row is None:
        return await get_file_by_md5(db, md5), False
    await db.flush()
    record = await get_file(db, row[0])
    return record, True


async def list_files(db: AsyncSession, department: str | None = None,
                     include_shared: bool = False,
                     limit: int | None = None, offset: int = 0):
    stmt = (
        select(FileInfo, User.username)
        .outerjoin(User, FileInfo.user_id == User.id)
        .where(FileInfo.deleted_at.is_(None))  # 软删除不可见
    )
    if department is not None:
        if include_shared:
            stmt = stmt.where(or_(
                FileInfo.department == department,
                shared_departments_clause(department),
            ))
        else:
            stmt = stmt.where(FileInfo.department == department)
    stmt = stmt.order_by(FileInfo.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.all())


async def count_files(db: AsyncSession, department: str | None = None,
                     include_shared: bool = False) -> int:
    stmt = select(func.count(FileInfo.id)).where(FileInfo.deleted_at.is_(None))
    if department is not None:
        if include_shared:
            stmt = stmt.where(or_(
                FileInfo.department == department,
                shared_departments_clause(department),
            ))
        else:
            stmt = stmt.where(FileInfo.department == department)
    result = await db.execute(stmt)
    return result.scalar_one()


async def delete_file(db: AsyncSession, file_id: int) -> None:
    await db.execute(delete(DownloadLog).where(DownloadLog.file_id == file_id))
    record = await get_file(db, file_id)
    if record is not None:
        await db.delete(record)
        await db.flush()




async def create_upload_task(db: AsyncSession, upload_id: str, user_id: int,
                             total_size: int | None = None,
                             total_chunks: int | None = None,
                             expected_md5: str | None = None) -> UploadTask:
    record = UploadTask(
        upload_id=upload_id, user_id=user_id,
        total_size=total_size, total_chunks=total_chunks, expected_md5=expected_md5,
    )
    db.add(record)
    await db.flush()
    return record


async def get_upload_task(db: AsyncSession, upload_id: str) -> UploadTask | None:
    stmt = select(UploadTask).where(UploadTask.upload_id == upload_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def delete_upload_task(db: AsyncSession, upload_id: str) -> None:
    stmt = delete(UploadTask).where(UploadTask.upload_id == upload_id)
    await db.execute(stmt)


async def create_chunk(db: AsyncSession, upload_id: str, chunk_index: int,
                       size_bytes: int, md5: str,
                       user_id: int | None = None) -> bool:
    # 判定用 RETURNING 不用 rowcount：psycopg3 对 DO NOTHING 的 rowcount 恒为 -1
    stmt = (
        pg_insert(UploadChunk)
        .values(
            upload_id=upload_id, chunk_index=chunk_index,
            size_bytes=size_bytes, md5=md5, user_id=user_id,
        )
        .on_conflict_do_nothing(index_elements=["upload_id", "chunk_index"])
        .returning(UploadChunk.id)
    )
    result = await db.execute(stmt)
    return result.first() is not None


async def get_chunk(db: AsyncSession, upload_id: str, chunk_index: int) -> UploadChunk | None:
    stmt = select(UploadChunk).where(
        UploadChunk.upload_id == upload_id,
        UploadChunk.chunk_index == chunk_index,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_chunks(db: AsyncSession, upload_id: str) -> list[UploadChunk]:
    stmt = (
        select(UploadChunk)
        .where(UploadChunk.upload_id == upload_id)
        .order_by(UploadChunk.chunk_index)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def delete_chunks(db: AsyncSession, upload_id: str) -> None:
    stmt = delete(UploadChunk).where(UploadChunk.upload_id == upload_id)
    await db.execute(stmt)

async def create_download_log(db: AsyncSession, file_id: int, ip: str, referer: str | None) -> DownloadLog:
    log = DownloadLog(
        file_id=file_id,
        ip=ip,
        referer=referer,
    )
    db.add(log)
    await db.flush()
    return log

async def count_downloads_by_file(db: AsyncSession,
                                  department: str | None = None,
                                  limit: int = 50) -> list[tuple[int, str, int]]:
    stmt = (
        select(
            DownloadLog.file_id,
            FileInfo.filename,
            func.count(DownloadLog.id).label("cnt"),
        )
        .join(FileInfo, DownloadLog.file_id == FileInfo.id)
        .group_by(DownloadLog.file_id, FileInfo.filename)
        .order_by(func.count(DownloadLog.id).desc())
        .limit(limit)
    )
    if department is not None:
        stmt = stmt.where(or_(
            FileInfo.department == department,
            shared_departments_clause(department),
        ))
    
    stmt = stmt.where(FileInfo.deleted_at.is_(None))
    result = await db.execute(stmt)
    return list(result.all())

async def get_files_by_ids(
    db: AsyncSession,
    ids: list[int],
    *,
    with_objects: bool = False,
) -> dict[int, str] | dict[int, FileInfo]:
    if not ids:
        return {}
    stmt = select(FileInfo).where(FileInfo.id.in_(ids))
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    if with_objects:
        return {f.id: f for f in rows}
    return {f.id: f.filename for f in rows}


async def get_page_image_paths(
    db: AsyncSession,
    ids: list[int],
) -> dict[int, str]:
    if not ids:
        return {}
    from models.knowledge_chunk import KnowledgeChunk

    stmt = (
        select(KnowledgeChunk.file_id, KnowledgeChunk.image_path)
        .where(
            KnowledgeChunk.file_id.in_(ids),
            KnowledgeChunk.type == "image",
            KnowledgeChunk.status == "ready",
            KnowledgeChunk.image_path.isnot(None),
        )
        .distinct(KnowledgeChunk.file_id)
        .order_by(KnowledgeChunk.file_id, KnowledgeChunk.page, KnowledgeChunk.image_path)
    )
    result = await db.execute(stmt)
    return {file_id: image_path for file_id, image_path in result.all()}


async def list_stale_upload_ids(db: AsyncSession, cutoff):
    # 必须以 upload_tasks 为驱动表，否则孤儿任务残留
    stmt = (
        select(
            UploadTask.upload_id,
            func.max(UploadChunk.created_at).label("last_seen"),
            func.count(UploadChunk.id).label("chunk_count"),
            func.coalesce(func.sum(UploadChunk.size_bytes), 0).label("stale_bytes"),
        )
        .outerjoin(UploadChunk, UploadChunk.upload_id == UploadTask.upload_id)
        .group_by(UploadTask.upload_id)
        .having(or_(
            func.max(UploadChunk.created_at).is_(None),
            func.max(UploadChunk.created_at) < cutoff,
        ))
    )
    result = await db.execute(stmt)
    return list(result.all())
