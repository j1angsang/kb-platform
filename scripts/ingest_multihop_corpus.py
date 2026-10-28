"""把 storage/corpus_multihop 下的多跳语料入库。

复用 seed_enterprise_data.ensure_doc 的完整流程（建 FileInfo / KnowledgeDoc、落对象存储、
交给 indexer 切片向量化），不手写 SQL —— 漏一个字段，后面建图和检索就拿不到正文。

这批文档全部 shared=True：它们是公司级制度，要能被任意部门检索到，
否则多跳测试会被部门隔离干扰（图通道同样受权限约束）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from config.db_conf import AsyncSessionLocal  # noqa: E402
from models import User  # noqa: E402
from models.index_job import IndexJob  # noqa: E402
from rag.indexer import index_document  # noqa: E402
from seed_enterprise_data import ensure_doc  # noqa: E402

# 语料随仓库走 eval/ 下，storage/ 是运行时对象存储目录（被 gitignore）
CORPUS_DIR = Path(__file__).resolve().parent.parent / "eval" / "corpus_multihop"
DEPARTMENT = "行政部"


async def main() -> None:
    files = sorted(CORPUS_DIR.glob("*.md"))
    if not files:
        print(f"没有找到语料：{CORPUS_DIR}")
        return

    async with AsyncSessionLocal() as db:
        owner = (
            await db.execute(select(User).where(User.role == "admin").order_by(User.id))
        ).scalars().first()
        if owner is None:
            raise SystemExit("没有 admin 账号，无法确定文档归属人")

        pending = []
        for path in files:
            content = path.read_text(encoding="utf-8")
            doc = await ensure_doc(db, owner, path.name, DEPARTMENT, content, shared=True)
            # index_document 只会在"已有 pending 任务"时开工，claim 不到就静默 return。
            # 只建 doc 不建 job 的话，脚本会打印"完成"但一个 chunk 都不写。
            existing_job = (
                await db.execute(select(IndexJob).where(IndexJob.doc_id == doc.id))
            ).scalars().first()
            if existing_job is None:
                db.add(IndexJob(doc_id=doc.id, state="pending"))
            elif existing_job.state in ("done", "failed"):
                existing_job.state = "pending"
                existing_job.last_error = None
            pending.append((doc.id, doc.index_status, path.name))
            print(f"准备 {path.name} -> doc={doc.id} status={doc.index_status}")
        await db.commit()

    for doc_id, status, name in pending:
        if status == "ready":
            print(f"跳过（已入库）{name}")
            continue
        await index_document(doc_id)
        print(f"入库完成 {name}")


if __name__ == "__main__":
    asyncio.run(main())
