"""应用登录与管理员授权 API。"""
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

import config
from access_control import COOKIE, SESSION_TTL, Grant, Principal, access_store
from nl2sql import service

router = APIRouter()


def principal(request: Request) -> Principal | None:
    """获取本次请求经验证的用户。"""
    return getattr(request.state, "principal", None)


def _admin(request: Request) -> Principal:
    user = principal(request)
    if user is None or not user.admin:
        raise HTTPException(403, "需要管理员权限")
    return user


def _profile(user: Principal) -> dict:
    names = {domain["code"]: domain["name"] for domain in access_store().domains()}
    domains = names.keys() if user.admin else user.domains
    return {**asdict(user), "domains": [{"code": code, "name": names[code]} for code in sorted(domains)]}


class LoginBody(BaseModel):
    """登录表单。"""

    username: str = Field(max_length=40)
    password: SecretStr = Field(max_length=128)


class GrantBody(BaseModel):
    """授权起止时间，使用 Unix 秒。"""

    code: str = Field(max_length=32)
    starts_at: int = Field(default=0, ge=0, le=253402300799)
    expires_at: int | None = Field(default=None, ge=0, le=253402300799)


class UserBody(BaseModel):
    """账号编辑表单，留空密码时保留原有密码。"""

    username: str = Field(max_length=40)
    name: str = Field(max_length=60)
    password: SecretStr = Field(default=SecretStr(""), max_length=128)
    active: bool = True
    admin: bool = False
    grants: list[GrantBody] = Field(default_factory=list, max_length=128)


class DomainBody(BaseModel):
    """业务域登记表单。"""

    code: str = Field(max_length=32)
    name: str = Field(max_length=60)


@router.get("/auth/me")
def me(request: Request) -> dict:
    """返回当前登录状态和有效业务域。"""
    user = principal(request)
    return {"required": config.ACCESS_CONTROL_ENABLED, "user": _profile(user) if user else None}


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    """验证账号密码并设置仅服务端可读的会话 Cookie。"""
    if not config.ACCESS_CONTROL_ENABLED:
        raise HTTPException(404, "当前为单用户模式")
    secure = request.url.scheme == "https"
    if not secure and request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(400, "请使用 HTTPS 登录")
    client = request.client.host if request.client else ""
    try:
        token = access_store().login(body.username, body.password.get_secret_value(), client)
    except OverflowError as error:
        raise HTTPException(429, str(error)) from error
    if token is None:
        raise HTTPException(401, "账号或密码错误")
    if old := request.cookies.get(COOKIE):
        access_store().logout(old)
    user = access_store().resolve(token)
    if user is None:
        raise HTTPException(401, "账号状态已变化，请重新登录")
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, secure=secure, httponly=True, samesite="strict")
    return {"required": True, "user": _profile(user)}


@router.post("/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    """退出登录并吊销服务端会话。"""
    if config.ACCESS_CONTROL_ENABLED and (token := request.cookies.get(COOKIE)):
        access_store().logout(token)
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, secure=request.url.scheme == "https", httponly=True, samesite="strict")
    return response


@router.get("/admin/users")
def users(request: Request) -> dict:
    """列出账号、业务域覆盖情况和最近操作记录。"""
    _admin(request)
    try:
        store, _ = service.get_metadata()
    except FileNotFoundError:
        store = None
    counts: dict[str, int] = {}
    for table in store.tables() if store else []:
        counts[table.domain_code] = counts.get(table.domain_code, 0) + 1
    access = access_store()
    domains = [item | {"table_count": counts.pop(item["code"], 0)} for item in access.domains()]
    return {"users": access.users(), "domains": domains, "unclassified": sum(counts.values()),
            "audit": access.audit()}


def _save_user(request: Request, body: UserBody, user_id: str | None = None) -> dict:
    user = _admin(request)
    try:
        return access_store().save_user(actor=user.id, username=body.username, name=body.name,
                                        active=body.active, admin=body.admin,
                                        grants=[Grant(**item.model_dump()) for item in body.grants],
                                        password=body.password.get_secret_value(), user_id=user_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/admin/users", status_code=201)
def create_user(request: Request, body: UserBody) -> dict:
    """创建账号并写入域授权。"""
    return _save_user(request, body)


@router.put("/admin/users/{user_id}")
def update_user(request: Request, user_id: str, body: UserBody) -> dict:
    """保存用户状态、管理角色与域授权。"""
    return _save_user(request, body, user_id)


@router.put("/admin/domains", status_code=204)
def save_domain(request: Request, body: DomainBody) -> Response:
    """登记业务域代码和显示名称。"""
    user = _admin(request)
    try:
        access_store().save_domain(user.id, body.code, body.name)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return Response(status_code=204)
