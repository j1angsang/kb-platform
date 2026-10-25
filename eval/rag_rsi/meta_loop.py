# -*- coding: utf-8 -*-
"""RAG 评测的外层 meta-loop。

内层（run_loop.py）：给定一种改进策略，调出检索配置。
外层（本文件）：比较多种“改进策略”，用 meta-train 拟合、meta-val 选择，
最后只在 test 上做一次审计。

边界：
- 这是有限的 meta-policy selection，不是开放式 RSI；
- 不改代码，不上线；策略空间是固定的三种。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import run_loop as rl

BASE = Path(__file__).resolve().parent
RESULTS = BASE / "results"
POLICY = BASE / "meta_policy.json"


def make_folds(dev: list[dict], k: int = 3) -> list[tuple[list[dict], list[dict]]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for case in dev:
        groups[case["category"]].append(case)
    buckets: list[list[dict]] = [[] for _ in range(k)]
    for group in groups.values():
        for i, case in enumerate(group):
            buckets[i % k].append(case)
    folds = []
    for i in range(k):
        val = buckets[i]
        train = [case for j, bucket in enumerate(buckets) if j != i for case in bucket]
        folds.append((train, val))
    return folds


def complexity(synonyms: dict[str, list[str]]) -> int:
    return sum(len(v) for v in synonyms.values())


def fit(synonyms: dict[str, list[str]], meta_train: list[dict], indexes) -> tuple[rl.Config, dict]:
    base_cfg = rl.Config(dict(synonyms), 0.0, 5)
    base = rl.evaluate(meta_train, indexes, base_cfg)
    best: tuple[tuple, rl.Config, dict] | None = None
    for threshold in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0):
        cfg = rl.Config(dict(synonyms), threshold, 5)
        ev = rl.evaluate(meta_train, indexes, cfg)
        if ev["recall_at_5"] + 0.01 < base["recall_at_5"]:
            continue
        if any(
            ev["by_category"].get(category, 0) + 0.01
            < base["by_category"].get(category, 0)
            for category in ("knowledge", "lexical")
        ):
            continue
        key = (ev["quality"], -complexity(synonyms), -threshold)
        if best is None or key > best[0]:
            best = (key, cfg, ev)
    assert best is not None
    return best[1], best[2]


def meta_strategies() -> dict[str, dict[str, list[str]]]:
    return {
        "threshold_only": {},
        "generic_synonyms": {
            "何时": ["什么时候"],
            "几点": ["什么时间"],
        },
        "domain_synonyms": {
            "年假": ["请假", "休假"],
            "报销": ["费用", "发票"],
            "出差": ["差旅", "住宿"],
            "采购": ["付款", "审批"],
            "入职": ["试用期", "转正"],
            "离职": ["交接", "减员"],
            "发布": ["上线", "回滚"],
            "数据库": ["变更", "备份"],
            "访客": ["门禁", "登记"],
            "绩效": ["评估", "面谈"],
        },
    }


def main() -> None:
    corpus = json.loads((BASE / "corpus.json").read_text(encoding="utf-8"))
    cases = rl.load_cases()
    dev = [c for c in cases if c["split"] == "dev"]
    test = [c for c in cases if c["split"] == "test"]
    folds = make_folds(dev, 3)
    indexes = rl.make_indexes(corpus)
    base_test = rl.evaluate(test, indexes, rl.Config())

    rows = []
    fitted = []
    for name, synonyms in meta_strategies().items():
        fold_scores = []
        for train, val in folds:
            cfg, _ = fit(synonyms, train, indexes)
            fold_scores.append(rl.evaluate(val, indexes, cfg)["quality"])
        full_cfg, full_train = fit(synonyms, dev, indexes)
        mean_val = round(sum(fold_scores) / len(fold_scores), 4)
        meta_score = round(mean_val - 0.005 * complexity(synonyms), 4)
        rows.append({
            "strategy": name,
            "synonyms_count": complexity(synonyms),
            "config": full_cfg.as_dict(),
            "train_quality": full_train["quality"],
            "fold_scores": fold_scores,
            "meta_val_mean": mean_val,
            "meta_score": meta_score,
        })
        fitted.append((name, full_cfg, mean_val, meta_score))

    name, chosen, chosen_val, _ = max(
        fitted,
        key=lambda item: (
            item[3],
            -complexity(item[1].synonyms),
            -item[1].min_score,
        ),
    )
    chosen_test = rl.evaluate(test, indexes, chosen)
    test_ok, test_reasons = rl.gate(base_test, chosen_test, chosen)
    committed = chosen if test_ok else rl.Config()
    committed_test = chosen_test if test_ok else base_test

    report = {
        "meta": {
            "case_count": len(cases), "dev": len(dev), "test": len(test),
            "folds": len(folds),
        },
        "baseline": {"test": base_test["quality"]},
        "strategies": rows,
        "selected": {
            "strategy": name,
            "config": chosen.as_dict(),
            "meta_val_mean": chosen_val,
            "test_quality": chosen_test["quality"],
            "test_gate": {"accepted": test_ok, "reasons": test_reasons},
        },
        "committed_config": committed.as_dict(),
    }
    POLICY.write_text(json.dumps(committed.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTS / "meta_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# RAG 外层 meta-loop",
        "",
        "> 内层调检索配置，外层比较“怎么调”的策略；3 折 meta-validation 选择，test 只审计。",
        "",
        f"- 数据：{len(cases)} 题（dev {len(dev)} / test {len(test)}；3 折 meta CV）",
        "",
        "| 策略 | 同义词数 | 各折 meta-val | 均值 | meta-score | 选中 |",
        "|---|---:|---|---:|---:|---|",
    ]
    for row in rows:
        folds_text = " / ".join(str(x) for x in row["fold_scores"])
        lines.append(
            f"| {row['strategy']} | {row['synonyms_count']} | {folds_text} | "
            f"{row['meta_val_mean']} | {row['meta_score']} | "
            f"{'是' if row['strategy'] == name else ''} |"
        )
    lines += [
        "",
        "## 选择结果",
        f"- 选中策略：`{name}`",
        f"- 配置：`{json.dumps(chosen.as_dict(), ensure_ascii=False)}`",
        f"- meta-val 均值：{chosen_val}",
        f"- test：{chosen_test['quality']}（gate={'通过' if test_ok else '拒绝，回退 baseline'}）",
        f"- 最终配置：`{json.dumps(committed.as_dict(), ensure_ascii=False)}`",
        "",
        "## 边界",
        "- 策略空间是固定的 3 种，不是开放式自我改写；",
        "- 3 折 meta-val 选策略，test 只做最终审计；",
        "- 不自动上线。",
    ]
    (RESULTS / "meta_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"cases={len(cases)} folds={len(folds)}")
    print(f"selected={name} meta_val={chosen_val} test={chosen_test['quality']} gate={test_ok}")
    print(f"saved {RESULTS / 'meta_report.md'}")


if __name__ == "__main__":
    main()
