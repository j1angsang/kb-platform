"""存量文档图谱回填 + 孤儿实体清理。

用法：
  python scripts/build_graph.py --dry-run          # 看会处理哪些文档、预估调用量
  python scripts/build_graph.py                    # 处理所有未建图的 approved 文档
  python scripts/build_graph.py --doc-id 12 --force  # 强制重跑某一份
  python scripts/build_graph.py --limit 5          # 只跑前 5 份，先验质量
  python scripts/build_graph.py --gc               # 清孤儿实体
  python scripts/build_graph.py --stats            # 看当前图规模

断点续跑靠 doc.kg_status：ready 的默认跳过，中断后重跑只会处理剩下的。
走的是和后台循环同一条 extract_doc_graph（claim job + 独立锁），
所以脚本和线上任务不会同时处理同一份文档。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from sqlalchemy import select  # noqa: E402

from config.db_conf import AsyncSessionLocal  # noqa: E402
from config.logging_conf import setup_logging  # noqa: E402
from crud import graph_job as graph_crud  # noqa: E402
from models.knowledge_doc import KnowledgeDoc  # noqa: E402
from rag.graph.config import KG_ENABLED, KG_MAX_CHUNKS_PER_DOC  # noqa: E402
from rag.graph.store import gc_orphans, stats  # noqa: E402
from rag.indexer import extract_doc_graph  # noqa: E402


async def list_candidates(doc_id: int | None, force: bool, limit: int | None) -> list[dict]:
    async with AsyncSessionLocal() as db:
        stmt = select(
            KnowledgeDoc.id, KnowledgeDoc.department,
            KnowledgeDoc.chunk_count, KnowledgeDoc.kg_status,
        ).where(
            KnowledgeDoc.status == "approved",
            KnowledgeDoc.index_status == "ready",
        )
        if doc_id is not None:
            stmt = stmt.where(KnowledgeDoc.id == doc_id)
        if not force:
            stmt = stmt.where(KnowledgeDoc.kg_status != "ready")
        stmt = stmt.order_by(KnowledgeDoc.id)
        if limit:
            stmt = stmt.limit(limit)
        rows = (await db.execute(stmt)).all()
    return [
        {"id": r[0], "department": r[1], "chunks": r[2] or 0, "kg_status": r[3]}
        for r in rows
    ]


async def enqueue_and_build(doc_id: int) -> None:
    """先入队再执行：让脚本与后台轮询走同一条路径，避免两套 claim 语义。"""
    async with AsyncSessionLocal() as db:
        doc = (await db.execute(
            select(KnowledgeDoc).where(KnowledgeDoc.id == doc_id)
        )).scalar_one_or_none()
        if doc is not None:
            doc.kg_status = "pending"
            doc.kg_error = None
        await graph_crud.enqueue_graph_job(db, doc_id)
        await db.commit()
    await extract_doc_graph(doc_id)


async def main() -> None:
    parser = argparse.ArgumentParser(description="知识图谱回填")
    parser.add_argument("--doc-id", type=int, default=None, help="只处理指定文档")
    parser.add_argument("--limit", type=int, default=None, help="最多处理几份")
    parser.add_argument("--force", action="store_true", help="kg_status=ready 的也重跑")
    parser.add_argument("--dry-run", action="store_true", help="只列出计划，不调 LLM")
    parser.add_argument("--gc", action="store_true", help="清理没有任何边的孤儿实体")
    parser.add_argument("--stats", action="store_true", help="打印图规模")
    args = parser.parse_args()

    setup_logging()

    if args.stats:
        async with AsyncSessionLocal() as db:
            data = await stats(db)
        print("图规模：", data)
        return

    if args.gc:
        async with AsyncSessionLocal() as db:
            removed = await gc_orphans(db)
            await db.commit()
        print(f"已清理孤儿实体：{removed}")
        return

    if not KG_ENABLED and not args.dry_run:
        print("KG_ENABLED=false，无法执行。请先在 .env 打开 KG_ENABLED 与 RERANK_ENABLED。")
        raise SystemExit(1)

    docs = await list_candidates(args.doc_id, args.force, args.limit)
    if not docs:
        print("没有待处理的文档（approved + index_status=ready + kg_status!=ready）。")
        return

    total_chunks = sum(d["chunks"] for d in docs)
    capped = sum(min(d["chunks"], KG_MAX_CHUNKS_PER_DOC) for d in docs)
    print(f"待处理文档 {len(docs)} 份，chunk 合计 {total_chunks}，"
          f"按单份上限 {KG_MAX_CHUNKS_PER_DOC} 预计实际抽取 {capped} 次 LLM 调用")
    for d in docs:
        print(f"  doc={d['id']} dept={d['department']} chunks={d['chunks']} kg={d['kg_status']}")

    if args.dry_run:
        print("\n--dry-run：未调用 LLM。")
        return

    started = time.perf_counter()
    ok = failed = 0
    for index, d in enumerate(docs, 1):
        t0 = time.perf_counter()
        try:
            await enqueue_and_build(d["id"])
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(docs)}] doc={d['id']} 失败 {type(exc).__name__}: {str(exc)[:160]}")
            continue
        ok += 1
        print(f"[{index}/{len(docs)}] doc={d['id']} 完成 耗时={time.perf_counter() - t0:.1f}s")

    print(
        f"\n回填结束：成功 {ok} 份，失败 {failed} 份，"
        f"总耗时 {time.perf_counter() - started:.1f}s"
    )
    async with AsyncSessionLocal() as db:
        print("图规模：", await stats(db))


if __name__ == "__main__":
    asyncio.run(main())
