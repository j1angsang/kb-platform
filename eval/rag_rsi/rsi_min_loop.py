# -*- coding: utf-8 -*-
"""任务级 RSI 最小闭环（检索配置自我改进）。

流程：
1. 诊断 Agent：模型读取 dev 失败样本，输出 failure type / 原因；
2. 编辑 Agent：模型基于诊断生成白名单配置 diff（query expansion / min_score / top_k）；
3. dev 回归：没有提升、召回下降或核心类别下降 -> 拒绝；
4. test 门控：test 指标回退 -> 拒绝；
5. 通过 -> 写入 config.json + history，失败 -> 回滚到上一版。

边界：
- 编辑对象是检索配置，不是模型权重；
- 没有自动修改“改进方法本身”；
- 不是生产级 RSI，不上线；test 只做门控，不参与编辑。
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from datetime import datetime
from pathlib import Path

import run_loop as rl

BASE = Path(__file__).resolve().parent
WORK = BASE / "rsi_workspace"
HISTORY = WORK / "history"
RESULTS = BASE / "results"
CONFIG_PATH = WORK / "config.json"
MODEL = "qwen3.5:4b-q4_K_M"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def call_model(prompt: str, model: str, num_predict: int = 1200) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "format": "json",
        "think": False,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": 2048,
            "num_predict": num_predict,
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=240) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = data.get("response", "")
    if not raw.strip():
        raise RuntimeError(
            f"empty response; done_reason={data.get('done_reason')} eval={data.get('eval_count')}"
        )
    return raw


def parse_json(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in response: {raw[:400]!r}")
    return json.loads(raw[start:end + 1])


def fallback_diagnosis(rows: list[dict]) -> dict:
    types = []
    for row in rows[:12]:
        if row["expect_refusal"] and row["hits"]:
            kind = "false_positive"
        elif row["expected_files"] and row["rank"] is None:
            kind = "retrieval_miss"
        elif row["expected_files"] and row["rank"] > 1:
            kind = "ranking"
        else:
            kind = "other"
        types.append({"id": row["id"], "type": kind, "reason": "deterministic fallback"})
    return {"summary": "model diagnosis unavailable; used deterministic labels", "failure_types": types}


def diagnose(rows: list[dict], model: str) -> tuple[dict, str]:
    failures = [r for r in rows if r["score"] < 1.0][:12]
    compact = [
        {
            "id": r["id"],
            "question": r["question"],
            "category": r["category"],
            "expected": r["expected_files"],
            "expect_refusal": r["expect_refusal"],
            "top_hits": [{"file": h["filename"], "score": h["score"]} for h in r["hits"][:3]],
            "rank": r["rank"],
        }
        for r in failures
    ]
    prompt = f"""你是检索失败诊断器。只输出 JSON。
对每个失败样本判断类型：retrieval_miss（应命中却没命中）、false_positive（本应拒答却召回）、ranking（命中了但排太后）、permission（跨部门/权限边界）、other。
输出格式：
{{"summary":"一句话","failure_types":[{{"id":"...","type":"...","reason":"一句话"}}]}}

失败样本：
{json.dumps(compact, ensure_ascii=False)}"""
    try:
        raw = call_model(prompt, model)
        return parse_json(raw), raw
    except Exception as exc:
        print(f"diagnose fallback: {type(exc).__name__}: {exc}")
        return fallback_diagnosis(failures), ""


def normalize_config(data: dict, current: rl.Config) -> rl.Config | None:
    raw_syn = data.get("synonyms", current.synonyms)
    if not isinstance(raw_syn, dict):
        return None
    synonyms: dict[str, list[str]] = {}
    for trigger, terms in list(raw_syn.items())[:8]:
        if not isinstance(trigger, str) or not 1 <= len(trigger) <= 6:
            continue
        if re.search(r"[?？]|什么|怎么|如何|多少|多久|哪|吗|呢|吧", trigger):
            continue
        if not isinstance(terms, list):
            continue
        clean = [
            t.strip() for t in terms
            if isinstance(t, str) and 1 <= len(t.strip()) <= 12
        ][:4]
        if clean:
            synonyms[trigger] = clean
    try:
        min_score = round(float(data.get("min_score", current.min_score)), 2)
        top_k = int(data.get("top_k", current.top_k))
    except (TypeError, ValueError):
        return None
    return rl.Config(synonyms=synonyms, min_score=max(0.0, min(10.0, min_score)), top_k=max(3, min(8, top_k)))


def diff_config(old: rl.Config, new: rl.Config) -> dict:
    return {
        "synonyms_added": {k: v for k, v in new.synonyms.items() if old.synonyms.get(k) != v},
        "synonyms_removed": [k for k in old.synonyms if k not in new.synonyms],
        "min_score": {"from": old.min_score, "to": new.min_score},
        "top_k": {"from": old.top_k, "to": new.top_k},
    }


def edit_config(rows: list[dict], diagnosis: dict, current: rl.Config, rejected: list[dict], model: str) -> tuple[rl.Config | None, dict, str]:
    failures = [r for r in rows if r["score"] < 1.0][:10]
    prompt = f"""你是检索配置编辑器。只能改白名单字段，不能改业务逻辑、不能改代码、不能改评测集。
允许字段：
- synonyms: 对象，触发词 -> 最多4个扩展词；触发词必须是 1-6 个字的通用检索词，禁止完整问题、问号，以及“什么/怎么/如何/多少/多久/哪/吗/呢/吧”；
- min_score: 0..10 的相关性下限；
- top_k: 3..8。

诊断：
{json.dumps(diagnosis, ensure_ascii=False)}

当前配置：
{json.dumps(current.as_dict(), ensure_ascii=False)}

之前被拒绝的修改（不要重复）：
{json.dumps(rejected[-3:], ensure_ascii=False)}

失败样本：
{json.dumps([{"id": r["id"], "question": r["question"], "expected": r["expected_files"], "hits": [h["filename"] for h in r["hits"][:3]]} for r in failures], ensure_ascii=False)}

只输出 JSON，格式：
{{"synonyms":{{"触发词":["扩展词"]}},"min_score":3.0,"top_k":5,"rationale":"一句话"}}"""
    try:
        raw = call_model(prompt, model)
        data = parse_json(raw)
        candidate = normalize_config(data, current)
        if candidate is None:
            raise ValueError("invalid config shape")
        return candidate, data, raw
    except Exception as exc:
        print(f"edit failed: {type(exc).__name__}: {exc}")
        return None, {}, ""


def dev_gate(before: dict, after: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if after["quality"] <= before["quality"] + 0.001:
        reasons.append(f"dev quality 未提升（{before['quality']} -> {after['quality']}）")
    if after["recall_at_5"] + 0.01 < before["recall_at_5"]:
        reasons.append(f"dev Recall@5 下降（{before['recall_at_5']} -> {after['recall_at_5']}）")
    for category in ("knowledge", "lexical"):
        if after["by_category"].get(category, 0) + 0.01 < before["by_category"].get(category, 0):
            reasons.append(f"dev {category} 下降")
    return not reasons, reasons


def summarize(evaluation: dict) -> dict:
    return {
        "quality": evaluation["quality"],
        "recall_at_5": evaluation["recall_at_5"],
        "mrr": evaluation["mrr"],
        "refusal_accuracy": evaluation["refusal_accuracy"],
        "by_category": evaluation["by_category"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--reset", action="store_true", help="从 baseline 重新开始，忽略已有 config.json")
    args = parser.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    HISTORY.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    corpus = json.loads((BASE / "corpus.json").read_text(encoding="utf-8"))
    cases = rl.load_cases()
    dev = [c for c in cases if c["split"] == "dev"]
    test = [c for c in cases if c["split"] == "test"]
    indexes = rl.make_indexes(corpus)

    current = rl.Config()
    if CONFIG_PATH.exists() and not args.reset:
        try:
            current = rl.Config(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            current = rl.Config()
    if args.reset:
        CONFIG_PATH.write_text(json.dumps(current.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    dev_eval = rl.evaluate(dev, indexes, current)
    test_eval = rl.evaluate(test, indexes, current)
    print(f"start config={current.as_dict()} dev={dev_eval['quality']} test={test_eval['quality']}")

    history = []
    rejected: list[dict] = []
    for round_no in range(1, args.rounds + 1):
        print(f"\n=== round {round_no} ===")
        diagnosis, diagnosis_raw = diagnose(dev_eval["rows"], args.model)
        candidate, candidate_data, candidate_raw = edit_config(
            dev_eval["rows"], diagnosis, current, rejected, args.model
        )
        if candidate is None:
            item = {
                "round": round_no,
                "setup": "model edit failed",
                "diagnosis": diagnosis,
                "candidate": None,
                "decision": "rejected",
                "reasons": ["模型没有返回合法配置"],
            }
            history.append(item)
            rejected.append(item)
            print("candidate: invalid; rejected")
            continue

        candidate_dev = rl.evaluate(dev, indexes, candidate)
        dev_ok, dev_reasons = dev_gate(dev_eval, candidate_dev)
        item = {
            "round": round_no,
            "diagnosis": diagnosis,
            "diagnosis_raw": diagnosis_raw,
            "candidate": candidate_data,
            "candidate_config": candidate.as_dict(),
            "diff": diff_config(current, candidate),
            "dev_before": summarize(dev_eval),
            "dev_after": summarize(candidate_dev),
            "decision": "pending",
            "reasons": [],
        }
        if not dev_ok:
            item["decision"] = "rejected"
            item["reasons"] = dev_reasons
            history.append(item)
            rejected.append({"diff": item["diff"], "reasons": dev_reasons})
            print(f"candidate dev={candidate_dev['quality']} rejected: {dev_reasons}")
            continue

        candidate_test = rl.evaluate(test, indexes, candidate)
        test_ok, test_reasons = rl.gate(test_eval, candidate_test, candidate)
        item["test_after"] = summarize(candidate_test)
        item["test_gate"] = {"accepted": test_ok, "reasons": test_reasons}
        if not test_ok:
            item["decision"] = "rejected"
            item["reasons"] = test_reasons
            history.append(item)
            rejected.append({"diff": item["diff"], "reasons": test_reasons})
            print(f"candidate test={candidate_test['quality']} rejected: {test_reasons}")
            continue

        # 演示版采用自动审批；真实系统这里应停在 pending_approval，等待人工确认。
        item["decision"] = "accepted"
        item["approval"] = "auto_demo"
        item["reasons"] = ["dev 提升且 test 门控通过"]
        history.append(item)
        current = candidate
        dev_eval = candidate_dev
        test_eval = candidate_test
        CONFIG_PATH.write_text(json.dumps(current.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"candidate accepted; dev={dev_eval['quality']} test={test_eval['quality']}")

    final = {
        "model": args.model,
        "config": current.as_dict(),
        "dev": summarize(dev_eval),
        "test": summarize(test_eval),
        "rounds": history,
        "scope": "task-level retrieval self-improvement; not full RSI",
    }
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_json = RESULTS / f"rsi_min_{stamp}.json"
    out_json.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    (WORK / "history" / f"rounds-{stamp}.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 任务级 RSI 最小闭环",
        "",
        "> 诊断 Agent + 编辑 Agent + dev 回归 + test 门控 + 自动审批/回滚。",
        "> 编辑对象仅限检索配置；不是生产级 RSI，也没有自动上线。",
        "",
        f"- 模型：`{args.model}`",
        f"- 轮数：{len(history)}",
        f"- 最终配置：`{json.dumps(current.as_dict(), ensure_ascii=False)}`",
        f"- 最终 dev/test：{dev_eval['quality']}/{test_eval['quality']}",
        "",
        "## 每轮结果",
        "| 轮次 | 候选改动 | dev | test | 决策 | 原因 |",
        "|---|---|---:|---:|---|---|",
    ]
    for item in history:
        diff = item.get("diff") or {}
        cand = item.get("candidate_config") or {}
        summary = json.dumps(diff, ensure_ascii=False)
        if len(summary) > 100:
            summary = summary[:97] + "..."
        lines.append(
            f"| {item['round']} | `{summary}` | "
            f"{item.get('dev_after', {}).get('quality', '-')} | "
            f"{item.get('test_after', {}).get('quality', '-')} | "
            f"{item['decision']} | {'; '.join(item.get('reasons', []))} |"
        )
    lines += [
        "",
        "## 诊断摘要",
    ]
    for item in history:
        lines.append(f"- round {item['round']}: {item.get('diagnosis', {}).get('summary', '')}")
    lines += [
        "",
        "## 边界",
        "- 不是完整 RSI：没有递归改进“改进方法”，也没有自动改代码或上线；",
        "- test 只做门控，不参与编辑；",
        "- 真实生产需要人工审批、版本回滚、金丝雀发布和更严格的监控。",
    ]
    out_md = RESULTS / "rsi_min_report.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"saved {out_md}")
    print(f"saved {out_json}")


if __name__ == "__main__":
    main()
