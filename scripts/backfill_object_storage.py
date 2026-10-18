"""把已有本地文件回填到 MinIO/S3。"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from sqlalchemy import select  # noqa: E402

from config.db_conf import AsyncSessionLocal  # noqa: E402
from models import FileInfo  # noqa: E402
from services.object_storage import object_storage  # noqa: E402
from services.storage import STORAGE_DIR  # noqa: E402


async def run(limit: int | None, dry_run: bool) -> int:
    if not object_storage.enabled:
        print("STORAGE_BACKEND=local，未启用对象存储；先设置 STORAGE_BACKEND=s3 再执行。")
        return 0

    async with AsyncSessionLocal() as db:
        stmt = select(FileInfo).order_by(FileInfo.id)
        if limit:
            stmt = stmt.limit(limit)
        records = (await db.execute(stmt)).scalars().all()

    uploaded = skipped = missing = failed = 0
    for record in records:
        local_path = STORAGE_DIR / record.stored_path
        if not local_path.exists() or local_path.stat().st_size == 0:
            missing += 1
            print(f"missing file_id={record.id} stored_path={record.stored_path}")
            continue
        if dry_run:
            uploaded += 1
            print(f"would_upload file_id={record.id} stored_path={record.stored_path} bytes={local_path.stat().st_size}")
            continue
        try:
            await object_storage.upload_async(record.stored_path, local_path)
            uploaded += 1
        except Exception as exc:
            failed += 1
            print(f"failed file_id={record.id} stored_path={record.stored_path} error={type(exc).__name__}: {exc}")
        else:
            print(f"uploaded file_id={record.id} stored_path={record.stored_path}")

    print(
        f"done total={len(records)} uploaded={uploaded} "
        f"missing={missing} failed={failed} dry_run={dry_run}"
    )
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.limit, args.dry_run)))


if __name__ == "__main__":
    main()
