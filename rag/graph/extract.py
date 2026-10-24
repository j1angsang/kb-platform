"""受限 LLM 抽取：只抽白名单里的实体类型和谓词。

为什么必须给白名单而不是开放域：
中文企业文档开放域抽取的噪声率高到不可用 —— 模型会把"以上""具体""相关"
这类词当实体，也会造出"关于""涉及"这种无边语义的谓词。图一旦被噪声填满，
多跳走出来的全是垃圾，而且噪声边会污染实体链接的向量空间。

部门名不交给模型猜：config/departments.py 的 ALLOWED_DEPARTMENTS 是唯一真相，
词典命中直接锁定 canonical name，比 LLM 抽准得多。这类确定性信息没有理由赌概率。
"""
import asyncio
import json
import logging
import os
import re
import time

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from config.departments import ALLOWED_DEPARTMENTS
from config.llm import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from rag.graph.config import (
    KG_EXTRACT_API_KEY,
    KG_EXTRACT_BASE_URL,
    KG_EXTRACT_CONCURRENCY,
    KG_EXTRACT_MODEL,
    KG_EXTRACT_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

# 实体类型白名单。加类型要同步考虑：图查询、前端筛选、评测题集都会受影响
ENTITY_TYPES = (
    "department",   # 部门
    "position",     # 岗位
    "role",         # 角色（领导/员工/管理员）
    "policy",       # 制度（年假制度、报销规定）
    "process",      # 流程（审批流程、报销流程）
    "system",       # 系统（OA、考勤系统）
    "form",         # 表单（报销单、请假单）
    "org",          # 组织（公司、委员会、工会）
)

# 谓词白名单。不超过 10 个，多了模型分不清边界
PREDICATES = (
    "适用",      # 制度 适用于 部门/岗位
    "审批",      # 角色 审批 流程/表单
    "归属",      # 岗位 归属于 部门
    "依据",      # 流程/制度 依据 制度
    "包含",      # 制度 包含 条款/子制度
    "负责",      # 岗位/部门 负责 流程/系统
    "替代",      # 制度 替代 旧制度
    "标准为",    # 制度 标准为 字面值（金额/天数）
    "提交至",    # 表单 提交至 部门/角色
    "发放",      # 部门 发放 物品/补贴
)

_DEPT_HINT = "、".join(ALLOWED_DEPARTMENTS)

_SYSTEM_PROMPT = f"""你是企业知识图谱抽取器。从给定文本中抽取实体关系三元组。

可用实体类型（只能从这些里选，选不出就不抽）：
department 部门 / position 岗位 / role 角色 / policy 制度 / process 流程 / system 系统 / form 表单 / org 组织

可用谓词（只能从这些里选）：
{" / ".join(PREDICATES)}

规则：
1. subject 必须是一个实体名，不能是句子、不能是代词、不能是"以上""相关"这类虚指。
2. 宾语是实体 → 填 object 和 object_type，不要填 value。
3. 宾语是字面值（金额/天数/比例/期限/人名）→ 只填 value，object 与 object_type 都留空。
4. 同义实体一律用最短通用名：写"年假"，不要写"带薪年休假福利政策"。
5. 本公司的部门只有这几个：{_DEPT_HINT}。文本提到这些部门时必须原样使用，不要改写。
6. 只抽文本中明确写出的关系，不要推理、不要补充常识。
7. 抽不出任何有效三元组时返回 {{"triples": []}}。

输出严格 JSON，不要 markdown 围栏，不要解释：
{{"triples": [{{"subject": "...", "subject_type": "department", "predicate": "适用", "object": "...", "object_type": "policy"}}]}}
"""

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_TRAILING_PUNCT_RE = re.compile(r"[，。；、,.;:：!！?？\s]+$")

_MAX_NAME_LEN = 64
_MAX_VALUE_LEN = 128


def _get_extract_llm() -> ChatOpenAI:
    """绝不复用 agents.get_llm()：主模型绑了工具，抽取可能产生 tool_calls。
    与 services/memory 的 get_memory_llm 同一个理由。"""
    return ChatOpenAI(
        model=KG_EXTRACT_MODEL or LLM_MODEL,
        base_url=KG_EXTRACT_BASE_URL or LLM_BASE_URL,
        api_key=KG_EXTRACT_API_KEY or LLM_API_KEY,
        temperature=0,
        timeout=KG_EXTRACT_TIMEOUT_SECONDS,
        max_retries=0,  # 重试交给 job 的 attempts，不在这里叠加重试
        max_tokens=1024,
    )


def normalize_department(name: str) -> str | None:
    """部门名对齐唯一真相。返回 None 表示不是部门。
    命中即锁定 canonical：包含关系比相等宽松，"技术部（研发中心）"也要归到"技术部"。
    """
    raw = (name or "").strip()
    if not raw:
        return None
    for dept in ALLOWED_DEPARTMENTS:
        if dept in raw:
            return dept
    return None


def _clean(text: str, limit: int) -> str:
    return _TRAILING_PUNCT_RE.sub("", (text or "").strip())[:limit]


def parse_triples(raw: str) -> list[dict]:
    """解析并校验 LLM 输出。任何不合约的行直接丢弃，不做"尽力修复"。

    修复坏行比丢弃更危险：修出来的实体名会以错误的形式进图，
    而后面的实体链接会持续放大这个错误。
    """
    text = (raw or "").strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        return []
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    rows = payload.get("triples")
    if not isinstance(rows, list):
        return []

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        subject = _clean(row.get("subject"), _MAX_NAME_LEN)
        predicate = (row.get("predicate") or "").strip()
        if not subject or predicate not in PREDICATES:
            continue

        subject_type = (row.get("subject_type") or "").strip()
        dept = normalize_department(subject)
        if dept:
            subject, subject_type = dept, "department"
        if subject_type not in ENTITY_TYPES:
            continue

        obj = _clean(row.get("object"), _MAX_NAME_LEN)
        obj_type = (row.get("object_type") or "").strip()
        value = _clean(row.get("value"), _MAX_VALUE_LEN)

        if obj:
            obj_dept = normalize_department(obj)
            if obj_dept:
                obj, obj_type = obj_dept, "department"
            if obj_type not in ENTITY_TYPES:
                continue
            if obj == subject:
                continue  # 自环，扩展时毫无信息量
            out.append({
                "subject": subject, "subject_type": subject_type,
                "predicate": predicate,
                "object": obj, "object_type": obj_type, "value": None,
                "confidence": 1.0,
            })
        elif value:
            out.append({
                "subject": subject, "subject_type": subject_type,
                "predicate": predicate,
                "object": None, "object_type": None, "value": value,
                "confidence": 0.9,  # 字面值边比实体边更容易抽错，降一档
            })
    return out


async def extract_chunk(llm, text: str, chunk_id: str) -> list[dict]:
    """单 chunk 抽取。异常向上抛，由调用方决定降级还是整体失败。"""
    started = time.perf_counter()
    resp = await asyncio.wait_for(
        llm.ainvoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=f"文本：\n{text}"),
        ]),
        timeout=KG_EXTRACT_TIMEOUT_SECONDS + 5,
    )
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    triples = parse_triples(raw)
    logger.info(
        "graph.extract chunk=%s triples=%d chars=%d elapsed_ms=%.1f",
        chunk_id, len(triples), len(text), (time.perf_counter() - started) * 1000,
    )
    return triples


async def extract_chunks(chunks: list[dict], concurrency: int = KG_EXTRACT_CONCURRENCY) -> dict:
    """并发抽取一批 chunk。

    单 chunk 失败只记 WARNING 并跳过 —— 照抄 OCR 通道的降级哲学：
    局部失败不能让整份文档的图谱标记失败。只有全崩才抛错。
    """
    if not chunks:
        return {"triples": [], "ok": 0, "failed": 0, "errors": []}

    llm = _get_extract_llm()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: list[list[dict]] = []
    errors: list[str] = []

    async def _one(chunk: dict) -> list[dict]:
        async with semaphore:
            try:
                return await extract_chunk(llm, chunk["text"], chunk["id"])
            except Exception as exc:
                errors.append(f"{chunk['id']}:{type(exc).__name__}:{str(exc)[:120]}")
                logger.warning(
                    "graph.extract_failed chunk=%s error=%s:%s 跳过该块",
                    chunk["id"], type(exc).__name__, str(exc)[:150],
                )
                return []

    started = time.perf_counter()
    results = await asyncio.gather(*(_one(c) for c in chunks))
    total = sum(len(r) for r in results)
    ok = sum(1 for r in results if r)
    logger.info(
        "graph.extract_batch chunks=%d ok=%d failed=%d triples=%d concurrency=%d elapsed_ms=%.1f",
        len(chunks), ok, len(errors), total, concurrency,
        (time.perf_counter() - started) * 1000,
    )
    if errors and ok == 0:
        raise RuntimeError(f"全部 chunk 抽取失败，首个错误：{errors[0]}")
    return {"triples": results, "ok": ok, "failed": len(errors), "errors": errors[:5]}
