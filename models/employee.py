
from datetime import date

from sqlalchemy import CheckConstraint, Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class Employee(Base):
    __tablename__ = "employees"

    __table_args__ = (
        CheckConstraint("status IN ('在职', '离职', '试用期')", name="check_status_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="员工主键")
    name: Mapped[str] = mapped_column(String(50), nullable=False, index=True, comment="姓名")
    position: Mapped[str] = mapped_column(String(50), nullable=False, comment="岗位")
    manager: Mapped[str] = mapped_column(String(50), nullable=True, comment="直属上级")
    hire_date: Mapped[date] = mapped_column(Date, nullable=False, comment="入职日期")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="在职", comment="在职/离职/试用期")
    department: Mapped[str] = mapped_column(String(50), nullable=False, index=True, comment="部门")
