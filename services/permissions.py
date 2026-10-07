from config.departments import UNASSIGNED
from models.user import User


def is_employee(user: User) -> bool:
    return user.role == "employee"


def is_admin(user: User) -> bool:
    return user.role == "admin"


def is_leader(user: User) -> bool:
    return user.role == "leader"


def is_manager(user: User) -> bool:
    return user.role != "employee"


def can_use_shared_upload(user: User) -> bool:
    return is_manager(user)


def can_manage_files(user: User) -> bool:
    return is_manager(user)


def can_review_knowledge(user: User) -> bool:
    return is_manager(user)


def can_view_all_departments(user: User) -> bool:
    return is_manager(user)


def can_auto_approve_doc(user: User) -> bool:
    return is_manager(user)


def can_review_doc(user: User, doc_department: str | None, is_shared: bool = False) -> bool:
    # leader 只能审本部门，部门为空/未分配则不能审；共享标记不剥夺本部门领导的审批权
    if is_employee(user):
        return False
    if is_admin(user):
        return True
    if is_leader(user):
        if not user.department or user.department == UNASSIGNED:
            return False
        return doc_department == user.department
    return False


def can_manage_file(user: User, record) -> bool:
    # admin 全放行；leader 仅本部门，record.department 是上传时快照；employee 拒绝
    if is_admin(user):
        return True
    if is_leader(user):
        if not user.department or user.department == UNASSIGNED:
            return False
        return bool(record.department) and record.department == user.department
    return False


def check_department_access(
    user: User,
    record_department: str | None,
    is_shared: bool = False,
    shared_departments: str | None = None,
) -> bool:
    if not is_employee(user):
        return True
    if record_department == user.department:
        return True
    if is_shared:
        depts = [d.strip() for d in (shared_departments or "").split(",") if d.strip()]
        return (not depts) or (user.department in depts)
    return False
