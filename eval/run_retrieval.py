"""检索层评测：直接跑 store.search + select_relevant_hits，不调用 LLM。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from sqlalchemy import select  # noqa: E402

import rag.store as store_mod  # noqa: E402
from config.db_conf import create_sync_engine  # noqa: E402
from eval.metrics import (  # noqa: E402
    hit_at_k,
    keyword_groups_ok,
    ndcg_at_k,
    percentile,
    reciprocal_rank,
)
from models import FileInfo  # noqa: E402
from rag.store import select_relevant_hits, store  # noqa: E402

DATASET = Path(__file__).resolve().parent / "eval_set.jsonl"
DEFAULT_REPORT = Path(__file__).resolve().parent / "results" / "latest.json"


def load_items(path: Path) -> list[dict]:
    items = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _file_names(ids: set[int], cache: dict[int, str]) -> None:
    missing = [file_id for file_id in ids if file_id and file_id not in cache]
    if not missing:
        return
    engine = create_sync_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(FileInfo.id, FileInfo.filename).where(FileInfo.id.in_(missing))
            ).all()
        cache.update({row[0]: row[1] for row in rows})
    finally:
        engine.dispose()


def _names(hits: list[dict], cache: dict[int, str]) -> list[str | None]:
    ids = {h["metadata"].get("file_id") for h in hits if h["metadata"].get("type") != "image"}
    _file_names({int(file_id) for file_id in ids if file_id}, cache)
    return [cache.get(h["metadata"].get("file_id")) for h in hits if h["metadata"].get("type") != "image"]


async def run_config(items: list[dict], fusion: str, rerank: bool, top_k: int) -> dict:
    store_mod.SEARCH_FUSION = fusion
    store_mod.RERANK_ENABLED = rerank
    cache: dict[int, str] = {}
    rows: list[dict] = []
    for item in items:
        started = time.perf_counter()
        hits = await store.search(item["question"], department=item["department"], top_k=top_k)
        selected = select_relevant_hits(hits, top_k=3)
        elapsed_ms = (time.perf_counter() - started) * 1000
        raw_names = _names(hits, cache)
        selected_names = _names(selected, cache)
        expected = item.get("expected_files", [])
        expected_set = set(expected)
        selected_hit = bool(expected_set & {name for name in selected_names if name})
        rows.append({
            "id": item["id"],
            "category": item["category"],
            "question": item["question"],
            "expected_files": expected,
            "raw_files": raw_names,
            "selected_files": selected_names,
            "hit_at_k": hit_at_k(raw_names, expected, top_k),
            "mrr": reciprocal_rank(raw_names, expected, top_k),
            "ndcg": ndcg_at_k(raw_names, expected, top_k),
            "selected_recall": selected_hit,
            "keyword_hit": keyword_groups_ok(
                "\n".join(hit.get("text", "") for hit in selected),
                item.get("expected_keywords", []),
            ) if expected else False,
            "noise": (
                sum(1 for name in selected_names if name and name not in expected_set) / len(selected_names)
                if selected_names else 0.0
            ),
            "refused": len(selected) == 0,
            "expect_refusal": bool(item.get("expect_refusal")),
            "elapsed_ms": elapsed_ms,
        })
    return rows


def summarize(rows: list[dict], category: str, top_k: int) -> dict:
    subset = [row for row in rows if row["category"] == category]
    if not subset:
        return {"count": 0}
    count = len(subset)
    return {
        "count": count,
        f"recall@{top_k}": round(sum(row["hit_at_k"] for row in subset) / count, 4),
        "selected_recall": round(sum(row["selected_recall"] for row in subset) / count, 4),
        "keyword_rate": round(sum(row["keyword_hit"] for row in subset) / count, 4),
        "mrr": round(sum(row["mrr"] for row in subset) / count, 4),
        f"ndcg@{top_k}": round(sum(row["ndcg"] for row in subset) / count, 4),
        "avg_noise": round(sum(row["noise"] for row in subset) / count, 4),
        "p50_ms": percentile([row["elapsed_ms"] for row in subset], 50),
        "p95_ms": percentile([row["elapsed_ms"] for row in subset], 95),
    }


def summarize_refusal(rows: list[dict]) -> dict:
    subset = [row for row in rows if row["expect_refusal"]]
    if not subset:
        return {"count": 0}
    count = len(subset)
    return {
        "count": count,
        "refusal_rate": round(sum(row["refused"] for row in subset) / count, 4),
        "false_positive": sum(1 for row in subset if not row["refused"]),
    }


def render_markdown(results: dict, top_k: int) -> str:
    lines = [
        "# 检索评测结果",
        "",
        f"- 评测题集：`eval/eval_set.jsonl`（{results['meta']['count']} 题）",
        f"- top_k：{top_k}；入选 top3；rerank 阈值取 `.env` 的 `RERANK_MIN_SCORE`",
        "- 延迟包含 embedding / 数据库 / rerank，**不要把它当成融合算法的纯耗时**",
        "",
        "| 配置 | 知识题 Recall@k | 知识题 MRR | 字面题 Recall@k | 字面题 MRR | 无答案拒绝率 | P95 ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for key, result in results["configs"].items():
        knowledge = result["knowledge"]
        lexical = result["lexical"]
        refusal = result["refusal"]
        lines.append(
            f"| {key} | {knowledge.get(f'recall@{top_k}')} | {knowledge.get('mrr')} | "
            f"{lexical.get(f'recall@{top_k}')} | {lexical.get('mrr')} | "
            f"{refusal.get('refusal_rate')} | {knowledge.get('p95_ms')} |"
        )
    lines += [
        "",
        "> 复跑：`.venv\\Scripts\\python.exe eval\\run_retrieval.py --fusion both --rerank both`",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", type=Path, default=DATASET)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--fusion", choices=("both", "weighted", "rrf"), default="both")
    parser.add_argument("--rerank", choices=("both", "on", "off"), default="both")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    items = load_items(args.set)
    await store.warmup_bm25()

    fusions = ("weighted", "rrf") if args.fusion == "both" else (args.fusion,)
    reranks = (False, True) if args.rerank == "both" else (args.rerank == "on",)
    configs: dict[str, dict] = {}
    for fusion in fusions:
        for rerank in reranks:
            key = f"{fusion}_{'rerank_on' if rerank else 'rerank_off'}"
            print(f"running {key} ...", flush=True)
            rows = await run_config(items, fusion, rerank, args.top_k)
            configs[key] = {
                "fusion": fusion,
                "rerank": rerank,
                "knowledge": summarize(rows, "knowledge", args.top_k),
                "lexical": summarize(rows, "lexical", args.top_k),
                "refusal": summarize_refusal(rows),
                "items": rows,
            }
            print(
                f"{key}: knowledge={configs[key]['knowledge']} "
                f"lexical={configs[key]['lexical']} refusal={configs[key]['refusal']}",
                flush=True,
            )

    meta = {
        "count": len(items),
        "dataset": str(args.set.relative_to(ROOT)) if str(args.set).startswith(str(ROOT)) else str(args.set),
        "top_k": args.top_k,
    }
    results = {"meta": meta, "configs": configs}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path = args.report.with_suffix(".md")
    markdown_path.write_text(render_markdown(results, args.top_k), encoding="utf-8")
    print(f"saved {args.report} and {markdown_path}")


if __name__ == "__main__":
    asyncio.run(main())
