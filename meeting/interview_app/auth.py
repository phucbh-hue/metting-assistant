"""Shim đăng nhập cho app phỏng vấn mounted trong Meeting: dùng CHUNG đăng nhập của Meeting (bearer token `ma_token`).

Giữ đúng API mà app.py của repo gọi: ALLOWED_DOMAIN, enabled(), user_of(request), user_of_ws(ws), middleware, router.
Trang index.html (cùng origin với Meeting) đọc `ma_token` ở localStorage và gửi kèm Authorization: Bearer / ?token=.
"""
import logging
import re
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from meeting import auth as mauth
from meeting import interview_roles

log = logging.getLogger("interview.auth")
router = APIRouter()

ALLOWED_DOMAIN = (mauth.DOMAINS[0] if getattr(mauth, "DOMAINS", None) else "urbox.vn")
PUBLIC_PATHS = {"/", "/api/health", "/auth/login", "/auth/logout"}
# Các endpoint CẤU HÌNH (chỉ owner/admin): tạo/sửa/xoá vị trí, tiêu chí, ứng viên, chấm hàng loạt, đọc JD/CV, cài đặt,
# quản lý giọng. KHÔNG gồm /sessions... (bắt đầu buổi, gợi ý, chấm buổi, sửa vai) - người phỏng vấn (user) vẫn làm được.
_WRITE = {"POST", "PUT", "DELETE", "PATCH"}
_CONFIGURE = re.compile(
    r"/api/(positions(/\d+(/(build|criteria|screen|candidates))?)?|candidates/\d+(/screen)?|extract|settings|voices(/\d+(/samples)?)?)$")


def enabled() -> bool:
    return mauth.ENABLED


def _user_from_token(tok):
    u = mauth.check(tok) if tok else None
    return {"email": u["email"], "name": u.get("name") or u["email"]} if u else None


def user_of(request: Request):
    if not enabled():
        return {"email": "local", "name": "Local", "service": True}
    h = request.headers.get("authorization", "")
    tok = h[7:].strip() if h[:7].lower() == "bearer " else request.query_params.get("token")
    return _user_from_token(tok)


def user_of_ws(ws):
    if not enabled():
        return {"email": "local", "name": "Local", "service": True}
    try:
        tok = ws.query_params.get("token")
    except Exception:
        tok = None
    return _user_from_token(tok)


@router.get("/auth/login")
def login(request: Request, next: str = "/"):
    return RedirectResponse("/")        # đăng nhập dùng trang chính của Meeting


@router.get("/auth/logout")
def logout():
    return RedirectResponse("/")


@router.get("/api/me")
def me(request: Request):
    u = user_of(request)
    if not u:
        return JSONResponse({"detail": "chưa đăng nhập"}, status_code=401)
    role = interview_roles.role_of(u)
    return {"email": u["email"], "name": u["name"], "auth": enabled(),
            "role": role, "can_configure": role in ("admin", "owner"), "admin": role == "admin"}


@router.get("/api/roles")
def list_roles(request: Request):
    if not interview_roles.is_admin(user_of(request)):
        return JSONResponse({"detail": "Chỉ admin"}, status_code=403)
    return {"people": interview_roles.people()}


@router.post("/api/roles")
async def set_role(request: Request):
    if not interview_roles.is_admin(user_of(request)):
        return JSONResponse({"detail": "Chỉ admin"}, status_code=403)
    body = await request.json()
    try:
        return interview_roles.set_role(body.get("email", ""), body.get("role", ""))
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)


async def middleware(request: Request, call_next):
    """Bảo vệ /api + /ws (trừ /api/health). TRANG luôn tải được (SPA đọc ma_token ở localStorage rồi tự gửi bearer;
    điều hướng trình duyệt không mang theo bearer nên không được chặn trang). Ngoài ra chặn endpoint CẤU HÌNH cho non-owner."""
    p = request.url.path                       # có thể là "/api/..." hoặc "/interview/api/..." tuỳ tầng -> dùng `in`
    gated = ("/api/" in p or "/ws/" in p) and not p.endswith("/api/health")
    if not enabled() or not gated:
        return await call_next(request)
    u = user_of(request)
    if u:
        if request.method in _WRITE and _CONFIGURE.search(p) and not interview_roles.is_owner(u):
            return JSONResponse({"detail": "Chỉ owner/admin mới cấu hình được phần Phỏng vấn (liên hệ admin để được cấp quyền owner)."},
                                status_code=403)
        return await call_next(request)
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/")
    return JSONResponse({"detail": "chưa đăng nhập"}, status_code=401)
