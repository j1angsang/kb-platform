import os
import shutil
from pathlib import Path
from uuid import uuid4

# 上一级 = 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent

# FILE_STORAGE_DIR 可覆盖，测试基座用它隔离磁盘
STORAGE_DIR = Path(os.getenv("FILE_STORAGE_DIR") or (BASE_DIR / "storage"))

# 页图落在 STORAGE_DIR/<存储名去扩展名>_pages/，命名规则只有这一处
DERIVED_DIR_SUFFIX = "_pages"


def safe_extension(filename: str) -> str:
    suffix = Path(filename).suffix
    cleaned = "".join(c for c in suffix if c.isalnum() or c == ".")
    return cleaned[:10]


def generate_stored_name(filename: str) -> str:
    return uuid4().hex + safe_extension(filename or "")


def resolve_storage_path(stored_name: str) -> Path:
    return STORAGE_DIR / stored_name


def derived_dir_for(stored_name: str) -> Path:
    return STORAGE_DIR / (Path(stored_name).stem + DERIVED_DIR_SUFFIX)


def delete_storage_artifacts(stored_name: str) -> None:
    # 入参不可信：删除目标必须严格在 STORAGE_DIR 内，等于根目录本身也拒删
    name = Path(stored_name).name
    root = STORAGE_DIR.resolve()
    targets = [
        STORAGE_DIR / name,
        STORAGE_DIR / (Path(name).stem + DERIVED_DIR_SUFFIX),
    ]
    for target in targets:
        if not target.exists():
            continue
        resolved = target.resolve()
        if resolved == root or root not in resolved.parents:
            raise ValueError(f"拒绝删除 STORAGE_DIR 本身或其之外的路径: {resolved}")
        if target.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()


def to_absolute(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return STORAGE_DIR / p


def to_relative(path: str | Path) -> str:
    p = Path(path).resolve()
    return str(p.relative_to(STORAGE_DIR.resolve()))
