"""FastAPI Server cho Meeting Assistant AI.

Phục vụ:
- REST API: Quản lý cuộc họp, hồ sơ người nói, đăng ký giọng nói, tra cứu MCP, xuất Artifacts
- WebSocket /ws/meeting/{id}/audio: Nhận luồng âm thanh PCM16 16kHz từ mic máy tính
- WebSocket /ws/meeting/{id}/events: Bắn sự kiện thời gian thực (transcript, hồ sơ người nói, wake-word,
  suy luận danh tính, thinking trace, sinh web/diagram, co-design chat duplex)
"""
import asyncio
import io
import json
import logging
import os
import re
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

# MEETING_ENV_FILE: tệp khóa nằm chỗ khác (bản Docker: data/.env trên ổ lưu lâu dài, để khóa nhập trong Cài đặt còn sau
# khi chạy lại). Biến môi trường do nền tảng chạy server đặt luôn được ưu tiên hơn tệp này.
load_dotenv(os.getenv("MEETING_ENV_FILE") or (Path(__file__).resolve().parent.parent / ".env"))
from meeting import envfile as _envfile  # noqa: E402
_ENV_FIXED = _envfile.clean_environ()   # trước khi các module khác đọc biến môi trường

import numpy as np  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from meeting import (artifacts, auth, cli_llm, db, decks, envfile, google_oauth, groups, live, llm, mcp,  # noqa: E402
                     recording, tts, voice, websearch)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("meeting.app")
for _name in ("uvicorn.access", "uvicorn.error"):        # token đăng nhập (?token=) không vào log
    logging.getLogger(_name).addFilter(auth.RedactTokenFilter())
if _ENV_FIXED:
    log.warning("meeting.app: đã bỏ dấu nháy / tiền tố \"TÊN=\" thừa trong biến môi trường: %s (nên sửa lại trên nền tảng "
                "chạy server)", ", ".join(_ENV_FIXED))

HERE = Path(__file__).resolve().parent
NO_LLM = ("Chưa có nguồn AI: đặt ANTHROPIC_API_KEY / GEMINI_API_KEY trong .env, hoặc chọn gói đăng ký "
          "(Claude.ai, ChatGPT, Gemini) trong Cài đặt")
DEFAULT_VOCAB = ["UrBox", "Kubernetes", "PostgreSQL", "Redis", "webhook", "idempotent", "voucher",
                 "merchant", "sprint", "DevOps", "latency", "schema", "microservices"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await asyncio.to_thread(db.init)
    await asyncio.to_thread(mcp.seed_mock_data)
    voice.ensure_model_async()
    await asyncio.to_thread(recording.purge_expired)     # bản ghi âm quá hạn lưu (RECORDING_RETENTION_DAYS)
    if tts.model_present():
        asyncio.get_running_loop().run_in_executor(None, tts.preload)   # nạp giọng đọc ở nền
    log.info("meeting.app: Server khởi động hoàn tất")
    yield
    for s in list(live.SESSIONS.values()):
        try:
            for st in list(s.streams.values()):
                await st.close()
            await s.drain(timeout=5)
            s.dispose()
        except Exception as e:
            log.warning("meeting.app: đóng session %s lỗi: %s", s.id, e)
    db.flush()                  # lưu trên máy: ghi nốt phần còn chờ trước khi tắt


app = FastAPI(title="Meeting Assistant AI", version="3.2.0", lifespan=lifespan)
_MEETING_PATH = re.compile(r"^/(?:api/meetings|ws/meeting)/(\d+)(?:/|$)")
_ARTIFACT_PATH = re.compile(r"^/api/artifacts/(\d+)(?:/|$)")
_GROUP_PATH = re.compile(r"^/api/groups/(\d+)(?:/|$)")
# id cuộc họp -> (email người tạo hoặc "", mã nhóm hoặc None). Người tạo không đổi; nhóm đổi qua API chuyển nhóm / xóa
# nhóm, các API đó xóa mục tương ứng.
_MEETING_KEYS: Dict[int, Tuple[str, Optional[int]]] = {}


def _meeting_key(mid: int) -> Optional[Tuple[str, Optional[int]]]:
    if mid not in _MEETING_KEYS:
        m = db.get_meeting(mid)
        if m is None:
            return None
        _MEETING_KEYS[mid] = (str(m.get("owner") or ""), m.get("group_id"))
    return _MEETING_KEYS[mid]


def _can_view(user: Optional[Dict[str, Any]], owner: str, gid: Optional[int]) -> bool:
    return auth.can_access(user, {"owner": owner, "group_id": gid}, groups.get_group(gid) if gid else None)


class MeetingAccessMiddleware:
    """Bản web: mỗi cuộc họp (API, WebSocket, sản phẩm AI của cuộc họp) chỉ người tạo và người trong nhóm của cuộc họp
    truy cập được; mỗi nhóm chỉ chủ nhóm và thành viên. Người khác nhận 404 như không tồn tại.
    Chạy SAU AuthMiddleware (đã biết người dùng)."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if auth.ENABLED and scope["type"] in ("http", "websocket"):
            path = scope.get("path") or ""
            mid = None
            m = _MEETING_PATH.match(path)
            if m:
                mid = int(m.group(1))
            else:
                a = _ARTIFACT_PATH.match(path)
                if a:
                    art = await asyncio.to_thread(db.get_artifact, int(a.group(1)))
                    mid = int(art["meeting_id"]) if art and art.get("meeting_id") is not None else None
            user = (scope.get("state") or {}).get("user")
            denied = False
            if mid is not None:
                key = await asyncio.to_thread(_meeting_key, mid)
                denied = key is not None and not await asyncio.to_thread(_can_view, user, *key)
            else:
                gm = _GROUP_PATH.match(path)
                if gm:
                    g = await asyncio.to_thread(groups.get_group, int(gm.group(1)))
                    denied = g is not None and groups.role_of(g, (user or {}).get("email")) is None
            if denied:
                if scope["type"] == "websocket":
                    from starlette.websockets import WebSocketClose
                    return await WebSocketClose(code=4404)(scope, receive, send)
                from starlette.responses import JSONResponse
                what = "nhóm" if mid is None else "cuộc họp"
                return await JSONResponse({"detail": f"Không tìm thấy {what}"}, status_code=404)(scope, receive, send)
        return await self.app(scope, receive, send)


# Thứ tự (ngoài -> trong): CORS -> đăng nhập -> quyền xem cuộc họp -> ứng dụng. Thêm sau = bọc ngoài.
app.add_middleware(MeetingAccessMiddleware)
# Bản web (AUTH_REQUIRED=1): chặn request chưa đăng nhập. CORS thêm SAU để bọc ngoài cùng: lỗi 401 vẫn có header CORS,
# trang trên Vercel (khác tên miền) đọc được và hiện màn hình đăng nhập.
app.add_middleware(auth.AuthMiddleware)
CORS_ORIGINS = [o.strip().rstrip("/") for o in (os.getenv("CORS_ORIGINS") or "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-Sample-Rate"])
app.mount("/static", StaticFiles(directory=str(HERE.parent / "static")), name="static")


# ==============================================================================
# REQUEST MODELS
# ==============================================================================
class MeetingCreate(BaseModel):
    title: str = "Cuộc họp nội bộ"
    description: str = ""
    meeting_type: str = "Technical Review"
    host_id: Optional[int] = None
    agenda: List[str] = Field(default_factory=list)
    expected_attendees: List[str] = Field(default_factory=list)
    vocab: Optional[List[str]] = None
    source: str = "mic"
    group_id: Optional[int] = None


class RenameReq(BaseModel):
    name: str
    role: str = ""
    email: str = ""
    save_voice: bool = False


class MergeReq(BaseModel):
    target_sid: int


class ReassignReq(BaseModel):
    target_sid: Optional[int] = None  # None = tạo người nói mới cho câu này


class AcceptReq(BaseModel):
    save_voice: bool = False
    name: Optional[str] = None


class StageReq(BaseModel):
    action: str
    artifact_id: Optional[int] = None
    slide: Optional[int] = None
    query: Optional[str] = None
    follow: Optional[bool] = None
    auto: bool = False
    kind: Optional[str] = None


class AssistantSettings(BaseModel):
    name: str
    aliases: List[str] = Field(default_factory=list)


class TtsSettings(BaseModel):
    engine: str = "auto"
    voice: str = "Linh"


class LibraryDirs(BaseModel):
    dirs: List[str] = Field(default_factory=list)


class OpenFileReq(BaseModel):
    path: str = Field(..., min_length=3, max_length=1000)


class ExplainReq(BaseModel):
    artifact_id: int
    node: str = Field("", max_length=200)       # mã nút của sơ đồ tư duy
    label: str = Field("", max_length=300)      # nhãn nút (sơ đồ Mermaid)
    req: str = Field("", max_length=80)         # mã yêu cầu để trang không áp dụng hai lần


class PresentReq(BaseModel):
    artifact_id: Optional[int] = None           # trống = nội dung đang chiếu


class PresentModeReq(BaseModel):
    mode: str                                   # auto | script | human
    source: Optional[str] = None                # notes | file | text
    artifact_id: Optional[int] = None
    path: Optional[str] = None
    query: Optional[str] = None
    text: Optional[str] = Field(None, max_length=60000)


class ProviderReq(BaseModel):
    provider: str
    api_fallback: Optional[bool] = None
    model: Optional[str] = Field(None, max_length=80)
    models: Optional[Dict[str, str]] = None      # {nguồn: model} cho nhiều nguồn một lần; "" = dùng mặc định


class SecretReq(BaseModel):
    name: str
    value: str = Field("", max_length=600)


class LoginCodeReq(BaseModel):
    code: str = Field(..., min_length=1, max_length=500)


class ProviderTestReq(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = Field(None, max_length=80)


class VoiceUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    department: Optional[str] = None
    email: Optional[str] = None


# ==============================================================================
# HELPERS
# ==============================================================================
async def _session_or_404(mid: int) -> live.MeetingSession:
    s = await live.get_session(mid)
    if s is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp")
    return s


async def _finish_op(s: live.MeetingSession):
    """Sau thao tác REST: chờ ghi DB xong rồi giải phóng session nếu cuộc họp đã kết thúc."""
    await s.drain()
    s.dispose_if_idle()


def _suggestion_from_inference(inf: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "identity_suggestion", "inference_id": inf["id"], "sid": inf.get("speaker_key"),
            "label": inf.get("unknown_label"), "suggested_name": inf.get("predicted_name"),
            "role": inf.get("predicted_role", ""), "confidence": inf.get("confidence", 0),
            "reasoning": inf.get("reasoning", ""), "evidence": inf.get("evidence", []), "conflict": False}


def _decode_audio(raw: bytes) -> bytes:
    """Nhận PCM16 16kHz mono thô hoặc file WAV (tự chuyển về 16kHz mono)."""
    if not raw.startswith(b"RIFF"):
        return raw
    try:
        with wave.open(io.BytesIO(raw), "rb") as w:
            if w.getsampwidth() != 2:
                raise HTTPException(status_code=400, detail="Chỉ hỗ trợ WAV 16-bit PCM")
            rate, ch = w.getframerate(), w.getnchannels()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="File WAV không hợp lệ")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != voice.RATE and len(x) > 1:
        n = int(len(x) * voice.RATE / rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def _anchor_for(vid: int) -> Dict[int, Dict[str, Any]]:
    full = db.get_voice(vid)
    if not full or not full.get("embedding") or not voice.is_valid_vector(full["embedding"]):
        return {}
    return {vid: {"name": full["name"], "vector": np.asarray(full["embedding"], dtype=np.float32),
                  "role": full.get("role", ""), "department": full.get("department", ""), "model": voice.model_of(full)}}


# ==============================================================================
# PAGES & HEALTH
# ==============================================================================
@app.get("/")
def index():
    # no-cache: trình duyệt luôn kiểm tra lại để nhận ngay bản giao diện mới sau khi cập nhật
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/vesper")
def vesper():
    return FileResponse(HERE.parent / "vesper.html")


@app.get("/config.js")
def web_config():
    """Cấu hình của trang: server tự phục vụ trang thì gọi API cùng địa chỉ. Bản Vercel tạo tệp này lúc build
    (scripts/build-web.mjs) với địa chỉ server riêng."""
    return Response("window.MA_CONFIG = window.MA_CONFIG || {};\n", media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    """Kiểm tra server còn sống (cho nền tảng chạy server; không cần đăng nhập, không lộ thông tin)."""
    return {"status": "ok"}


# ==============================================================================
# ĐĂNG NHẬP (bản web: AUTH_REQUIRED=1)
# ==============================================================================
class GoogleLoginReq(BaseModel):
    credential: str = Field(..., min_length=10, max_length=8192)


def _user(request: Request) -> Optional[Dict[str, Any]]:
    return getattr(request.state, "user", None)


@app.get("/api/auth/config")
def auth_config():
    return {"required": auth.ENABLED, "google_client_id": auth.CLIENT_ID if auth.ENABLED else "",
            "domains": auth.DOMAINS if auth.ENABLED else []}


@app.post("/api/auth/google")
async def auth_google(req: GoogleLoginReq):
    """ID token của Google Identity Services -> token đăng nhập của ứng dụng."""
    if not auth.ENABLED:
        raise HTTPException(status_code=400, detail="Bản chạy trên máy không cần đăng nhập")
    try:
        user = await asyncio.to_thread(auth.verify_google, req.credential)
    except auth.AuthError as e:
        raise HTTPException(status_code=403, detail=str(e))
    log.info("meeting.auth: %s đăng nhập", user["email"])
    return {**auth.issue(user), "user": {**user, "admin": auth.is_admin(user)}}


@app.get("/api/auth/me")
def auth_me(request: Request):
    return {"required": auth.ENABLED, "user": _user(request)}


def _db_status_text(st: Dict[str, Any]) -> str:
    mode = st.get("mode")
    if mode == "atlas":
        return f"Đã kết nối MongoDB Atlas ({st['database']})"
    if mode in ("mongodb", "mongodb_local"):
        return f"Đã kết nối MongoDB ({st['database']})"
    if mode == "local_file":
        why = f" Lý do: {st['atlas_error']}." if st.get("atlas_error") else ""
        return "Chưa kết nối được Atlas: đang lưu trên máy (data/local_db), không mất khi tắt server." + why
    return "In-Memory (dữ liệu mất khi tắt server)"


@app.get("/api/storage")
async def storage():
    """Đang lưu ở Atlas hay trên máy, lý do, và các cuộc họp trên máy chưa đồng bộ."""
    return await asyncio.to_thread(db.storage_info)


@app.post("/api/storage/sync")
async def storage_sync(payload: Optional[Dict[str, Any]] = None):
    """Đưa các cuộc họp ghi lúc mất kết nối Atlas (kho trên máy) lên Atlas."""
    dry = bool((payload or {}).get("dry_run"))
    try:
        return await asyncio.to_thread(db.sync_local_to_atlas, dry)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/health")
def health():
    db_st = db.get_status()
    soniox_key = bool(os.getenv("SONIOX_API_KEY"))
    return {
        "status": "ok",
        "campp": voice.get_diagnostics(),
        "soniox": {"status": "Đã cấu hình API key" if soniox_key else "Chưa cấu hình SONIOX_API_KEY",
                   "ready": soniox_key, "model": live.MeetingStream.MODEL},
        "web_search": {"provider": artifacts.web_provider(), "playwright": websearch.available(),
                       "claude": bool(os.getenv("ANTHROPIC_API_KEY"))},
        "mongodb": {"status": _db_status_text(db_st), "ready": True, "database": db_st["database"],
                    "is_mock": db_st["is_mock"], "mode": db_st["mode"], "atlas_error": db_st["atlas_error"]},
        "llm": {"provider": artifacts.provider(), "ready": artifacts.llm_available(),
                "claude_ready": bool(os.getenv("ANTHROPIC_API_KEY")),
                "gemini_ready": bool(os.getenv("GEMINI_API_KEY"))},
        "auto_enroll_voices": live.AUTO_ENROLL_AI,
        "voices_count": len(db.list_voices()),
        "live_sessions": len(live.SESSIONS),
    }


@app.get("/api/stats")
def stats(request: Request):
    u = _user(request)
    return db.get_stats(_visible_filter(u))


@app.get("/api/settings/assistant")
def get_assistant_settings():
    return {**llm.assistant_config(refresh=True), "generic": ["trợ lý ơi", "hey assistant", "@ai", "bot ơi"]}


@app.put("/api/settings/assistant")
def put_assistant_settings(req: AssistantSettings):
    """Đặt tên gọi trợ lý. Áp dụng ngay cho việc nhận lời gọi; từ vựng Soniox cập nhật ở lần bật mic sau."""
    try:
        cfg = llm.set_assistant_config(req.name, req.aliases)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**cfg, "generic": ["trợ lý ơi", "hey assistant", "@ai", "bot ơi"]}


# ------------------------------------------------------------ thư viện tài liệu trên máy ---
@app.get("/api/library")
async def library(q: str = "", limit: int = 60):
    """Tệp PDF / PowerPoint / Word / Markdown trong các thư mục tài liệu (khớp câu tìm, hoặc mới sửa trước)."""
    if auth.ENABLED:            # bản web: tài liệu nằm trên máy người dùng, trình duyệt tự liệt kê
        return {"items": [], "roots": [], "client": True}
    items = await asyncio.to_thread(decks.browse, q, max(1, min(int(limit), 200)))
    return {"items": items, "roots": [{"path": str(r), "name": r.name} for r in decks.library_roots()]}


def _library_settings() -> Dict[str, Any]:
    try:
        saved = [str(d) for d in json.loads(db.get_setting("library_dirs", "[]") or "[]")]
    except ValueError:
        saved = []
    env_dirs = [d for d in (os.getenv("LIBRARY_DIRS") or "").split(os.pathsep) if d.strip()]
    return {"roots": [{"path": str(r), "name": r.name} for r in decks.library_roots()], "saved": saved,
            "env": env_dirs, "defaults": os.getenv("LIBRARY_DEFAULTS", "1").strip() != "0",
            "slides_dir": str(decks.SLIDES_DIR), "libreoffice": bool(decks._soffice()), "client": auth.ENABLED}


@app.get("/api/settings/library")
def get_library_settings():
    return _library_settings()


@app.put("/api/settings/library")
def put_library_settings(req: LibraryDirs):
    """Thư mục tài liệu thêm (ngoài slides/, Desktop, Documents, Downloads, OneDrive)."""
    dirs: List[str] = []
    for d in req.dirs[:30]:
        d = os.path.expandvars(os.path.expanduser((d or "").strip().strip('"')))
        if not d:
            continue
        if not Path(d).is_dir():
            raise HTTPException(status_code=400, detail=f"Không thấy thư mục: {d}")
        if d not in dirs:
            dirs.append(d)
    db.set_setting("library_dirs", json.dumps(dirs, ensure_ascii=False))
    decks.invalidate()
    return _library_settings()


@app.get("/api/deck-assets/{key}/{name}")
def deck_asset(key: str, name: str):
    """Ảnh từng trang của tài liệu đã mở (dựng trên máy, nằm trong data/deck_assets)."""
    p = decks.asset_path(key, name)
    if p is None or not p.is_file():
        raise HTTPException(status_code=404, detail="Không có ảnh")
    return FileResponse(str(p), headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/meetings/{mid}/open-file")
async def open_file(mid: int, req: OpenFileReq):
    """Mở một tệp trong thư mục tài liệu lên màn hình trình chiếu, rồi hỏi tự trình bày hay theo kịch bản."""
    if auth.ENABLED:
        raise HTTPException(status_code=400, detail="Bản web mở tài liệu từ máy của anh chị: dùng Mở tài liệu để chọn tệp")
    p = decks.allowed(req.path)
    if p is None:
        raise HTTPException(status_code=400, detail="Tệp không đọc được hoặc không nằm trong thư mục tài liệu (thêm thư mục trong Cài đặt)")
    s = await _session_or_404(mid)
    q = await s.subscribe()
    try:
        await s.open_path(str(p))
    finally:
        s.unsubscribe(q)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    s.dispose_if_idle()
    return {"events": events, "artifact_id": s.stage["artifact_id"]}


class ClientLibraryReq(BaseModel):
    files: List[Dict[str, Any]] = Field(default_factory=list, max_length=5000)


class ClientFileErrorReq(BaseModel):
    request_id: str = Field(..., max_length=64)
    error: str = Field("", max_length=300)


@app.post("/api/meetings/{mid}/client-library")
async def set_client_library(mid: int, req: ClientLibraryReq):
    """Bản web: danh sách TÊN tệp trong các thư mục người dùng chọn trên máy, để trợ lý tìm tệp theo lời nói."""
    s = await _session_or_404(mid)
    n = s.set_client_library(req.files)
    s.dispose_if_idle()
    return {"files": n}


@app.post("/api/meetings/{mid}/doc-upload")
async def upload_document(mid: int, file: UploadFile = File(...), purpose: str = Form("open"),
                          request_id: str = Form(""), folder: str = Form("")):
    """Tệp gửi lên từ máy người dùng (bản web). purpose: open (mở lên màn hình), script (tệp kịch bản để trình bày),
    hoặc request_id (trợ lý vừa xin tệp này theo lời nói). Đọc xong là xóa, chỉ giữ ảnh từng trang."""
    if purpose not in ("open", "script"):
        raise HTTPException(status_code=400, detail="purpose phải là open hoặc script")
    s = await _session_or_404(mid)

    def chunks():
        while True:
            b = file.file.read(1024 * 1024)
            if not b:
                return
            yield b
    try:
        path = await asyncio.to_thread(decks.save_upload, mid, file.filename or "tai-lieu", chunks())
    except ValueError as e:
        if request_id:
            s.resolve_client_file(request_id, error=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    name = decks.safe_name(file.filename or "tai-lieu")
    if request_id:              # trợ lý đang chờ tệp này: phần còn lại (mở, đọc kịch bản, xóa tệp) do trợ lý làm
        if not s.resolve_client_file(request_id, path=str(path)):
            decks.discard_upload(path)
            raise HTTPException(status_code=410, detail="Trợ lý không còn chờ tệp này")
        return {"accepted": True}
    q = await s.subscribe()
    try:
        if purpose == "script":
            try:
                text = await asyncio.to_thread(decks.read_text, str(path))
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Không đọc được tệp kịch bản: {e}")
            res = await s.answer_present({"mode": "script", "source": "text", "text": text, "name": name})
        else:
            await s.open_path(str(path), display=(name, folder.strip()[:120]))
            res = {"artifact_id": s.stage["artifact_id"]}
    finally:
        s.unsubscribe(q)
        decks.discard_upload(path)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    s.dispose_if_idle()
    return {**res, "events": events}


@app.post("/api/meetings/{mid}/client-file-error")
async def client_file_error(mid: int, req: ClientFileErrorReq):
    """Trình duyệt không gửi được tệp trợ lý xin (chưa cho phép đọc thư mục, tệp đã xóa...): trợ lý báo lại ngay."""
    s = live.SESSIONS.get(mid)
    return {"ok": bool(s and s.resolve_client_file(req.request_id, error=req.error or "trình duyệt không gửi được tệp"))}


@app.post("/api/meetings/{mid}/explain")
async def explain_diagram_node(mid: int, req: ExplainReq):
    """Bấm vào một ý của sơ đồ: trợ lý giải thích ý đó theo nội dung cuộc họp (đọc to và hiện bên cạnh sơ đồ)."""
    s = await _session_or_404(mid)
    try:
        res = await s.explain_node(req.artifact_id, node=req.node, label=req.label, req=req.req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    s.dispose_if_idle()
    return res


@app.post("/api/meetings/{mid}/present")
async def present_on_stage(mid: int, req: PresentReq):
    """Nút Thuyết trình cho sơ đồ, dashboard: soạn lời nếu chưa có rồi trình bày từng phần (slide do trang tự đọc)."""
    s = await _session_or_404(mid)
    art = await s._get_artifact(req.artifact_id if req.artifact_id is not None else s.stage["artifact_id"])
    if not art or art.get("kind") not in ("diagram", "dashboard"):
        raise HTTPException(status_code=400, detail="Chỉ thuyết trình được sơ đồ hoặc dashboard ở đây")
    await s.present_artifact(art)
    s.dispose_if_idle()
    return {"ok": True, "artifact_id": s.stage["artifact_id"]}


@app.post("/api/meetings/{mid}/present-mode")
async def present_mode(mid: int, req: PresentModeReq):
    """Trả lời câu hỏi cách trình bày bằng nút bấm: tự trình bày, theo ghi chú, theo tệp kịch bản, dán kịch bản."""
    if req.mode not in ("auto", "script", "human"):
        raise HTTPException(status_code=400, detail="mode phải là auto, script hoặc human")
    ans: Dict[str, Any] = {"mode": req.mode}
    if req.mode == "script":
        if req.source not in ("notes", "file", "text"):
            raise HTTPException(status_code=400, detail="source phải là notes, file hoặc text")
        ans["source"] = req.source
        if req.source == "file":
            if req.path and auth.ENABLED:
                raise HTTPException(status_code=400, detail="Bản web: chọn tệp kịch bản trên máy để gửi lên")
            if req.path:
                p = decks.allowed(req.path)
                if p is None:
                    raise HTTPException(status_code=400, detail="Tệp kịch bản không nằm trong thư mục tài liệu")
                ans["path"] = str(p)
            elif req.query:
                ans["query"] = req.query
            else:
                raise HTTPException(status_code=400, detail="Thiếu đường dẫn hoặc tên tệp kịch bản")
        if req.source == "text":
            if not (req.text or "").strip():
                raise HTTPException(status_code=400, detail="Kịch bản đang trống")
            ans["text"] = req.text
    if req.artifact_id is not None:
        ans["artifact_id"] = req.artifact_id
    s = await _session_or_404(mid)
    if req.artifact_id is not None and (s._ask or {}).get("artifact_id") != req.artifact_id:
        s._ask = {"kind": "present_mode", "artifact_id": req.artifact_id, "until": time.monotonic() + live.ASK_S}
    res = await s.answer_present(ans)
    s.dispose_if_idle()
    return res


# ------------------------------------------------------------ nguồn AI: API key hoặc gói đăng ký ---
def _providers_info() -> Dict[str, Any]:
    api = [{"id": "claude", "kind": "api", "label": "Claude API (Anthropic)", "installed": True,
            "logged_in": bool(os.getenv("ANTHROPIC_API_KEY")), "login": "Đặt ANTHROPIC_API_KEY trong tệp .env"},
           {"id": "gemini", "kind": "api", "label": "Gemini API (Google)", "installed": True,
            "logged_in": bool(os.getenv("GEMINI_API_KEY")), "login": "Đặt GEMINI_API_KEY trong tệp .env"}]
    keys = envfile.status()
    api[0]["key_masked"], api[1]["key_masked"] = keys["ANTHROPIC_API_KEY"]["masked"], keys["GEMINI_API_KEY"]["masked"]
    subs = cli_llm.status()
    for it in api + subs:
        it["model_setting"] = artifacts.provider_model(it["id"])
        it["model_default"] = artifacts.default_model(it["id"])
        it["model"] = artifacts.effective_model(it["id"]) or "mặc định của gói"
    return {"current": artifacts.provider(), "api_fallback": artifacts.api_fallback(),
            "ready": artifacts.llm_available(), "providers": api + subs}


@app.get("/api/llm/providers")
async def llm_providers():
    return await asyncio.to_thread(_providers_info)


@app.put("/api/llm/provider")
async def set_llm_provider(req: ProviderReq):
    try:
        await asyncio.to_thread(artifacts.set_provider, req.provider, req.api_fallback, req.model, req.models)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await asyncio.to_thread(_providers_info)


def _local_only(request: Request) -> None:
    """Đổi khóa / đăng nhập gói chỉ từ chính máy đang chạy ứng dụng (bản web: chỉ quản trị viên)."""
    if auth.ENABLED:
        if not auth.is_admin(_user(request)):
            raise HTTPException(status_code=403, detail="Chỉ quản trị viên được đổi khóa và đăng nhập gói AI trên server")
        return
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="Chỉ đổi khóa và đăng nhập từ chính máy đang chạy ứng dụng")


@app.get("/api/settings/secrets")
def get_secrets():
    """Khóa dịch vụ trong .env: chỉ báo đã có hay chưa và 4 ký tự cuối, không trả về giá trị."""
    return envfile.status()


@app.put("/api/settings/secrets")
def put_secret(req: SecretReq, request: Request):
    """Nhập khóa ngay trên giao diện (máy mới chỉ cần chạy app rồi điền), ghi vào .env và áp dụng ngay."""
    _local_only(request)
    try:
        st = envfile.set_value(req.name, req.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        artifacts.reset_clients()
    return {"name": req.name, **st}


def _sub_provider(provider: str) -> str:
    if provider not in cli_llm.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Nguồn AI không hợp lệ: {provider}")
    return provider


@app.post("/api/llm/connect/{provider}")
async def llm_connect(provider: str, request: Request):
    """Nút Kết nối: CLI chính chủ mở trình duyệt để đăng nhập Claude.ai / ChatGPT / Google."""
    _local_only(request)
    try:
        return await asyncio.to_thread(cli_llm.start_login, _sub_provider(provider))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/llm/connect/{provider}")
def llm_connect_status(provider: str):
    return cli_llm.login_status(_sub_provider(provider))


@app.post("/api/llm/connect/{provider}/code")
def llm_connect_code(provider: str, req: LoginCodeReq, request: Request):
    _local_only(request)
    try:
        return cli_llm.submit_login_code(_sub_provider(provider), req.code)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/llm/connect/{provider}")
def llm_connect_cancel(provider: str, request: Request):
    _local_only(request)
    return cli_llm.cancel_login(_sub_provider(provider))


@app.get("/api/llm/models")
async def llm_models(provider: str, refresh: bool = False):
    """Model chọn được của một nguồn AI: API key thì lấy từ nhà cung cấp (không tốn token), gói đăng ký thì từ CLI."""
    try:
        return await artifacts.list_models(provider, refresh=refresh)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/llm/test")
async def test_llm_provider(req: ProviderTestReq):
    """Gửi một câu hỏi rất ngắn qua nguồn AI để kiểm tra đăng nhập / API key (tốn rất ít hạn mức)."""
    name = (req.provider or artifacts.provider()).strip().lower()
    model = (req.model or "").strip() or None
    if model and not artifacts.MODEL_RE.fullmatch(model):
        raise HTTPException(status_code=400, detail=f"Tên model không hợp lệ: {model}")
    t0 = time.time()
    artifacts.set_meeting(None, "kiểm tra nguồn AI")
    sys_p, ask = "Trả lời bằng tiếng Việt, đúng một câu ngắn.", "Chào một câu ngắn để kiểm tra kết nối."
    try:
        if name in cli_llm.PROVIDERS:
            text = await artifacts._cli_text(name, sys_p, ask, model=model)
        elif name == "claude" and os.getenv("ANTHROPIC_API_KEY"):
            text = await artifacts._claude_text(sys_p, ask, 60, model=model)
        elif name == "gemini" and os.getenv("GEMINI_API_KEY"):
            text = await artifacts._gemini_text(sys_p, ask, model=model)
        else:
            raise RuntimeError("Nguồn này chưa có API key trong .env" if name in artifacts.API_PROVIDERS else f"Nguồn AI không hợp lệ: {name}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "provider": name, "model": model or artifacts.effective_model(name),
            "text": (text or "").strip()[:300], "seconds": round(time.time() - t0, 1)}


@app.get("/api/directory")
def directory():
    """Danh bạ nhân sự (Mock MCP) để chọn nhanh khi đặt tên người nói."""
    return {"employees": mcp.call_tool("query_employee_directory", {"query": ""}).get("employees", [])}


# ==============================================================================
# VOICES API (QUẢN LÝ & THU MẪU SINH TRẮC HỌC GIỌNG NÓI)
# ==============================================================================
@app.get("/api/voices")
def get_voices():
    return {"voices": db.list_voices()}


@app.post("/api/voices/enroll-audio")
async def enroll_voice_audio(name: str = Form(...), role: str = Form(""), department: str = Form(""),
                             email: str = Form(""), audio: UploadFile = File(...)):
    """Thu mẫu giọng trực tiếp từ microphone (Voice Studio) - người được thu tự đồng ý."""
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Vui lòng nhập họ và tên")
    pcm = _decode_audio(await audio.read())
    if not pcm:
        raise HTTPException(status_code=400, detail="Dữ liệu âm thanh trống")
    v_sec = voice.voiced_s(pcm)
    if v_sec < voice.MIN_ANCHOR_S:
        raise HTTPException(status_code=400, detail=(
            f"Mẫu giọng quá ngắn: chỉ có {v_sec:.1f}s tiếng nói rõ, cần ít nhất {voice.MIN_ANCHOR_S:.0f}s. "
            "Hãy đọc to, rõ và gần micro hơn."))
    vec = await asyncio.to_thread(voice.embed, pcm, voice.MIN_ANCHOR_S)
    if vec is None:
        raise HTTPException(status_code=400, detail="Không trích xuất được vector đặc trưng giọng nói")
    vid = await asyncio.to_thread(db.save_voice, name, vec.tolist(), role.strip() or "Thành viên",
                                  department.strip(), email.strip(), "self_enrolled_mic", False, 1, "replace", voice.MODEL_ID)
    anchor = await asyncio.to_thread(_anchor_for, vid)
    for s in list(live.SESSIONS.values()):
        s.speakers.update_anchors(anchor, rebind=True)
        await s._sync_speakers()
    log.info("meeting.app: đã thu mẫu giọng cho '%s' (ID %d, %.1fs tiếng nói)", name, vid, v_sec)
    return {"success": True, "voice_id": vid, "name": name, "role": role.strip(),
            "voiced_seconds": round(v_sec, 2), "dimension": voice.DIM}


@app.post("/api/voices")
async def register_voice(name: str = Form(...), role: str = Form(""), department: str = Form(""),
                         email: str = Form(""), audio: Optional[UploadFile] = File(None)):
    if not name.strip():
        raise HTTPException(status_code=400, detail="Tên không được để trống")
    embedding = None
    if audio is not None:
        pcm = _decode_audio(await audio.read())
        vec = await asyncio.to_thread(voice.embed, pcm, voice.MIN_ANCHOR_S) if pcm else None
        if vec is not None:
            embedding = vec.tolist()
    vid = await asyncio.to_thread(db.save_voice, name.strip(), embedding, role.strip(), department.strip(),
                                  email.strip(), "admin", False, 1, "replace" if embedding else "merge", voice.MODEL_ID)
    return {"success": True, "id": vid, "name": name.strip(), "has_embedding": embedding is not None}


@app.patch("/api/voices/{vid}")
async def edit_voice(vid: int, req: VoiceUpdate):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "name" in fields and not fields["name"].strip():
        raise HTTPException(status_code=400, detail="Tên không được để trống")
    ok = await asyncio.to_thread(db.update_voice, vid, fields)
    if not ok:
        raise HTTPException(status_code=404, detail="Không tìm thấy hồ sơ giọng nói")
    anchor = await asyncio.to_thread(_anchor_for, vid)
    for s in list(live.SESSIONS.values()):
        s.speakers.update_anchors(anchor, rebind=False)
    v = await asyncio.to_thread(db.get_voice, vid) or {}
    v.pop("embedding", None)
    return {"success": True, "voice": v}


@app.delete("/api/voices/{vid}")
async def remove_voice(vid: int):
    ok = await asyncio.to_thread(db.delete_voice, vid)
    for s in list(live.SESSIONS.values()):
        s.forget_voice(vid)
        await s._sync_speakers()
    return {"success": ok}


# ==============================================================================
# MEETINGS API
# ==============================================================================
def _visible_filter(u: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Điều kiện lọc các cuộc họp một người xem được (bản web): của mình, của nhóm mình, và cuộc họp cũ nếu là quản trị viên."""
    if not auth.ENABLED or not u:
        return None
    return db.owner_filter(u["email"], include_unowned=auth.is_admin(u), group_ids=groups.group_ids_for(u["email"]))


@app.get("/api/meetings")
def get_meetings(request: Request, group_id: Optional[int] = None):
    u = _user(request)
    if group_id is not None:
        g = groups.get_group(group_id)
        if g is None or groups.role_of(g, (u or {}).get("email")) is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy nhóm")
        return {"meetings": db.list_meetings_with_stats(where={"group_id": g["id"]})}
    return {"meetings": db.list_meetings_with_stats(where=_visible_filter(u))}


@app.post("/api/meetings")
def create_meeting(req: MeetingCreate, request: Request):
    title = req.title.strip() or "Cuộc họp nội bộ"
    if req.group_id is not None:
        g = groups.get_group(req.group_id)
        if g is None or groups.role_of(g, (_user(request) or {}).get("email")) is None:
            raise HTTPException(status_code=403, detail="Anh chị không thuộc nhóm này nên không tạo cuộc họp trong nhóm được")
    mid = db.create_meeting(
        title=title,
        description=req.description.strip(),
        host_id=req.host_id,
        meeting_type=req.meeting_type.strip(),
        agenda=[a.strip() for a in req.agenda if a and a.strip()],
        expected_attendees=[a.strip() for a in req.expected_attendees if a and a.strip()],
        vocab=req.vocab if req.vocab else DEFAULT_VOCAB,
        source=req.source,
        owner=(_user(request) or {}).get("email") if auth.ENABLED else None,
    )
    if req.group_id is not None:
        groups.set_meeting_group(mid, req.group_id)
    _MEETING_KEYS.pop(mid, None)
    return {"success": True, "meeting_id": mid, "title": title, "group_id": req.group_id}


@app.get("/api/meetings/{mid}")
async def get_meeting_details(mid: int):
    m = await asyncio.to_thread(db.get_meeting, mid)
    if not m:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp")
    s = live.SESSIONS.get(mid)
    if s is None and m.get("status") != "ended":
        s = await live.get_session(mid)
    if s is None:
        speakers = [d for d in await asyncio.to_thread(db.list_speakers, mid)
                    if d.get("merged_into") is None and (d.get("n_segments") or 0) > 0]
        segs = await asyncio.to_thread(db.get_segments, mid)
        if segs and (not speakers or any(x.get("speaker_key") is None for x in segs)):
            s = await live.get_session(mid)  # dữ liệu cũ: dựng hồ sơ người nói từ nhãn rồi lưu lại
    if s is not None:
        await s.drain()
        segs, speakers = s.segments, s.public_speakers()
        suggestions = s.identity.pending_suggestions()
        s.dispose_if_idle()
    else:
        suggestions = [_suggestion_from_inference(i) for i in await asyncio.to_thread(db.get_inferences, mid, "pending")]
    return {
        "meeting": m,
        "segments": segs,
        "speakers": speakers,
        "suggestions": suggestions,
        "artifacts": await asyncio.to_thread(db.get_artifacts, mid),
        "recording": {**await asyncio.to_thread(recording.info, mid),
                      "enabled": bool((m.get("recording") or {}).get("enabled")) and m.get("status") != "ended"},
    }


@app.put("/api/meetings/{mid}")
def update_meeting_details(mid: int, payload: Dict[str, Any]):
    if not db.get_meeting(mid):
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp")
    ok = db.update_meeting(mid, payload)
    s = live.SESSIONS.get(mid)
    if s is not None and ok:
        s.meeting.update({k: v for k, v in payload.items() if k in db.MEETING_EDITABLE})
        s.title = s.meeting.get("title", s.title)
    return {"success": ok}


def _can_manage_meeting(u: Optional[Dict[str, Any]], m: Dict[str, Any]) -> bool:
    """Xóa / chuyển nhóm một cuộc họp: người tạo, hoặc chủ nhóm đang chứa cuộc họp (cuộc họp cũ chưa có chủ, không
    thuộc nhóm: quản trị viên). Thành viên nhóm chỉ xem và họp, không xóa cuộc họp của người khác."""
    if not auth.ENABLED:
        return True
    email = (u or {}).get("email", "")
    if (m.get("owner") or "") == email and email:
        return True
    g = groups.get_group(m["group_id"]) if m.get("group_id") else None
    if g is not None and groups.role_of(g, email) == "owner":
        return True
    return not m.get("owner") and not m.get("group_id") and auth.is_admin(u)


@app.delete("/api/meetings/{mid}")
async def delete_meeting_record(mid: int, request: Request):
    m = await asyncio.to_thread(db.get_meeting, mid)
    if m is not None and not _can_manage_meeting(_user(request), m):
        raise HTTPException(status_code=403, detail="Chỉ người tạo cuộc họp hoặc chủ nhóm được xóa cuộc họp này")
    s = live.SESSIONS.get(mid)
    if s is not None:
        await s.close()
    await asyncio.to_thread(recording.delete, mid)
    await asyncio.to_thread(decks.delete_uploads, mid)
    _MEETING_KEYS.pop(mid, None)
    return {"success": await asyncio.to_thread(db.delete_meeting, mid)}


# ==============================================================================
# KẾT NỐI GOOGLE (bản web): Drive để lưu biên bản, ghi âm
# ==============================================================================
class GoogleConnectReq(BaseModel):
    return_to: str = Field("", max_length=500)


def _base_url(request: Request) -> str:
    """Địa chỉ công khai của server (redirect URI của Google). Sau proxy HTTPS (Render) dùng X-Forwarded-Proto."""
    env = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if env:
        return env
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    return f"{proto}://{request.headers.get('host') or request.url.netloc}"


def _safe_return(url: str, request: Request) -> str:
    """Chỉ quay về trang của chính ứng dụng (server, hoặc giao diện trong CORS_ORIGINS), không chuyển hướng tùy ý."""
    from urllib.parse import urlparse
    base = _base_url(request)
    if not url:
        return base + "/"
    u = urlparse(url)
    origin = f"{u.scheme}://{u.netloc}"
    allowed = {base} | {o for o in CORS_ORIGINS if o != "*"}
    if u.scheme not in ("http", "https") or origin not in allowed:
        raise HTTPException(status_code=400, detail="Địa chỉ quay về sau khi kết nối Google không hợp lệ")
    return origin + (u.path or "/")


@app.get("/api/google/status")
def google_status(request: Request):
    u = _user(request)
    return {"available": auth.ENABLED and google_oauth.configured(), **google_oauth.status((u or {}).get("email"))}


@app.post("/api/google/connect")
def google_connect(req: GoogleConnectReq, request: Request):
    """Địa chỉ trang đồng ý của Google; trình duyệt chuyển sang đó, xong Google chuyển về /api/google/callback."""
    if not auth.ENABLED:
        raise HTTPException(status_code=400, detail="Kết nối Google Drive chỉ có ở bản web (cần đăng nhập)")
    u = _user(request)
    try:
        return {"url": google_oauth.auth_url(u["email"], _safe_return(req.return_to, request), _base_url(request))}
    except google_oauth.OAuthError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    from urllib.parse import quote
    back = _base_url(request) + "/"
    try:
        back = google_oauth.read_state(state, check_expiry=False).get("r") or back
    except google_oauth.OAuthError:
        pass
    if error:
        return RedirectResponse(f"{back}#/?drive=denied", status_code=303)
    try:
        res = await google_oauth.exchange(code, state, _base_url(request))
    except google_oauth.OAuthError as e:
        return RedirectResponse(f"{back}#/?drive=err&msg={quote(str(e)[:240])}", status_code=303)
    return RedirectResponse(f"{res['return_to'] or back}#/?drive=ok", status_code=303)


@app.post("/api/google/disconnect")
async def google_disconnect(request: Request):
    u = _user(request)
    if u:
        await google_oauth.disconnect(u["email"])
    return {"available": auth.ENABLED and google_oauth.configured(), **google_oauth.status((u or {}).get("email"))}


# ==============================================================================
# NHÓM CUỘC HỌP
# ==============================================================================
class GroupCreate(BaseModel):
    name: str = Field(..., max_length=200)


class GroupPatch(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    members: Optional[List[str]] = Field(None, max_length=200)
    recording_drive_days: Optional[int] = None


class MeetingGroupReq(BaseModel):
    group_id: Optional[int] = None


def _group_public(g: Dict[str, Any], email: Optional[str]) -> Dict[str, Any]:
    drive = g.get("drive") or {}
    return {"id": g["id"], "name": g["name"], "owner": g.get("owner"), "members": g.get("members") or [],
            "role": groups.role_of(g, email), "meeting_count": g.get("meeting_count"),
            "recording_drive_days": g.get("recording_drive_days"), "created_at": g.get("created_at"),
            "drive": {k: drive.get(k) for k in ("folder_url", "status", "error") if drive.get(k)}}


def _group_for(gid: int, request: Request, need_owner: bool = False) -> Dict[str, Any]:
    email = (_user(request) or {}).get("email")
    g = groups.get_group(gid)
    if g is None or groups.role_of(g, email) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhóm")
    if need_owner and groups.role_of(g, email) != "owner":
        raise HTTPException(status_code=403, detail="Chỉ chủ nhóm được thay đổi nhóm")
    return g


@app.get("/api/groups")
def list_groups(request: Request):
    email = (_user(request) or {}).get("email")
    return {"groups": [_group_public(g, email) for g in groups.list_groups_for(email)]}


@app.post("/api/groups")
def create_group(req: GroupCreate, request: Request):
    email = (_user(request) or {}).get("email")
    try:
        g = groups.create_group(req.name, email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _group_public({**g, "meeting_count": 0}, email)


@app.get("/api/groups/{gid}")
def get_group(gid: int, request: Request):
    g = _group_for(gid, request)
    return _group_public({**g, "meeting_count": len(groups.group_meeting_ids(g["id"]))}, (_user(request) or {}).get("email"))


@app.patch("/api/groups/{gid}")
def patch_group(gid: int, req: GroupPatch, request: Request):
    g = _group_for(gid, request, need_owner=True)
    try:
        g = groups.update_group(g["id"], name=req.name, members=req.members, recording_drive_days=req.recording_drive_days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _group_public({**g, "meeting_count": len(groups.group_meeting_ids(g["id"]))}, (_user(request) or {}).get("email"))


@app.delete("/api/groups/{gid}")
def delete_group(gid: int, request: Request):
    g = _group_for(gid, request, need_owner=True)
    for mid in groups.group_meeting_ids(g["id"]):
        _MEETING_KEYS.pop(mid, None)
    return {"success": groups.delete_group(g["id"])}


@app.put("/api/meetings/{mid}/group")
def move_meeting_group(mid: int, req: MeetingGroupReq, request: Request):
    """Chuyển cuộc họp vào nhóm (hoặc gỡ khỏi nhóm): người tạo / chủ nhóm hiện tại, và phải thuộc nhóm đích."""
    u = _user(request)
    m = db.get_meeting(mid)
    if not m:
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc họp")
    if not _can_manage_meeting(u, m):
        raise HTTPException(status_code=403, detail="Chỉ người tạo cuộc họp hoặc chủ nhóm được chuyển cuộc họp này")
    if req.group_id is not None:
        g = groups.get_group(req.group_id)
        if g is None or groups.role_of(g, (u or {}).get("email")) is None:
            raise HTTPException(status_code=403, detail="Anh chị không thuộc nhóm đích")
    groups.set_meeting_group(mid, req.group_id)
    _MEETING_KEYS.pop(mid, None)
    s = live.SESSIONS.get(mid)
    if s is not None:
        if req.group_id is None:
            s.meeting.pop("group_id", None)
        else:
            s.meeting["group_id"] = req.group_id
    return {"success": True, "group_id": req.group_id}


class RecordingReq(BaseModel):
    enabled: bool
    consent: bool = False      # bật: chủ phòng xác nhận mọi người trong phòng đã đồng ý ghi âm


@app.post("/api/meetings/{mid}/recording")
async def set_meeting_recording(mid: int, req: RecordingReq, request: Request):
    """Bật / tắt lưu âm thanh cuộc họp trên máy này (dữ liệu sinh trắc học: cần xác nhận mọi người đồng ý)."""
    if req.enabled and not req.consent:
        raise HTTPException(status_code=400, detail="Cần xác nhận mọi người trong phòng đã đồng ý ghi âm")
    s = await _session_or_404(mid)
    if req.enabled and not s.is_live():
        raise HTTPException(status_code=400, detail="Cuộc họp đã kết thúc")
    u = _user(request)
    return await s.set_recording(req.enabled, consent_by=u["email"] if u else "meeting_host")


@app.delete("/api/meetings/{mid}/recording")
async def delete_meeting_recording(mid: int):
    """Xóa toàn bộ âm thanh đã lưu của cuộc họp (và tắt ghi âm nếu đang bật)."""
    s = live.SESSIONS.get(mid)
    if s is not None and s.recording_on():
        await s.set_recording(False)
    return {"success": await asyncio.to_thread(recording.delete, mid)}


@app.post("/api/meetings/{mid}/archive")
async def archive_meeting(mid: int):
    """Kết thúc cuộc họp và lập biên bản ở chế độ nền (sự kiện artifact_created khi xong)."""
    s = await _session_or_404(mid)
    res = await s.finish(generate_minutes=True)
    return {"success": True, "meeting_id": mid, **res}


@app.post("/api/meetings/{mid}/end")
async def end_meeting(mid: int):
    s = await _session_or_404(mid)
    res = await s.finish(generate_minutes=False)
    return {"success": True, "meeting_id": mid, **res}


@app.post("/api/meetings/{mid}/set-host")
def set_meeting_host(mid: int, payload: Dict[str, Any]):
    host_id = payload.get("host_id")
    if not host_id:
        raise HTTPException(status_code=400, detail="Thiếu host_id")
    db.update_meeting(mid, {"host_id": int(host_id)})
    s = live.SESSIONS.get(mid)
    if s is not None:
        s.set_host(int(host_id))
    return {"success": True, "host_id": host_id}


# ==============================================================================
# SPEAKERS: ĐỔI TÊN, GỘP, SỬA NGƯỜI NÓI CỦA TỪNG CÂU, LƯU GIỌNG
# ==============================================================================
@app.get("/api/meetings/{mid}/speakers")
async def list_meeting_speakers(mid: int):
    s = await _session_or_404(mid)
    out = s.public_speakers()
    s.dispose_if_idle()
    return {"speakers": out}


@app.post("/api/meetings/{mid}/speakers/{sid}/rename")
async def rename_meeting_speaker(mid: int, sid: int, req: RenameReq):
    s = await _session_or_404(mid)
    try:
        res = await s.rename_speaker(sid, req.name, role=req.role, email=req.email, save_voice=req.save_voice)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _finish_op(s)
    return res


@app.post("/api/meetings/{mid}/speakers/{sid}/merge")
async def merge_meeting_speaker(mid: int, sid: int, req: MergeReq):
    s = await _session_or_404(mid)
    try:
        res = await s.merge_speakers(sid, req.target_sid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await _finish_op(s)
    return res


@app.post("/api/meetings/{mid}/speakers/{sid}/save-voice")
async def save_speaker_voice(mid: int, sid: int, request: Request):
    s = await _session_or_404(mid)
    u = _user(request)
    res = await s.save_profile_voice(sid, consent_by=u["email"] if u else "meeting_host_confirm")
    await _finish_op(s)
    return res


class TtsReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    speed: Optional[float] = Field(None, ge=0.6, le=1.6)


@app.get("/api/usage")
async def llm_usage(request: Request):
    """Số lần gọi LLM, token vào/ra, chi phí ước tính: toàn bộ, theo cuộc họp, theo việc, theo model."""
    data = await asyncio.to_thread(db.usage_summary)
    if auth.ENABLED:            # bản web: không lộ tên cuộc họp của người khác
        u, keys = _user(request), await asyncio.to_thread(db.meeting_keys)
        for row in data.get("by_meeting") or []:
            mid = row.get("meeting_id")
            if mid is not None and not _can_view(u, *keys.get(mid, ("", None))):
                row["title"] = "(cuộc họp của người khác)"
    return data


@app.get("/api/meetings/{mid}/export")
async def export_meeting(mid: int):
    """Toàn bộ dữ liệu cuộc họp (kể cả vector giọng) dạng JSON để lưu ra tệp hoặc phân tích ngoại tuyến."""
    data = await asyncio.to_thread(db.export_meeting, mid)
    if data is None:
        raise HTTPException(status_code=404, detail="Cuộc họp không tồn tại")
    ls = live.SESSIONS.get(mid)
    if ls is not None:   # phiên đang mở: lấy luôn các câu vừa xử lý nhưng DB chưa kịp ghi
        await ls.drain()
        data["segments"] = await asyncio.to_thread(db.get_segments, mid, True)
    return Response(content=json.dumps(data, ensure_ascii=False, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="meeting-{mid}.json"'})


@app.get("/api/tts/status")
async def tts_status():
    """Giọng đọc đang dùng (Soniox hoặc Piper trên máy) và đã sẵn sàng chưa."""
    return tts.status()


@app.get("/api/settings/tts")
def get_tts_settings():
    return {**tts.settings(), "active": tts.engine(), "voices": tts.SONIOX_VOICES,
            "soniox_ready": tts.soniox_ready(), "piper_ready": tts.model_present()}


@app.put("/api/settings/tts")
def put_tts_settings(req: TtsSettings):
    """Chọn nguồn giọng đọc: auto (Soniox nếu có khóa), soniox, piper (trên máy) và giọng Soniox."""
    try:
        tts.save_settings(req.engine, req.voice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_tts_settings()


@app.post("/api/tts/stream")
async def tts_stream(req: TtsReq):
    """Đọc văn bản, trả PCM 16-bit mono từng mảnh ngay khi có (header X-Sample-Rate) để trình duyệt phát ngay.

    Soniox: gửi câu tới Soniox để đọc; lỗi hoặc không có khóa thì đọc bằng Piper trên máy."""
    try:
        rate, engine, chunks = await tts.open_stream(req.text, req.speed)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return StreamingResponse(chunks, media_type="application/octet-stream",
                             headers={"X-Sample-Rate": str(rate), "X-Tts-Engine": engine, "Cache-Control": "no-store"})


@app.post("/api/tts")
async def tts_speak(req: TtsReq):
    """Đọc văn bản thành WAV ngay trên máy chủ (không gửi nội dung ra dịch vụ ngoài)."""
    try:
        wav = await asyncio.to_thread(tts.synthesize, req.text, req.speed)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.post("/api/meetings/{mid}/stage")
async def control_stage(mid: int, req: StageReq):
    """Điều khiển màn hình trình bày: show / next / prev / goto / topic / back."""
    from meeting.live import MeetingSession
    if req.action not in MeetingSession.STAGE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Lệnh không hợp lệ: {req.action}")
    s = await _session_or_404(mid)
    try:
        res = await s.stage_action(req.action, artifact_id=req.artifact_id, slide=req.slide, query=req.query,
                                   follow=req.follow, auto=req.auto, kind=req.kind)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    s.dispose_if_idle()
    return res


@app.post("/api/meetings/{mid}/insights")
async def meeting_insights(mid: int, payload: Optional[Dict[str, Any]] = None):
    """Trợ lý xem lại cuộc họp và nêu nhận xét (đọc to trên giao diện)."""
    if not artifacts.llm_available():
        raise HTTPException(status_code=503, detail=NO_LLM)
    s = await _session_or_404(mid)
    try:
        items = await s.analyze_now((payload or {}).get("focus", ""))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gọi LLM thất bại: {e}")
    s.dispose_if_idle()
    return {"insights": items}


@app.post("/api/meetings/{mid}/reanalyze")
async def reanalyze_meeting(mid: int):
    """Chạy lại nhận diện người nói cho toàn bộ cuộc họp (giữ tên đã đặt)."""
    s = await _session_or_404(mid)
    res = await s.reanalyze()
    await _finish_op(s)
    return res


@app.post("/api/meetings/{mid}/segments/{seq}/speaker")
async def reassign_meeting_segment(mid: int, seq: int, req: ReassignReq):
    s = await _session_or_404(mid)
    try:
        res = await s.reassign_segment(seq, req.target_sid)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await _finish_op(s)
    return res


@app.post("/api/meetings/{mid}/segments/{seq}/split-after")
async def split_speaker_after(mid: int, seq: int):
    """Từ câu này trở đi là người khác: tách thành người nói mới (sửa lỗi hai giọng giống nhau bị gộp)."""
    s = await _session_or_404(mid)
    try:
        res = await s.split_speaker_from(seq)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _finish_op(s)
    return res


# ==============================================================================
# SUY LUẬN DANH TÍNH (AI ĐOÁN TÊN) & XÁC NHẬN GỢI Ý
# ==============================================================================
@app.post("/api/meetings/{mid}/infer-speakers")
async def infer_meeting_speakers(mid: int):
    """AI đọc hội thoại để đoán tên cho mọi người nói chưa định danh."""
    if not artifacts.llm_available():
        raise HTTPException(status_code=503, detail=NO_LLM)
    s = await _session_or_404(mid)
    try:
        results = await s.identity.run(force=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gọi LLM thất bại: {e}")
    await _finish_op(s)
    return {"success": True, "meeting_id": mid, "results": results,
            "renamed": [r for r in results if r["action"] == "applied"],
            "suggested": [r for r in results if r["action"] == "suggested"],
            "count": len(results)}


@app.post("/api/meetings/{mid}/inferences/{iid}/accept")
async def accept_inference(mid: int, iid: int, req: AcceptReq):
    s = await _session_or_404(mid)
    try:
        res = await s.identity.accept(iid, save_voice=req.save_voice, name=req.name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await _finish_op(s)
    return res


@app.post("/api/meetings/{mid}/inferences/{iid}/dismiss")
async def dismiss_inference(mid: int, iid: int):
    s = await _session_or_404(mid)
    res = await s.identity.dismiss(iid)
    await _finish_op(s)
    return res


@app.post("/api/meetings/{mid}/confirm-identity")
async def confirm_identity(mid: int, payload: Dict[str, Any]):
    """(Tương thích API cũ) Đổi tên theo nhãn hiển thị và lưu mẫu giọng."""
    unknown_label, confirmed_name = payload.get("unknown_label"), payload.get("confirmed_name")
    if not unknown_label or not confirmed_name:
        raise HTTPException(status_code=400, detail="Thiếu unknown_label hoặc confirmed_name")
    s = await _session_or_404(mid)
    p = s.speakers.find_by_label(unknown_label)
    if p is None:
        raise HTTPException(status_code=404, detail=f"Không có người nói '{unknown_label}' trong cuộc họp")
    res = await s.rename_speaker(p.sid, confirmed_name, role=payload.get("role", ""),
                                 email=payload.get("email", ""), save_voice=payload.get("save_voice", True))
    await _finish_op(s)
    return res


# ==============================================================================
# AI ASSISTANT & CO-DESIGN CHAT DUPLEX
# ==============================================================================
@app.post("/api/meetings/{mid}/command")
async def assistant_command(mid: int, payload: Dict[str, Any]):
    """Câu lệnh gõ cho trợ lý, xử lý giống hệt khi gọi bằng giọng nói (chuyển slide, nhắc bài, hỏi đáp...).

    Kết quả phát qua WebSocket sự kiện; đồng thời trả về danh sách sự kiện để trang không có WebSocket
    (cuộc họp đã kết thúc) vẫn hiển thị được."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Thiếu nội dung câu lệnh")
    s = await _session_or_404(mid)
    intent = llm.stage_intent(text)
    loose = bool(intent and intent.get("action") == "explain" and intent.get("loose"))   # có thể là câu hỏi chung
    needs_llm = (intent is None or loose) and not (s.stage["artifact_id"] and llm.is_edit_command(text))
    if needs_llm and not artifacts.llm_available():
        raise HTTPException(status_code=503, detail=NO_LLM)
    q = await s.subscribe()
    try:
        await s._handle_ai_activation(text, text, llm.assistant_config()["name"], source="text")
    finally:
        s.unsubscribe(q)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    s.dispose_if_idle()
    return {"events": events}


@app.post("/api/meetings/{mid}/ai-ask")
async def ask_assistant(mid: int, payload: Dict[str, Any]):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Thiếu nội dung câu hỏi")
    if not artifacts.llm_available():
        raise HTTPException(status_code=503, detail=NO_LLM)
    ls = live.SESSIONS.get(mid)
    segs = ls.segments if ls is not None else await asyncio.to_thread(db.get_segments, mid)

    async def _on_thinking(text: str):
        if ls is not None:
            await ls.emit({"type": "ai_thinking", "text": text})

    async def _on_tool(tool_info: Dict[str, Any]):
        if ls is not None:
            await ls.emit({"type": "ai_tool_call", "tool": tool_info.get("tool"), "args": tool_info.get("args")})

    async def _on_insights(items: List[Dict[str, str]]):
        if ls is not None:
            await ls.emit({"type": "ai_insights", "items": items, "source": "request"})

    async def _on_progress(text: str, kind: str = "progress"):
        if ls is not None:
            await ls.emit({"type": "ai_progress", "text": text, "kind": kind})

    try:
        res = await llm.think_and_act(meeting_id=mid, prompt=prompt, segments=segs, trigger="chat_message",
                                      on_thinking=_on_thinking, on_tool=_on_tool, on_insights=_on_insights,
                                      on_progress=_on_progress, meeting=ls.meeting if ls is not None else None,
                                      stage_art=ls._stage_summary() if ls is not None else None,
                                      library=await ls._library_summary() if ls is not None else None)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Trợ lý AI lỗi: {e}")
    if ls is not None:
        await ls.emit({"type": "ai_response", "response": res})
        await ls.apply_ai_result(res)
    return res


@app.post("/api/meetings/{mid}/co-design")
async def co_design(mid: int, payload: Dict[str, Any]):
    artifact_id, feedback = payload.get("artifact_id"), (payload.get("feedback") or "").strip()
    if not artifact_id or not feedback:
        raise HTTPException(status_code=400, detail="Thiếu artifact_id hoặc feedback")
    ls = live.SESSIONS.get(mid)
    slide = payload.get("slide")
    if slide is None and ls is not None and ls.stage["artifact_id"] == int(artifact_id):
        slide = ls.stage["slide"]
    try:
        res = await artifacts.co_design_refine(meeting_id=mid, artifact_id=int(artifact_id), user_feedback=feedback,
                                               slide_index=int(slide) if slide is not None else None)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Co-design lỗi: {e}")
    if ls is not None:
        await ls.emit({"type": "artifact_updated", "artifact": res})
        if ls.stage["artifact_id"] in (int(artifact_id), None):
            await ls.stage_action("show", artifact_id=res["id"], slide=res.get("focus_slide", slide))
    return res


@app.get("/api/artifacts/{aid}")
def get_artifact_content(aid: int):
    art = db.get_artifact(aid)
    if not art:
        raise HTTPException(status_code=404, detail="Không tìm thấy artifact")
    return art


# ==============================================================================
# WEBSOCKET CHANNELS
# ==============================================================================
def _dumps(ev: Dict[str, Any]) -> str:
    return json.dumps(ev, ensure_ascii=False, default=str)


@app.websocket("/ws/meeting/{mid}/events")
async def ws_events(ws: WebSocket, mid: int):
    """Kênh sự kiện thời gian thực. Gửi và nhận chạy song song, mọi tin gửi đi đều qua một hàng đợi."""
    await ws.accept()
    s = await live.get_session(mid)
    if s is None:
        await ws.close(code=4004)
        return
    q = await s.subscribe()
    try:
        init = s.snapshot()
        init["artifacts"] = await asyncio.to_thread(db.get_artifacts, mid)
        await ws.send_text(_dumps(init))

        async def sender():
            while True:
                await ws.send_text(_dumps(await q.get()))

        async def receiver():
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "ping":
                    q.put_nowait({"type": "pong"})
                elif msg.get("type") == "co_design_chat" and msg.get("artifact_id") and msg.get("feedback"):
                    asyncio.create_task(_ws_co_design(s, int(msg["artifact_id"]), str(msg["feedback"])))

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                log.debug("ws_events %d kết thúc: %s", mid, exc)
    except WebSocketDisconnect:
        pass
    finally:
        s.unsubscribe(q)
        s.dispose_if_idle()


async def _ws_co_design(s: live.MeetingSession, aid: int, feedback: str):
    try:
        updated = await artifacts.co_design_refine(s.id, aid, feedback)
        await s.emit({"type": "artifact_updated", "artifact": updated})
    except Exception as e:
        await s.emit({"type": "ai_error", "text": f"Co-design lỗi: {e}"})


@app.websocket("/ws/meeting/{mid}/audio")
async def ws_audio(ws: WebSocket, mid: int):
    """Kênh nhận âm thanh PCM16 16kHz mono từ mic.
    State Guard: chỉ cho phép ghi âm khi cuộc họp tồn tại và đang 'live'.
    Một cuộc họp chỉ có một nguồn mic: thiết bị bật sau sẽ tiếp quản."""
    await ws.accept()
    m = await asyncio.to_thread(db.get_meeting, mid)
    if not m:
        log.warning("ws_audio: từ chối, meeting %d không tồn tại", mid)
        await ws.send_text(_dumps({"type": "error", "code": 4004,
                                   "text": "Cuộc họp không tồn tại. Vui lòng tạo cuộc họp trước khi bật mic."}))
        await ws.close(code=4004)
        return
    if m.get("status") == "ended":
        log.warning("ws_audio: từ chối, meeting %d đã kết thúc", mid)
        await ws.send_text(_dumps({"type": "error", "code": 4003,
                                   "text": "Cuộc họp này đã kết thúc và được lưu trữ (Archived). Không thể ghi âm thêm."}))
        await ws.close(code=4003)
        return

    s = await live.get_session(mid)
    owner = object()
    try:
        stream = await s.start_audio(owner, "mic", diarize=True)
    except Exception as e:
        await ws.send_text(_dumps({"type": "error", "code": 4500, "text": f"Không mở được dịch vụ nhận dạng giọng nói: {e}"}))
        await ws.close(code=4500)
        return
    await ws.send_text(_dumps({"type": "ready", "stream": "mic", "meeting_id": mid}))
    log.info("meeting.app: mic kết nối cho cuộc họp %d", mid)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if s.audio_owner is not owner:
                await ws.send_text(_dumps({"type": "error", "code": 4009,
                                           "text": "Một thiết bị khác vừa bật mic cho cuộc họp này."}))
                break
            if msg.get("bytes"):
                await stream.feed(msg["bytes"])
            elif msg.get("text") == "stop":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await s.stop_audio(owner)
        log.info("meeting.app: mic ngắt kết nối cho cuộc họp %d", mid)
