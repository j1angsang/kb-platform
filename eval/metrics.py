"""检索评测的纯函数指标，不依赖数据库和 LLM。"""
from __future__ import annotations

import math
from collections.abc import Sequence


def normalize(text: str) -> str:
    return "".join(str(text or "").split())


def keyword_groups_ok(text: str, groups: Sequence[str]) -> bool:
    """每一组至少命中一个候选词；组内用 | 分隔同义词。"""
    text = normalize(text)
    for group in groups or []:
        alternatives = [normalize(item) for item in str(group).split("|") if normalize(item)]
        if alternatives and not any(item in text for item in alternatives):
            return False
    return True


def hit_at_k(names: Sequence[str | None], expected: Sequence[str], k: int) -> bool:
    expected_set = set(expected)
    return any(name in expected_set for name in names[:k] if name)


def reciprocal_rank(names: Sequence[str | None], expected: Sequence[str], k: int) -> float:
    expected_set = set(expected)
    for rank, name in enumerate(names[:k], start=1):
        if name in expected_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(names: Sequence[str | None], expected: Sequence[str], k: int) -> float:
    expected_set = set(expected)
    gains = [1.0 if name in expected_set else 0.0 for name in names[:k]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(expected_set), k)))
    return dcg / ideal if ideal else 0.0


def percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((p / 100) * (len(ordered) - 1))))
    return round(ordered[index], 2)
