from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class User(Base):
    __tablename__ = 'users'
    id:Mapped[int] = mapped_column(primary_key=True,autoincrement=True,comment="用户id")
    # 用户名必须继承旧库的大小写不敏感语义，用 CITEXT 实现
    username:Mapped[str] = mapped_column(CITEXT,nullable=False,unique=True,index=True,comment="唯一用户名")
    hashed_password:Mapped[str] = mapped_column(String(255),nullable=False,comment="哈希密码")
    created_at:Mapped[datetime] = mapped_column(DateTime(timezone=True),server_default=func.now(),nullable=False,comment="加入时间")
    department:Mapped[str | None] = mapped_column(String(50),nullable=True,comment="部门名")
    role:Mapped[str] = mapped_column(String(20),nullable=False,default="employee",comment="角色")
    position: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="职位")
    # JWT 带 tv 声明，与库值不符 = 旧 token 立即失效；改密/降权时 +1 全端下线
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="令牌版本")
    # 账号停用位；False 时登录与既有 token 全部拒绝
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true", comment="账号启用")