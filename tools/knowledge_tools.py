"""工具层：全是「查」权限，LLM 拿不到部门参数"""
import logging
from typing import Annotated

from langchain.tools import InjectedState, InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from sqlalchemy import or_, select

from config.db_conf import AsyncSessionLocal
from crud.file import shared_departments_clause
from models import FileInfo, KnowledgeDoc
from rag.store import select_relevant_hits, store
from services.prompt_guard import (
    INJECTION_INSTRUCTION,
    scan_injection,
    wrap_knowledge,
    wrap_untrusted,
)
from tools._retry import retry_async, user_readable_error

logger = logging.getLogger(__name__)

# 必须模块级名字 + 运行时全局解析，测试靠 monkeypatch 本模块 store 注入故障


@tool(parse_docstring=True)
async def search_knowledge(query: str, state: Annotated[dict, InjectedState]) -> str:
    """语义检索企业知识库，返回知识原文和来源文件。

    Args:
        query: 检索关键词或完整问题。
    """
    # 身份从可信 State 取，LLM 不可见
    user_department = state["user_department"]
    allow_all = state.get("allow_all_departments", False)
    top_k = 3
    try:
        hits = await _search_with_db(query, None if allow_all else user_department, top_k)
        if not hits:
            return "未检索到相关内容。请换关键词，或联系管理员补充知识。"
        lines = []
        for i, h in enumerate(hits, 1):
            src = f"{h['filename']}" + (f" 第{h['page']}页" if h.get("page") else "") + (f" [{h['sheet']}]" if h.get("sheet") else "")
            flags = scan_injection(h.get("text", ""))
            if flags:
                from services.metrics import injection_suspected_total

                injection_suspected_total.labels(channel="tool").inc()
                logger.warning(
                    "knowledge_tools.injection_suspected tool=search_knowledge filename=%s rules=%s",
                    h.get("filename"), flags,
                )
            lines.append(wrap_untrusted(
                f"[{src}] {h['text']}",
                index=i,
                source=src or "knowledge",
            ))
        return INJECTION_INSTRUCTION + "\n检索结果：\n" + wrap_knowledge(lines)
    except Exception as e:
        # 给模型可读降级文案，异常细节不发给 LLM
        logger.exception("knowledge_tools.search_failed query_len=%d top_k=%d", len(query), top_k)
        return user_readable_error(e)


async def _search_with_db(query: str, user_department: str, top_k: int = 3) -> list[dict]:
    from crud.file import get_files_by_ids
    async def _search_once():
        return await store.search(query, department=user_department, top_k=top_k)

    raw_hits = await retry_async("search_knowledge", _search_once)
    hits = select_relevant_hits(raw_hits, top_k=top_k)
    if not hits:
        return []
    file_ids = {h["metadata"]["file_id"] for h in hits if h["metadata"].get("file_id")}
    async with AsyncSessionLocal() as db:
        names = await get_files_by_ids(db, list(file_ids))
    out = []
    for h in hits:
        m = h["metadata"]
        out.append({
            "text": h["text"],
            "filename": names.get(m.get("file_id")),
            "page": m.get("page"),
            "sheet": m.get("sheet"),
            "department": m.get("department"),   # metadata 键名就叫 department，store.py 定死的
        })
    return out


@tool
async def list_department_docs(
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> ToolMessage:
    """查询本部门已入库的文档列表。员工问'我们部门有哪些文档'时使用。"""
    user_department = state["user_department"]
    allow_all = state.get("allow_all_departments", False)
    async with AsyncSessionLocal() as db:
        stmt = select(KnowledgeDoc, FileInfo.filename).join(
            FileInfo, KnowledgeDoc.file_id == FileInfo.id
        ).where(KnowledgeDoc.index_status == "ready")
        if not allow_all:
            # 共享部门必须走精确数组语义，否则"技术部A"的共享文档漏进技术部
            stmt = stmt.where(or_(
                KnowledgeDoc.department == user_department,
                shared_departments_clause(user_department),
            ))
        stmt = stmt.order_by(KnowledgeDoc.id.desc()).limit(20)
        rows = (await db.execute(stmt)).all()
    if not rows:
        return ToolMessage(content="本部门暂无已入库的文档。", tool_call_id=tool_call_id)
    label = "全部已入库文档" if allow_all else "本部门已入库文档"
    preview_rows = rows[:3]
    lines = [f"[{i}] {fn}" for i, (_, fn) in enumerate(preview_rows, 1)]
    if len(rows) > 3:
        lines.append(f"……共 {len(rows)} 份，完整清单见引用来源")
    artifact_rows = [
        {"file_id": doc.file_id, "doc_id": doc.id, "filename": fn}
        for doc, fn in rows
    ]
    return ToolMessage(
        content=f"{label}：\n" + "\n".join(lines),
        tool_call_id=tool_call_id,
        artifact={"rows": artifact_rows},
    )


@tool(parse_docstring=True)
async def check_doc_status(
    doc_id: int,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> ToolMessage:
    """按文档 ID 查询一份文档的审核/入库状态。

    Args:
        doc_id: 知识文档 ID。
    """
    user_department = state["user_department"]
    allow_all = state.get("allow_all_departments", False)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(KnowledgeDoc, FileInfo.filename, FileInfo.is_shared, FileInfo.shared_departments, FileInfo.id)
            .join(FileInfo, KnowledgeDoc.file_id == FileInfo.id)
            .where(KnowledgeDoc.id == doc_id)
        )).first()
    if not row:
        return ToolMessage(content=f"未找到文档 {doc_id}。", tool_call_id=tool_call_id)
    doc, fn, is_shared, shared_departments, file_id = row
    if not allow_all and doc.department != user_department:
        depts = [d.strip() for d in (shared_departments or "").split(",") if d.strip()]
        can_view = is_shared and ((not depts) or (user_department in depts))
        if not can_view:
            return ToolMessage(content="该文档属于其他部门，无权限查询。", tool_call_id=tool_call_id)
    return ToolMessage(
        content=f"[1] 文档《{fn}》当前状态：审核 {doc.status}，入库 {doc.index_status}，共 {doc.chunk_count} 块",
        tool_call_id=tool_call_id,
        artifact={"rows": [{"file_id": file_id, "filename": fn, "doc_id": doc.id}]},
    )


TOOLS = [search_knowledge, list_department_docs, check_doc_status]
