"""Đăng nhập cho bản chạy trên web (AUTH_REQUIRED=1): tài khoản Google, chỉ email thuộc ALLOWED_DOMAIN.

Chạy trên máy (mặc định, run.cmd) không cần đăng nhập.

Luồng:
- Trang web hiện nút "Đăng nhập với Google" (Google Identity Services, GOOGLE_OAUTH_CLIENT_ID).
- Google trả về ID token; server kiểm tra với Google (đúng client, email đã xác minh, đúng tên miền).
- Server cấp token riêng (ký HMAC bằng AUTH_SECRET, hạn AUTH_TTL_HOURS giờ).
- Trang web gửi token này:
  - kèm mỗi request: `Authorization: Bearer <token>`;
  - với WebSocket và ảnh: tham số `?token=` (trình duyệt không gắn header được).
- Giao diện (Vercel) và server (máy riêng) khác tên miền, nên không dùng cookie.

Quyền quản trị (AUTH_ADMIN_EMAILS):
- Đổi cài đặt chung (khóa dịch vụ, nguồn AI, giọng đọc, thư mục tài liệu, đồng bộ lưu trữ) và đăng nhập gói AI trên server.
- Để trống thì không ai đổi được cài đặt chung qua web.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

log = logging.getLogger("meeting.auth")


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list:
    return [x.strip().lower() for x in (os.getenv(name) or default).split(",") if x.strip()]


ENABLED = _flag("AUTH_REQUIRED")
CLIENT_ID = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
DOMAINS = _list("ALLOWED_DOMAIN", "urbox.vn")
ADMINS = set(_list("AUTH_ADMIN_EMAILS"))
TTL_S = float(os.getenv("AUTH_TTL_HOURS") or 12) * 3600
SECRET = (os.getenv("AUTH_SECRET") or "").encode()
if ENABLED and not SECRET:
    # Không có khóa ký cố định: token mất hiệu lực mỗi lần server khởi động lại (mọi người phải đăng nhập lại)
    SECRET = secrets.token_bytes(32)
    log.warning("meeting.auth: chưa đặt AUTH_SECRET, dùng khóa tạm (đăng nhập mất hiệu lực khi server khởi động lại)")
if ENABLED and not CLIENT_ID:
    log.warning("meeting.auth: AUTH_REQUIRED=1 nhưng chưa có GOOGLE_OAUTH_CLIENT_ID: không ai đăng nhập được")

# Không cần đăng nhập: trang, tài nguyên tĩnh, kiểm tra server còn sống, cấu hình và đổi token đăng nhập.
# /api/health (tên DB, số mẫu giọng...) vẫn cần đăng nhập; nền tảng chạy server dùng /healthz.
PUBLIC_PATHS = {"/", "/vesper", "/config.js", "/favicon.ico", "/healthz", "/api/auth/config", "/api/auth/google",
                "/api/google/callback"}      # Google chuyển hướng về đây (kiểm bằng state đã ký, không bằng token)
PUBLIC_PREFIXES = ("/static/",)
# Chỉ quản trị viên được ĐỔI (mọi method trừ GET/HEAD) các mục cài đặt dùng chung của server
# (Kết nối gói đăng ký /api/llm/connect là của từng người trên bản web, không cần quyền quản trị.)
ADMIN_PREFIXES = ("/api/settings/", "/api/llm/provider", "/api/storage/sync")


class AuthError(Exception):
    pass


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def is_admin(user: Optional[Dict[str, Any]]) -> bool:
    return bool(user) and str(user.get("email", "")).lower() in ADMINS


def allowed(user: Dict[str, Any], method: str, path: str) -> bool:
    if method.upper() in ("GET", "HEAD", "OPTIONS"):
        return True
    return is_admin(user) or not path.startswith(ADMIN_PREFIXES)


def can_access(user: Optional[Dict[str, Any]], meeting: Optional[Dict[str, Any]],
               group: Optional[Dict[str, Any]] = None) -> bool:
    """Bản web: được xem một cuộc họp nếu là người tạo, hoặc là chủ nhóm / thành viên của nhóm chứa cuộc họp (group).
    Cuộc họp tạo trước khi có đăng nhập (chưa có chủ, không thuộc nhóm) thuộc về quản trị viên.
    Chạy trên máy: ai cũng xem được."""
    if not ENABLED:
        return True
    meeting = meeting or {}
    email = str((user or {}).get("email", "")).lower()
    owner = str(meeting.get("owner") or "").lower()
    if owner and email == owner:
        return True
    if group and email and (email == str(group.get("owner") or "").lower() or email in (group.get("members") or [])):
        return True
    if not owner and not meeting.get("group_id"):
        return is_admin(user)
    return False


def domain_ok(email: str) -> bool:
    email = (email or "").strip().lower()
    return bool(DOMAINS) and any(email.endswith("@" + d) for d in DOMAINS)


# ------------------------------------------------------------- Google ---
def _tokeninfo(credential: str) -> Dict[str, Any]:
    """Hỏi Google ID token này có hợp lệ không (chữ ký, hạn dùng); trả về các trường của token."""
    url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token": credential})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise AuthError("Phiên đăng nhập Google không hợp lệ hoặc đã hết hạn, hãy đăng nhập lại")
    except (OSError, ValueError):
        raise AuthError("Không kết nối được Google để kiểm tra đăng nhập, thử lại sau")


def verify_google(credential: str) -> Dict[str, str]:
    """ID token Google -> {email, name}. Lỗi (AuthError) nếu sai client, chưa xác minh email hoặc khác tên miền."""
    if not CLIENT_ID:
        raise AuthError("Server chưa cấu hình GOOGLE_OAUTH_CLIENT_ID")
    if not credential or len(credential) > 8192:
        raise AuthError("Thiếu thông tin đăng nhập Google")
    info = _tokeninfo(credential)
    if info.get("aud") != CLIENT_ID:
        raise AuthError("Token Google không dành cho ứng dụng này")
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise AuthError("Token không do Google cấp")
    if float(info.get("exp") or 0) < time.time():
        raise AuthError("Phiên đăng nhập Google đã hết hạn, hãy đăng nhập lại")
    email = str(info.get("email") or "").strip().lower()
    if str(info.get("email_verified")).lower() != "true":
        raise AuthError("Email Google chưa được xác minh")
    if not domain_ok(email):
        raise AuthError(f"Chỉ tài khoản @{', @'.join(DOMAINS)} được dùng ứng dụng này")
    return {"email": email, "name": str(info.get("name") or email.split("@")[0])}


# -------------------------------------------------------------- token ---
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())


def issue(user: Dict[str, str], now: Optional[float] = None) -> Dict[str, Any]:
    exp = int((now or time.time()) + TTL_S)
    body = _b64(json.dumps({"e": user["email"], "n": user.get("name", ""), "x": exp},
                           ensure_ascii=False, separators=(",", ":")).encode())
    return {"token": f"v1.{body}.{_sign(body)}", "expires_at": exp}


def check(token: Optional[str], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Token hợp lệ -> {email, name, admin}; sai chữ ký, hết hạn hoặc khác tên miền -> None."""
    if not token or not token.startswith("v1.") or token.count(".") != 2:
        return None
    _, body, sig = token.split(".")
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    if float(data.get("x") or 0) < (now or time.time()) or not domain_ok(data.get("e", "")):
        return None
    user = {"email": data["e"], "name": data.get("n") or data["e"]}
    user["admin"] = is_admin(user)
    return user


def token_from_scope(scope: Dict[str, Any]) -> Optional[str]:
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            val = v.decode("latin-1")
            if val[:7].lower() == "bearer ":
                return val[7:].strip()
    qs = urllib.parse.parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    return (qs.get("token") or [None])[0]


# --------------------------------------------------------- middleware ---
class AuthMiddleware:
    """Chặn mọi request / WebSocket chưa đăng nhập khi AUTH_REQUIRED=1 (ASGI thuần: áp dụng cả cho WebSocket)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not ENABLED or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if (scope["type"] == "http" and scope.get("method") == "OPTIONS") or is_public(path):
            return await self.app(scope, receive, send)
        user = check(token_from_scope(scope))
        if user is None or (scope["type"] == "http" and not allowed(user, scope["method"], path)):
            if scope["type"] == "websocket":
                from starlette.websockets import WebSocketClose
                return await WebSocketClose(code=4401)(scope, receive, send)
            from starlette.responses import JSONResponse
            code, msg = (401, "Cần đăng nhập") if user is None else (403, "Chỉ quản trị viên được đổi cài đặt chung")
            return await JSONResponse({"detail": msg}, status_code=code)(scope, receive, send)
        scope.setdefault("state", {})["user"] = user
        return await self.app(scope, receive, send)


class RedactTokenFilter(logging.Filter):
    """Không ghi token (tham số ?token= của WebSocket / ảnh) vào log truy cập."""
    PAT = re.compile(r"(token=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(self.PAT.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True
