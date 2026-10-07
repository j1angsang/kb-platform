import asyncio
from datetime import date

from sqlalchemy import select

from config.db_conf import AsyncSessionLocal
from models import Employee

# 列顺序：序号 姓名 岗位 直属上级 入职日期 状态
PEOPLE = [
    ("张伟", "技术总监", "CEO", date(2020, 3, 15), "在职"),
    ("陈静", "高级架构师", "技术总监", date(2021, 7, 1), "在职"),
    ("李明", "开发经理", "技术总监", date(2020, 11, 20), "在职"),
    ("王芳", "前端开发工程师", "开发经理", date(2022, 2, 10), "在职"),
    ("赵强", "后端开发工程师", "开发经理", date(2022, 5, 22), "在职"),
    ("刘洋", "全栈开发工程师", "开发经理", date(2023, 1, 5), "在职"),
    ("孙丽", "测试工程师", "开发经理", date(2022, 8, 15), "在职"),
    ("周涛", "运维工程师", "技术总监", date(2021, 9, 1), "在职"),
    ("吴雪", "数据分析师", "高级架构师", date(2023, 3, 20), "在职"),
    ("郑浩", "信息安全工程师", "技术总监", date(2022, 6, 10), "试用期"),
]

DEPARTMENT = "技术部"


async def main():
    async with AsyncSessionLocal() as db:
        inserted = 0
        for name, position, manager, hire_date, status in PEOPLE:
            exists = (await db.execute(
                select(Employee).where(
                    Employee.name == name,
                    Employee.department == DEPARTMENT,
                )
            )).first()
            if exists:
                print(f"跳过，已存在：{name}")
                continue
            db.add(Employee(
                name=name,
                position=position,
                manager=manager,
                hire_date=hire_date,
                status=status,
                department=DEPARTMENT,
            ))
            inserted += 1
        await db.commit()
        print(f"完成：新插入 {inserted} 人，表内共 {len(PEOPLE)} 人")


if __name__ == "__main__":
    asyncio.run(main())
