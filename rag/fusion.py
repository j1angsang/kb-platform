"""倒数排名融合（RRF）。"""
from __future__ import annotations

from collections.abc import Sequence

DEFAULT_RRF_K = 60


def rrf_fuse(
    rankings: Sequence[Sequence[str]],
    weights: Sequence[float] | None = None,
    k: int = DEFAULT_RRF_K,
) -> dict[str, float]:
    """把多路有序结果融合成 ``{doc_id: score}``。"""
    if k < 1:
        raise ValueError(f"RRF k 必须 >= 1，当前 {k}")
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights 长度必须与 rankings 一致")

    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        if weight <= 0:
            continue
        seen: set[str] = set()
        for rank, doc_id in enumerate(ranking, start=1):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
    return scores


def normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    """把 RRF 分数按最大值归一化到 0~1，仅用于展示。"""
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {doc_id: 0.0 for doc_id in scores}
    return {doc_id: score / top for doc_id, score in scores.items()}
