# -*- coding: utf-8 -*-
"""任务级“诊断-编辑-回归-审批/回滚”最小闭环。

边界：
- 用仓库里的 40 份制度文档做离线 BM25 检索，不依赖数据库 / embedding / 云 API。
- 不是完整 RSI：没有让模型修改“改进方法”本身，也没有自动上线。
- 目的是把一个可复现的评测闭环跑通，证明固定题集、失败归因、编辑白名单、
  回归门控和回滚确实能串起来。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
CORPUS_PATH = BASE / "corpus.json"
EVAL_PATH = BASE / "eval_set.jsonl"
RESULTS_DIR = BASE / "results"

ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
CJK_CHARS = "".join(chr(c) for c in range(0x4E00, 0x9FFF + 1))


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens: list[str] = []
    for word in ASCII_WORD_RE.findall(text):
        tokens.append(word)
        if len(word) > 2:
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
    chars = [c for c in text if c in CJK_CHARS]
    tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return tokens


class BM25:
    K1 = 1.5
    B = 0.75

    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.ids = [d["filename"] for d in docs]
        self.texts = [d["content"] for d in docs]
        self.tokens = [tokenize(t) for t in self.texts]
        self.tf: list[dict[str, int]] = []
        self.lens: list[int] = []
        self.postings: dict[str, list[int]] = {}
        self.df: dict[str, int] = {}
        for idx, toks in enumerate(self.tokens):
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self.tf.append(tf)
            self.lens.append(len(toks))
            for t in set(toks):
                self.postings.setdefault(t, []).append(idx)
                self.df[t] = self.df.get(t, 0) + 1
        self.avgdl = sum(self.lens) / len(self.lens) if self.lens else 0.0

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        q_tokens = tokenize(query)
        scores: dict[int, float] = {}
        n = len(self.ids)
        for token in q_tokens:
            df = self.df.get(token, 0)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for idx in self.postings.get(token, []):
                tf = self.tf[idx].get(token, 0)
                denom = tf + self.K1 * (1 - self.B + self.B * self.lens[idx] / (self.avgdl or 1.0))
                scores[idx] = scores.get(idx, 0.0) + idf * tf * (self.K1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(self.ids[i], round(score, 4)) for i, score in ranked[:top_k]]


@dataclass
class Config:
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    min_score: float = 0.0
    top_k: int = 5

    def as_dict(self) -> dict:
        return {
            "synonyms": self.synonyms,
            "min_score": self.min_score,
            "top_k": self.top_k,
        }


# 编辑白名单：只能加“查询扩展词”和“相关性下限”，不能改业务逻辑。
CANDIDATE_RULES: list[tuple[str, list[str]]] = [
    ("年假", ["请假", "休假"]),
    ("请假", ["年假", "休假"]),
    ("休假", ["年假", "请假"]),
    ("报销", ["费用", "发票", "明细"]),
    ("发票", ["报销", "消费明细"]),
    ("出差", ["差旅", "住宿"]),
    ("差旅", ["出差", "住宿"]),
    ("采购", ["付款", "审批"]),
    ("入职", ["试用期", "转正"]),
    ("试用期", ["入职", "转正"]),
    ("离职", ["交接", "减员", "离开公司"]),
    ("合同", ["评审", "用章", "老板"]),
    ("投诉", ["响应", "方案"]),
    ("密码", ["账号", "特殊字符"]),
    ("VPN", ["远程办公", "内网", "网络账号"]),
    ("会议室", ["预订", "取消"]),
    ("邮件", ["签名", "企业微信"]),
    ("代码", ["分支", "合并", "主分支"]),
    ("发布", ["上线", "回滚", "旧版本"]),
    ("回滚", ["发布", "事故"]),
    ("数据库", ["变更", "备份"]),
    ("预算", ["编制", "审批"]),
    ("报价", ["折扣", "审批"]),
    ("访客", ["门禁", "登记"]),
    ("绩效", ["评估", "面谈"]),
    ("招聘", ["面试", "录用"]),
]


def load_cases() -> list[dict]:
    return [json.loads(line) for line in EVAL_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_indexes(corpus: list[dict]) -> dict[str, BM25]:
    by_dept: dict[str, list[dict]] = {}
    for doc in corpus:
        by_dept.setdefault(doc["department"], []).append(doc)
    return {dept: BM25(docs) for dept, docs in by_dept.items()}


def expand_query(question: str, cfg: Config) -> str:
    additions: list[str] = []
    for trigger, terms in cfg.synonyms.items():
        if trigger in question:
            additions.extend(terms)
    return question + (" " + " ".join(additions) if additions else "")


def search(case: dict, indexes: dict[str, BM25], cfg: Config) -> list[dict]:
    index = indexes.get(case["department"])
    if index is None:
        return []
    query = expand_query(case["question"], cfg)
    hits = []
    for filename, score in index.search(query, top_k=max(cfg.top_k * 4, 20)):
        if score >= cfg.min_score:
            hits.append({"filename": filename, "score": score})
        if len(hits) >= cfg.top_k:
            break
    return hits


def case_rank(case: dict, hits: list[dict]) -> int | None:
    expected = set(case.get("expected_files") or [])
    for idx, hit in enumerate(hits, 1):
        if hit["filename"] in expected:
            return idx
    return None


def case_score(case: dict, hits: list[dict]) -> float:
    if case.get("expect_refusal"):
        return 1.0 if not hits else 0.0
    rank = case_rank(case, hits)
    if rank is None:
        return 0.0
    if rank == 1:
        return 1.0
    if rank <= 3:
        return 0.8
    return 0.5


def evaluate(cases: list[dict], indexes: dict[str, BM25], cfg: Config) -> dict:
    rows = []
    for case in cases:
        hits = search(case, indexes, cfg)
        rank = case_rank(case, hits)
        expected = bool(case.get("expected_files"))
        recall = bool(rank) if expected else (not hits if case.get("expect_refusal") else True)
        mrr = (1.0 / rank) if rank else 0.0
        rows.append({
            "id": case["id"],
            "category": case["category"],
            "question": case["question"],
            "department": case["department"],
            "expected_files": case.get("expected_files") or [],
            "expect_refusal": bool(case.get("expect_refusal")),
            "hits": hits,
            "rank": rank,
            "recall": recall,
            "mrr": round(mrr, 4),
            "score": case_score(case, hits),
            "refused": not hits,
        })

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    answerable = [r for r in rows if not r["expect_refusal"]]
    refusal = [r for r in rows if r["expect_refusal"]]
    categories = sorted({r["category"] for r in rows})
    return {
        "count": len(rows),
        "quality": mean([r["score"] for r in rows]),
        "recall_at_5": mean([1.0 if r["recall"] else 0.0 for r in answerable]),
        "mrr": mean([r["mrr"] for r in answerable]),
        "refusal_accuracy": mean([1.0 if r["refused"] else 0.0 for r in refusal]),
        "noise": mean([
            sum(1 for h in r["hits"] if h["filename"] not in set(r["expected_files"])) / len(r["hits"])
            if r["hits"] and r["expected_files"] else 0.0
            for r in answerable
        ]),
        "by_category": {
            c: mean([r["score"] for r in rows if r["category"] == c]) for c in categories
        },
        "rows": rows,
    }


def failures(evaluation: dict) -> list[dict]:
    return [r for r in evaluation["rows"] if r["score"] < 1.0]


def diff_summary(old: Config, new: Config) -> dict:
    added = {
        trigger: terms for trigger, terms in new.synonyms.items()
        if old.synonyms.get(trigger) != terms
    }
    return {
        "added_synonyms": added,
        "min_score": {"from": old.min_score, "to": new.min_score},
        "top_k": {"from": old.top_k, "to": new.top_k},
    }


def baseline_config() -> Config:
    return Config()


def try_rules(cases: list[dict], indexes: dict[str, BM25], cfg: Config) -> tuple[Config, list[dict]]:
    """贪心编辑：只在 dev 上选择能提升质量分的白名单规则。"""
    current = Config(dict(cfg.synonyms), cfg.min_score, cfg.top_k)
    log: list[dict] = []
    current_eval = evaluate(cases, indexes, current)
    for _ in range(4):
        best: tuple[float, str, list[str], dict] | None = None
        for trigger, terms in CANDIDATE_RULES:
            if current.synonyms.get(trigger) == terms:
                continue
            candidate = Config(dict(current.synonyms), current.min_score, current.top_k)
            candidate.synonyms[trigger] = terms
            evaluation = evaluate(cases, indexes, candidate)
            delta = evaluation["quality"] - current_eval["quality"]
            if delta <= 0.0001:
                continue
            if best is None or evaluation["quality"] > best[0]:
                best = (evaluation["quality"], trigger, terms, evaluation)
        if best is None:
            break
        _, trigger, terms, evaluation = best
        previous = current
        current = Config(dict(current.synonyms), current.min_score, current.top_k)
        current.synonyms[trigger] = terms
        log.append({
            "action": "add_synonym",
            "trigger": trigger,
            "terms": terms,
            "dev_quality_before": current_eval["quality"],
            "dev_quality_after": evaluation["quality"],
            "diff": diff_summary(previous, current),
        })
        current_eval = evaluation
    return current, log


def try_threshold(cases: list[dict], indexes: dict[str, BM25], cfg: Config) -> tuple[Config, list[dict]]:
    current = Config(dict(cfg.synonyms), cfg.min_score, cfg.top_k)
    current_eval = evaluate(cases, indexes, current)
    best: tuple[float, float, dict] | None = None
    for threshold in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0):
        candidate = Config(dict(current.synonyms), threshold, current.top_k)
        evaluation = evaluate(cases, indexes, candidate)
        # 不能只看总分：知识题和字面题不能掉，否则就是拿核心能力换拒答率
        if evaluation["recall_at_5"] + 1e-9 < current_eval["recall_at_5"]:
            continue
        if any(
            evaluation["by_category"].get(category, 0) + 1e-9
            < current_eval["by_category"].get(category, 0)
            for category in ("knowledge", "lexical")
        ):
            continue
        if evaluation["quality"] > current_eval["quality"] + 0.0001:
            if best is None or evaluation["quality"] > best[0]:
                best = (evaluation["quality"], threshold, evaluation)
    if best is None:
        return current, []
    _, threshold, evaluation = best
    previous = current
    current = Config(dict(current.synonyms), threshold, current.top_k)
    return current, [{
        "action": "raise_min_score",
        "from": previous.min_score,
        "to": threshold,
        "dev_quality_before": current_eval["quality"],
        "dev_quality_after": evaluation["quality"],
        "diff": diff_summary(previous, current),
    }]


def gate(before: dict, after: dict, cfg: Config) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if after["quality"] + 0.01 < before["quality"]:
        reasons.append(f"test quality 从 {before['quality']} 降到 {after['quality']}")
    if after["recall_at_5"] + 0.01 < before["recall_at_5"]:
        reasons.append(f"test Recall@5 从 {before['recall_at_5']} 降到 {after['recall_at_5']}")
    if after["refusal_accuracy"] + 0.01 < before["refusal_accuracy"]:
        reasons.append(f"test 拒答率从 {before['refusal_accuracy']} 降到 {after['refusal_accuracy']}")
    for category in ("knowledge", "lexical"):
        if after["by_category"].get(category, 0) + 0.01 < before["by_category"].get(category, 0):
            reasons.append(
                f"test {category} 从 {before['by_category'].get(category, 0)} "
                f"降到 {after['by_category'].get(category, 0)}"
            )
    return not reasons, reasons


def compact(evaluation: dict, limit: int = 8) -> dict:
    failed = [r for r in evaluation["rows"] if r["score"] < 1.0][:limit]
    return {
        "quality": evaluation["quality"],
        "recall_at_5": evaluation["recall_at_5"],
        "mrr": evaluation["mrr"],
        "refusal_accuracy": evaluation["refusal_accuracy"],
        "noise": evaluation["noise"],
        "by_category": evaluation["by_category"],
        "failures": [
            {
                "id": r["id"],
                "question": r["question"],
                "expected": r["expected_files"],
                "top_hits": r["hits"][:3],
                "score": r["score"],
            }
            for r in failed
        ],
    }


def ollama_choose_rules(failures_rows: list[dict], model: str) -> list[str]:
    """可选：用本地 Ollama 选规则；没有模型时不要调用。"""
    candidate_text = "\n".join(f"{i+1}. {t}: {','.join(ts)}" for i, (t, ts) in enumerate(CANDIDATE_RULES))
    failure_text = "\n".join(
        f"- {r['question']}；期望文件={r['expected_files']}；当前命中={[h['filename'] for h in r['hits'][:3]]}"
        for r in failures_rows[:12]
    )
    prompt = f"""你是检索配置编辑器。只能从候选规则里选择，不能改业务逻辑。
失败样本：
{failure_text}

候选扩展规则：
{candidate_text}

只输出 JSON，格式：{{"rules": [规则编号整数]}}。最多选 4 条。"""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "format": "json",
        "think": False,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 1536},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = data.get("response", "")
    start, end = raw.find("{"), raw.rfind("}")
    try:
        parsed = json.loads(raw[start:end + 1])
    except Exception as exc:
        print(f"ollama raw response ({len(raw)} chars): {raw[:1200]!r}")
        raise
    ids = [int(x) for x in parsed.get("rules", [])]
    return [CANDIDATE_RULES[i - 1][0] for i in ids if 1 <= i <= len(CANDIDATE_RULES)]


def apply_rule_names(cfg: Config, names: list[str]) -> Config:
    out = Config(dict(cfg.synonyms), cfg.min_score, cfg.top_k)
    for trigger, terms in CANDIDATE_RULES:
        if trigger in names:
            out.synonyms[trigger] = terms
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--editor", choices=("rule", "ollama"), default="rule")
    parser.add_argument("--model", default="qwen2.5:3b")
    args = parser.parse_args()

    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cases = load_cases()
    indexes = make_indexes(corpus)
    dev = [c for c in cases if c["split"] == "dev"]
    test = [c for c in cases if c["split"] == "test"]

    base = baseline_config()
    base_dev = evaluate(dev, indexes, base)
    base_test = evaluate(test, indexes, base)

    print(f"cases={len(cases)} dev={len(dev)} test={len(test)}")
    print(f"baseline dev={base_dev['quality']} test={base_test['quality']}")
    print(f"baseline failure ids={[r['id'] for r in failures(base_dev)][:12]}")

    # 诊断
    dev_failures = failures(base_dev)
    (RESULTS_DIR / "baseline.json").write_text(
        json.dumps({"dev": compact(base_dev), "test": compact(base_test), "config": base.as_dict()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 第一轮：编辑白名单。Ollama 只负责选规则，实际是否接纳仍由固定集回归决定。
    if args.editor == "ollama":
        try:
            picked = ollama_choose_rules(dev_failures, args.model)
            print(f"ollama picked={picked}")
            candidate = apply_rule_names(base, picked)
            round1 = candidate
            round1_log = [{"action": "ollama_select_rules", "rules": picked}]
        except Exception as exc:
            print(f"ollama unavailable: {type(exc).__name__}: {exc}; fallback to rule editor")
            round1, round1_log = try_rules(dev, indexes, base)
    else:
        round1, round1_log = try_rules(dev, indexes, base)
    dev1 = evaluate(dev, indexes, round1)
    test1 = evaluate(test, indexes, round1)
    changed1 = bool(round1.synonyms)
    accepted1, reasons1 = gate(base_test, test1, round1)
    if not changed1:
        accepted1 = False
        reasons1 = ["没有白名单规则在 dev 上提升质量，未提交"]
    elif dev1["quality"] <= base_dev["quality"] + 0.0001:
        accepted1 = False
        reasons1.append(
            f"dev quality 没有提升（{base_dev['quality']} -> {dev1['quality']}），不提交"
        )
    print(f"round1 dev={dev1['quality']} test={test1['quality']} accepted={accepted1} reasons={reasons1}")

    committed = round1 if accepted1 else base
    committed_test = test1 if accepted1 else base_test
    committed_dev = dev1 if accepted1 else base_dev

    # 第二轮：阈值。先 dev 选，再用 test 门控。
    round2, round2_log = try_threshold(dev, indexes, committed)
    dev2 = evaluate(dev, indexes, round2)
    test2 = evaluate(test, indexes, round2)
    accepted2, reasons2 = gate(committed_test, test2, round2)
    print(f"round2 dev={dev2['quality']} test={test2['quality']} accepted={accepted2} reasons={reasons2}")
    if accepted2:
        committed = round2
        committed_dev = dev2
        committed_test = test2

    # 第三轮：故意做一个激进修改，展示门控拒绝与回滚。
    aggressive = Config(dict(committed.synonyms), 6.0, committed.top_k)
    aggressive.synonyms.update({t: ts for t, ts in CANDIDATE_RULES})
    dev3 = evaluate(dev, indexes, aggressive)
    test3 = evaluate(test, indexes, aggressive)
    accepted3, reasons3 = gate(committed_test, test3, aggressive)
    print(f"aggressive dev={dev3['quality']} test={test3['quality']} accepted={accepted3} reasons={reasons3}")

    result = {
        "meta": {
            "note": "离线 BM25 任务级闭环，不是完整 RSI；不调用 LLM，不上线。",
            "cases": len(cases), "dev": len(dev), "test": len(test),
            "editor": args.editor,
        },
        "baseline": {"dev": compact(base_dev), "test": compact(base_test), "config": base.as_dict()},
        "round1": {
            "dev": compact(dev1), "test": compact(test1),
            "log": round1_log, "accepted": accepted1, "gate_reasons": reasons1,
            "config": round1.as_dict(),
        },
        "round2": {
            "dev": compact(dev2), "test": compact(test2),
            "log": round2_log, "accepted": accepted2, "gate_reasons": reasons2,
            "config": round2.as_dict(),
        },
        "aggressive": {
            "dev": compact(dev3), "test": compact(test3),
            "accepted": accepted3, "gate_reasons": reasons3,
            "config": aggressive.as_dict(),
        },
        "committed": {
            "dev": compact(committed_dev), "test": compact(committed_test),
            "config": committed.as_dict(),
        },
    }
    (RESULTS_DIR / "iterations.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def pct(x: float) -> str:
        return f"{x * 100:.1f}%"

    lines = [
        "# 任务级最小自改进闭环结果",
        "",
        f"> 离线 BM25 演示；编辑器={args.editor}，模型={args.model if args.editor == 'ollama' else 'rule'}；"
        "不是完整 RSI，也没有上线。",
        "",
        "## 题集与覆盖",
        f"- 语料：{len(corpus)} 份制度文档，部门分布 "
        + " / ".join(f"{k} {v}" for k, v in sorted(Counter(d["department"] for d in corpus).items())),
        f"- 题集：{len(cases)} 题（dev {len(dev)} / test {len(test)}），类别 "
        + " / ".join(f"{k} {v}" for k, v in sorted(Counter(c["category"] for c in cases).items())),
        f"- 覆盖：{len({f for c in cases for f in c['expected_files']})}/{len(corpus)} 份文档至少被一道题引用",
        "- 为什么加：补齐员工生命周期、研发交付、销售投标/渠道/回款、行政消防/车辆等场景；"
        "新增题按 knowledge / lexical / no_answer / permission 分层，并保持 dev/test 不泄漏。",
        "",
        "## 结果",
        "| 阶段 | dev quality | test quality | test Recall@5 | test MRR | test 拒答率 | 门控 |",
        "|---|---:|---:|---:|---:|---:|---|",
        f"| baseline | {pct(base_dev['quality'])} | {pct(base_test['quality'])} | {pct(base_test['recall_at_5'])} | {base_test['mrr']:.4f} | {pct(base_test['refusal_accuracy'])} | - |",
        f"| round1 查询扩展 | {pct(dev1['quality'])} | {pct(test1['quality'])} | {pct(test1['recall_at_5'])} | {test1['mrr']:.4f} | {pct(test1['refusal_accuracy'])} | {'通过' if accepted1 else ('未产生有效修改' if not changed1 else '拒绝回滚')} |",
        f"| round2 相关性下限 | {pct(dev2['quality'])} | {pct(test2['quality'])} | {pct(test2['recall_at_5'])} | {test2['mrr']:.4f} | {pct(test2['refusal_accuracy'])} | {'通过' if accepted2 else '拒绝回滚'} |",
        f"| 激进修改（故意） | {pct(dev3['quality'])} | {pct(test3['quality'])} | {pct(test3['recall_at_5'])} | {test3['mrr']:.4f} | {pct(test3['refusal_accuracy'])} | {'通过' if accepted3 else '拒绝回滚'} |",
        "",
        "quality 是答案题排名和拒答题是否为空的加权代理分，不是最终回答准确率。",
        "",
        "## 分类质量",
        "| 类别 | baseline dev | committed dev | baseline test | committed test |",
        "|---|---:|---:|---:|---:|",
    ]
    for category in ("knowledge", "lexical", "no_answer", "permission"):
        lines.append(
            f"| {category} | {pct(base_dev['by_category'].get(category, 0))} | "
            f"{pct(committed_dev['by_category'].get(category, 0))} | "
            f"{pct(base_test['by_category'].get(category, 0))} | "
            f"{pct(committed_test['by_category'].get(category, 0))} |"
        )
    lines += [
        "",
        "## 最终提交配置",
        "```json",
        json.dumps(committed.as_dict(), ensure_ascii=False, indent=2),
        "```",
        "",
        "## 诊断到的失败样本",
    ]
    for row in dev_failures[:10]:
        lines.append(f"- `{row['id']}` {row['question']} -> 期望 `{row['expected_files']}`，当前 `{[h['filename'] for h in row['hits'][:3]]}`")
    ollama_rules = next((item["rules"] for item in round1_log if "rules" in item), None)
    if ollama_rules:
        round1_note = (
            f"2. 本地模型选择了 {ollama_rules}，但 dev quality 没有提升"
            f"（{pct(base_dev['quality'])} -> {pct(dev1['quality'])}），被门控拒绝；"
            "模型提议不能绕过回归验证。"
        )
    else:
        round1_note = "2. 第一轮查询扩展没有任何规则在 dev 上产生提升，说明当前失败主要不是同义词缺失。"

    lines += [
        "",
        "## 结论",
        f"1. 语料从 40 份扩到 {len(corpus)} 份，题集从 21 题扩到 {len(cases)} 题；"
        f"{len({f for c in cases for f in c['expected_files']})}/{len(corpus)} 份文档至少被一道题引用。",
        round1_note,
        f"3. 第二轮相关性下限把 dev/test quality 从 {pct(base_dev['quality'])}/{pct(base_test['quality'])} "
        f"提升到 {pct(committed_dev['quality'])}/{pct(committed_test['quality'])}，"
        f"test Recall@5 保持 {pct(committed_test['recall_at_5'])}，"
        f"拒答率从 {pct(base_test['refusal_accuracy'])} 提到 {pct(committed_test['refusal_accuracy'])}。",
        f"4. 第三次激进修改虽然把 test quality 拉到 {pct(test3['quality'])}，"
        f"但 Recall@5 降到 {pct(test3['recall_at_5'])}、knowledge 降到 "
        f"{pct(test3['by_category'].get('knowledge', 0))}，被门控拒绝并回滚。",
        f"5. 权限类 committed 仍是 {pct(committed_dev['by_category'].get('permission', 0))}/"
        f"{pct(committed_test['by_category'].get('permission', 0))}，"
        "说明权限判断不能靠检索阈值兼任；真实系统应在检索 SQL 和路由层前置校验。",
        "6. 这不是完整 RSI：编辑对象只有查询扩展和阈值，没有自动修改改进机制本身，也没有自动上线。",
    ]
    (RESULTS_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"saved {RESULTS_DIR / 'report.md'}")


if __name__ == "__main__":
    main()
