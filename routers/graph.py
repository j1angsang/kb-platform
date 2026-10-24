"""知识图谱管理接口。

设计边界（改之前先读）：
1. 这是治理后台，只给管理者 —— 终端员工问问题走 chat，不需要看见图。
2. 实体响应里没有正文。邻域返回的每条边带 chunk_id，正文证据单独过一遍权限 SQL。
3. **诚实说明一处现状**：`require_manager` 已经挡掉了 employee，而
   `can_view_all_departments` 对 leader/admin 都返回 True（与 chat 检索同一套语义），
   所以下面的部门过滤在当前入口下恒为真、不会真正剔除任何行。
   保留它是为了将来把图谱开放给员工时不用重写 —— 那时它会立刻生效。
   真正验证过、会生效的越权防线在检索期：`rag/graph/store.neighbor_chunks`
   用的是同一条 `_permission_clause`，已用对照实验验证（见 docs/GRAPH_IMPLEMENTATION.md §9.3）。
4. 不要把过滤删掉换成"管理者反正都能看"——那等于把未来开放的路径留成裸奔。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.db_conf import get_db
from models.knowledge_chunk import KnowledgeChunk
from models.knowledge_doc import KnowledgeDoc
from models.user import User
from rag.graph.config import KG_ENABLED, KG_MAX_HOPS
from rag.graph.link import link_query
from rag.graph.pipeline import build_document_graph
from rag.graph.store import (
    entity_neighbors,
    gc_orphans,
    list_entities,
    merge_entities,
    neighbor_chunks,
    split_entity,
    stats,
)
from rag.store import PgVectorStore
from routers.auth import get_current_user
from schemas.graph import (
    GraphEdgeOut,
    GraphEntityOut,
    GraphEntityPage,
    GraphMergeIn,
    GraphNeighborOut,
    GraphSplitIn,
    GraphStatsOut,
)
from services.permissions import can_view_all_departments, is_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/graph", tags=["知识图谱"])


async def require_manager(current_user: User = Depends(get_current_user)) -> User:
    if not is_manager(current_user):
        raise HTTPException(status_code=403, detail="仅管理者可访问知识图谱")
    return current_user


@router.get("/stats", response_model=GraphStatsOut)
async def graph_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    data = await stats(db)
    doc_rows = (await db.execute(
        select(KnowledgeDoc.kg_status, func.count()).group_by(KnowledgeDoc.kg_status)
    )).all()
    return GraphStatsOut(
        **data,
        kg_enabled=KG_ENABLED,
        docs_by_status={str(k or "unknown"): int(v) for k, v in doc_rows},
    )


@router.get("/entities", response_model=GraphEntityPage)
async def graph_entities(
    q: str | None = None,
    type: str | None = None,  # noqa: A002 —— 与前端筛选字段同名，保持契约一致
    limit: int = 30,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    limit = max(1, min(limit, 100))
    items, total = await list_entities(db, q=q, type=type, limit=limit, offset=max(0, offset))
    return GraphEntityPage(items=[GraphEntityOut(**i) for i in items], total=total)


@router.get("/entities/{entity_id}/neighbors", response_model=GraphNeighborOut)
async def graph_entity_neighbors(
    entity_id: int,
    limit: int = 40,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    data = await entity_neighbors(db, entity_id, limit=max(1, min(limit, 100)))
    if data["entity"] is None:
        raise HTTPException(status_code=404, detail="实体不存在")

    chunk_ids = [e["chunk_id"] for e in data["edges"]]
    visible: dict[str, str] = {}
    filenames: dict[str, str] = {}
    if chunk_ids:
        stmt = select(KnowledgeChunk.id, KnowledgeChunk.content, KnowledgeChunk.file_id).where(
            KnowledgeChunk.id.in_(chunk_ids),
            KnowledgeChunk.status == "ready",
        )
        # 证据正文必须走与检索完全相同的权限表达式，不能另写一套
        if not can_view_all_departments(current_user):
            stmt = stmt.where(PgVectorStore._permission_clause(current_user.department))
        for cid, content, file_id in (await db.execute(stmt)).all():
            visible[cid] = (content or "")[:200]
            filenames[cid] = str(file_id) if file_id else ""

    edges: list[GraphEdgeOut] = []
    hidden = 0
    for e in data["edges"]:
        if e["chunk_id"] not in visible:
            hidden += 1
            edges.append(GraphEdgeOut(**e, evidence=None, evidence_filename=None))
            continue
        edges.append(GraphEdgeOut(
            **e,
            evidence=visible[e["chunk_id"]],
            evidence_filename=filenames.get(e["chunk_id"]) or None,
        ))
    return GraphNeighborOut(
        entity=GraphEntityOut(**data["entity"]),
        edges=edges,
        hidden_edges=hidden,
    )


@router.post("/entities/merge", response_model=dict)
async def graph_merge_entities(
    payload: GraphMergeIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    try:
        result = await merge_entities(db, payload.source_id, payload.target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return result


@router.post("/entities/{entity_id}/split", response_model=dict)
async def graph_split_entity(
    entity_id: int,
    payload: GraphSplitIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    try:
        new_id = await split_entity(db, entity_id, payload.alias)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await db.commit()
    return {"source_id": entity_id, "new_entity_id": new_id, "alias": payload.alias}


@router.post("/gc", response_model=dict)
async def graph_gc(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    removed = await gc_orphans(db)
    await db.commit()
    return {"orphans_deleted": removed}


@router.get("/link", response_model=dict)
async def graph_link_preview(
    q: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    """调试用：看一个 query 会链接到哪些实体、又会扩出哪些 chunk。"""
    entities = await link_query(db, q)
    hits = []
    if entities:
        hits = await neighbor_chunks(
            db,
            [e["id"] for e in entities],
            department=None if can_view_all_departments(current_user) else current_user.department,
            limit=20,
        )
    return {
        "query": q,
        "max_hops": KG_MAX_HOPS,
        "entities": entities,
        "chunks": [
            {
                "chunk_id": h["id"],
                "hop": h.get("_graph_hop"),
                "confidence": h.get("_graph_confidence"),
                "department": h["metadata"].get("department"),
                "file_id": h["metadata"].get("file_id"),
                "preview": (h.get("text") or "")[:120],
            }
            for h in hits
        ],
    }


@router.post("/docs/{doc_id}/rebuild", response_model=dict)
async def graph_rebuild_doc(
    doc_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_manager),
):
    """同步重建单文档的图（管理端手动触发，不走队列）。

    刻意做成同步：回填几十份文档时，队列的退避和租约反而让排查变难，
    手动触发要的就是"点了等结果，错了立刻看到异常栈"。
    """
    if not KG_ENABLED:
        raise HTTPException(status_code=400, detail="KG_ENABLED=false，图谱功能未启用")
    doc = (await db.execute(
        select(KnowledgeDoc).where(KnowledgeDoc.id == doc_id)
    )).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if not can_view_all_departments(current_user) and doc.department != current_user.department:
        raise HTTPException(status_code=403, detail="只能重建本部门文档的图谱")
    try:
        result = await build_document_graph(doc_id)
    except Exception as exc:
        logger.exception("graph.rebuild_failed doc=%s", doc_id)
        raise HTTPException(status_code=500, detail=f"重建失败：{str(exc)[:200]}")
    return result
