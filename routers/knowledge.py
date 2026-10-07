import asyncio
import logging
import os
import time
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.db_conf import get_db
from config.rate_limit import check_or_429, client_ip, make_limiter
from crud import file as file_crud
from crud import knowledge as knowledge_crud
from models import KnowledgeDoc
from models.user import User
from agents import review_graph
from rag.indexer import index_document
from rag.store import store
from routers.auth import get_current_user
from services.permissions import (
    can_auto_approve_doc,
    can_review_doc,
    is_admin,
    is_employee,
    is_leader,
)
from schemas.knowledge import ApproveIn, KnowledgeDocOut, KnowledgeDocPage, RejectIn, SubmitIn, SearchOut, SearchHit
from sqlalchemy import select
router = APIRouter(prefix="/knowledge", tags=["知识库"])

KNOWLEDGE_SEARCH_USER_LIMITER = make_limiter(
    int(os.getenv("RATE_LIMIT_SEARCH_MAX", "30")),
    int(os.getenv("RATE_LIMIT_SEARCH_WINDOW", "60")),
)
logger = logging.getLogger(__name__)


@router.post("/submit", response_model=KnowledgeDocOut, status_code=201)
async def submit_doc(
    payload: SubmitIn,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file = await file_crud.get_file(db, payload.file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="文件不存在")

    if file.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="只能提交自己上传的文件")
    if file.is_shared and (file.department or "") != (current_user.department or ""):
        raise HTTPException(status_code=403, detail="共享文档只能由本部门成员提交")

    existing = await knowledge_crud.get_doc_by_file(db, payload.file_id)
    if existing is not None:
        raise HTTPException(status_code=400, detail="该文件已提交过审核，驳回后可重新提交")

    doc = await knowledge_crud.create_doc(
        db, file_id=file.id, user_id=current_user.id,
        department=file.department or current_user.department or "未分配",
    )
    auto_approved = can_auto_approve_doc(current_user)
    if auto_approved:
        doc.status = "approved"
        doc.index_status = "indexing"
        await db.flush()
    await knowledge_crud.add_log(db, doc.id, action="submit", actor_id=current_user.id)
    if auto_approved:
        await knowledge_crud.add_log(
            db, doc.id, action="auto_approve", actor_id=current_user.id,
            comment=f"提交人角色 {current_user.role}，免审自动通过",
        )
    if auto_approved:
        await knowledge_crud.enqueue_index_job(db, doc.id)
    await db.commit()
    if auto_approved:
        background_tasks.add_task(index_document, doc.id)
    else:
        await review_graph.submit_for_review(doc.id)
    logger.info(
        "knowledge.index_enqueued trigger=submit doc_id=%s file_id=%s user_id=%s "
        "auto_approved=%s status=%s index_status=%s",
        doc.id, file.id, current_user.id, auto_approved, doc.status, doc.index_status,
    )
    return KnowledgeDocOut.model_validate(doc).model_copy(
        update={"filename": file.filename, "uploader": current_user.username},
    )


@router.get("/docs", response_model=KnowledgeDocPage)
async def list_docs(
    status: str | None = None,
    department: str | None = None,
    scope: str | None = None,
    page: int = 1,
    page_size: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    page = max(1, page)
    # 上限必须对齐前端审核页 page_size=100，旧上限 50 会静默截断
    page_size = max(1, min(page_size, 100))
    exclude_shared = False
    if scope == "reviewable":
        if is_employee(current_user):
            raise HTTPException(status_code=403, detail="无权审核")
        include_shared = False
        exclude_shared = False
        status = "pending"
        if is_leader(current_user):
            dept = current_user.department
        else:
            dept = department
    elif is_employee(current_user):
        dept = current_user.department
        include_shared = True
    else:
        dept = department
        include_shared = False
    total = await knowledge_crud.count_docs(
        db, department=dept, status=status,
        include_shared=include_shared, exclude_shared=exclude_shared,
    )
    rows = await knowledge_crud.list_docs(
        db,
        department=dept,
        status=status,
        include_shared=include_shared,
        exclude_shared=exclude_shared,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    items = [
        KnowledgeDocOut.model_validate(doc).model_copy(update={
            "filename": f.filename,
            "uploader": un,
            "is_shared": f.is_shared,
            "shared_departments": f.shared_departments,
        })
        for doc, f, un in rows
    ]
    return KnowledgeDocPage(items=items, total=total, page=page, page_size=page_size)


def _check_reviewer(current_user: User, doc: KnowledgeDoc, is_shared: bool = False) -> None:
    # 员工无权审；领导只审本部门，department 是提交时快照；共享文档只有管理员能审；不能审自己提交的
    if is_employee(current_user):
        raise HTTPException(status_code=403, detail="无权审核")
    if not can_review_doc(current_user, doc.department, is_shared=is_shared):
        raise HTTPException(status_code=403, detail="无权审核该文档，非本部门或共享文档")
    if doc.user_id == current_user.id:
        raise HTTPException(status_code=403, detail="不能审核自己提交的文档")


@router.post("/{doc_id}/approve", response_model=KnowledgeDocOut)
async def approve_doc(
    doc_id: int,
    payload: ApproveIn,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    doc = await knowledge_crud.get_doc(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status != "pending":
        raise HTTPException(status_code=400, detail=f"当前状态 {doc.status}，不能通过")
    file = await file_crud.get_file(db, doc.file_id)
    _check_reviewer(current_user, doc, is_shared=bool(file and file.is_shared))

    try:
        outcome = await review_graph.resume_review(
            doc_id, "approved", actor_id=current_user.id, comment=payload.comment,
        )
    except Exception:
        # 并发双审：中断点已被消费 → 统一映射 409，不把图错误冒成 500
        raise HTTPException(status_code=409, detail="文档已被其他审核人处理")
    if not outcome["applied"]:
        raise HTTPException(status_code=409, detail="文档已被其他审核人处理")
    await db.refresh(doc)
    background_tasks.add_task(index_document, doc.id)
    logger.info(
        "knowledge.index_enqueued trigger=approve doc_id=%s file_id=%s reviewer_id=%s status=approved index_status=indexing",
        doc.id, doc.file_id, current_user.id,
    )
    return KnowledgeDocOut.model_validate(doc)


@router.post("/{doc_id}/reject", response_model=KnowledgeDocOut)
async def reject_doc(
    doc_id: int,
    payload: RejectIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    doc = await knowledge_crud.get_doc(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status != "pending":
        raise HTTPException(status_code=400, detail=f"当前状态 {doc.status}，不能驳回")
    file = await file_crud.get_file(db, doc.file_id)
    _check_reviewer(current_user, doc, is_shared=bool(file and file.is_shared))

    try:
        outcome = await review_graph.resume_review(
            doc_id, "rejected", actor_id=current_user.id, comment=payload.comment,
        )
    except Exception:
        raise HTTPException(status_code=409, detail="文档已被其他审核人处理")
    if not outcome["applied"]:
        raise HTTPException(status_code=409, detail="文档已被其他审核人处理")
    await db.refresh(doc)
    logger.info(
        "knowledge.reject doc_id=%s file_id=%s reviewer_id=%s reason=%s",
        doc.id, doc.file_id, current_user.id, payload.comment[:120],
    )
    return KnowledgeDocOut.model_validate(doc)


@router.post("/{doc_id}/resubmit", response_model=KnowledgeDocOut)
async def resubmit_doc(
    doc_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    doc = await knowledge_crud.get_doc(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="只能重提自己提交的文档")
    if doc.status != "rejected":
        raise HTTPException(status_code=400, detail=f"当前状态 {doc.status}，不能重提")

    doc.error_message = None
    await knowledge_crud.add_log(db, doc_id, action="resubmit", actor_id=current_user.id)
    if can_auto_approve_doc(current_user):
        doc.status = "approved"
        doc.index_status = "indexing"
        await knowledge_crud.add_log(
            db, doc_id, action="auto_approve", actor_id=current_user.id,
            comment=f"提交人角色 {current_user.role}，重提免审自动通过",
        )
        await knowledge_crud.enqueue_index_job(db, doc_id)
        await db.flush()
        await db.commit()
        background_tasks.add_task(index_document, doc.id)
        logger.info(
            "knowledge.index_enqueued trigger=resubmit doc_id=%s file_id=%s user_id=%s "
            "auto_approved=True status=approved index_status=indexing",
            doc.id, doc.file_id, current_user.id,
        )
        return KnowledgeDocOut.model_validate(doc)
    doc.status = "pending"
    doc.index_status = "pending"
    await db.flush()
    await db.commit()
    await review_graph.submit_for_review(doc_id)
    logger.info("knowledge.resubmit doc_id=%s file_id=%s user_id=%s", doc.id, doc.file_id, current_user.id)
    return KnowledgeDocOut.model_validate(doc)


@router.get("/{doc_id}/graph-history")
async def doc_graph_history(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    doc = await knowledge_crud.get_doc(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.user_id != current_user.id:
        file = await file_crud.get_file(db, doc.file_id)
        _check_reviewer(current_user, doc, is_shared=bool(file and file.is_shared))
    return {"doc_id": doc_id, "thread_id": review_graph.thread_id_for_doc(doc_id),
            "snapshots": await review_graph.get_review_history(doc_id)}


@router.post("/{doc_id}/retry-index", response_model=KnowledgeDocOut)
async def retry_index(
    doc_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    doc = await knowledge_crud.get_doc(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.user_id != current_user.id:
        file = await file_crud.get_file(db, doc.file_id)
        _check_reviewer(current_user, doc, is_shared=bool(file and file.is_shared))
    # 判据含 failed 和 pending，只认 failed 的话被 reject 覆盖成 pending 后永久无法入库
    if doc.status != "approved":
        raise HTTPException(status_code=400, detail=f"当前状态 {doc.status}，不能重试")
    if doc.index_status == "indexing":
        took_over = await knowledge_crud.requeue_stale_job_for_doc(db, doc_id)
        if not took_over:
            raise HTTPException(status_code=400, detail="当前入库状态 indexing，不能重试")
    elif doc.index_status not in ("failed", "pending"):
        raise HTTPException(status_code=400, detail=f"当前入库状态 {doc.index_status}，不能重试")
    result = await db.execute(
        update(KnowledgeDoc)
        .where(
            KnowledgeDoc.id == doc_id,
            KnowledgeDoc.status == "approved",
            KnowledgeDoc.index_status.in_(["failed", "pending"]),
        )
        .values(index_status="indexing")
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="入库任务已被其他请求接管")
    await db.refresh(doc)
    await knowledge_crud.enqueue_index_job(db, doc_id)
    await db.flush()
    await db.commit()
    background_tasks.add_task(index_document, doc.id)
    logger.info(
        "knowledge.index_enqueued trigger=retry doc_id=%s file_id=%s user_id=%s",
        doc.id, doc.file_id, current_user.id,
    )
    return KnowledgeDocOut.model_validate(doc)

@router.get("/search", response_model=SearchOut)
async def search_docs(
    request: Request,
    q: str,
    department: str | None = None,
    top_k: int = 5,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if is_employee(current_user):
        department = current_user.department
    top_k = max(1, min(top_k, 20))
    await check_or_429(KNOWLEDGE_SEARCH_USER_LIMITER, f"knowledge:search:user:{current_user.id}", action="检索请求", dimension="user", user_id=current_user.id)
    started = time.perf_counter()
    try:
        hits = await store.search(q, department=department, top_k=top_k)
    except Exception:
        logger.exception("knowledge.search 检索失败 query=%s department=%s", q, department or "-all-")
        raise HTTPException(status_code=503, detail="暂时无法检索，请稍后再试")
    duration_ms = (time.perf_counter() - started) * 1000
    from services.metrics import retrieval_duration_ms
    retrieval_duration_ms.observe(duration_ms)
    if not hits:
        logger.info(
            "knowledge.search query=%s department=%s top_k=%d hits=0 "
            "relative_drop_applied=False duration_ms=%.1f",
            q, department or "-all-", top_k, duration_ms,
        )
        return SearchOut(query=q, hits=[])
    file_ids = {h["metadata"].get("file_id") for h in hits}
    names = await file_crud.get_files_by_ids(db, [i for i in file_ids if i])
    logger.info(
        "knowledge.search query=%s department=%s top_k=%d hits=%d scores=%s "
        "relative_drop_applied=False duration_ms=%.1f",
        q, department or "-all-", top_k, len(hits),
        [h.get("score") for h in hits],
        (time.perf_counter() - started) * 1000,
    )
    return SearchOut(
        query=q,
        hits=[
            SearchHit(
                text=h["text"],
                score=float(h.get("score", 0.0)),
                file_id=h["metadata"].get("file_id"),
                filename=names.get(h["metadata"].get("file_id")),
                department=h["metadata"].get("department"),
                page=h["metadata"].get("page"),
                sheet=h["metadata"].get("sheet"),
                image_path=h["metadata"].get("image_path"),
                source=h["metadata"].get("source"),  # OCR 文本块标 ocr/ocr_lowconf，其余 None
            )
            for h in hits
        ],
    )
