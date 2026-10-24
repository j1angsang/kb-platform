"""图谱接口的请求/响应模型。

关键约束：实体输出结构里**没有 content 字段**。
实体本身不带内容，要读内容只能沿 chunk_id 拿证据，而证据必须过部门权限。
"""
from pydantic import BaseModel, Field


class GraphEntityOut(BaseModel):
    id: int
    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)
    mention_count: int = 0


class GraphEntityPage(BaseModel):
    items: list[GraphEntityOut]
    total: int


class GraphEdgeOut(BaseModel):
    direction: str          # in / out
    predicate: str
    other_id: int | None = None
    other_name: str | None = None
    other_type: str | None = None
    value: str | None = None      # 字面值宾语（金额/天数），此时 other_* 为空
    chunk_id: str
    confidence: float = 1.0
    # 只有请求者对该 chunk 有权限时才非空；无权直接留空，不做"部分打码"
    evidence: str | None = None
    evidence_filename: str | None = None


class GraphNeighborOut(BaseModel):
    entity: GraphEntityOut | None = None
    edges: list[GraphEdgeOut] = Field(default_factory=list)
    hidden_edges: int = 0      # 因权限被隐藏的边数，只报数量不报内容


class GraphMergeIn(BaseModel):
    source_id: int
    target_id: int


class GraphSplitIn(BaseModel):
    alias: str


class GraphTypeCount(BaseModel):
    type: str
    count: int


class GraphStatsOut(BaseModel):
    entities: int
    merged_entities: int
    relations: int
    value_relations: int
    by_type: list[GraphTypeCount] = Field(default_factory=list)
    kg_enabled: bool = False
    docs_by_status: dict[str, int] = Field(default_factory=dict)
