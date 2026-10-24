"""图通道评测：三路消融 + 图独占命中率。

核心指标是 graph_only_rate ——
图通道召回的 chunk 里，有多少是向量 top20 和 BM25 top20 都没召到的。
这个数是 0，就说明图白做了：候选池里本来就有的东西，加个通道没有任何增量。

只看 Recall 会被扩池"骗"：池子变大后 Recall 通常会涨，但那可能只是
cross-encoder 多看了几十个候选，跟图没关系。graph_only 才是图的净贡献。

用法：
  python eval/run_graph.py --graph both
  python eval/run_graph.py --graph on --top-k 5
"""
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

DATASET = Path(__file__).resolve().parent / "graph_set.jsonl"
DEFAULT_REPORT = Path(__file__).resolve().parent / "results" / "graph_latest.json"


def load_items(path: Path) -> list[dict]:
    items = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _file_names(ids: set[int], cache: dict[int, str]) -> None:
    missing = [fid for fid in ids if fid and fid not in cache]
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
    _file_names({int(fid) for fid in ids if fid}, cache)
    return [
        cache.get(h["metadata"].get("file_id"))
        for h in hits
        if h["metadata"].get("type") != "image"
    ]


def _is_graph_only(hit: dict, vector_ids: set[str], bm25_ids: set[str]) -> bool:
    return (
        hit.get("_graph_hop") is not None
        and hit["id"] not in vector_ids
        and hit["id"] not in bm25_ids
    )


async def run_config(items: list[dict], graph_on: bool, top_k: int) -> dict:
    store_mod.RERANK_ENABLED = True     # 图通道的硬前提，见 config/settings.py 的 fail-fast
    store_mod.KG_ENABLED = graph_on
    cache: dict[int, str] = {}
    rows: list[dict] = []

    for item in items:
        started = time.perf_counter()
        hits, meta = await store.search(
            item["question"], department=item["department"], top_k=top_k, return_meta=True
        )
        selected = select_relevant_hits(hits, top_k=3)
        elapsed_ms = (time.perf_counter() - started) * 1000

        raw_names = _names(hits, cache)
        selected_names = _names(selected, cache)
        expected = item.get("expected_files", [])
        expected_set = set(expected)

        vector_ids = set(meta.get("vector_ids") or [])
        bm25_ids = set(meta.get("bm25_ids") or [])
        graph_hits = meta.get("graph_hits") or []
        graph_only = [h for h in graph_hits if _is_graph_only(h, vector_ids, bm25_ids)]
        graph_only_selected = [
            h for h in selected
            if _is_graph_only(h, vector_ids, bm25_ids)
        ]

        rows.append({
            "id": item["id"],
            "category": item["category"],
            "hops": item.get("hops", 1),
            "question": item["question"],
            "expected_files": expected,
            "raw_files": raw_names,
            "selected_files": selected_names,
            "hit_at_k": hit_at_k(raw_names, expected, top_k),
            "mrr": reciprocal_rank(raw_names, expected, top_k),
            "ndcg": ndcg_at_k(raw_names, expected, top_k),
            "selected_recall": bool(expected_set & {n for n in selected_names if n}),
            "keyword_hit": keyword_groups_ok(
                "\n".join(h.get("text", "") for h in selected),
                item.get("expected_keywords", []),
            ) if expected else False,
            "noise": (
                sum(1 for n in selected_names if n and n not in expected_set) / len(selected_names)
                if selected_names else 0.0
            ),
            "refused": len(selected) == 0,
            "expect_refusal": bool(item.get("expect_refusal")),
            "linked_entities": [e["name"] for e in (meta.get("linked_entities") or [])],
            "graph_hits": len(graph_hits),
            "graph_only": len(graph_only),
            "graph_only_selected": len(graph_only_selected),
            "pool_size": meta.get("pool_size", 0),
            "elapsed_ms": elapsed_ms,
        })
    return {"rows": rows}


def summarize(rows: list[dict], category: str | None, top_k: int) -> dict:
    subset = [r for r in rows if category is None or r["category"] == category]
    if not subset:
        return {"count": 0}
    n = len(subset)
    graph_hit_rows = [r for r in subset if r["graph_hits"] > 0]
    return {
        "count": n,
        f"recall@{top_k}": round(sum(r["hit_at_k"] for r in subset) / n, 4),
        "selected_recall": round(sum(r["selected_recall"] for r in subset) / n, 4),
        "keyword_rate": round(sum(r["keyword_hit"] for r in subset) / n, 4),
        "mrr": round(sum(r["mrr"] for r in subset) / n, 4),
        f"ndcg@{top_k}": round(sum(r["ndcg"] for r in subset) / n, 4),
        "avg_noise": round(sum(r["noise"] for r in subset) / n, 4),
        "graph_linked_rate": round(len(graph_hit_rows) / n, 4),
        "graph_hits_total": sum(r["graph_hits"] for r in subset),
        "graph_only_total": sum(r["graph_only"] for r in subset),
        "graph_only_selected_total": sum(r["graph_only_selected"] for r in subset),
        # 图独占率：分母是图召回总量，只统计真的召回过的题目
        "graph_only_rate": (
            round(sum(r["graph_only"] for r in graph_hit_rows)
                  / max(1, sum(r["graph_hits"] for r in graph_hit_rows)), 4)
            if graph_hit_rows else 0.0
        ),
        "p50_ms": percentile([r["elapsed_ms"] for r in subset], 50),
        "p95_ms": percentile([r["elapsed_ms"] for r in subset], 95),
    }


def summarize_refusal(rows: list[dict]) -> dict:
    subset = [r for r in rows if r["expect_refusal"]]
    if not subset:
        return {"count": 0}
    return {
        "count": len(subset),
        "refusal_rate": round(sum(r["refused"] for r in subset) / len(subset), 4),
        "false_positive": sum(1 for r in subset if not r["refused"]),
    }


def render_markdown(results: dict, top_k: int) -> str:
    lines = [
        "# 图通道消融评测",
        "",
        f"- 题集：`eval/graph_set.jsonl`（{results['meta']['count']} 题）",
        f"- top_k={top_k}，入选 top3，rerank 恒开（图通道的硬前提）",
        "- **graph_only_rate**：图召回中，向量 top20 与 BM25 top20 都未命中的比例。"
        "这是图的净贡献；为 0 说明图白做",
        "",
        "| 配置 | 多跳/聚合 Recall@k | 对照题 Recall@k | MRR | avg_noise | 拒答率 | graph_only_rate | 图独占进选中 | P95 ms |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for key, res in results["configs"].items():
        adv = res["advantage"]
        ctrl = res["control"]
        refusal = res["refusal"]
        lines.append(
            f"| {key} | {adv.get(f'recall@{top_k}')} | {ctrl.get(f'recall@{top_k}')} | "
            f"{adv.get('mrr')} | {adv.get('avg_noise')} | {refusal.get('refusal_rate')} | "
            f"{adv.get('graph_only_rate')} | {adv.get('graph_only_selected_total')} | "
            f"{adv.get('p95_ms')} |"
        )
    lines += [
        "",
        "> 复跑：`.venv\\Scripts\\python.exe eval\\run_graph.py --graph both`",
    ]
    return "\n".join(lines)


ADVANTAGE_CATEGORIES = ("aggregation", "indirect", "multihop")
CONTROL_CATEGORIES = ("control",)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", type=Path, default=DATASET)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--graph", choices=("both", "on", "off"), default="both")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    items = load_items(args.set)
    await store.warmup_bm25()

    graph_flags = (False, True) if args.graph == "both" else (args.graph == "on",)
    configs: dict[str, dict] = {}
    for flag in graph_flags:
        key = "graph_on" if flag else "graph_off"
        print(f"running {key} ...", flush=True)
        result = await run_config(items, flag, args.top_k)
        rows = result["rows"]
        adv_rows = [r for r in rows if r["category"] in ADVANTAGE_CATEGORIES]
        ctrl_rows = [r for r in rows if r["category"] in CONTROL_CATEGORIES]
        configs[key] = {
            "graph": flag,
            "advantage": summarize(adv_rows, None, args.top_k),
            "control": summarize(ctrl_rows, None, args.top_k),
            "refusal": summarize_refusal(rows),
            "by_category": {
                c: summarize(rows, c, args.top_k)
                for c in sorted({r["category"] for r in rows})
            },
            "items": rows,
        }
        print(
            f"{key}: adv={configs[key]['advantage']} ctrl={configs[key]['control']} "
            f"refusal={configs[key]['refusal']}",
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
    md_path = args.report.with_suffix(".md")
    md_path.write_text(render_markdown(results, args.top_k), encoding="utf-8")
    print(f"saved {args.report} and {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
