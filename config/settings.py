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
