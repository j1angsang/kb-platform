# RRF 代码逐行讲解

这份文档只讲这次改造涉及的代码，目标是让你能对着代码说清楚“每一行在干什么”。

涉及文件：

- `rag/fusion.py`：RRF 纯函数
- `rag/store.py`：检索、融合、门控
- `.env.example`：配置
- `eval/run_retrieval.py`：评测脚本

---

## 1. 先看全景

一次检索的流程：

```text
query
  ├─ 向量检索（pgvector）→ 文本候选 + 图片候选，按距离排序
  ├─ BM25 检索（内存索引）→ 文本候选，按 BM25 分排序
  ├─ 文本候选做 RRF（或 weighted）
  ├─ 可选 rerank（cross-encoder）
  ├─ select_relevant_hits 门控
  └─ 返回文本 + 图片
```

图片不参与 RRF：图片只有向量一路，RRF 需要两路名次；图片仍用原来的 `IMAGE_SCORE_THRESHOLD`。

---

## 2. `rag/fusion.py`：RRF 本体

### 2.1 公式

```text
rrf(d) = Σ_i  w_i / (k + rank_i(d))
```

- `rank_i(d)`：文档 d 在第 i 路结果里的名次，从 **1** 开始；
- `k`：平滑常量，常用 60；
- `w_i`：这一路的权重，默认等权。

### 2.2 核心代码

```python
def rrf_fuse(rankings, weights=None, k=60):
    if k < 1:
        raise ValueError(...)
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights 长度必须与 rankings 一致")

    scores = {}
    for ranking, weight in zip(rankings, weights):
        if weight <= 0:
            continue
        seen = set()
        for rank, doc_id in enumerate(ranking, start=1):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
    return scores
```

逐行解释：

1. `weights is None`：不传权重就等权，适合“两路一样可信”的场景；
2. `weight <= 0`：这一路不参与，直接跳过；
3. `seen`：同一路里重复出现的 id 只算第一次，避免重复加分；
4. `enumerate(ranking, start=1)`：名次从 1 开始，和公式一致；
5. `weight / (k + rank)`：名次越靠前，分数越大；`k` 越大，头部差距越小；
6. `scores.get(doc_id, 0.0) + ...`：多路命中会累加，这正是 RRF 的“共识”思想。

### 2.3 手算例子

两路结果：

```text
向量：["a", "b", "c"]
BM25：["b", "a", "d"]
k=60，权重 0.6 / 0.4
```

| 文档 | 向量贡献 | BM25 贡献 | 总分 |
|---|---|---|---|
| a | 0.6/61 = 0.009836 | 0.4/62 = 0.006452 | **0.016288** |
| b | 0.6/62 = 0.009677 | 0.4/61 = 0.006557 | **0.016235** |
| c | 0.6/63 = 0.009524 | 无 | 0.009524 |
| d | 无 | 0.4/63 = 0.006349 | 0.006349 |

a 在向量里第一、BM25 里第二；b 在 BM25 里第一、向量里第二。两者总分非常接近，但 a 略高——RRF 奖励“两路都靠前”的文档。

### 2.4 归一化

```python
def normalize_scores(scores):
    top = max(scores.values())
    return {doc_id: score / top for doc_id, score in scores.items()}
```

- 只用于展示/日志，让 `score` 落在 0~1；
- **不改变排序**；
- **不要拿归一化后的 RRF 分数做绝对阈值**，它没有“相关性”的物理含义。

---

## 3. `rag/store.py`：候选怎么来

### 3.1 向量候选

```python
text_rows = ...
image_rows = ...
vector_hits = [...]
text_vector_hits = [h for h in vector_hits if h["metadata"].get("type") != "image"]
image_hits = [h for h in vector_hits if h["metadata"].get("type") == "image"]
```

- 文本和图片分开：文本参与 RRF，图片不参与；
- 图片保留 `score = 1 - distance/2`，给 `IMAGE_SCORE_THRESHOLD` 用；
- 给图片补 `fusion_score=0`、`_relevance_score=score`，只是让字段统一，不参与融合。

### 3.2 BM25 候选

```python
bm25_hits = self.bm25.search(query, top_k=max(top_k * 4, 20)) if use_bm25 else []
bm25_scores = dict(bm25_hits)
max_bm = max(bm25_scores.values()) if bm25_scores else 0.0
```

- `bm25_hits` 是 `[(chunk_id, bm25_score), ...]`，已经按分数倒序；
- `max_bm` 只用于算 `_relevance_score` 的 BM25 归一，不再用于 RRF；
- 如果 `SEARCH_BM25_WEIGHT=0`，`use_bm25=False`，整条 BM25 链路跳过。

### 3.3 合并候选

```python
candidate_ids = list(vector_by_id.keys())
for cid, _ in bm25_hits:
    if cid not in vector_by_id:
        candidate_ids.append(cid)
```

- 先放向量候选，再补 BM25-only 候选；
- BM25-only 候选要做一次部门权限过滤：`_matches_department`，否则会把无权内容带进来；
- 每个候选都算 `_vector_score`、`_bm25_score`、`_bm25_norm`、`_relevance_score`。

### 3.4 `_relevance_score` 为什么不是 `max(vector, bm25)`

```python
if use_bm25 and bm > 0 and max_bm > 0:
    if v_score > 0:
        relevance_score = (VECTOR_WEIGHT * v_score + BM25_WEIGHT * bm_norm) / (VECTOR_WEIGHT + BM25_WEIGHT)
    else:
        relevance_score = bm_norm * 0.95
else:
    relevance_score = v_score
```

这里**沿用原来的加权分**，原因：

- `SEARCH_MIN_SCORE=0.80` / `SEARCH_MAX_DROP=0.015` 是按这个分数标定的；
- 如果改成 `max(vector, bm25)`，只要有一路 BM25 top1，`_relevance_score` 就是 1.0，门控 floor 会被抬到 0.985，反而误杀其他相关文档；
- 用原加权分，门控语义和改造前一致，RRF 只改排序。

注意：这里的 `relevance_score` 只用于门控，不再用于排序。

---

## 4. `rag/store.py`：RRF 分支

```python
if SEARCH_FUSION == "rrf":
    vector_ranking = [h["id"] for h in text_vector_hits]
    bm25_ranking = [cid for cid, _ in bm25_hits]
    rrf_scores = rrf_fuse(
        [vector_ranking, bm25_ranking],
        weights=[VECTOR_WEIGHT, BM25_WEIGHT],
        k=RRF_K,
    )
    visible_scores = {c["id"]: rrf_scores.get(c["id"], 0.0) for c in candidates}
    normalized = normalize_scores(visible_scores)
    for c in candidates:
        c["fusion_score"] = round(visible_scores[c["id"]], 6)
        c["score"] = round(normalized.get(c["id"], 0.0), 4)
    candidates.sort(key=lambda h: (h["fusion_score"], h["_relevance_score"]), reverse=True)
```

逐行解释：

1. `vector_ranking`：向量文本候选的 id，已经是距离升序，所以顺序就是名次；
2. `bm25_ranking`：BM25 候选 id，已经是分数降序；
3. `rrf_fuse`：两路排名 + 权重 + k，算出 `{id: rrf}`；
4. `visible_scores`：只保留当前候选，避免被权限过滤掉的 BM25 文档影响归一化；
5. `normalize_scores`：把 RRF 归一化到 0~1，给 `score` 展示；
6. `fusion_score`：原始 RRF 分数，排序和排查用；
7. 排序：先按 RRF，再按 `_relevance_score` 做 tie-break。

### 4.1 weighted 分支

```python
else:
    for c in candidates:
        ...
        c["score"] = round(fused_score, 4)
        c["fusion_score"] = c["score"]
    candidates.sort(key=lambda h: h["score"], reverse=True)
```

- 和改造前保持一致；
- `SEARCH_FUSION=weighted` 时行为回退；
- 这样你不用改数据库、不用改前端，就能 A/B 或线上回滚。

---

## 5. `select_relevant_hits`：门控

### 5.1 RRF 分支

```python
if SEARCH_FUSION == "rrf":
    ordered = sorted(hits, key=lambda h: h.get("fusion_score", 0.0), reverse=True)

    def _relevance(hit):
        return float(hit.get("_relevance_score", hit.get("score", 0.0)))

    top_relevance = _relevance(ordered[0])
    floor = max(SEARCH_MIN_SCORE, top_relevance - SEARCH_MAX_DROP)
    selected = [h for h in ordered if _relevance(h) >= floor][:top_k]
```

逐行解释：

1. 先按 RRF 排序，**不要按 `score` 重排**，否则会把 RRF 排序覆盖掉；
2. `_relevance` 取 `_relevance_score`；如果老数据没有这个字段，退回 `score`；
3. `top_relevance` 是本次候选里的最高相关性分；
4. `floor = max(0.80, top_relevance - 0.015)`，和改造前的语义一致；
5. `selected` 保留 RRF 顺序，只把相关性不够的过滤掉。

### 5.2 为什么先排序再过滤，而不是先过滤再排序

- 先排序：门控只决定“留不留”，不改变 RRF 的相对顺序；
- 先过滤：会改变“top_k 截断”的成员，可能把 RRF 认为更靠前的文档挤掉；
- 两种都能解释，但“先排序后过滤”更符合“RRF 负责排序、门控负责相关性”的分工。

### 5.3 rerank 优先级

`select_relevant_hits` 一进来先看 `rerank_score`：

```python
if any(h.get("rerank_fallback") for h in hits):
    return []
reranked = [h for h in hits if h.get("rerank_score") is not None]
if reranked:
    ...按 rerank_score 门控...
```

- 重排失败：直接拒答，不退回旧排序；
- 有重排分：用重排分门控，RRF 只决定候选顺序；
- 没有重排：才进 RRF/weighted 分支。

这解释了评测里的现象：**关掉 rerank，两种融合都是 0/5 拒绝；打开 rerank 才 5/5。**

---

## 6. 配置与日志

`.env`：

```ini
SEARCH_FUSION=rrf
RRF_K=60
SEARCH_MIN_SCORE=0.80
SEARCH_MAX_DROP=0.015
SEARCH_VECTOR_WEIGHT=0.6
SEARCH_BM25_WEIGHT=0.4
```

- `SEARCH_FUSION=rrf`：默认；
- `SEARCH_FUSION=weighted`：回滚；
- `SEARCH_VECTOR_WEIGHT` / `SEARCH_BM25_WEIGHT`：在 RRF 里是**名次权重**，不是分数权重；
- `SEARCH_MIN_SCORE` / `SEARCH_MAX_DROP`：RRF 模式下作用于 `_relevance_score`。

日志关键字段：

```text
rag.search ... fusion=rrf rrf_k=60 vector_hits=23 bm25_hits=2
  top_vector_score=... top_bm25_score=...
  top_fused_score=1.0 top_fusion_score=0.016393 top_relevance=0.9432
```

- `top_fusion_score`：原始 RRF 分数；
- `top_fused_score`：归一化后的展示分；
- `top_relevance`：门控分；
- 排查时先看 `fusion` / `rrf_k`，再看 `top_relevance` 和 `selected`。

---

## 7. 边界情况

| 情况 | 行为 |
|---|---|
| 候选为空 | 直接返回空；不会调用 RRF |
| 只有向量一路 | BM25 行为空，RRF 退化为向量排名；`weighted` 也一样 |
| 只有 BM25 一路 | 向量候选为空，BM25-only 候选仍需过部门过滤 |
| 某路权重为 0 | `rrf_fuse` 跳过该路；`use_bm25` 也会关掉 BM25 |
| 图片候选 | 不参与 RRF，保留向量分给图片阈值 |
| 重排失败 | `rerank_fallback=True` → 拒答，不退回旧排序 |
| 老数据没有 `_relevance_score` | `select_relevant_hits` 退回 `score` |
| 重复 id | `rrf_fuse` 只算第一次，避免重复加分 |

---

## 8. 如果你要改，改哪里

| 想改 | 位置 |
|---|---|
| 改 RRF 公式/k | `rag/fusion.py` 的 `rrf_fuse`；`RRF_K` |
| 改用 weighted | `.env` 的 `SEARCH_FUSION=weighted` |
| 改门控阈值 | `.env` 的 `SEARCH_MIN_SCORE` / `SEARCH_MAX_DROP` |
| 改 rerank 门控 | `RERANK_ENABLED` / `RERANK_MIN_SCORE` |
| 增加第三路检索 | 在 `store.search` 里多一个 ranking，传给 `rrf_fuse` 的 `rankings` 列表 |
| 改评测题 | `eval/eval_set.jsonl` |

---

## 9. 面试怎么讲（30 秒版）

> “我把混合检索从加权分数融合改成了 RRF。原来的问题是 BM25 用本次查询最大值归一，跨查询不稳定，而且融合分和门控阈值耦合。RRF 只看名次，不依赖分数量纲；但我没有把 RRF 分数直接套 0.80 阈值，而是把排序分、相关性分、重排分拆开：RRF 排序，`_relevance_score` 和 `rerank_score` 门控。评测集 21 题上 Recall@5 都是 1.0，没有证明 RRF 更强，但确认没有退化；同时发现无答案题只有开 rerank 才能拒答。这个改造的价值是可解释、可回滚、可 A/B，而不是一个漂亮的提升数字。”
