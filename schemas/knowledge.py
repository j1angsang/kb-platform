from datetime import datetime

from pydantic import BaseModel, ConfigDict


class KnowledgeDocOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_id: int
    filename: str | None = None
    department: str
    status: str
    index_status: str
    error_message: str | None = None
    chunk_count: int
    created_at: datetime
    uploader: str | None = None
    is_shared: bool = False
    shared_departments: str | None = None


class KnowledgeDocPage(BaseModel):
    items: list[KnowledgeDocOut]
    total: int
    page: int
    page_size: int


class SubmitIn(BaseModel):
    file_id: int


class ApproveIn(BaseModel):
    comment: str | None = None


class RejectIn(BaseModel):
    comment: str

class SearchHit(BaseModel):
    text: str
    score: float
    file_id: int
    filename: str | None = None
    department: str | None = None
    page: int | None = None
    sheet: str | None = None
    image_path: str | None = None
    source: str | None = None


class SearchOut(BaseModel):
    query: str
    hits: list[SearchHit]