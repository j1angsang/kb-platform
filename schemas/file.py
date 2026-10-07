from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from config.departments import ALLOWED_DEPARTMENTS
from services.upload import validate_upload_id_http

# 非法格式抛 HTTPException(400)，不是 Pydantic 默认的 422
UploadId = Annotated[str, AfterValidator(validate_upload_id_http)]

# md5 必须 32 位十六进制：防把任意串当查询条件
Md5Hex = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{32}$")]

# 宽度与列宽一致，超长会在 commit 阶段炸 500 且文件已落盘
FILENAME_MAX_LENGTH = 255
CONTENT_TYPE_MAX_LENGTH = 100


def normalize_shared_departments(value: str | list[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    else:
        parts = [str(p).strip() for p in value]
    parts = [p for p in parts if p]
    if not parts:
        return None
    invalid = [p for p in parts if p not in ALLOWED_DEPARTMENTS]
    if invalid:
        raise ValueError(
            "非法部门：" + "、".join(invalid)
            + "；合法取值：" + "、".join(ALLOWED_DEPARTMENTS)
        )
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ",".join(seen)


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    size_bytes: int
    content_type: str
    md5: str
    created_at: datetime
    uploader: str | None = None
    department: str | None = None
    is_shared: bool = False
    shared_departments: str | None = None


class FilePage(BaseModel):
    items: list[FileOut]
    total: int
    page: int
    page_size: int


class ShareIn(BaseModel):
    all: bool = True
    departments: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("departments")
    @classmethod
    def _departments_in_allowlist(cls, v: list[str]) -> list[str]:
        cleaned = [d.strip() for d in v if d.strip()]
        invalid = [d for d in cleaned if d not in ALLOWED_DEPARTMENTS]
        if invalid:
            raise ValueError(
                "非法部门：" + "、".join(invalid)
                + "；合法取值：" + "、".join(ALLOWED_DEPARTMENTS)
            )
        return cleaned


class UploadInitIn(BaseModel):
    # md5 保持 str：init 的上限检查返回 413，必须先于格式校验，收紧会变 422
    filename: str = Field(max_length=FILENAME_MAX_LENGTH)
    total_size: int = Field(ge=0)
    chunk_size: int = Field(ge=1)
    total_chunks: int = Field(ge=1)
    content_type: str = Field(default="application/octet-stream", max_length=CONTENT_TYPE_MAX_LENGTH)
    md5: str


class UploadInitOut(BaseModel):
    upload_id: str
    chunk_size: int
    total_chunks: int


class UploadCompleteIn(BaseModel):
    # 对账权威在库 upload_tasks，这里的 md5 只是传输校验
    upload_id: UploadId
    filename: str = Field(max_length=FILENAME_MAX_LENGTH)
    content_type: str = Field(max_length=CONTENT_TYPE_MAX_LENGTH)
    total_chunks: int = Field(ge=1)
    md5: Md5Hex
    shared: bool = False
    shared_departments: str | None = None

    @field_validator("shared_departments")
    @classmethod
    def _shared_departments_allowlist(cls, v: str | None) -> str | None:
        return normalize_shared_departments(v)

class DownloadStatsOut(BaseModel):
    file_id: int
    filename: str
    download_count: int
