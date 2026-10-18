import asyncio
import logging
import os
import time
from pathlib import Path

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.db_conf import AsyncSessionLocal
from models.knowledge_chunk import KnowledgeChunk
from rag.bm25 import BM25Index
from rag.embeddings import embed_image, embed_query, embed_texts
from rag.fusion import normalize_scores, rrf_fuse
from rag.rerank import RERANK_ENABLED, RERANK_MIN_SCORE, RERANK_TOP_N, rerank
from services.storage import STORAGE_DIR


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return float(default)
    return float(raw)


SEARCH_MIN_SCORE = _env_float("SEARCH_MIN_SCORE", 0.80)
SEARCH_MAX_DROP = _env_float("SEARCH_MAX_DROP", 0.015)
VECTOR_WEIGHT = _env_float("SEARCH_VECTOR_WEIGHT", 0.6)
BM25_WEIGHT = _env_float("SEARCH_BM25_WEIGHT", 0.4)
# 图片命中阈值 0.58 由图片标注集标定，用归一化 score
IMAGE_SCORE_THRESHOLD = _env_float("SEARCH_IMAGE_SCORE_THRESHOLD", 0.58)
IMAGE_CANDIDATE_TOP_K = max(int(os.getenv("SEARCH_IMAGE_CANDIDATE_TOP_K", "8")), 1)

if VECTOR_WEIGHT < 0 or BM25_WEIGHT < 0 or (VECTOR_WEIGHT + BM25_WEIGHT) <= 0:
    raise RuntimeError(
        "SEARCH_VECTOR_WEIGHT / SEARCH_BM25_WEIGHT 必须非负且不能同时为 0，"
        f"当前 vector={VECTOR_WEIGHT} bm25={BM25_WEIGHT}"
    )

SEARCH_FUSION = (os.getenv("SEARCH_FUSION") or "rrf").strip().lower()
if SEARCH_FUSION not in ("rrf", "weighted"):
    raise RuntimeError(
        f"SEARCH_FUSION 非法：{SEARCH_FUSION!r}，只允许 rrf|weighted"
    )
RRF_K = int(os.getenv("RRF_K", "60"))
if RRF_K < 1:
    raise RuntimeError(f"RRF_K 必须 >= 1，当前 {RRF_K}")

logger = logging.getLogger(__name__)


def select_relevant_hits(hits: list[dict], top_k: int = 3) -> list[dict]:
    started = time.perf_counter()
    hits = [h for h in hits if h.get("metadata", {}).get("type") != "image"]
    # 重排失败 = 本轮不注入知识，不能退回旧排序放行
    if any(h.get("rerank_fallback") for h in hits):
        logger.warning(
            "rag.select failsafe reason=rerank_fallback hits=%d selected=0 检索层本轮不可门控，拒答",
            len(hits),
        )
        return []
    reranked = [h for h in hits if h.get("rerank_score") is not None]
    if reranked:
        reranked = sorted(reranked, key=lambda h: h["rerank_score"], reverse=True)
        selected = [h for h in reranked if h["rerank_score"] >= RERANK_MIN_SCORE][:top_k]
        logger.info(
            "rag.select mode=rerank hits=%d reranked=%d selected=%d top_rerank=%.4f "
            "min_rerank=%.2f selected_scores=%s duration_ms=%.1f",
            len(hits), len(reranked), len(selected),
            reranked[0]["rerank_score"], RERANK_MIN_SCORE,
            [round(h["rerank_score"], 4) for h in selected],
            (time.perf_counter() - started) * 1000,
        )
        return selected
    if not hits:
        logger.info(
            "rag.select hits=0 selected=0 top_score=N/A truncated=False reason=empty "
            "min_score=%.2f max_drop=%.3f duration_ms=0.0",
            SEARCH_MIN_SCORE, SEARCH_MAX_DROP,
        )
        return []
    if SEARCH_FUSION == "rrf":
        # 门控用 _relevance_score，不用 RRF 分
        ordered = sorted(hits, key=lambda h: h.get("fusion_score", 0.0), reverse=True)

        def _relevance(hit: dict) -> float:
            return float(hit.get("_relevance_score", hit.get("score", 0.0)))

        top_relevance = _relevance(ordered[0])
        floor = max(SEARCH_MIN_SCORE, top_relevance - SEARCH_MAX_DROP)
        selected = [h for h in ordered if _relevance(h) >= floor][:top_k]
        logger.info(
            "rag.select mode=rrf hits=%d selected=%d top_relevance=%.4f floor=%.4f "
            "fusion_scores=%s relevance_scores=%s duration_ms=%.1f",
            len(hits), len(selected), top_relevance, floor,
            [round(float(h.get("fusion_score", 0.0)), 6) for h in ordered[:5]],
            [round(_relevance(h), 4) for h in ordered[:5]],
            (time.perf_counter() - started) * 1000,
        )
        return selected
    hits = sorted(hits, key=lambda h: h.get("score", 0), reverse=True)
    top_score = float(hits[0].get("score", 0))
    selected = []
    reason = "all_selected"
    for index, h in enumerate(hits):
        score = float(h.get("score", 0))
        if score < SEARCH_MIN_SCORE:
            reason = "min_score"
            break
        if top_score - score > SEARCH_MAX_DROP:
            reason = "max_drop"
            break
        selected.append(h)
        if len(selected) >= top_k:
            reason = "top_k" if index < len(hits) - 1 else "all_selected"
            break
    selected_scores = [round(float(h.get("score", 0)), 4) for h in selected]
    logger.info(
        "rag.select mode=weighted hits=%d selected=%d top_score=%.4f truncated=%s reason=%s "
        "min_score=%.2f max_drop=%.3f selected_scores=%s duration_ms=%.1f",
        len(hits), len(selected), top_score, len(selected) < len(hits), reason,
        SEARCH_MIN_SCORE, SEARCH_MAX_DROP, selected_scores,
        (time.perf_counter() - started) * 1000,
    )
    return selected


class PgVectorStore:

    def __init__(self) -> None:
        self.bm25 = BM25Index()
        self._bm25_dirty = True
        self._rebuild_lock = asyncio.Lock()
        self._rebuild_task: asyncio.Task | None = None

    @staticmethod
    def _metadata(row: KnowledgeChunk) -> dict:
        return {
            "file_id": row.file_id,
            "doc_id": row.doc_id,
            "department": row.department,
            "status": row.status,
            "chunk_index": row.chunk_index,
            "type": row.type,
            "is_shared": bool(row.is_shared),
            "shared_departments": row.shared_departments or "",
            "image_path": row.image_path,
            "page": row.page,
            "sheet": row.sheet,
            "source": row.source,  # OCR 文本块标 ocr / ocr_lowconf，普通文本与图片块为 None
        }

    async def upsert_chunks(
        self,
        db,
        file_id: int,
        doc_id: int | None,
        department: str,
        status: str,
        chunks: list[dict],
        is_shared: bool = False,
        shared_departments: str | None = None,
    ) -> list[str]:
        """调用方提供 AsyncSession，保证与业务状态同事务"""
        records: list[dict] = []
        text_ids: list[str] = []
        text_docs: list[str] = []
        image_records: list[tuple[dict, str]] = []

        for ch in chunks:
            cid = f"{file_id}:{ch['seq']}"
            base = {
                "id": cid,
                "file_id": file_id,
                "doc_id": doc_id,
                "chunk_index": ch["seq"],
                "type": ch["type"],
                "department": department,
                "is_shared": is_shared,
                "shared_departments": shared_departments or "",
                "status": status,
                "page": ch.get("page"),
                "sheet": ch.get("sheet"),
                "source": ch.get("source"),  # parser 的 OCR 文本块带 ocr/ocr_lowconf
            }
            if ch["type"] == "image":
                rel_path = str(Path(ch["image_path"]).relative_to(STORAGE_DIR))
                base.update({"content": "[图片]", "image_path": rel_path})
                image_records.append((base, str(Path(STORAGE_DIR / rel_path))))
            else:
                base.update({"content": ch["content"], "image_path": None})
                text_ids.append(cid)
                text_docs.append(ch["content"])
                records.append(base)

        if text_docs:
            text_vectors = await asyncio.to_thread(embed_texts, text_docs)
            for record, vector in zip(records, text_vectors):
                record["embedding"] = vector

        for record, abs_image in image_records:
            vector = await asyncio.to_thread(embed_image, abs_image)
            record["embedding"] = vector
            records.append(record)

        if records:
            stmt = pg_insert(KnowledgeChunk).values(records)
            update_cols = {
                col: stmt.excluded[col]
                for col in (
                    "file_id", "doc_id", "chunk_index", "content", "type",
                    "image_path", "page", "sheet", "source", "department",
                    "is_shared", "shared_departments", "status", "embedding",
                )
            }
            await db.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols))

        self._bm25_dirty = True
        return text_ids

    async def delete_by_file(self, db, file_id: int) -> None:
        await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.file_id == file_id))
        self._bm25_dirty = True

    async def count(self) -> int:
        async with AsyncSessionLocal() as db:
            return int(await db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0)

    async def _load_bm25_docs(self, db) -> list[dict]:
        """只 select BM25 需要的列，绝不取 embedding；列清单与 _metadata 键一一对应"""
        from models.file import FileInfo as _FileInfo

        rows = (
            await db.execute(
                select(
                    KnowledgeChunk.id,
                    KnowledgeChunk.content,
                    KnowledgeChunk.file_id,
                    KnowledgeChunk.doc_id,
                    KnowledgeChunk.department,
                    KnowledgeChunk.status,
                    KnowledgeChunk.chunk_index,
                    KnowledgeChunk.type,
                    KnowledgeChunk.is_shared,
                    KnowledgeChunk.shared_departments,
                    KnowledgeChunk.image_path,
                    KnowledgeChunk.page,
                    KnowledgeChunk.sheet,
                    KnowledgeChunk.source,
                ).where(
                    KnowledgeChunk.type == "text",
                    KnowledgeChunk.status == "ready",
                    # BM25 快照必须同步过滤软删文件，否则软删内容会"死而复生"
                    KnowledgeChunk.file_id.is_(None)
                    | KnowledgeChunk.file_id.not_in(
                        select(_FileInfo.id).where(_FileInfo.deleted_at.is_not(None))
                    ),
                )
            )
        ).all()
        return [
            {
                "id": r.id,
                "text": r.content,
                "metadata": {
                    "file_id": r.file_id,
                    "doc_id": r.doc_id,
                    "department": r.department,
                    "status": r.status,
                    "chunk_index": r.chunk_index,
                    "type": r.type,
                    "is_shared": bool(r.is_shared),
                    "shared_departments": r.shared_departments or "",
                    "image_path": r.image_path,
                    "page": r.page,
                    "sheet": r.sheet,
                    "source": r.source,
                },
            }
            for r in rows
        ]

    async def rebuild_bm25(self) -> None:
        """单飞锁防并发；只允许启动 warmup 与后台任务调用"""
        async with self._rebuild_lock:
            async with AsyncSessionLocal() as db:
                docs = await self._load_bm25_docs(db)
            self.bm25.rebuild(docs)
            self._bm25_dirty = False

    def _schedule_rebuild(self) -> None:
        if self._rebuild_task is None or self._rebuild_task.done():
            self._rebuild_task = asyncio.create_task(self._rebuild_safe())

    async def _rebuild_safe(self) -> None:
        try:
            await self.rebuild_bm25()
        except Exception:
            logger.exception("rag.bm25_rebuild_failed")

    async def warmup_bm25(self) -> None:
        try:
            await self.rebuild_bm25()
            logger.info("rag.bm25_warmup docs=%d", len(self.bm25.doc_map))
        except Exception:
            logger.exception("rag.bm25_warmup_failed")

    def notify_bm25_stale(self) -> None:
        """标脏 + 调度后台重建，防止已软删内容经 BM25 字面命中复现"""
        self._bm25_dirty = True
        self._schedule_rebuild()

    @staticmethod
    def _matches_department(meta: dict, department: str | None) -> bool:
        if not department:
            return True
        if meta.get("department") == department:
            return True
        if meta.get("is_shared"):
            depts = [d.strip() for d in (meta.get("shared_departments") or "").split(",") if d.strip()]
            return (not depts) or (department in depts)
        return False

    @staticmethod
    def _permission_clause(department: str):
        return or_(
            KnowledgeChunk.department == department,
            and_(
                KnowledgeChunk.is_shared.is_(True),
                or_(
                    KnowledgeChunk.shared_departments.is_(None),
                    KnowledgeChunk.shared_departments == "",
                    sa_text(
                        ":dept = ANY(string_to_array(shared_departments, ','))"
                    ).bindparams(dept=department),
                ),
            ),
        )

    async def search(
        self,
        query: str,
        department: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        started = time.perf_counter()
        async with AsyncSessionLocal() as db:
            # 部门隔离是高选择性过滤，HNSW 默认 ef_search 会凑不满 top_k，调高换召回
            await db.execute(sa_text("SET LOCAL hnsw.ef_search = 100"))
            total = int(await db.scalar(select(func.count()).select_from(KnowledgeChunk)) or 0)
            if total == 0:
                logger.info(
                    "rag.search query=%s department=%s collection_total=0 vector_hits=0 "
                    "bm25_hits=0 returned=0 duration_ms=%.1f reason=collection_empty",
                    query, department or "-all-", (time.perf_counter() - started) * 1000,
                )
                return []

            use_bm25 = BM25_WEIGHT > 0
            if use_bm25 and self._bm25_dirty:
                self._schedule_rebuild()

            vector_started = time.perf_counter()
            qv = await asyncio.to_thread(embed_query, query)
            distance = KnowledgeChunk.embedding.cosine_distance(qv).label("distance")
            stmt = select(KnowledgeChunk, distance).where(KnowledgeChunk.status == "ready")
            from models.file import FileInfo as _FileInfo
            stmt = stmt.where(
                KnowledgeChunk.file_id.is_(None)
                | KnowledgeChunk.file_id.not_in(
                    select(_FileInfo.id).where(_FileInfo.deleted_at.is_not(None))
                )
            )
            if department:
                stmt = stmt.where(self._permission_clause(department))
            vector_n = max(top_k * 4, 20)
            text_rows = (await db.execute(
                stmt.where(KnowledgeChunk.type != "image").order_by(distance).limit(min(vector_n, total))
            )).all()
            image_rows = (await db.execute(
                stmt.where(KnowledgeChunk.type == "image").order_by(distance).limit(IMAGE_CANDIDATE_TOP_K)
            )).all()
            rows = [*text_rows, *image_rows]

            vector_hits = []
            for row, dist in rows:
                vector_hits.append({
                    "id": row.id,
                    "text": row.content,
                    "metadata": self._metadata(row),
                    "score": 1 - float(dist) / 2,
                })
            text_vector_hits = [
                h for h in vector_hits if h["metadata"].get("type") != "image"
            ]
            image_hits = [
                h for h in vector_hits if h["metadata"].get("type") == "image"
            ]
            for h in image_hits:
                h["fusion_score"] = 0.0
                h["_relevance_score"] = round(h["score"], 4)
                h["_vector_score"] = round(h["score"], 4)
                h["_bm25_score"] = 0.0
                h["_bm25_norm"] = 0.0
            vector_by_id = {h["id"]: h for h in text_vector_hits}
            vector_ms = (time.perf_counter() - vector_started) * 1000

            bm25_started = time.perf_counter()
            bm25_hits = self.bm25.search(query, top_k=max(top_k * 4, 20)) if use_bm25 else []
            bm25_scores = dict(bm25_hits)
            max_bm = max(bm25_scores.values()) if bm25_scores else 0.0
            bm25_ms = (time.perf_counter() - bm25_started) * 1000

            candidate_ids = list(vector_by_id.keys())
            if use_bm25:
                for cid, _ in bm25_hits:
                    if cid not in vector_by_id:
                        candidate_ids.append(cid)

            candidates = []
            for cid in candidate_ids:
                vh = vector_by_id.get(cid)
                if vh is None:
                    bm_doc = self.bm25.doc_map.get(cid)
                    if not bm_doc:
                        continue
                    meta = bm_doc.get("metadata", {})
                    if not self._matches_department(meta, department):
                        continue
                    text = bm_doc.get("text", "")
                    v_score = 0.0
                else:
                    meta = vh["metadata"]
                    text = vh["text"]
                    v_score = vh["score"]

                bm = bm25_scores.get(cid, 0.0)
                bm_norm = bm / max_bm if (use_bm25 and max_bm > 0) else 0.0
                if use_bm25 and bm > 0 and max_bm > 0:
                    if v_score > 0:
                        relevance_score = (
                            VECTOR_WEIGHT * v_score + BM25_WEIGHT * bm_norm
                        ) / (VECTOR_WEIGHT + BM25_WEIGHT)
                    else:
                        relevance_score = bm_norm * 0.95
                else:
                    relevance_score = v_score
                candidates.append({
                    "id": cid,
                    "text": text,
                    "metadata": meta,
                    "_vector_score": round(v_score, 4),
                    "_bm25_score": round(bm, 4),
                    "_bm25_norm": round(bm_norm, 4),
                    "_relevance_score": round(relevance_score, 4),
                })

            if SEARCH_FUSION == "rrf":
                vector_ranking = [h["id"] for h in text_vector_hits]
                bm25_ranking = [cid for cid, _ in bm25_hits]
                rrf_scores = rrf_fuse(
                    [vector_ranking, bm25_ranking],
                    weights=[VECTOR_WEIGHT, BM25_WEIGHT],
                    k=RRF_K,
                )
                visible_scores = {
                    c["id"]: rrf_scores.get(c["id"], 0.0) for c in candidates
                }
                normalized = normalize_scores(visible_scores)
                for c in candidates:
                    c["fusion_score"] = round(visible_scores[c["id"]], 6)
                    c["score"] = round(normalized.get(c["id"], 0.0), 4)
                candidates.sort(
                    key=lambda h: (h["fusion_score"], h["_relevance_score"]),
                    reverse=True,
                )
            else:
                for c in candidates:
                    v_score = c["_vector_score"]
                    bm = c["_bm25_score"]
                    bm_norm = c["_bm25_norm"]
                    if use_bm25 and bm > 0 and max_bm > 0:
                        if v_score > 0:
                            fused_score = (
                                VECTOR_WEIGHT * v_score + BM25_WEIGHT * bm_norm
                            ) / (VECTOR_WEIGHT + BM25_WEIGHT)
                        else:
                            fused_score = bm_norm * 0.95
                    else:
                        fused_score = v_score
                    c["score"] = round(fused_score, 4)
                    c["fusion_score"] = c["score"]
                candidates.sort(key=lambda h: h["score"], reverse=True)

            if RERANK_ENABLED:
                text_pool = candidates[:RERANK_TOP_N]
                fused = [*(await rerank(query, text_pool, top_n=RERANK_TOP_N)), *image_hits]
            else:
                fused = [*candidates, *image_hits]
            text_result = [h for h in fused if h["metadata"].get("type") != "image"][:top_k]
            image_result = [h for h in fused if h["metadata"].get("type") == "image"][:IMAGE_CANDIDATE_TOP_K]
            result = [*text_result, *image_result]
            top_vector = max((h.get("score", 0) for h in vector_hits), default=None)
            top_bm25 = max(bm25_scores.values(), default=None)
            top_fused = max((h["score"] for h in result), default=None)
            top_fusion = max((h.get("fusion_score", 0) for h in candidates), default=None)
            top_relevance = max((h.get("_relevance_score", 0) for h in candidates), default=None)
            logger.info(
                "rag.search query=%s department=%s fusion=%s rrf_k=%d "
                "vector_hits=%d bm25_hits=%d candidates=%d returned=%d "
                "top_vector_score=%s top_bm25_score=%s top_fused_score=%s "
                "top_fusion_score=%s top_relevance=%s vector_ms=%.1f bm25_ms=%.1f "
                "duration_ms=%.1f weights=%.2f/%.2f min_score=%.3f max_drop=%.3f",
                query, department or "-all-", SEARCH_FUSION, RRF_K,
                len(vector_hits), len(bm25_hits), len(candidate_ids), len(result),
                top_vector, top_bm25, top_fused, top_fusion, top_relevance,
                vector_ms, bm25_ms, (time.perf_counter() - started) * 1000,
                VECTOR_WEIGHT, BM25_WEIGHT, SEARCH_MIN_SCORE, SEARCH_MAX_DROP,
            )
            return result


store = PgVectorStore()