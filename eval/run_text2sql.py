"""Text2SQL 评测：自然语言 -> SQL -> 校验 -> 只读执行 -> 结果比对。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from services.text2sql import Text2SQLError, answer_question  # noqa: E402

DATASET = Path(__file__).resolve().parent / "text2sql_set.jsonl"


def load_items() -> list[dict]:
    return [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def normalize(rows: list[dict], ordered: bool) -> list[list[str]]:
    values = [
        [
            "" if value is None else str(value)
            for value in (row.values() if isinstance(row, dict) else row)
        ]
        for row in rows
    ]
    return values if ordered else sorted(values)


async def main(limit: int | None) -> int:
    items = load_items()
    if limit:
        items = items[:limit]
    passed = 0
    for item in items:
        try:
            result = await answer_question(
                item["question"], department="技术部", allow_all=True,
            )
            actual = normalize(result.rows, item.get("ordered", False))
            candidates = item.get("expected_any") or [item["expected"]]
            ok = any(
                actual == normalize(candidate, item.get("ordered", False))
                for candidate in candidates
            )
            detail = f"actual={actual}"
        except Text2SQLError as exc:
            ok = False
            detail = f"error={exc.code}:{exc.public_message}"
        except Exception as exc:
            ok = False
            detail = f"error={type(exc).__name__}:{exc}"
        passed += int(ok)
        print(f"{item['id']} {'PASS' if ok else 'FAIL'} {item['question']} {detail}")
    print(f"text2sql total={len(items)} passed={passed} failed={len(items) - passed}")
    return 1 if passed != len(items) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.limit)))
