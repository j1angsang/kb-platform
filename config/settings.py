from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# FILE_STORAGE_DB_NAME 有安全默认值不是必填；SERVICE_* 未使用，仅备查
REQUIRED_ENV: dict[str, str] = {
    "DB_PASSWORD": "PostgreSQL 密码",
    "SECRET_KEY": "JWT 签名密钥",
    "LLM_BASE_URL": "LLM API Base URL",
    "LLM_API_KEY": "LLM API Key",
    "LLM_MODEL": "LLM 模型名",
    "EMBEDDING_API_KEY": "向量 embedding API Key",
    "EMBEDDING_MODEL": "向量 embedding 模型名",
}


def missing_settings() -> list[str]:
    return [name for name in REQUIRED_ENV if not (os.getenv(name) or "").strip()]


def validate_settings() -> None:
    missing = missing_settings()
    if missing:
        lines = [f"- {name}: {REQUIRED_ENV[name]}" for name in missing]
        detail = "缺少必填环境变量，请参考 .env.example：" + chr(10) + chr(10).join(lines)
        raise RuntimeError(detail)
    # 声明不支持视觉但图片通道是核心路径则拒绝启动：静默失效比崩溃更糟
    from config.llm import LLM_SUPPORTS_VISION
    if not LLM_SUPPORTS_VISION:
        raise RuntimeError(
            "LLM_SUPPORTS_VISION=false：主模型声明不支持视觉，而图片检索通道"
            "是本产品核心路径，agents/graph.py rag_hook 会注入多模态图片。"
            "请换支持视觉的模型后启动。"
        )

    # 图通道只扩充候选池，相关性判定必须交给 cross-encoder 的绝对分。
    # 关掉 rerank 时图候选没有任何绝对分可用，只能回落 _relevance_score，
    # 而那条路径的门控地板是 SEARCH_MIN_SCORE=0.80（按余弦标定）——
    # 图候选会被全杀或误放。这个组合必须启动即失败，不能等到线上才发现检索变空。
    from rag.graph.config import KG_ENABLED
    from rag.rerank import RERANK_ENABLED
    if KG_ENABLED and not RERANK_ENABLED:
        raise RuntimeError(
            "KG_ENABLED=true 要求 RERANK_ENABLED=true：图通道只负责把候选送进重排池，"
            "相关性判定依赖 cross-encoder 的 rerank_score（绝对分）。"
            "关掉 rerank 时图候选没有绝对分，会绕过门控或被门控全杀。"
        )
