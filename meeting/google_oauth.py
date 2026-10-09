"""Kết nối Google của từng người (bản web): OAuth 2.0 authorization code + refresh token.

- Quyền: `drive.file` (chỉ thấy file / thư mục do ứng dụng tạo hoặc người dùng chọn), `calendar.events.readonly` (đọc
  lịch để bot vào Google Meet, bản 3.18), kèm `openid email` để biết đúng tài khoản. Người dùng bỏ đánh dấu một quyền
  trên màn hình của Google thì tính năng đó báo cần kết nối lại, tính năng kia vẫn chạy.
- Tài khoản Google phải trùng email đăng nhập ứng dụng.
- Refresh token mã hóa AES-GCM (khóa sinh từ GOOGLE_TOKEN_KEY hoặc AUTH_SECRET) trước khi lưu vào Atlas
  (`google_tokens`); access token chỉ giữ trong RAM tới gần hết hạn.
- Google báo `invalid_grant` (người dùng thu hồi quyền, đổi mật khẩu, quá lâu không dùng): xóa kết nối, báo NeedReconnect.

Cần trên Google Cloud Console: OAuth client loại Web application có redirect URI `<server>/api/google/callback`, bật
Google Drive API và Google Calendar API, thêm 2 quyền trên vào màn hình đồng ý (nên để loại Internal).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from meeting import db

log = logging.getLogger("meeting.google")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
SCOPES = ("openid", "email", DRIVE_SCOPE, CALENDAR_SCOPE)
STATE_TTL_S = 600

_transport: Optional[httpx.AsyncBaseTransport] = None        # test thay bằng httpx.MockTransport
_ACCESS: Dict[str, Tuple[str, float]] = {}                  # email -> (access token, hết hạn lúc)


class OAuthError(Exception):
    """Lỗi khi kết nối (viết cho người dùng đọc)."""


class NeedReconnect(OAuthError):
    """Chưa kết nối, hoặc Google đã thu hồi quyền: người dùng cần bấm Kết nối Google lại."""


def client_id() -> str:
    return (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()


def configured() -> bool:
    return bool(client_id() and (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip())


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, transport=_transport)


def _col():
    return db._get_db()["google_tokens"]


# ------------------------------------------------------------------ mã hóa ---
def _key(purpose: bytes) -> bytes:
    secret = (os.getenv("GOOGLE_TOKEN_KEY") or os.getenv("AUTH_SECRET") or "").strip()
    if not secret:
        raise OAuthError("Server chưa đặt AUTH_SECRET nên chưa lưu được kết nối Google (báo quản trị viên)")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"meeting-copilot-google", info=purpose).derive(secret.encode())


def _encrypt(text: str, email: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key(b"refresh-token")).encrypt(nonce, text.encode(), email.encode())
    return base64.b64encode(nonce + ct).decode()


def _decrypt(blob: str, email: str) -> str:
    raw = base64.b64decode(blob)
    return AESGCM(_key(b"refresh-token")).decrypt(raw[:12], raw[12:], email.encode()).decode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_state(email: str, return_to: str, now: Optional[float] = None) -> str:
    body = _b64(json.dumps({"e": email, "r": return_to, "x": int((now or time.time()) + STATE_TTL_S),
                            "n": secrets.token_hex(8)}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(b"oauth-state"), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_state(state: str, check_expiry: bool = True) -> Dict[str, Any]:
    try:
        body, sig = (state or "").split(".")
        good = _b64(hmac.new(_key(b"oauth-state"), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            raise ValueError
        data = json.loads(_unb64(body))
    except (ValueError, TypeError):
        raise OAuthError("Yêu cầu kết nối Google không hợp lệ, hãy bấm Kết nối lại")
    if check_expiry and float(data.get("x") or 0) < time.time():
        raise OAuthError("Phiên kết nối Google đã quá 10 phút, hãy bấm Kết nối lại")
    return data


# --------------------------------------------------------------- luồng OAuth ---
def redirect_uri(base_url: str) -> str:
    return base_url.rstrip("/") + "/api/google/callback"


def auth_url(email: str, return_to: str, base_url: str) -> str:
    if not configured():
        raise OAuthError("Server chưa cấu hình GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET")
    return AUTH_ENDPOINT + "?" + urlencode({
        "client_id": client_id(), "redirect_uri": redirect_uri(base_url), "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "login_hint": email, "state": make_state(email, return_to)})


def _id_email(id_token: str) -> str:
    """Email trong id_token. Token nhận thẳng từ máy chủ Google qua HTTPS nên chỉ cần đọc phần nội dung."""
    try:
        return str(json.loads(_unb64(id_token.split(".")[1])).get("email") or "").lower()
    except (ValueError, IndexError, AttributeError):
        return ""


async def _token_request(data: Dict[str, str]) -> Dict[str, Any]:
    async with _http() as c:
        try:
            r = await c.post(TOKEN_ENDPOINT, data={**data, "client_id": client_id(),
                                                   "client_secret": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")})
        except httpx.HTTPError as e:
            raise OAuthError(f"Không kết nối được Google: {e}")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        err = str(body.get("error") or r.status_code)
        if err == "invalid_grant":
            raise NeedReconnect("Kết nối Google đã hết hạn hoặc bị thu hồi, hãy bấm Kết nối Google lại")
        raise OAuthError(f"Google từ chối ({err}): {str(body.get('error_description') or '')[:120]}".rstrip(": "))
    return body


async def exchange(code: str, state: str, base_url: str) -> Dict[str, str]:
    """Đổi mã Google trả về lấy token, lưu kết nối. Trả về {email, return_to}."""
    st = read_state(state)
    email = str(st.get("e") or "").lower()
    try:
        tok = await _token_request({"code": code, "redirect_uri": redirect_uri(base_url), "grant_type": "authorization_code"})
    except NeedReconnect:
        raise OAuthError("Mã đăng nhập Google không còn dùng được, hãy bấm Kết nối lại")
    g_email = _id_email(tok.get("id_token") or "")
    if g_email != email:
        raise OAuthError(f"Anh chị vừa chọn tài khoản Google {g_email or '(không rõ)'}, khác email đăng nhập {email}. "
                         "Hãy chọn đúng tài khoản công ty.")
    scopes = str(tok.get("scope") or "").split()
    if DRIVE_SCOPE not in scopes and CALENDAR_SCOPE not in scopes:
        raise OAuthError("Anh chị chưa cho phép quyền nào (Google Drive hoặc Lịch): hãy đánh dấu các ô trên màn hình "
                         "của Google")
    refresh = tok.get("refresh_token")
    old = _col().find_one({"email": email}) or {}
    if not refresh and not old.get("refresh_enc"):
        raise OAuthError("Google không gửi khóa dùng lâu dài, hãy bấm Kết nối lại")
    fields = {"email": email, "google_email": g_email, "scopes": scopes, "connected_at": time.time()}
    if refresh:
        fields["refresh_enc"] = _encrypt(refresh, email)
    _col().update_one({"email": email}, {"$set": fields}, upsert=True)
    if tok.get("access_token"):
        _ACCESS[email] = (tok["access_token"], time.time() + float(tok.get("expires_in") or 3600) - 60)
    log.info("meeting.google: %s đã kết nối Google (Drive: %s, Lịch: %s)", email, DRIVE_SCOPE in scopes,
             CALENDAR_SCOPE in scopes)
    return {"email": email, "return_to": str(st.get("r") or "")}


async def access_token(email: str) -> str:
    """Access token còn hạn của người này (tự làm mới). Chưa kết nối / bị thu hồi: NeedReconnect."""
    email = (email or "").lower()
    hit = _ACCESS.get(email)
    if hit and hit[1] > time.time():
        return hit[0]
    doc = _col().find_one({"email": email})
    if not doc or not doc.get("refresh_enc"):
        raise NeedReconnect(f"{email} chưa kết nối Google")
    try:
        refresh = _decrypt(doc["refresh_enc"], email)
    except Exception:
        raise NeedReconnect("Không đọc được khóa Google đã lưu (khóa của server đã đổi), hãy kết nối lại")
    try:
        tok = await _token_request({"refresh_token": refresh, "grant_type": "refresh_token"})
    except NeedReconnect:
        _col().delete_one({"email": email})
        _ACCESS.pop(email, None)
        log.warning("meeting.google: kết nối Google của %s đã bị thu hồi", email)
        raise
    _ACCESS[email] = (tok["access_token"], time.time() + float(tok.get("expires_in") or 3600) - 60)
    return tok["access_token"]


def status(email: Optional[str]) -> Dict[str, Any]:
    doc = _col().find_one({"email": (email or "").lower()}) if email else None
    if not doc or not doc.get("refresh_enc"):
        return {"connected": False}
    scopes = doc.get("scopes") or []
    return {"connected": True, "google_email": doc.get("google_email"), "connected_at": doc.get("connected_at"),
            "drive": DRIVE_SCOPE in scopes, "calendar": CALENDAR_SCOPE in scopes}


def forget_access(email: str) -> None:
    """Access token bị Google từ chối (401) trước hạn: lần sau lấy token mới."""
    _ACCESS.pop((email or "").lower(), None)


def get_doc(email: str) -> Dict[str, Any]:
    return db._strip(_col().find_one({"email": (email or "").lower()})) or {}


def update_doc(email: str, **fields: Any) -> None:
    if fields:
        _col().update_one({"email": (email or "").lower()}, {"$set": fields})


async def disconnect(email: str) -> None:
    email = (email or "").lower()
    doc = _col().find_one({"email": email})
    if doc and doc.get("refresh_enc"):
        try:
            refresh = _decrypt(doc["refresh_enc"], email)
            async with _http() as c:
                await c.post(REVOKE_ENDPOINT, data={"token": refresh})
        except Exception as e:                      # vẫn xóa ở phía mình
            log.info("meeting.google: thu hồi token ở Google lỗi (%s), vẫn xóa kết nối", e)
    _col().delete_one({"email": email})
    _ACCESS.pop(email, None)
