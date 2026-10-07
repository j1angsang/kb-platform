# 审批要跨请求恢复，HumanInTheLoopMiddleware 只在单次 ainvoke 内中断，
import logging
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from config.db_conf import AsyncSessionLocal
from crud import file as file_crud
from crud import knowledge as knowledge_crud
from models import KnowledgeDoc
from rag.store import store
from services.memory import get_checkpointer, thread_config

logger = logging.getLogger(__name__)


def thread_id_for_doc(doc_id: int) -> str:
    return f"review-{doc_id}"


class ReviewState(TypedDict):
    doc_id: int
    precheck_ok: bool
    precheck_notes: list[str]
    decision: str | None      # approved/rejected 由 resume 注入
    comment: str | None
    actor_id: int | None
    applied: bool


async def _precheck_node(state: ReviewState) -> dict:
    doc_id = state["doc_id"]
    notes: list[str] = []
    async with AsyncSessionLocal() as db:
        doc = await knowledge_crud.get_doc(db, doc_id)
        if doc is None:
            notes.append("文档不存在")
        else:
            if doc.status != "pending":
                notes.append(f"文档状态 {doc.status}，不在待审队列")
            file = await file_crud.get_file(db, doc.file_id)
            if file is None:
                notes.append("文件元数据不存在")
            else:
                from services.storage import STORAGE_DIR

                if not (STORAGE_DIR / file.stored_path).exists():
                    notes.append("磁盘实体丢失")
    return {"precheck_ok": not notes, "precheck_notes": notes}


async def _review_gate_node(state: ReviewState) -> dict:
    if not state.get("precheck_ok"):
        return {"decision": "precheck_failed"}
    resume = interrupt({
        "doc_id": state["doc_id"],
        "precheck_notes": state.get("precheck_notes", []),
        "prompt": "等待人工审批：approve 或 reject",
    })
    return {
        "decision": (resume or {}).get("decision"),
        "comment": (resume or {}).get("comment"),
        "actor_id": (resume or {}).get("actor_id"),
    }


async def _apply_node(state: ReviewState) -> dict:
    doc_id = state["doc_id"]
    decision = state.get("decision")
    actor_id = state.get("actor_id")
    comment = state.get("comment")
    async with AsyncSessionLocal() as db:
        from sqlalchemy import update

        if decision == "precheck_failed":
            await knowledge_crud.add_log(
                db, doc_id, action="precheck_failed", actor_id=actor_id,
                comment="；".join(state.get("precheck_notes", []))[:255],
            )
            await db.commit()
            return {"applied": True}
        if decision == "approved":
            result = await db.execute(
                update(KnowledgeDoc)
                .where(KnowledgeDoc.id == doc_id, KnowledgeDoc.status == "pending")
                .values(status="approved", index_status="indexing", reviewed_by=actor_id,
                        reviewed_at=datetime.now(timezone.utc))
            )
            if result.rowcount == 0:
                await db.rollback()
                return {"applied": False}
            await knowledge_crud.add_log(db, doc_id, action="approve", actor_id=actor_id, comment=comment)
            await knowledge_crud.enqueue_index_job(db, doc_id)  # 入库意图同事务落库
            await db.commit()
            logger.info("review.applied doc_id=%s decision=approved actor_id=%s", doc_id, actor_id)
            return {"applied": True}
        if decision == "rejected":
            result = await db.execute(
                update(KnowledgeDoc)
                .where(KnowledgeDoc.id == doc_id, KnowledgeDoc.status == "pending")
                .values(status="rejected", index_status="pending", reviewed_by=actor_id,
                        reviewed_at=datetime.now(timezone.utc))
            )
            if result.rowcount == 0:
                await db.rollback()
                return {"applied": False}
            doc = await knowledge_crud.get_doc(db, doc_id)
            if doc is not None:
                await store.delete_by_file(db, doc.file_id)
            await knowledge_crud.add_log(db, doc_id, action="reject", actor_id=actor_id, comment=comment)
            await db.commit()
            logger.info("review.applied doc_id=%s decision=rejected actor_id=%s", doc_id, actor_id)
            return {"applied": True}
        return {"applied": False}


def _build_review_graph():
    checkpointer = get_checkpointer()
    builder = StateGraph(ReviewState)
    builder.add_node("precheck", _precheck_node)
    builder.add_node("review_gate", _review_gate_node)
    builder.add_node("apply", _apply_node)
    builder.add_edge(START, "precheck")
    builder.add_edge("precheck", "review_gate")
    builder.add_edge("review_gate", "apply")
    builder.add_edge("apply", END)
    return builder.compile(checkpointer=checkpointer)


async def submit_for_review(doc_id: int) -> None:
    graph = _build_review_graph()
    await graph.ainvoke({"doc_id": doc_id}, config=thread_config(thread_id_for_doc(doc_id)))
    logger.info("review.parked doc_id=%s thread_id=%s", doc_id, thread_id_for_doc(doc_id))


async def resume_review(doc_id: int, decision: str, *, actor_id: int | None,
                        comment: str | None = None) -> dict:
    graph = _build_review_graph()
    result = await graph.ainvoke(
        Command(resume={"decision": decision, "comment": comment, "actor_id": actor_id}),
        config=thread_config(thread_id_for_doc(doc_id)),
    )
    return {
        "applied": bool(result.get("applied")),
        "decision": result.get("decision"),
        "precheck_notes": list(result.get("precheck_notes") or []),
    }


async def get_review_history(doc_id: int) -> list[dict]:
    graph = _build_review_graph()
    config = thread_config(thread_id_for_doc(doc_id))
    out = []
    async for snap in graph.aget_state_history(config):
        out.append({
            "step": (snap.metadata or {}).get("step"),
            "created_at": snap.created_at,
            "next": list(snap.next or []),
            "values": {
                k: v for k, v in (snap.values or {}).items()
                if k in ("doc_id", "precheck_ok", "precheck_notes", "decision")
            },
        })
    return out


async def is_parked_at_review(doc_id: int) -> bool:
    graph = _build_review_graph()
    snap = await graph.aget_state(thread_config(thread_id_for_doc(doc_id)))
    return bool(snap and snap.next)
