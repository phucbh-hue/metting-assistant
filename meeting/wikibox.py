"""Kho tri thức WikiBox (wiki.urbox.dev) qua MCP thật, thay kho tri thức giả lập (mcp_server/kb) - bản 14/10/2026.

WikiBox là nền tảng RAG của UrBox (giao diện "WikiBox", máy chủ MCP ở <WIKIBOX_URL>/mcp, 6 công cụ chỉ đọc: tìm tài liệu,
đọc nội dung, hỏi Thủ thư...). Thông tin công khai của máy chủ (/.well-known/oauth-protected-resource) cho biết:
- OAuth 2.1 + PKCE S256, tự đăng ký client (dynamic client registration), quyền `wikibox.read`, token Bearer ở header;
- hoặc API key cá nhân tạo ở WikiBox > Tùy chỉnh cá nhân > WikiBox MCP (cũng gửi dạng Bearer).
Cả hai đều chạy DƯỚI QUYỀN CỦA CHÍNH NGƯỜI ĐÓ trong workspace họ chọn: mỗi người chỉ đọc được tài liệu mình được xem.

Kết nối của từng người (bản web: email đăng nhập; bản trên máy: một kết nối chung LOCAL). Server giữ token (mã hóa
AES-GCM như kết nối Google, khóa từ WIKIBOX_TOKEN_KEY / GOOGLE_TOKEN_KEY / AUTH_SECRET) và tự gọi MCP, nên model nào cũng
dùng được (Claude API, Gemini, gói Claude.ai / ChatGPT qua CLI): model chỉ thấy kết quả tra cứu, không thấy token.
Cuộc họp dùng kết nối của người tạo cuộc họp (Bảng BD của nhóm cũng vậy).
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from meeting import auth, db

log = logging.getLogger("meeting.wikibox")

SCOPE = "wikibox.read"
LOCAL = "local"                       # bản trên máy (không đăng nhập): một kết nối chung
PENDING_TTL_S = 600
TOOLS_TTL_S = 3600
CALL_TIMEOUT_S = 30.0
SEARCH_TIMEOUT_S = 8.0                # Bảng BD: tìm trong wiki không được làm chậm câu trả lời quá lâu
_transport: Optional[httpx.AsyncBaseTransport] = None      # test thay bằng httpx.MockTransport (OAuth)
_META: Dict[str, Any] = {}
_ACCESS: Dict[str, Tuple[str, float]] = {}                # email -> (access token, hết hạn lúc)


class WikiboxError(Exception):
    """Lỗi viết cho người dùng đọc."""


class NeedConnect(WikiboxError):
    """Chưa kết nối WikiBox, hoặc kết nối đã bị thu hồi."""


def base_url() -> str:
    return (os.getenv("WIKIBOX_URL") or "https://wiki.urbox.dev").strip().rstrip("/")


def mcp_url() -> str:
    return (os.getenv("WIKIBOX_MCP_URL") or base_url() + "/mcp").strip()


def enabled() -> bool:
    return bool(base_url()) and (os.getenv("WIKIBOX_DISABLED") or "").strip().lower() not in ("1", "true", "yes")


def who(email: Optional[str]) -> str:
    """Khóa kết nối: email người dùng ở bản web, LOCAL ở bản chạy trên máy."""
    return (email or "").strip().lower() if auth.ENABLED else LOCAL


def owner_of(meeting: Optional[Dict[str, Any]]) -> str:
    return who((meeting or {}).get("owner"))


def _col():
    return db._get_db()["wikibox_tokens"]


def _pending():
    return db._get_db()["wikibox_pending"]


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, transport=_transport, follow_redirects=True)


# ------------------------------------------------------------------ mã hóa ---
def _key() -> bytes:
    secret = (os.getenv("WIKIBOX_TOKEN_KEY") or os.getenv("GOOGLE_TOKEN_KEY") or os.getenv("AUTH_SECRET") or "").strip()
    if not secret:
        raise WikiboxError("Server chưa đặt AUTH_SECRET (hoặc WIKIBOX_TOKEN_KEY) nên chưa lưu được kết nối WikiBox")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"meeting-copilot-wikibox", info=b"token").derive(secret.encode())


def _enc(text: str, owner: str) -> str:
    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(_key()).encrypt(nonce, text.encode(), owner.encode())).decode()


def _dec(blob: str, owner: str) -> str:
    raw = base64.b64decode(blob)
    return AESGCM(_key()).decrypt(raw[:12], raw[12:], owner.encode()).decode()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# --------------------------------------------------------- thông tin máy chủ ---
def _same_host_https(url: str) -> str:
    """Máy chủ đứng sau proxy tự ghi http:// trong metadata: cùng máy chủ thì gọi bằng https như địa chỉ đã cấu hình."""
    b, u = urlparse(base_url()), urlparse(url or "")
    if b.scheme == "https" and u.scheme == "http" and u.netloc == b.netloc:
        return "https" + url[4:]
    return url


async def _get_json(url: str) -> Dict[str, Any]:
    async with _http() as c:
        try:
            r = await c.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise WikiboxError(f"Không kết nối được WikiBox: {e}")
    if r.status_code >= 400:
        raise WikiboxError(f"WikiBox trả lỗi {r.status_code} khi đọc {urlparse(url).path}")
    try:
        return r.json()
    except ValueError:
        raise WikiboxError("WikiBox trả về dữ liệu không đọc được")


async def metadata(refresh: bool = False) -> Dict[str, Any]:
    """{resource, authorize, token, register, revoke} từ /.well-known của WikiBox (nhớ 1 giờ)."""
    if _META.get("at", 0) > time.time() - 3600 and not refresh and _META.get("base") == base_url():
        return _META["v"]
    pr = await _get_json(base_url() + "/.well-known/oauth-protected-resource")
    issuer = _same_host_https(((pr.get("authorization_servers") or [base_url()])[0]).rstrip("/"))
    asm = await _get_json(issuer + "/.well-known/oauth-authorization-server")
    v = {"resource": pr.get("resource") or mcp_url(), "scopes": pr.get("scopes_supported") or [SCOPE],
         "authorize": _same_host_https(asm.get("authorization_endpoint") or ""),
         "token": _same_host_https(asm.get("token_endpoint") or ""),
         "register": _same_host_https(asm.get("registration_endpoint") or ""),
         "revoke": _same_host_https(asm.get("revocation_endpoint") or "")}
    if not (v["authorize"] and v["token"]):
        raise WikiboxError("WikiBox chưa bật đăng nhập OAuth cho MCP: dùng API key cá nhân thay thế")
    _META.update(v=v, at=time.time(), base=base_url())
    return v


async def _client(redirect_uri: str) -> str:
    """client_id của ứng dụng này trên WikiBox: đăng ký một lần cho mỗi redirect URI (nhớ trong DB)."""
    key = "wikibox_client:" + hashlib.sha256(f"{base_url()}|{redirect_uri}".encode()).hexdigest()[:24]
    saved = db.get_setting(key, "")
    if saved:
        return json.loads(saved)["client_id"]
    meta = await metadata()
    if not meta["register"]:
        raise WikiboxError("WikiBox không cho ứng dụng tự đăng ký: dùng API key cá nhân thay thế")
    async with _http() as c:
        try:
            r = await c.post(meta["register"], json={
                "client_name": "UrBox Meeting Copilot", "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none", "scope": SCOPE})
        except httpx.HTTPError as e:
            raise WikiboxError(f"Không kết nối được WikiBox: {e}")
    if r.status_code >= 400:
        raise WikiboxError(f"WikiBox từ chối đăng ký ứng dụng ({r.status_code}): {r.text[:160]}")
    cid = str((r.json() or {}).get("client_id") or "")
    if not cid:
        raise WikiboxError("WikiBox không trả về client_id khi đăng ký ứng dụng")
    db.set_setting(key, json.dumps({"client_id": cid, "redirect_uri": redirect_uri, "at": time.time()}))
    log.info("meeting.wikibox: đã đăng ký ứng dụng với WikiBox cho %s", redirect_uri)
    return cid


# --------------------------------------------------------------- luồng OAuth ---
def redirect_uri(server_base: str) -> str:
    return server_base.rstrip("/") + "/api/wikibox/callback"


async def auth_url(email: str, return_to: str, server_base: str) -> str:
    """Trang đồng ý của WikiBox (người dùng đăng nhập wiki, chọn workspace, bấm Cho phép)."""
    owner = who(email)
    _key()                                            # báo sớm khi server chưa có khóa mã hóa
    meta = await metadata()
    ru = redirect_uri(server_base)
    cid = await _client(ru)
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(24)
    _pending().delete_many({"at": {"$lt": time.time() - PENDING_TTL_S}})
    _pending().insert_one({"state": state, "owner": owner, "verifier": verifier, "return_to": return_to,
                           "redirect_uri": ru, "client_id": cid, "at": time.time()})
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return meta["authorize"] + "?" + urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": ru, "scope": SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": meta["resource"]})


def return_of(state: str) -> str:
    p = _pending().find_one({"state": str(state or "")[:100]}) or {}
    return str(p.get("return_to") or "")


async def _token_request(data: Dict[str, str]) -> Dict[str, Any]:
    meta = await metadata()
    async with _http() as c:
        try:
            r = await c.post(meta["token"], data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise WikiboxError(f"Không kết nối được WikiBox: {e}")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        err = str(body.get("error") or r.status_code)
        if err in ("invalid_grant", "invalid_client"):
            raise NeedConnect("Kết nối WikiBox đã hết hạn hoặc bị thu hồi, hãy bấm Kết nối WikiBox lại")
        raise WikiboxError(f"WikiBox từ chối ({err}): {str(body.get('error_description') or '')[:120]}".rstrip(": "))
    if not body.get("access_token"):
        raise WikiboxError("WikiBox không trả về access token")
    return body


def _save_tokens(owner: str, tok: Dict[str, Any], client_id: str, keep_refresh: str = "") -> None:
    now = time.time()
    fields = {"owner": owner, "kind": "oauth", "client_id": client_id, "scope": str(tok.get("scope") or SCOPE),
              "access_enc": _enc(tok["access_token"], owner),
              "expires_at": now + float(tok.get("expires_in") or 3600) - 60, "updated_at": now}
    refresh = tok.get("refresh_token") or keep_refresh
    if refresh:
        fields["refresh_enc"] = _enc(refresh, owner)
    _col().update_one({"owner": owner}, {"$set": fields, "$setOnInsert": {"connected_at": now}}, upsert=True)
    _ACCESS[owner] = (tok["access_token"], fields["expires_at"])


async def exchange(code: str, state: str) -> Dict[str, str]:
    """WikiBox chuyển về /api/wikibox/callback: đổi mã lấy token, lưu kết nối, đọc danh sách công cụ."""
    p = _pending().find_one_and_delete({"state": str(state or "")[:100]})
    if not p or float(p.get("at") or 0) < time.time() - PENDING_TTL_S:
        raise WikiboxError("Phiên kết nối WikiBox đã quá 10 phút hoặc không hợp lệ, hãy bấm Kết nối lại")
    meta = await metadata()
    tok = await _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect_uri"],
                                "client_id": p["client_id"], "code_verifier": p["verifier"],
                                "resource": meta["resource"]})
    _save_tokens(p["owner"], tok, p["client_id"])
    _col().update_one({"owner": p["owner"]}, {"$unset": {"key_enc": "", "tools": "", "tools_at": ""}})
    log.info("meeting.wikibox: %s đã kết nối WikiBox (OAuth)", p["owner"])
    try:
        await tools(p["owner"], refresh=True)
    except WikiboxError as e:
        log.warning("meeting.wikibox: kết nối xong nhưng chưa đọc được danh sách công cụ: %s", e)
    return {"owner": p["owner"], "return_to": str(p.get("return_to") or "")}


async def save_key(email: str, api_key: str) -> Dict[str, Any]:
    """Dán API key cá nhân (WikiBox > Tùy chỉnh cá nhân > WikiBox MCP > Tạo key mới). Thử đọc công cụ trước khi lưu."""
    owner = who(email)
    key = (api_key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if len(key) < 16 or re.search(r"\s", key):
        raise WikiboxError("API key không đúng dạng: dán nguyên key WikiBox vừa tạo (chỉ hiện một lần)")
    try:
        found = await _list_tools_with(key)           # key sai thì báo ngay, không lưu
    except WikiboxError as e:
        if "token bị từ chối" in str(e):
            raise WikiboxError("WikiBox từ chối API key này (sai, đã thu hồi, hoặc thiếu quyền Truy xuất / retrieve)")
        raise
    now = time.time()
    _col().update_one({"owner": owner}, {"$set": {"owner": owner, "kind": "key", "key_enc": _enc(key, owner),
                                                  "tools": found, "tools_at": now, "updated_at": now},
                                         "$unset": {"access_enc": "", "refresh_enc": "", "expires_at": ""},
                                         "$setOnInsert": {"connected_at": now}}, upsert=True)
    _ACCESS.pop(owner, None)
    log.info("meeting.wikibox: %s đã kết nối WikiBox (API key, %d công cụ)", owner, len(found))
    return status(email)


async def access_token(owner: str, force_refresh: bool = False) -> str:
    """Token Bearer còn hạn của người này (API key, hoặc access token OAuth tự làm mới). Chưa kết nối: NeedConnect."""
    doc = _col().find_one({"owner": owner})
    if not doc:
        raise NeedConnect("Chưa kết nối WikiBox")
    try:
        if doc.get("kind") == "key":
            return _dec(doc["key_enc"], owner)
        hit = _ACCESS.get(owner)
        if hit and hit[1] > time.time() and not force_refresh:
            return hit[0]
        if not force_refresh and doc.get("access_enc") and float(doc.get("expires_at") or 0) > time.time():
            tok = _dec(doc["access_enc"], owner)
            _ACCESS[owner] = (tok, float(doc["expires_at"]))
            return tok
        refresh = _dec(doc["refresh_enc"], owner) if doc.get("refresh_enc") else ""
    except WikiboxError:
        raise
    except Exception:
        raise NeedConnect("Không đọc được khóa WikiBox đã lưu (khóa của server đã đổi), hãy kết nối lại")
    if not refresh:
        raise NeedConnect("Kết nối WikiBox đã hết hạn, hãy bấm Kết nối WikiBox lại")
    meta = await metadata()
    try:
        tok = await _token_request({"grant_type": "refresh_token", "refresh_token": refresh,
                                    "client_id": doc.get("client_id") or "", "resource": meta["resource"]})
    except NeedConnect:
        _col().delete_one({"owner": owner})
        _ACCESS.pop(owner, None)
        log.warning("meeting.wikibox: kết nối WikiBox của %s đã bị thu hồi", owner)
        raise
    _save_tokens(owner, tok, doc.get("client_id") or "", keep_refresh=refresh)
    return tok["access_token"]


def status(email: Optional[str]) -> Dict[str, Any]:
    owner = who(email)
    doc = _col().find_one({"owner": owner}) if owner else None
    out: Dict[str, Any] = {"available": enabled(), "url": base_url(), "mcp_url": mcp_url(), "connected": False}
    if doc and (doc.get("key_enc") or doc.get("refresh_enc") or doc.get("access_enc")):
        names = [t.get("name") for t in doc.get("tools") or []]
        st = search_tool(doc.get("tools") or [])
        out.update(connected=True, kind=doc.get("kind"), connected_at=doc.get("connected_at"), tools=names,
                   search_tool=st[0] if st else None, error=doc.get("error") or "")
    return out


async def disconnect(email: Optional[str]) -> Dict[str, Any]:
    owner = who(email)
    doc = _col().find_one({"owner": owner})
    if doc and doc.get("refresh_enc"):
        try:
            meta = await metadata()
            if meta.get("revoke"):
                async with _http() as c:
                    await c.post(meta["revoke"], data={"token": _dec(doc["refresh_enc"], owner),
                                                       "client_id": doc.get("client_id") or ""})
        except Exception as e:                        # vẫn xóa ở phía mình
            log.info("meeting.wikibox: thu hồi token ở WikiBox lỗi (%s), vẫn xóa kết nối", e)
    _col().delete_one({"owner": owner})
    _ACCESS.pop(owner, None)
    return status(email)


def connected(owner: str) -> bool:
    return enabled() and _col().count_documents({"owner": owner}, limit=1) > 0


# --------------------------------------------------------------- gọi MCP ---
def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


class Unauthorized(Exception):
    """WikiBox trả 401/403 (token sai, hết hạn, bị thu hồi). SDK MCP đổi lỗi này thành lỗi chung -32603, nên tự ghi nhận."""


async def _mcp(token: str, fn: Callable[[Any], Awaitable[Any]]) -> Any:
    """Mở một phiên MCP (streamable HTTP, Bearer), chạy fn(session), đóng phiên. Test thay hàm này."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    denied = {"status": 0}

    async def _seen(resp):
        if resp.status_code in (401, 403):
            denied["status"] = resp.status_code
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"},
                                      timeout=httpx2.Timeout(CALL_TIMEOUT_S)) as http:
        http.event_hooks = {**http.event_hooks, "response": [*http.event_hooks.get("response", []), _seen]}
        try:
            async with streamable_http_client(mcp_url(), http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    return await fn(s)
        except BaseException as e:                    # gồm cả ExceptionGroup của anyio
            if denied["status"]:
                raise Unauthorized(f"HTTP {denied['status']}") from None
            raise e


def _tool_list(res: Any) -> List[Dict[str, Any]]:
    return [{"name": t.name, "description": (t.description or "")[:600],
             "input_schema": _attr(t, "input_schema", "inputSchema", default={}) or {}} for t in res.tools]


async def _list_tools_with(token: str) -> List[Dict[str, Any]]:
    try:
        res = await asyncio.wait_for(_mcp(token, lambda s: s.list_tools()), CALL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise WikiboxError("WikiBox không phản hồi (quá 30 giây)")
    except Exception as e:
        raise WikiboxError(f"Không gọi được WikiBox MCP ({_short(e)})")
    return _tool_list(res)


def _short(e: BaseException) -> str:
    """Lỗi lồng trong ExceptionGroup của anyio: lấy lỗi gốc đầu tiên, viết ngắn."""
    while hasattr(e, "exceptions") and getattr(e, "exceptions"):
        e = e.exceptions[0]
    text = str(e) or type(e).__name__
    if isinstance(e, Unauthorized) or "401" in text or "Unauthorized" in text:
        return "token bị từ chối"
    return text[:160]


async def _with_token(owner: str, fn: Callable[[Any], Awaitable[Any]]) -> Any:
    """Gọi MCP bằng token của người này; OAuth bị từ chối thì làm mới token và thử lại một lần."""
    token = await access_token(owner)
    try:
        return await asyncio.wait_for(_mcp(token, fn), CALL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise WikiboxError("WikiBox không phản hồi (quá 30 giây)")
    except Exception as e:
        doc = _col().find_one({"owner": owner}) or {}
        if doc.get("kind") != "oauth" or "token bị từ chối" not in _short(e):
            raise WikiboxError(f"Không gọi được WikiBox MCP ({_short(e)})")
    token = await access_token(owner, force_refresh=True)
    try:
        return await asyncio.wait_for(_mcp(token, fn), CALL_TIMEOUT_S)
    except Exception as e:
        raise WikiboxError(f"Không gọi được WikiBox MCP ({_short(e)})")


async def tools(owner: str, refresh: bool = False) -> List[Dict[str, Any]]:
    """Công cụ của WikiBox (tools/list), nhớ trong DB 1 giờ."""
    doc = _col().find_one({"owner": owner})
    if not doc:
        raise NeedConnect("Chưa kết nối WikiBox")
    if not refresh and doc.get("tools") and float(doc.get("tools_at") or 0) > time.time() - TOOLS_TTL_S:
        return doc["tools"]
    try:
        found = _tool_list(await _with_token(owner, lambda s: s.list_tools()))
    except WikiboxError as e:
        _col().update_one({"owner": owner}, {"$set": {"error": str(e)}})
        if doc.get("tools"):
            return doc["tools"]                       # lỗi tạm: dùng danh sách cũ
        raise
    _col().update_one({"owner": owner}, {"$set": {"tools": found, "tools_at": time.time(), "error": ""}})
    return found


def parse_result(result: Any) -> Dict[str, Any]:
    """CallToolResult -> dict: ưu tiên structured content, rồi JSON trong text, rồi chữ thường."""
    from meeting import mcp_client
    return mcp_client.parse_result(result)


async def call(owner: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    names = {t["name"] for t in await tools(owner)}
    if name not in names:
        return {"error": f"WikiBox không có công cụ {name}"}
    try:
        return parse_result(await _with_token(owner, lambda s: s.call_tool(name, dict(arguments or {}))))
    except WikiboxError as e:
        return {"error": str(e)}


# --------------------------------------------------------- tìm tài liệu ---
_QUERY_KEYS = ("query", "q", "question", "keyword", "keywords", "search", "text", "prompt")
_LIMIT_KEYS = ("top_k", "limit", "k", "max_results", "n", "size", "match_count")


def search_tool(found: List[Dict[str, Any]]) -> Optional[Tuple[str, str, str]]:
    """(tên công cụ tìm kiếm, tham số câu hỏi, tham số số kết quả) đoán theo tên / mô tả / schema."""
    best, best_score = None, 0
    for t in found or []:
        props = ((t.get("input_schema") or {}).get("properties") or {})
        q = next((k for k in _QUERY_KEYS if k in props), None)
        if q is None:
            q = next((k for k, v in props.items() if (v or {}).get("type") == "string"), None)
        if q is None:
            continue
        name, desc = t.get("name", "").lower(), (t.get("description") or "").lower()
        score = (6 if "search" in name else 5 if "retriev" in name else 3 if ("query" in name or "find" in name) else 0)
        score += 2 if any(w in desc for w in ("search", "tìm", "retriev", "truy xuất")) else 0
        score -= 4 if any(w in name for w in ("chat", "ask", "librarian", "answer")) else 0
        if score > best_score:
            limit = next((k for k in _LIMIT_KEYS if k in props), "")
            best, best_score = (t["name"], q, limit), score
    return best


_LIST_KEYS = ("results", "hits", "items", "documents", "chunks", "data", "matches", "result")


def normalize_hits(data: Any, k: int = 5) -> List[Dict[str, Any]]:
    """Kết quả tìm kiếm (dạng chưa biết trước) -> [{title, text, url, score, kb}]."""
    items: Any = data
    if isinstance(data, dict):
        items = next((data[x] for x in _LIST_KEYS if isinstance(data.get(x), list)), None)
        if items is None and isinstance(data.get("text"), str):
            return [{"title": "WikiBox", "text": data["text"][:1800], "url": "", "score": None, "kb": ""}] if data["text"].strip() else []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, str):
            it = {"content": it}
        if not isinstance(it, dict):
            continue
        doc = it.get("document") if isinstance(it.get("document"), dict) else {}
        title = next((str(v) for v in (it.get("title"), it.get("document_title"), doc.get("title"), it.get("name"),
                                       it.get("file_name"), doc.get("file_name"), it.get("source")) if v), "Tài liệu WikiBox")
        text = next((str(v) for v in (it.get("content"), it.get("text"), it.get("snippet"), it.get("chunk"),
                                      it.get("excerpt"), it.get("matched_content"), it.get("summary")) if v), "")
        url = next((str(v) for v in (it.get("url"), it.get("link"), it.get("source_url"), it.get("document_url"),
                                     doc.get("url")) if v and str(v).startswith(("http://", "https://"))), "")
        score = next((v for v in (it.get("score"), it.get("relevance"), it.get("similarity")) if isinstance(v, (int, float))), None)
        kb = str(it.get("knowledge_base_name") or it.get("kb_name") or it.get("knowledge_base") or "")
        if text.strip() or title:
            out.append({"title": title[:200], "text": re.sub(r"\s+", " ", text).strip()[:1800], "url": url,
                        "score": score, "kb": kb[:120]})
    return out[:k]


async def search(owner: str, query: str, k: int = 5) -> List[Dict[str, Any]]:
    """Tìm trong kho tri thức của người này. Chưa kết nối / không có công cụ tìm kiếm: []. Lỗi: WikiboxError."""
    if not query.strip() or not connected(owner):
        return []
    st = search_tool(await tools(owner))
    if st is None:
        return []
    name, qkey, lkey = st
    args: Dict[str, Any] = {qkey: query[:500]}
    if lkey:
        args[lkey] = k
    res = parse_result(await _with_token(owner, lambda s: s.call_tool(name, args)))
    if res.get("error"):
        raise WikiboxError(str(res["error"])[:200])
    return normalize_hits(res, k)


async def search_for_meeting(meeting: Optional[Dict[str, Any]], query: str, k: int = 5) -> List[Dict[str, Any]]:
    """Bảng BD: tìm bằng kết nối của người tạo cuộc họp; lỗi / chậm quá SEARCH_TIMEOUT_S thì bỏ qua (trả [])."""
    owner = owner_of(meeting)
    if not owner or not connected(owner):
        return []
    try:
        return await asyncio.wait_for(search(owner, query, k), SEARCH_TIMEOUT_S)
    except (WikiboxError, asyncio.TimeoutError) as e:
        log.info("meeting.wikibox: tìm '%s' lỗi/chậm: %s", query[:60], e)
        return []


def agent_tool_lines(found: List[Dict[str, Any]], limit: int = 8) -> Dict[str, str]:
    """{"wikibox.<tên>": "wikibox.<tên>(tham số): mô tả"} để đưa vào danh sách công cụ của trợ lý."""
    out = {}
    for t in (found or [])[:limit]:
        props = ((t.get("input_schema") or {}).get("properties") or {})
        req = set((t.get("input_schema") or {}).get("required") or [])
        args = ", ".join(f"{p}{'' if p in req else '?'}" for p in list(props)[:6])
        desc = re.sub(r"\s+", " ", t.get("description") or "").strip()[:220]
        out[f"wikibox.{t['name']}"] = f"wikibox.{t['name']}({args}): [Kho tri thức WikiBox của UrBox] {desc}"
    return out
