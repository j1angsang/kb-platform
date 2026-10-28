from datetime import datetime, timedelta, timezone
import logging
import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from jose import JWTError, jwt
import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from config.db_conf import get_db
from config.departments import UNASSIGNED
from config.logging_conf import bind_user_id
from rag.permission import bind_enforced_department
from services.permissions import can_view_all_departments
from config.rate_limit import check_or_429, client_ip, make_limiter
from crud import ops_audit as crud_ops_audit
from models.user import User
from services.permissions import is_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["认证"])

LOGIN_ACCOUNT_LIMITER = make_limiter(10, 60)
LOGIN_IP_LIMITER = make_limiter(30, 60)
REGISTER_ACCOUNT_LIMITER = make_limiter(5, 300)
REGISTER_IP_LIMITER = make_limiter(10, 300)


SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY 未配置:请检查 .env")
ALGORITHM = "HS256"
# token 有效期 60 分钟；改密/降权靠 token_version 全端下线
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
security = HTTPBearer()

def hash_password(password:str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain:str, hashed_password:str) -> bool:
    # 非法格式哈希 checkpw 会抛 ValueError：按密码错误处理，别变成 500
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False

def create_access_token(user_id:int, token_version: int = 0) ->str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub":str(user_id), "exp":expire, "tv": token_version}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(credentials:HTTPAuthorizationCredentials = Depends(security),db:AsyncSession = Depends(get_db))->User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="无效或过期的 token")
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=401, detail="token 缺少用户信息")
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    if int(payload.get("tv", 0)) != user.token_version:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="账号已停用")
    bind_user_id(user.id)
    # 绑定到 ORM 强制注入的权限上下文：admin/leader 可见全部部门，其余按本部门过滤。
    # 部门为空时必须落 None（= 不限制）而不是空串，否则会变成"谁都看不到"
    bind_enforced_department(None if can_view_all_departments(user) else (user.department or None))
    return user


@router.post("/change-password")
async def change_password(
    old_password: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 改密后 token_version +1，所有已签发 token 立即失效，被盗会话一并下线
    if not verify_password(old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="旧密码错误")
    if len(new_password) < 6:
        raise HTTPException(status_code=422, detail="新密码至少 6 位")
    current_user.hashed_password = hash_password(new_password)
    current_user.token_version += 1
    await crud_ops_audit.add_ops_log(
        db, action="change_password", target_type="user",
        actor_id=current_user.id, target_id=current_user.id,
    )
    await db.commit()
    logger.info("auth.password_changed user_id=%s", current_user.id)
    token = create_access_token(current_user.id, current_user.token_version)
    return {"access_token": token, "token_type": "bearer"}


@router.post("/refresh")
async def refresh_token(current_user: User = Depends(get_current_user)):
    token = create_access_token(current_user.id, current_user.token_version)
    return {"access_token": token, "token_type": "bearer"}

@router.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    await check_or_429(REGISTER_ACCOUNT_LIMITER, f"register:user:{username.strip().lower()}", action="注册请求", dimension="account", username=username)
    await check_or_429(REGISTER_IP_LIMITER, f"register:ip:{client_ip(request)}", action="注册请求", dimension="ip", username=username)
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(username=username, hashed_password=hash_password(password),department = UNASSIGNED)
    db.add(user)
    await db.flush()
    await db.commit()
    return {"id": user.id, "username": user.username,"department":user.department}


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    await check_or_429(LOGIN_ACCOUNT_LIMITER, f"login:user:{username.strip().lower()}", action="登录尝试", dimension="account", username=username)
    await check_or_429(LOGIN_IP_LIMITER, f"login:ip:{client_ip(request)}", action="登录尝试", dimension="ip", username=username)
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        await crud_ops_audit.add_ops_log(
            db, action="login_failure", target_type="auth",
            actor_id=user.id if user else None, target_id=user.id if user else None,
            detail={"username": username, "ip": client_ip(request)},
        )
        await db.commit()
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="账号已停用")

    await crud_ops_audit.add_ops_log(
        db, action="login_success", target_type="auth",
        actor_id=user.id, target_id=user.id, detail={"ip": client_ip(request)},
    )
    await db.commit()
    token = create_access_token(user.id, user.token_version)
    return {
        "access_token": token,
        "token_type": "bearer",
        "username": user.username,
        "department": user.department,
        "role": user.role,
        "position": user.position,
    }

@router.post("/set-position")
async def set_position(
        username: str = Form(...),
        position: str = Form(...),
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="无权设置职位")
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    old_position = user.position
    user.position = position
    await db.flush()
    await db.commit()
    logger.info(
        "admin.position_changed operator_id=%s operator=%s target_id=%s target=%s "
        "from=%s to=%s",
        current_user.id, current_user.username, user.id, user.username,
        old_position or "-", position,
    )
    return {"username": user.username,"position":user.position}

