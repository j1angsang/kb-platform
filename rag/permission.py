"""知识块权限的 ORM 层强制注入。

背景：`_permission_clause` 原本靠各处手动调用（检索、图查询、管理接口各自 remember），
新增一个查询接口忘了调，就是一个越权洞。这里用 SQLAlchemy 的 `do_orm_execute` 事件
把同一个条件挂到所有 `KnowledgeChunk` 查询上，让"漏写"在物理上不可能发生。

四条边界（改之前先读）：
1. **只拦 SELECT，只作用于 KnowledgeChunk**，其他表一律不碰。
2. **没有"生效部门"时不注入。** 建图、GC、评测、迁移这些后台任务不经过 HTTP，
   contextvar 是 None —— 它们本来就不该受某个部门的限制。
3. 显式 `execution_options(skip_perm=True)` 可跳过，留给运维脚本和一次性修数用。
4. **不删除原有的显式调用。** 重复条件会被查询优化器消掉，
   宁可 SQL 里多一个 AND，也不要某条路径漏掉。
"""
import os
from contextvars import ContextVar

import sqlalchemy as sa
from sqlalchemy import and_, event, or_
from sqlalchemy.orm import Session, with_loader_criteria

from models.knowledge_chunk import KnowledgeChunk

# 默认开启；需要排查问题时可以临时关掉对照
PERMISSION_ENFORCE = (os.getenv("PERMISSION_ENFORCE") or "true").strip().lower() == "true"

# 当前请求"应当看到哪个部门"。None 表示不做部门限制（后台任务 / admin / leader）
ENFORCED_DEPARTMENT: ContextVar[str | None] = ContextVar("kb_enforced_department", default=None)


def permission_clause(department: str, cls=KnowledgeChunk):
    """部门可见性：本部门可见，或共享且（未限定部门 / 部门列表含本部门）。

    用 `strpos` 而不是 `= ANY(string_to_array(...))`：后者要写裸列名，
    在 `with_loader_criteria` 为 JOIN 别名生成条件时会指错表；`strpos` 全程走 `cls`，
    跟随别名走，语义完全等价（`",A,B,"` 里找 `",B,"`）。
    """
    return or_(
        cls.department == department,
        and_(
            cls.is_shared.is_(True),
            or_(
                cls.shared_departments.is_(None),
                cls.shared_departments == "",
                sa.func.strpos(
                    sa.func.concat(",", cls.shared_departments, ","),
                    sa.func.concat(",", department, ","),
                ) > 0,
            ),
        ),
    )


def bind_enforced_department(department: str | None) -> None:
    """由认证层在拿到当前用户后调用；传 None 表示该用户可见全部部门。"""
    ENFORCED_DEPARTMENT.set(department)


def current_enforced_department() -> str | None:
    return ENFORCED_DEPARTMENT.get()


@event.listens_for(Session, "do_orm_execute")
def _inject_permission(state) -> None:
    if not PERMISSION_ENFORCE:
        return
    if not state.is_select:
        return
    if state.execution_options.get("skip_perm"):
        return
    department = ENFORCED_DEPARTMENT.get()
    if not department:
        return
    # 用 lambda 而不是直接传表达式：SQLAlchemy 会追踪闭包变量，
    # department 变化时自动失效缓存，不会把 A 部门的条件复用到 B 部门。
    state.statement = state.statement.options(
        with_loader_criteria(
            KnowledgeChunk,
            lambda cls: permission_clause(department, cls),
            include_aliases=True,
        )
    )


class PermissionContextMiddleware:
    """每个 HTTP 请求一个干净的权限上下文。

    必须这么做：contextvar 在协程复用里不会自动清空，上一个请求的部门会残留到下一个，
    表现为"用户 A 偶尔能看到 B 部门的文档"——这类越权最难查，因为它不是每次都发生。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = ENFORCED_DEPARTMENT.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            ENFORCED_DEPARTMENT.reset(token)
