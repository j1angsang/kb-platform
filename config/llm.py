import os

from dotenv import load_dotenv

load_dotenv()

# 不给兜底模型名：缺失必须 fail-fast，静默默认值只会变成运行时看不懂的 400
LLM_MODEL = (os.getenv("LLM_MODEL") or "").strip()
LLM_BASE_URL = (os.getenv("LLM_BASE_URL") or "").strip()
LLM_API_KEY = (os.getenv("LLM_API_KEY") or "").strip()

_missing = [
    name for name, value in (
        ("LLM_MODEL", LLM_MODEL),
        ("LLM_BASE_URL", LLM_BASE_URL),
        ("LLM_API_KEY", LLM_API_KEY),
    ) if not value
]
if _missing:
    raise RuntimeError(
        f"LLM 配置缺失: {', '.join(_missing)}，请检查 .env；不给兜底模型名，"
        "配错模型名必须在启动时大声失败，而不是运行时 400"
    )

# 换纯文本模型必须显式声明 false，启动即报错；改模型不改这里，图片通道会静默失效
LLM_SUPPORTS_VISION = (os.getenv("LLM_SUPPORTS_VISION") or "true").strip().lower() in (
    "1", "true", "yes", "on"
)

# 必须与主模型真实窗口一致：调大=摘要从不触发，调小=过早触发
_raw_max_input = (os.getenv("MODEL_MAX_INPUT_TOKENS") or "1000000").strip()
try:
    MODEL_MAX_INPUT_TOKENS = int(_raw_max_input)
except ValueError:
    raise RuntimeError(
        f"MODEL_MAX_INPUT_TOKENS 非法值: {_raw_max_input!r}，必须是正整数"
    )
if MODEL_MAX_INPUT_TOKENS <= 0:
    raise RuntimeError(
        f"MODEL_MAX_INPUT_TOKENS 非法值: {MODEL_MAX_INPUT_TOKENS}，必须是正整数"
    )

_raw_fraction = (os.getenv("SUMMARY_TRIGGER_FRACTION") or "0.8").strip()
try:
    SUMMARY_TRIGGER_FRACTION = float(_raw_fraction)
except ValueError:
    raise RuntimeError(
        f"SUMMARY_TRIGGER_FRACTION 非法值: {_raw_fraction!r}，必须是 0-1 之间的小数"
    )
if not (0 < SUMMARY_TRIGGER_FRACTION < 1):
    raise RuntimeError(
        f"SUMMARY_TRIGGER_FRACTION 非法值: {SUMMARY_TRIGGER_FRACTION}，必须满足 0 < x < 1"
    )

# 留空则用官方 DEFAULT_SUMMARY_PROMPT，非空则经官方 summary_prompt= 注入
SUMMARY_PROMPT = (os.getenv("SUMMARY_PROMPT") or "").strip()
