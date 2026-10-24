"""图谱配置集中点。

风格对齐 rag/rerank.py：模块级读 env + 越界即 raise，不允许带病运行。
KG_ENABLED 与 RERANK_ENABLED 的跨模块约束放 config/settings.py（那里集中做跨模块校验，
先例是 LLM_SUPPORTS_VISION），不放这里，避免本模块 import rag.rerank 形成隐性耦合。
"""
import os


def _flag(name: str, default: str = "false") -> bool:
    return (os.getenv(name) or default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} 非法值: {raw!r}，必须是整数")


def _float(name: str, default: float) -> float:
    raw = (os.getenv(name) or str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name} 非法值: {raw!r}，必须是数字")


KG_ENABLED = _flag("KG_ENABLED")

# 跳数。硬顶 3：递归 CTE 靠 hop 上限防环，放大到 4 以上行数会指数膨胀，
# 且中文企业文档 3 跳以上的关系几乎全是噪声。
KG_MAX_HOPS = _int("KG_MAX_HOPS", 2)
if not 1 <= KG_MAX_HOPS <= 3:
    raise RuntimeError(f"KG_MAX_HOPS={KG_MAX_HOPS} 越界：必须在 1~3，递归 CTE 靠它防环")

# 图通道一次最多补充多少个候选进 rerank 池
KG_CANDIDATE_TOP_K = _int("KG_CANDIDATE_TOP_K", 12)
if KG_CANDIDATE_TOP_K < 1:
    raise RuntimeError(f"KG_CANDIDATE_TOP_K={KG_CANDIDATE_TOP_K} 必须 >= 1")

# 边置信度门限：低于此值的边不参与扩展
KG_MIN_CONFIDENCE = _float("KG_MIN_CONFIDENCE", 0.5)
if not 0 <= KG_MIN_CONFIDENCE <= 1:
    raise RuntimeError(f"KG_MIN_CONFIDENCE={KG_MIN_CONFIDENCE} 必须在 0~1")

# 实体链接：query → 实体名录的最小余弦相似度。
# 0.82 是实测标定结果，不是拍的：
#   「员工生病要请长假」→ 病假 0.8497、「新来的同事多久转正」→ 转正 0.8530
#   —— 原来的 0.86 刚好把这两类换说法全部挡在门外，图通道等于没开。
# 敢放宽的前提是**图候选必须再过 cross-encoder 门控**：链错的实体扩出来的 chunk
# 会被 rerank_score 杀掉。这是"图只扩池、不判相关性"换来的容错空间。
# 代价是 rerank 池变大（RERANK_TOP_N + KG_CANDIDATE_TOP_K），别再往低调，
# 0.75 一档会把"考勤管理制度"这种弱相关实体也拉进来，池子被噪声占满。
KG_LINK_MIN_SCORE = _float("KG_LINK_MIN_SCORE", 0.82)
KG_LINK_TOP_N = _int("KG_LINK_TOP_N", 5)

# 词典路的泛实体停用词。
# 教训来自实测：「员工生病要请长假」先命中「员工」（dict 0.99），
# 于是图扩到《报销管理制度》—— 因为"员工"这个实体自己挂着一条边。
# 这类词在任何 query 里都可能出现，对定位零区分度，必须在链接期过滤。
# 停用词只影响链接，不影响抽取：实体本身仍然在库里，只是不当作检索入口。
KG_LINK_STOPWORDS = {
    w.strip() for w in
    (os.getenv("KG_LINK_STOPWORDS")
     or "员工,公司,人员,部门,领导,同事,本人,大家,职工,全体,相关人员").split(",")
    if w.strip()
}

# 实体自动合并：宁可漏，不可错。
# 错并的代价 >> 漏并的代价：漏并只是图碎片化、多跳走不通；错并会让下游推理
# 沿着错误的边扩散，给出看起来合理但完全错误的答案，且无法事后拆分。
#
# 两次全量回填的实测数据（全是被错并的）：
#   销售经理 -> 销售总监   0.9583
#   销售经理 -> 销售主管   0.9744   ← 与下面这条几乎同分，余弦无法区分
#   直属上级 -> 直属领导   0.9719   ← 这条其实是同义词，该并
#   考勤和差旅制度 -> 考勤管理制度 0.9343
#   财务总监 -> 财务负责人  0.9447
# 结论：中文职位名/制度名的字面相似度与语义相似度严重脱节，纯向量判不出来。
# 所以阈值定 0.98（几乎等于"字面全同才并"），把判断权交给人工合并界面。
KG_MERGE_MIN_SCORE = _float("KG_MERGE_MIN_SCORE", 0.98)
# 词面互为子串才放宽，且短词至少 3 字 —— "财务"是"财务负责人"的子串但显然不是一回事，
# 这类两字泛词一律不准靠子串合并。
KG_MERGE_SUBSTRING_SCORE = _float("KG_MERGE_SUBSTRING_SCORE", 0.90)
KG_MERGE_SUBSTRING_MIN_LEN = _int("KG_MERGE_SUBSTRING_MIN_LEN", 3)

# 单文档最多抽多少 chunk：超大 PDF 不做全量，超出部分只记录跳过原因
KG_MAX_CHUNKS_PER_DOC = _int("KG_MAX_CHUNKS_PER_DOC", 200)
# 抽取并发：LLM 调用是 IO 密集，但并发太高会触发上游限流
KG_EXTRACT_CONCURRENCY = _int("KG_EXTRACT_CONCURRENCY", 4)
if not 1 <= KG_EXTRACT_CONCURRENCY <= 16:
    raise RuntimeError(f"KG_EXTRACT_CONCURRENCY={KG_EXTRACT_CONCURRENCY} 必须在 1~16")
KG_EXTRACT_TIMEOUT_SECONDS = _float("KG_EXTRACT_TIMEOUT_SECONDS", 45)
if not (0 < KG_EXTRACT_TIMEOUT_SECONDS < 300):
    raise RuntimeError(f"KG_EXTRACT_TIMEOUT_SECONDS={KG_EXTRACT_TIMEOUT_SECONDS} 必须在 (0, 300)")

# 支撑度门限：提及次数低于此值的实体不建正式节点，只留关系。
# 不设这个门限，图里会塞满一次性名词，实体数能翻几倍。
KG_MIN_MENTION = _int("KG_MIN_MENTION", 1)

# 任务队列（语义与 INDEX_JOB_* 一致）
KG_JOB_LEASE_SECONDS = _int("KG_JOB_LEASE_SECONDS", 900)
KG_JOB_MAX_ATTEMPTS = _int("KG_JOB_MAX_ATTEMPTS", 2)
KG_JOB_POLL_SECONDS = _int("KG_JOB_POLL_SECONDS", 15)

# 抽取模型：默认复用主 LLM，但必须是"不绑工具"的干净模型（同 get_memory_llm 的理由）
KG_EXTRACT_MODEL = (os.getenv("KG_EXTRACT_MODEL") or "").strip()
KG_EXTRACT_BASE_URL = (os.getenv("KG_EXTRACT_BASE_URL") or "").strip()
KG_EXTRACT_API_KEY = (os.getenv("KG_EXTRACT_API_KEY") or "").strip()
