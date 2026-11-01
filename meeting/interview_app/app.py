"""Interview Assistant - backend.

Chạy:  op run --env-file=.env.op -- .venv/bin/uvicorn interview.app:app --host 127.0.0.1 --port 8009
Hoặc:  interview/start.sh  (tự lo token 1Password + tailscale serve để browser có https -> mic)

Luồng chính:
  browser mic ──PCM16k──> /ws/session/{id}/audio ──> Soniox (diarize) ──> segments
  Recall bot  ──PCM16k/người──> /ws/recall/{id} ──> Soniox (1 stream/người) ──> segments
  mọi sự kiện (interim/final/assist/...) ──> /ws/session/{id}/events ──> browser
"""
import asyncio
import audioop
import base64
import binascii
import hashlib
import secrets
import json
import logging
import os
import time
import wave
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from meeting.interview_app import auth, db, files, live, llm, panel, recall, rolefix, voice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("interview")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

app = FastAPI(title="Interview Assistant")
app.middleware("http")(auth.middleware)
app.include_router(auth.router)

# Body lớn nhất hợp lệ là một file 10MB dạng base64 (~13.4MB). Starlette đọc CẢ body
# vào RAM trước khi tới endpoint, nên phải chặn theo Content-Length ngay đầu - Render
# free có 512MB cho cả app lẫn các buổi đang chạy. Audio đi websocket, không qua đây.
MAX_BODY = 16 * 1024 * 1024

# Neon gói free tắt compute sau ~5 phút không có truy vấn: HR đọc một CV vài phút rồi bấm sang trang
# khác là phải chờ Neon thức dậy và nối lại. Trong lúc CÓ NGƯỜI DÙNG (request trong WARM_ACTIVE_S gần
# nhất, không tính health check của Render) thì cứ WARM_EVERY_S chạm DB một lần; hết người dùng thì
# để Neon tắt như cũ, không tốn thêm giờ compute ban đêm.
WARM_EVERY_S, WARM_ACTIVE_S = 240, 900
_last_use = 0.0
_warm_task = None


async def _keep_db_warm():
    while True:
        await asyncio.sleep(WARM_EVERY_S)
        if time.monotonic() - _last_use < WARM_ACTIVE_S:
            try:
                await asyncio.to_thread(db.ping)
            except Exception as e:
                log.warning("giữ DB thức: %s", e)


@app.middleware("http")
async def body_limit(request: Request, call_next):
    global _last_use, _warm_task
    if request.url.path != "/api/health":
        _last_use = time.monotonic()
        if _warm_task is None and db.PG:
            _warm_task = asyncio.create_task(_keep_db_warm())
    n = request.headers.get("content-length", "")
    if n.isdigit() and int(n) > MAX_BODY:
        return JSONResponse({"detail": "Dữ liệu gửi lên quá lớn (tối đa 10MB mỗi file)"}, status_code=413)
    return await call_next(request)
db.init()
voice.ensure_model_async()      # service tạo tay trên Render không chạy bước tải model trong render.yaml
if db.NEW_COLS_OK:
    try:
        _n = db.prune_voices(voice.MODEL_ID)
        if _n:
            log.warning("voices: đã xoá %d giọng lưu bằng model khác %s - người phỏng vấn cần thu lại giọng",
                        _n, voice.MODEL_ID)
    except Exception as e:
        log.error("voices: không xoá được giọng của model cũ (%s: %s)", type(e).__name__, e)

if auth.enabled():
    log.info("auth: Google OAuth, chỉ @%s", auth.ALLOWED_DOMAIN)
else:
    log.warning("auth TẮT (chưa có GOOGLE_OAUTH_CLIENT_ID) - app mở toang, "
                "chỉ dùng được ở local")

LIVE: dict[int, live.InterviewSession] = {}      # session id -> phiên đang chạy trong RAM


@app.get("/")
def index():
    # no-cache = vẫn cache nhưng hỏi lại server mỗi lần (ETag -> 304 nếu không đổi). Thiếu nó
    # thì sau mỗi lần deploy trình duyệt còn chạy index.html cũ một lúc, gọi API mới bằng UI cũ.
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    return {"ok": True, "gemini": bool(os.getenv("GEMINI_API_KEY")), "soniox": bool(os.getenv("SONIOX_API_KEY")),
            "recall": bool(recall.key()), "panel": bool(panel.key()),
            "panel_catalog": panel.CATALOG, "provider": llm.PROVIDER,
            "public_ws": os.getenv("PUBLIC_WS_BASE", ""),
            "model": llm.CLAUDE_MODEL if llm.PROVIDER == "claude" else llm.MODEL,
            "role_model": llm.ROLE_MODEL if llm.PROVIDER == "claude" else llm.MODEL,
            "auth": auth.enabled(), "domain": auth.ALLOWED_DOMAIN if auth.enabled() else None,
            "voice": voice.available(), "voice_status": voice.status(), "voice_model": voice.MODEL_ID,
            # Bản đang chạy - Render tự đặt biến này; để biết deploy đã lên chưa mà không cần đăng nhập.
            "commit": os.getenv("RENDER_GIT_COMMIT", "")[:7],
            "live_sessions": list(LIVE)}


# ----------------------------------------------------------------- settings ---
@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(body: dict):
    db.set_settings(body)
    return db.get_settings()


# ---------------------------------------------------------------- positions ---
class PositionIn(BaseModel):
    title: str | None = None
    jd: str | None = None
    requirements: str | None = None
    background: str | None = None
    persona: str | None = None
    rubric: list | None = None
    vocab: list | None = None
    criteria: dict | None = None      # {source, file, must_have, nice_to_have} - HR sửa tay


def _lines(xs, cap: int = 25) -> list[str]:
    out, seen = [], set()
    for x in xs or []:
        s = " ".join(str(x or "").split())
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:cap]


def _clean_rubric(rubric: list) -> list:
    """Rubric từ trình duyệt (HR sửa tay) hoặc từ model: bỏ dòng không tên, ép kiểu từng trường."""
    out = []
    for c in rubric or []:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        try:
            w = max(0, int(round(float(c.get("weight") or 0))))
        except (TypeError, ValueError):
            w = 0
        out.append({"name": " ".join(str(c["name"]).split()), "description": str(c.get("description") or "").strip(),
                    "weight": w, "signals": _lines(c.get("signals"), 10),
                    "sample_questions": _lines(c.get("sample_questions"), 10)})
    return out


def _clean_criteria(d: dict) -> dict:
    return {"source": "hr" if d.get("source") == "hr" else "ai", "file": str(d.get("file") or "")[:200],
            "must_have": _lines(d.get("must_have")), "nice_to_have": _lines(d.get("nice_to_have")),
            "at": time.time()}


def _position(pid: int) -> dict:
    p = db.get_position(pid)
    if not p:
        raise HTTPException(404)
    p["criteria"] = p.get("criteria") or {}
    return p


@app.get("/api/positions")
def positions():
    return db.list_positions()


@app.post("/api/positions")
def create_position(body: PositionIn):
    f = {k: v for k, v in body.model_dump().items() if v is not None}
    if not f.get("title"):
        raise HTTPException(400, "Thiếu title")
    f.pop("criteria", None)
    pid = db.create_position(**f)
    return _position(pid)


@app.get("/api/positions/{pid}")
def get_position(pid: int):
    p = _position(pid)
    cands = db.list_candidates(pid)
    for c in cands:
        c["screening"] = _screen_view(c, p, full=False)
        c["has_cv"] = bool((c.get("cv") or "").strip())
        c.pop("cv", None)                  # danh sách không cần cả CV, và CV thì dài
        if c.get("evaluation"):
            # Danh sách chỉ hiện điểm, khuyến nghị và % phù hợp. Cả bản chấm là ~11KB mỗi ứng viên:
            # 15 ứng viên thành 225KB JSON phải dựng trên 0,1 CPU của Render free mỗi lần mở trang.
            ev = _decorate_eval(c["evaluation"], p)
            c["evaluation"] = {"overall_score": ev.get("overall_score"), "recommendation": ev.get("recommendation"),
                               "match": {"final": ev["match"]["final"]}}
    p["candidates"] = cands
    p["criteria_sig"] = _crit_sig(p)
    p["criteria_job"] = _crit_job_view(pid)
    return p


@app.put("/api/positions/{pid}")
def update_position(pid: int, body: PositionIn):
    f = {k: v for k, v in body.model_dump().items() if v is not None}
    if "rubric" in f:
        f["rubric"] = _clean_rubric(f["rubric"])
    if "criteria" in f:
        f["criteria"] = _clean_criteria(f["criteria"])
        if not db.NEW_COLS_OK:
            f.pop("criteria")             # DB chưa có cột (migrate lỗi): lưu phần còn lại, đừng 500
    db.update_position(pid, **f)
    if "rubric" in f or "criteria" in f:
        _screen_missing_from_thread(pid)
    return _position(pid)


@app.delete("/api/positions/{pid}")
def delete_position(pid: int):
    for c in db.list_candidates(pid):
        _cancel_screening_from_thread(c["id"])
    db.delete_position(pid)
    return {"ok": True}


def _llm_fail(e: Exception, what: str) -> HTTPException:
    """Lỗi tầng LLM -> 502 kèm nguyên văn.

    Không bọc thì FastAPI trả 500 với body "Internal Server Error", và
    index.html chỉ có đúng chuỗi đó để hiện lên toast - người dùng không biết
    hết quota, sai model hay JD quá dài (gặp thật 22/09).
    """
    log.exception("%s lỗi", what)
    return HTTPException(502, f"{what} lỗi: {type(e).__name__}: {str(e)[:300]}")


# ------------------------------------------------- tạo / nhập tiêu chí chạy NỀN ---
# AI tạo hay nhập tiêu chí mất 20-60 giây. Trước đây trình duyệt chờ trọn request: rời trang giữa
# chừng thì quay lại không thấy gì đang chạy, HR tưởng AI dừng và bấm lại. Giờ endpoint chỉ khởi
# động việc rồi trả về ngay; trạng thái nằm ở server, GET /positions/{pid} trả kèm `criteria_job`
# để trang nào mở cũng thấy và tự theo dõi. Chỉ giữ trong RAM (một process, như LIVE và SCREENING):
# server khởi động lại giữa chừng thì việc đó mất, trang thôi hiện "đang chạy" và bấm lại được.
CRIT_JOBS: dict[int, dict] = {}       # pid -> {"kind": build|import, "status": running|error, ...}


def _crit_job_view(pid: int) -> dict | None:
    j = CRIT_JOBS.get(pid)
    return {k: j[k] for k in ("kind", "status", "started_at", "file", "error") if k in j} if j else None


def _start_crit_job(pid: int, kind: str, work, file: str = "") -> dict:
    """Chạy `work()` (coroutine function) ở nền. Lỗi của nó (HTTPException hay gì khác) thành
    `error` của việc, hiện ở trang vị trí; xong êm thì bỏ việc khỏi CRIT_JOBS - kết quả đã ở DB."""
    cur = CRIT_JOBS.get(pid)
    if cur and cur["status"] == "running":
        raise HTTPException(409, "AI đang xử lý tiêu chí của vị trí này - đợi xong rồi thử lại")
    job = {"kind": kind, "status": "running", "started_at": time.time(), "file": file}
    CRIT_JOBS[pid] = job

    async def run():
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except HTTPException as e:
            job.update(status="error", error=e.detail)
        except Exception as e:
            log.exception("vị trí %s: %s lỗi ngoài model", pid, kind)
            job.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")
        else:
            if CRIT_JOBS.get(pid) is job:
                CRIT_JOBS.pop(pid, None)

    job["task"] = asyncio.create_task(run())
    return _crit_job_view(pid)


@app.post("/api/positions/{pid}/build")
async def build_position(pid: int, force: bool = False):
    """AI ĐỀ XUẤT tiêu chí từ JD (+ vocab + persona), chạy nền. Chỉ dùng khi HR chưa đưa bộ tiêu chí.

    HR đã đưa rồi thì 409: đề xuất của AI không bao giờ lặng lẽ đè lên quyết
    định của HR. `force=1` là HR tự bấm "bỏ tiêu chí HR, để AI đề xuất".
    """
    p = await asyncio.to_thread(_position, pid)
    if p["criteria"].get("source") == "hr" and not force:
        raise HTTPException(409, "Vị trí đang dùng tiêu chí do HR đưa ra - AI không đề xuất đè lên. "
                                 "Muốn thay thì bấm \"Bỏ tiêu chí HR, để AI đề xuất\".")

    async def work():
        try:
            b = await asyncio.wait_for(llm.build_position(p), READ_TIMEOUT_S)
        except Exception as e:
            raise _too_long(e, "Tạo khung năng lực") or _llm_fail(e, "Tạo khung năng lực")
        upd = {"rubric": _clean_rubric([c.model_dump() for c in b.competencies]), "vocab": b.vocab}
        if db.NEW_COLS_OK:                # thiếu cột criteria thì "Tạo khung" cũ vẫn phải chạy
            upd["criteria"] = {"source": "ai", "file": "", "must_have": _lines(b.must_have),
                               "nice_to_have": _lines(b.nice_to_have), "at": time.time()}
        # Đọc lại vị trí trước khi ghi: AI chạy cả phút và HR được bảo cứ làm việc khác, persona HR vừa
        # tự gõ trong lúc chờ thì không đè.
        now = await asyncio.to_thread(_position, pid)
        if not (now.get("persona") or "").strip():
            upd["persona"] = b.persona
        await asyncio.to_thread(db.update_position, pid, **upd)
        await _screen_missing(pid)
    return {"job": _start_crit_job(pid, "build", work)}


class FileIn(BaseModel):
    filename: str = ""
    data: str                         # base64 của file, không kèm tiền tố "data:...;base64,"


class ExtractIn(FileIn):
    kind: str                         # cv | jd


class CriteriaIn(BaseModel):
    filename: str = ""
    data: str | None = None           # file (base64), hoặc
    text: str | None = None           # nội dung dán tay


MAX_B64 = files.MAX_BYTES * 4 // 3 + 64
READ_TIMEOUT_S = 240          # đọc một file / nhập một bảng tiêu chí; quá thì báo, đừng treo request


async def _upload(filename: str, b64: str) -> files.Doc:
    """JSON + base64 thay vì multipart: khỏi thêm python-multipart chỉ cho một nút tải file.

    Giải mã + bóc chữ chạy trong thread: parse vài MB XML mất cả giây CPU, chạy
    thẳng trên event loop là websocket của mọi buổi đang phỏng vấn đứng theo.
    """
    raw = b64.split(",", 1)[1] if b64.startswith("data:") else b64
    if len(raw) > MAX_B64:
        raise HTTPException(413, f"{filename or 'File'}: quá {files.MAX_BYTES // 1024 // 1024}MB")

    def work():
        return files.read(filename, base64.b64decode(raw, validate=True))
    try:
        return await asyncio.to_thread(work)
    except files.FileError as e:
        raise HTTPException(400, f"{filename or 'File'}: {e}")
    except (binascii.Error, ValueError):
        raise HTTPException(400, "File gửi lên sai định dạng (cần base64)")


def _too_long(e: Exception, what: str) -> HTTPException | None:
    if isinstance(e, llm.OutputTooLong):
        return HTTPException(422, f"{what}: tài liệu quá dài, AI chép không hết - tải bản ngắn hơn "
                                  "(khoảng 15 trang trở xuống) hoặc dán phần chính vào ô")
    if isinstance(e, TimeoutError):
        return HTTPException(504, f"{what}: AI trả lời quá lâu - thử lại sau ít phút")
    return None


@app.post("/api/extract")
async def extract(body: ExtractIn):
    """File JD/CV -> text để trình duyệt điền vào ô nhập liệu. Không lưu gì: file chỉ qua RAM.

    CV được bỏ số điện thoại, email, địa chỉ, ngày sinh... (dặn model, rồi lọc
    lại bằng regex): app chỉ cần CV để đánh giá năng lực, và đó là dữ liệu cá
    nhân (NĐ 13/2023) - không cần thì không giữ.
    """
    if body.kind not in ("cv", "jd"):
        raise HTTPException(400, "kind phải là cv hoặc jd")
    doc = await _upload(body.filename, body.data)
    t0 = time.monotonic()
    try:
        r = (await asyncio.wait_for(llm.read_document(body.kind, doc), READ_TIMEOUT_S)).model_dump()
        via = "ai"
    except Exception as e:
        if doc.parts or not doc.text:
            raise _too_long(e, "Đọc file") or _llm_fail(e, "Đọc file")
        # docx/txt đã có chữ sẵn: model lỗi thì vẫn điền nguyên văn, còn hơn bắt gõ lại
        log.warning("extract %s: model lỗi (%s) - trả nguyên văn chữ trong file", body.kind, e)
        r = {"name": "", "cv": doc.text} if body.kind == "cv" else {"title": "", "jd": doc.text, "requirements": ""}
        via = "raw"
    if body.kind == "cv":
        r["cv"] = files.scrub_contacts(r.get("cv") or "")
        nm = " ".join((r.get("name") or "").split())
        r["name"] = nm.title() if nm.isupper() else nm      # CV hay in hoa cả tên: "LÊ MINH KHOA"
    # Không log tên file: tên file CV thường là tên ứng viên.
    log.info("extract %s (%s, %d byte) qua %s trong %.1fs", body.kind, doc.kind or "?",
             len(doc.text) or sum(len(b) for b, _ in doc.parts), via, time.monotonic() - t0)
    return {**r, "filename": doc.name, "via": via, "type": doc.kind}


def _merge_vocab(old, new, cap: int = 100) -> list:
    return _lines(list(old or []) + list(new or []), cap)


@app.post("/api/positions/{pid}/criteria")
async def import_criteria(pid: int, body: CriteriaIn):
    """Bộ tiêu chí HR đưa (file Job Analysis, Word, PDF... hoặc dán text) -> rubric của vị trí, chạy nền.

    Tiêu chí và tỷ trọng giữ nguyên như HR đặt; model chỉ thêm dấu hiệu quan
    sát được và câu hỏi gợi mở. Vocab gộp thêm, persona và bối cảnh chỉ điền
    khi đang trống - thứ HR đã tự viết thì không đè. File được kiểm và bóc chữ
    ngay trong request (file hỏng báo liền), chỉ phần model đọc là chạy nền.
    """
    if not db.NEW_COLS_OK:
        raise HTTPException(503, "Database chưa có cột criteria (bước migrate lúc khởi động lỗi - xem log server)")
    p = await asyncio.to_thread(_position, pid)
    if body.data:
        doc = await _upload(body.filename, body.data)
    elif (body.text or "").strip():
        doc = files.Doc("tiêu chí dán tay", text=body.text.strip()[:files.MAX_CHARS], kind="text")
    else:
        raise HTTPException(400, "Chưa có file hay nội dung tiêu chí")

    async def work():
        try:
            r = await asyncio.wait_for(llm.import_criteria(p, doc), READ_TIMEOUT_S)
        except Exception as e:
            raise _too_long(e, "Đọc tiêu chí") or _llm_fail(e, "Đọc tiêu chí")
        rubric = _clean_rubric([c.model_dump() for c in r.competencies])
        if not rubric:
            raise HTTPException(422, "Không thấy tiêu chí đánh giá nào trong tài liệu - kiểm tra lại file, "
                                     "hoặc để AI đề xuất từ JD")
        now = await asyncio.to_thread(_position, pid)     # như build: thứ HR sửa trong lúc chờ thì giữ
        upd = {"rubric": rubric, "vocab": _merge_vocab(now.get("vocab"), r.vocab),
               "criteria": {"source": "hr", "file": doc.name if body.data else "", "must_have": _lines(r.must_have),
                            "nice_to_have": _lines(r.nice_to_have), "at": time.time()}}
        if not (now.get("persona") or "").strip() and r.persona.strip():
            upd["persona"] = r.persona.strip()
        if not (now.get("background") or "").strip() and r.background.strip():
            upd["background"] = r.background.strip()
        await asyncio.to_thread(db.update_position, pid, **upd)
        log.info("vị trí %s: nhập %d tiêu chí HR, %d yêu cầu bắt buộc", pid, len(rubric), len(upd["criteria"]["must_have"]))
        await _screen_missing(pid)
    return {"job": _start_crit_job(pid, "import", work, file=doc.name if body.data else "")}


# ------------------------------------------------------------ sàng lọc CV ---
# Chạy NỀN, không bắt trình duyệt chờ: một CV mất 15-40s, HR tải cả chục CV một
# lúc, và rời trang giữa chừng không được làm mất kết quả. Trạng thái nằm trong
# candidates.screening (DB) để mọi trang cùng thấy; SCREENING chỉ giữ task đang
# chạy trong process này - buổi "running" mà không có task ở đây là đã bị ngắt
# (server khởi động lại), hiện ra là lỗi để bấm chấm lại.
SCREENING: dict[int, asyncio.Task] = {}
SCREEN_PARALLEL = 3
SCREEN_TIMEOUT_S = 240        # một CV; model treo thì trả chỗ cho CV khác thay vì giữ slot mãi
_screen_active = 0


def _crit_sig(p: dict) -> str:
    """Dấu vân tay của bộ tiêu chí, để biết bản sàng lọc đã cũ. Cố tình KHÔNG gồm tỷ trọng:
    độ phù hợp % tính lại từ điểm từng tiêu chí ngay lúc đọc, đổi tỷ trọng không cần chấm lại."""
    cr = p.get("criteria") or {}
    basis = [[c.get("name"), c.get("description")] for c in p.get("rubric") or []]
    blob = json.dumps([basis, cr.get("must_have") or [], cr.get("nice_to_have") or []], ensure_ascii=False)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _cv_sig(c: dict) -> str:
    return hashlib.sha1(((c.get("cv") or "") + "\x00" + (c.get("notes") or "")).encode()).hexdigest()[:12]


def _fresh_screening(c: dict, p: dict) -> dict | None:
    """Bản sàng lọc còn ĐÚNG với CV + tiêu chí hiện tại, hoặc None.

    Chỉ bản này mới được đưa cho model (gợi ý, chấm sau buổi), vào hộp "Cần kiểm
    chứng" trong buổi, và vào bản chụp trước/sau: kết quả của CV cũ (đang chấm
    lại, chấm lại lỗi, CV bị xoá) mà trộn vào là trích dẫn những câu không còn
    nằm trong CV.
    """
    s = c.get("screening") or {}
    if s.get("result") and s.get("sig") == _crit_sig(p) and s.get("cv_sig") == _cv_sig(c):
        return s
    return None


def _for_llm(c: dict, p: dict) -> dict:
    return {**c, "screening": _fresh_screening(c, p)}


def _req_status(s) -> str:
    s = str(s or "").strip().lower().replace("-", "_").replace(" ", "_")
    return s if s in llm.REQ_STATUS else {"yes": "met", "đạt": "met", "no": "not_met",
                                          "không_đạt": "not_met", "một_phần": "partial"}.get(s, "unknown")


def _align(items: list, key: str, wants: list, blank) -> list:
    """Xếp kết quả của model theo đúng danh sách gốc (tiêu chí / yêu cầu) và đúng tên gốc.

    Model hay viết lại tên (đảo phần tiếng Anh lên trước, thêm bớt chữ) hoặc bỏ sót
    một mục. Ba lượt, lượt sau chỉ xét phần lượt trước chưa ghép được:
      1. trùng tên (bỏ hoa thường, khoảng trắng, NFC) - làm cho HẾT danh sách trước,
         không thì "Kinh nghiệm" có thể ghép nhầm mục "Kinh nghiệm quản lý" vốn trùng
         đúng tên của tiêu chí đứng sau;
      2. giống nhất trên toàn bảng (cặp giống nhất ghép trước, >= NAME_SIM_MIN);
      3. cùng thứ tự - chỉ khi số mục bằng nhau, lúc đó thứ tự mới là bằng chứng.
    """
    got: dict[int, int] = {}                                  # vị trí trong wants -> vị trí trong items
    used: set[int] = set()
    for k, w in enumerate(wants):
        nw = llm.norm_name(w)
        for i, x in enumerate(items):
            if i not in used and llm.norm_name(x.get(key)) == nw:
                got[k] = i
                used.add(i)
                break
    pairs = sorted(((llm.name_sim(w, x.get(key)), k, i) for k, w in enumerate(wants) if k not in got
                    for i, x in enumerate(items) if i not in used), reverse=True)
    for sim, k, i in pairs:
        if sim < llm.NAME_SIM_MIN:
            break
        if k not in got and i not in used:
            got[k] = i
            used.add(i)
    if len(items) == len(wants):
        for k in range(len(wants)):
            if k not in got and k not in used:
                got[k] = k
                used.add(k)
    return [{**items[got[k]], key: w} if k in got else blank(w) for k, w in enumerate(wants)]


def _blank_req(q: str) -> dict:
    return {"requirement": q, "status": "unknown", "note": ""}


def _align_reqs(ev: dict, p: dict):
    cr = p.get("criteria") or {}
    for key in ("must_have", "nice_to_have"):
        xs = [dict(x, status=_req_status(x.get("status"))) for x in ev.get(key) or []]
        want = cr.get(key) or []
        # Model không kiểm yêu cầu nào (bản chấm cũ, hoặc vị trí chưa có danh sách lúc chấm)
        # thì để trống - điền "chưa rõ" cho cả loạt là nói hộ model điều nó chưa nói.
        ev[key] = _align(xs, "requirement", want, _blank_req) if want and xs else []


def _clean_screening(r: dict, p: dict) -> dict:
    rub = p.get("rubric") or []
    crit = []
    for x in r.get("criteria") or []:
        try:
            sc = max(0, min(5, int(x.get("score") or 0)))
        except (TypeError, ValueError):
            sc = 0
        crit.append({**x, "score": sc})
    r["criteria"] = _align(crit, "name", [c["name"] for c in rub],
                           lambda n: {"name": n, "score": 0, "evidence": [], "assessment": "(AI chưa chấm tiêu chí này)"}) \
        if rub else crit
    _align_reqs(r, p)
    rec = str(r.get("recommendation") or "").strip().lower()
    r["recommendation"] = rec if rec in ("interview", "consider", "reject") else \
        {"yes": "interview", "maybe": "consider", "no": "reject"}.get(rec, "consider")
    return r


def _screen_view(c: dict, p: dict, full: bool = True) -> dict:
    """Trạng thái sàng lọc cho trình duyệt. Độ phù hợp % tính LẠI mỗi lần đọc, theo tỷ trọng hiện tại.

    Chạy trong thread (endpoint def), được phép đọc DB.
    """
    raw = c.get("screening") or {}
    if raw.get("status") == "running" and c["id"] not in SCREENING:
        # Dòng `c` được đọc TRƯỚC lúc xét SCREENING, còn task thì ghi kết quả rồi mới rời
        # SCREENING - lọt vào đúng khe đó là thấy "running mà không có task" rồi báo nhầm
        # bị ngắt, và trang ứng viên thôi theo dõi luôn (bắt được khi chạy test lặp). Đọc
        # lại một lần: vẫn "running" mà không có task thì mới đúng là server đã khởi động lại.
        raw = (db.get_candidate(c["id"]) or {}).get("screening") or {}
    s = dict(raw)
    if c["id"] in SCREENING:
        s["status"] = "running"           # có task là đang chạy, dù DB lỡ ghi gì từ lượt vừa bị huỷ
    elif not s:
        return {"status": "none"}
    elif s.get("status") == "running":
        s.update(status="error", error="Bị ngắt giữa chừng (server vừa khởi động lại) - bấm Chấm lại")
    r = s.get("result")
    if r:
        m = llm.score_match(p.get("rubric") or [], {x.get("name"): x.get("score") for x in r.get("criteria") or []})
        s.update(match=m["match"], coverage=m["coverage"], avg=m["avg"], recommendation=r.get("recommendation"))
        must = r.get("must_have") or []
        s["must"] = {k: sum(1 for x in must if x.get("status") == k) for k in llm.REQ_STATUS}
        s["stale"] = _fresh_screening({**c, "screening": raw}, p) is None
    if not full:
        s.pop("result", None)
    return s


async def _screen_slot():
    """Giữ một chỗ chấm. Có buổi phỏng vấn đang chạy thì chỉ chấm 1 CV một lúc: sàng lọc dùng
    chung hạn mức API với nút "Hỏi gì tiếp?", và một lần 429 là gợi ý chậm thêm 2-12 giây."""
    global _screen_active
    while _screen_active >= (1 if LIVE else SCREEN_PARALLEL):
        await asyncio.sleep(0.5)
    _screen_active += 1


async def _screen(cid: int):
    """Task nền chấm một CV. Mọi truy vấn DB chạy trong thread: trên Neon mỗi câu là một
    vòng mạng sang Singapore, chạy thẳng trên event loop là audio các buổi đang phỏng vấn khựng."""
    global _screen_active
    slot = False
    try:
        await _screen_slot()
        slot = True
        c = await asyncio.to_thread(db.get_candidate, cid)
        p = await asyncio.to_thread(db.get_position, c["position_id"]) if c else None
        if not c or not p:
            return
        p["criteria"] = p.get("criteria") or {}
        # Chụp dấu vân tay TRƯỚC khi gọi model: CV đổi giữa chừng thì task này đã bị
        # huỷ (force), tiêu chí đổi giữa chừng thì kết quả tự hiện là cũ.
        sig, cvs, t0 = _crit_sig(p), _cv_sig(c), time.monotonic()
        try:
            r = await asyncio.wait_for(llm.screen_cv(p, c), SCREEN_TIMEOUT_S)
            upd = {"status": "done", "at": time.time(), "sig": sig, "cv_sig": cvs,
                   "seconds": round(time.monotonic() - t0, 1), "result": _clean_screening(r.model_dump(), p)}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = "AI trả lời quá lâu" if isinstance(e, TimeoutError) else f"{type(e).__name__}: {str(e)[:240]}"
            log.warning("sàng lọc CV ứng viên %s lỗi: %s", cid, err)
            cur = ((await asyncio.to_thread(db.get_candidate, cid)) or {}).get("screening") or {}
            upd = {**cur, "status": "error", "at": time.time(), "error": err}
        if SCREENING.get(cid) is not asyncio.current_task():
            return                        # đã có lượt mới thay chỗ - đừng ghi đè trạng thái của nó
        await asyncio.to_thread(db.update_candidate, cid, screening=upd)
        if upd["status"] == "done":
            log.info("sàng lọc CV ứng viên %s xong sau %.0fs", cid, time.monotonic() - t0)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("sàng lọc CV ứng viên %s: lỗi ngoài model", cid)
    finally:
        if slot:
            _screen_active -= 1
        if SCREENING.get(cid) is asyncio.current_task():
            SCREENING.pop(cid, None)


# Bắt đầu sàng lọc gồm hai nửa ở hai nơi khác nhau:
#   _prep_screening  truy vấn/ghi DB - chạy trong thread (endpoint def, hoặc asyncio.to_thread)
#   _spawn           huỷ/tạo task - BẮT BUỘC chạy trên event loop (asyncio.create_task)
# Endpoint def gọi _spawn qua anyio.from_thread.run_sync; endpoint async gọi thẳng.
def _prep_screening(cid: int, c: dict | None = None, p: dict | None = None) -> str:
    """-> "start" | "clear" (CV rỗng: xoá kết quả cũ) | "skip"."""
    if not db.NEW_COLS_OK:
        return "skip"
    c = c or db.get_candidate(cid)
    if not c:
        return "skip"
    if not (c.get("cv") or "").strip():
        if c.get("screening"):
            db.update_candidate(cid, screening=None)
        return "clear"
    p = p or db.get_position(c["position_id"])
    if not p or not p.get("rubric"):
        return "skip"
    prev = c.get("screening") or {}
    db.update_candidate(cid, screening={**prev, "status": "running", "started_at": time.time(), "error": None})
    return "start"


def _spawn(cid: int, state: str, force: bool = False) -> bool:
    t = SCREENING.get(cid)
    running = t is not None and not t.done()
    if state == "clear":
        if running:
            t.cancel()
            SCREENING.pop(cid, None)
        return False
    if state != "start":
        return False
    if running:
        if not force:
            return True
        t.cancel()
    SCREENING[cid] = asyncio.create_task(_screen(cid))
    return True


async def start_screening(cid: int, force: bool = False) -> bool:
    """Từ code async. force = huỷ lượt đang chạy (CV vừa đổi) rồi chạy lại.
    Không bao giờ ném lỗi: sàng lọc hỏng không được làm hỏng việc lưu hồ sơ hay mở buổi."""
    try:
        return _spawn(cid, await asyncio.to_thread(_prep_screening, cid), force)
    except Exception:
        log.exception("không bắt đầu được sàng lọc CV ứng viên %s", cid)
        return False


def _start_screening_from_thread(cid: int, force: bool = False) -> bool:
    """Như start_screening, gọi từ endpoint def (đang ở threadpool của Starlette)."""
    try:
        state = _prep_screening(cid)
        return anyio.from_thread.run_sync(_spawn, cid, state, force)
    except Exception:
        log.exception("không bắt đầu được sàng lọc CV ứng viên %s", cid)
        return False


def _cancel_screening_from_thread(cid: int):
    t = SCREENING.pop(cid, None)
    if t:
        t.get_loop().call_soon_threadsafe(t.cancel)


def _missing_prep(pid: int) -> list[int]:
    """Ứng viên có CV mà CHƯA từng được chấm (vd tải CV trước, tạo tiêu chí sau), đã đánh dấu running.
    Bản đã chấm theo tiêu chí cũ thì không tự chạy lại - tốn tiền theo số ứng viên, để HR bấm."""
    p = db.get_position(pid)
    out = []
    for c in db.list_candidates(pid):
        if (c.get("cv") or "").strip() and not (c.get("screening") or {}).get("result") \
                and c["id"] not in SCREENING and _prep_screening(c["id"], c, p) == "start":
            out.append(c["id"])
    return out


async def _screen_missing(pid: int) -> int:
    try:
        cids = await asyncio.to_thread(_missing_prep, pid)
        return sum(_spawn(cid, "start") for cid in cids)
    except Exception:
        log.exception("vị trí %s: không bắt đầu được sàng lọc các CV chưa chấm", pid)
        return 0


def _screen_missing_from_thread(pid: int) -> int:
    try:
        cids = _missing_prep(pid)
        return anyio.from_thread.run_sync(lambda: sum(_spawn(cid, "start") for cid in cids))
    except Exception:
        log.exception("vị trí %s: không bắt đầu được sàng lọc các CV chưa chấm", pid)
        return 0


def _position_screen_prep(pid: int, scope: str) -> list[int]:
    p = _position(pid)
    if not p.get("rubric"):
        raise HTTPException(400, "Vị trí chưa có tiêu chí đánh giá")
    out = []
    for c in db.list_candidates(pid):
        if not (c.get("cv") or "").strip():
            continue
        st = _screen_view(c, p, full=False)
        if (scope == "all" or st["status"] in ("none", "error") or st.get("stale")) \
                and _prep_screening(c["id"], c, p) == "start":
            out.append(c["id"])
    return out


@app.post("/api/positions/{pid}/screen")
async def screen_position(pid: int, scope: str = "stale"):
    """scope=stale: ứng viên chưa chấm, chấm lỗi, hoặc tiêu chí/CV đã đổi. scope=all: chấm lại hết."""
    if not db.NEW_COLS_OK:
        raise HTTPException(503, "Database chưa có cột screening (bước migrate lúc khởi động lỗi - xem log server)")
    cids = await asyncio.to_thread(_position_screen_prep, pid, scope)
    return {"started": sum(_spawn(cid, "start", force=scope == "all") for cid in cids)}


# ------------------------------------------------ chấm sau buổi: CV + phỏng vấn ---
def _finalize_eval(ev: dict, p: dict, c: dict) -> dict:
    """Bản chấm vừa sinh: xếp theo đúng tiêu chí, và CHỤP LẠI bản sàng lọc CV mà lần chấm này dùng.

    Bản chụp là "trước" của phép so trước/sau, và phải đúng là thứ model vừa đối
    chiếu để viết `cv_check` - nên chỉ chụp bản còn khớp CV + tiêu chí (_fresh_screening,
    cũng là bản duy nhất được đưa cho model). CV sửa và chấm lại CV về sau thì bản
    chấm buổi này vẫn giữ nguyên bản chụp của nó.
    """
    rub = p.get("rubric") or []
    if rub:
        ev["competencies"] = _align(ev.get("competencies") or [], "name", [x["name"] for x in rub],
                                    lambda n: {"name": n, "score": 0, "evidence": [], "cv_check": "",
                                               "assessment": "(AI chưa chấm tiêu chí này)"})
    _align_reqs(ev, p)
    s = _fresh_screening(c, p)
    if s:
        r = s["result"]
        ev["cv_snapshot"] = {"scores": {x.get("name"): x.get("score") or 0 for x in r.get("criteria") or []},
                             "must_have": {x.get("requirement"): x.get("status") for x in r.get("must_have") or []},
                             "at": s.get("at")}
    return ev


def _decorate_eval(ev: dict, p: dict) -> dict:
    """Điểm tổng và độ phù hợp tính BẰNG CODE lúc đọc, theo tỷ trọng hiện tại của vị trí.

    overall_score của model bị thay: model cộng trung bình có trọng số không
    phải lúc nào cũng đúng, và so sánh ứng viên phải dùng cùng một phép tính.
    """
    ev = dict(ev)
    rub = p.get("rubric") or []
    scores = {x.get("name"): x.get("score") for x in ev.get("competencies") or []}
    m = llm.score_match(rub, scores)
    if m["avg"] is None and scores:             # tiêu chí đã đổi tên sau khi chấm
        m = llm.score_match([], scores)
    if m["avg"] is not None:
        ev["overall_score"] = m["avg"]
    snap = ev.get("cv_snapshot")
    cv = None
    if snap:
        cm = llm.score_match(rub, snap.get("scores") or {})
        cv = {"match": cm["match"], "coverage": cm["coverage"], "scores": snap.get("scores") or {},
              "must_have": snap.get("must_have") or {}}
    ev["match"] = {"final": m["match"], "coverage": m["coverage"], "cv": cv}
    return ev


def _screen_brief(c: dict, p: dict) -> dict | None:
    """Phần sàng lọc CV người phỏng vấn cần NGAY trong buổi: điều cần kiểm chứng.
    Chỉ bản còn khớp CV + tiêu chí - câu hỏi về một CV đã thay là câu hỏi sai người."""
    s = _fresh_screening(c, p)
    if not s:
        return None
    r = s["result"]
    m = llm.score_match(p.get("rubric") or [], {x.get("name"): x.get("score") for x in r.get("criteria") or []})
    return {"match": m["match"], "coverage": m["coverage"], "questions": r.get("questions") or [],
            "requirements": [x for x in r.get("must_have") or [] if x.get("status") != "met"],
            "gaps": r.get("gaps") or [], "red_flags": r.get("red_flags") or []}


def _hash(x) -> str:
    return hashlib.sha1(json.dumps(x, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:10]


@app.get("/api/positions/{pid}/compare")
async def compare_position(pid: int, refresh: bool = False):
    p = _position(pid)
    sessions = db.evaluated_sessions(pid)
    if len(sessions) < 2:
        raise HTTPException(400, f"Cần ít nhất 2 ứng viên đã có đánh giá (hiện có {len(sessions)})")
    # Khoá cache gồm cả NỘI DUNG bản chấm (chấm lại một buổi, cùng id) và bộ tiêu chí cùng tỷ trọng
    # (điểm tổng tính lại theo tỷ trọng mới) - đổi một trong hai là nhận định cũ đã có thể sai.
    key = json.dumps([[s["id"], _hash(s["evaluation"])] for s in sessions]
                     + [_crit_sig(p), [c.get("weight") for c in p.get("rubric") or []]])
    for s in sessions:
        s["evaluation"] = _decorate_eval(s["evaluation"], p)
    # ma trận điểm tính bằng code, không nhờ LLM đếm
    names = [s["candidate_name"] for s in sessions]
    comps = [c["name"] for c in (p.get("rubric") or [])] or sorted(
        {c["name"] for s in sessions for c in s["evaluation"].get("competencies", [])})
    matrix = []
    for cn in comps:
        rowd = {"competency": cn, "scores": {}}
        for s in sessions:
            sc = next((c["score"] for c in s["evaluation"].get("competencies", [])
                       if llm.norm_name(c["name"]) == llm.norm_name(cn)), None)
            rowd["scores"][s["candidate_name"]] = sc
        matrix.append(rowd)
    overall = {}
    for s in sessions:
        ev = s["evaluation"]
        m = ev.get("match") or {}
        overall[s["candidate_name"]] = {
            "score": ev.get("overall_score"), "recommendation": ev.get("recommendation"),
            "headline": ev.get("headline"), "session_id": s["id"], "candidate_id": s["candidate_id"],
            "match": m.get("final"), "cv_match": (m.get("cv") or {}).get("match"),
            # False = bản chấm không kiểm yêu cầu nào (bản chấm cũ) - khác hẳn "đạt hết"
            "must_checked": bool(ev.get("must_have")),
            # yêu cầu bắt buộc CHƯA đạt đủ sau phỏng vấn, không đạt lên trước
            "must_open": sorted(({"requirement": x.get("requirement"), "status": x.get("status")}
                                 for x in ev.get("must_have") or [] if x.get("status") != "met"),
                                key=lambda x: {"not_met": 0, "partial": 1}.get(x["status"], 2))}
    narrative = None
    if not refresh:
        ns = db.get_settings().get(f"compare_{pid}")
        if ns and ns.get("key") == key:
            narrative = ns["narrative"]
    if narrative is None:
        try:
            c = await llm.compare(p, [{"candidate_name": s["candidate_name"], "evaluation": s["evaluation"]} for s in sessions])
        except Exception as e:
            raise _llm_fail(e, "So sánh ứng viên")
        narrative = c.model_dump()
        db.set_settings({f"compare_{pid}": {"key": key, "narrative": narrative, "at": time.time()}})
    return {"position": {"id": pid, "title": p["title"]}, "candidates": names, "matrix": matrix,
            "overall": overall, "narrative": narrative}


# --------------------------------------------------------------- candidates ---
class CandidateIn(BaseModel):
    name: str | None = None
    cv: str | None = None
    notes: str | None = None


def _candidate_out(c: dict, p: dict | None = None) -> dict:
    p = p or _position(c["position_id"])
    c["screening"] = _screen_view(c, p)
    return c


def _candidate_fields(body: CandidateIn) -> dict:
    f = {k: v for k, v in body.model_dump().items() if v is not None}
    if f.get("cv"):
        # CV dán tay cũng bỏ email/SĐT như CV đọc từ file: app chỉ cần CV để đánh giá năng lực.
        f["cv"] = files.scrub_contacts(f["cv"])
    return f


# def thường (threadpool), không phải async: vài truy vấn DB mỗi lần lưu, trên Neon là
# vài vòng mạng - chạy trên event loop thì audio các buổi đang phỏng vấn khựng theo.
# Sàng lọc nền vẫn bắt đầu được: _start_screening_from_thread nhờ event loop tạo task.
@app.post("/api/positions/{pid}/candidates")
def create_candidate(pid: int, body: CandidateIn):
    if not (body.name or "").strip():
        raise HTTPException(400, "Thiếu tên")
    p = _position(pid)
    cid = db.create_candidate(position_id=pid, **_candidate_fields(body))
    if (db.get_candidate(cid).get("cv") or "").strip():
        _start_screening_from_thread(cid)       # vị trí có tiêu chí thì chấm luôn
    return _candidate_out(db.get_candidate(cid), p)


@app.get("/api/candidates/{cid}")
def get_candidate(cid: int):
    c = db.get_candidate(cid)
    if not c:
        raise HTTPException(404)
    c["sessions"] = db.list_sessions(cid)
    p = _position(c["position_id"])
    c["position"] = p
    le = db.latest_evaluation(cid)
    if le and le.get("evaluation"):
        ev = _decorate_eval(le["evaluation"], p)
        c["latest_eval"] = {"session_id": le["id"], "at": le["started_at"], "overall_score": ev.get("overall_score"),
                            "recommendation": ev.get("recommendation"), "match": ev.get("match")}
    return _candidate_out(c, p)


@app.put("/api/candidates/{cid}")
def update_candidate(cid: int, body: CandidateIn):
    old = db.get_candidate(cid)
    if not old:
        raise HTTPException(404)
    db.update_candidate(cid, **_candidate_fields(body))
    c = db.get_candidate(cid)
    p = _position(c["position_id"])
    # CV hoặc ghi chú đổi -> chấm lại ngay (huỷ lượt đang chạy với CV cũ; CV bị xoá thì
    # xoá luôn kết quả cũ). Tiêu chí đổi thì KHÔNG tự chạy ở đây: "Bắt đầu phỏng vấn"
    # cũng lưu hồ sơ trước, và chấm lại vì tiêu chí đổi là việc HR bấm "Chấm CV".
    if _cv_sig(c) != _cv_sig(old):
        _start_screening_from_thread(cid, force=True)
    elif (c.get("cv") or "").strip() and _screen_view(c, p, full=False)["status"] in ("none", "error"):
        _start_screening_from_thread(cid)
    return _candidate_out(db.get_candidate(cid), p)


@app.post("/api/candidates/{cid}/screen")
async def screen_candidate(cid: int):
    c = await asyncio.to_thread(db.get_candidate, cid)
    if not c:
        raise HTTPException(404)
    if not (c.get("cv") or "").strip():
        raise HTTPException(400, "Ứng viên chưa có CV")
    if not db.NEW_COLS_OK:
        raise HTTPException(503, "Database chưa có cột screening (bước migrate lúc khởi động lỗi - xem log server)")
    if not await start_screening(cid, force=True):
        raise HTTPException(400, "Vị trí chưa có tiêu chí đánh giá - tạo tiêu chí trước")
    return await asyncio.to_thread(lambda: _candidate_out(db.get_candidate(cid)))


@app.delete("/api/candidates/{cid}")
def delete_candidate(cid: int):
    _cancel_screening_from_thread(cid)
    db.delete_candidate(cid)
    return {"ok": True}


# ----------------------------------------------------------------- sessions ---
class SessionIn(BaseModel):
    source: str = "mic"               # mic | recall | replay | ext
    meeting_url: str | None = None    # recall
    file: str | None = None           # replay: tên file trong interview/mock hoặc bench
    speed: float = 1.0                # replay
    prime: str | None = None          # mic | replay: giọng mẫu người phỏng vấn, base64 PCM16 16kHz mono
    voices: list[int] = []            # mic | replay: giọng đã lưu của những người phỏng vấn có mặt
    host: int | None = None           # giọng đã lưu của chủ phòng (người cầm máy, người gọi trợ lý)


# Giọng mẫu ngắn quá thì Soniox không đủ để gán nhãn chắc; dài quá thì phải
# chờ lâu lúc mở buổi, vì engine realtime xử lý xấp xỉ thời gian thực và mic
# chỉ được mở sau khi nó xử lý xong đoạn mẫu (xem live.BaseStream.prime).
PRIME_MIN_S, PRIME_MAX_S = 4, 15


def _prime_pcm(b64: str) -> bytes:
    try:
        pcm = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(400, "Giọng mẫu sai định dạng (cần base64 PCM16 16kHz mono)")
    sec = len(pcm) / (live.RATE * 2)
    if len(pcm) % 2 or not (PRIME_MIN_S <= sec <= PRIME_MAX_S):
        raise HTTPException(400, f"Giọng mẫu phải dài {PRIME_MIN_S}-{PRIME_MAX_S} giây (đang là {sec:.1f}s)")
    return pcm


# ------------------------------------------------------------------- voices ---
# Giọng đã lưu để gọi TÊN người nói. Chỉ lưu vector 192 số, không bao giờ lưu
# audio; vẫn là dữ liệu sinh trắc học (dữ liệu cá nhân nhạy cảm theo NĐ
# 13/2023) nên: bắt buộc xác nhận đồng ý, ghi ai xác nhận lúc nào, xoá được.
class VoiceIn(BaseModel):
    name: str
    pcm: str                          # base64 PCM16 16kHz mono, 4-15 giây
    consent: bool = False             # người được lưu giọng đã đồng ý


class VoiceSampleIn(BaseModel):
    pcm: str


class VoiceNameIn(BaseModel):
    name: str


async def _voice_vec(b64: str):
    if not voice.available():
        raise HTTPException(503, "Server chưa có model nhận diện giọng (python -m interview.voice download)")
    pcm = _prime_pcm(b64)
    v = await asyncio.get_event_loop().run_in_executor(live.VOICE_POOL, voice.embed, pcm, voice.MIN_ANCHOR_S)
    if v is None:
        raise HTTPException(400, "Không nghe rõ giọng - thu lại, nói liền mạch và gần mic hơn")
    return v


def _floats(v) -> list:
    return [round(float(x), 6) for x in v]


@app.get("/api/voices")
def list_voices():
    return db.list_voices()


@app.post("/api/voices")
async def create_voice(body: VoiceIn, request: Request = None):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Thiếu tên")
    if not body.consent:
        raise HTTPException(400, "Cần xác nhận người được lưu giọng đã đồng ý")
    if not db.NEW_COLS_OK:
        raise HTTPException(503, "Database chưa thêm được cột model của giọng - chưa lưu giọng được (xem log)")
    v = await _voice_vec(body.pcm)
    u = auth.user_of(request) if request is not None else None
    by = (u or {}).get("email", "")
    vid = db.create_voice(name, _floats(v), voice.MODEL_ID, consent_by=by)
    log.info("voices: lưu giọng '%s' (id %s), xác nhận đồng ý bởi %s", name, vid, by or "?")
    return db.get_voice(vid)


@app.post("/api/voices/{vid}/samples")
async def add_voice_sample(vid: int, body: VoiceSampleIn):
    """Thu thêm một mẫu và gộp vào vector: mẫu ở mic/phòng khác làm nhận diện bền hơn."""
    cur = db.get_voice(vid, with_embedding=True)
    if not cur:
        raise HTTPException(404)
    v = await _voice_vec(body.pcm)
    merged = voice.merge(voice.as_vector(cur["embedding"]), cur["n_samples"] or 1, v)
    db.update_voice(vid, embedding=_floats(merged), n_samples=(cur["n_samples"] or 1) + 1)
    return db.get_voice(vid)


@app.put("/api/voices/{vid}")
def rename_voice(vid: int, body: VoiceNameIn):
    if not body.name.strip():
        raise HTTPException(400, "Thiếu tên")
    db.update_voice(vid, name=body.name.strip())
    return db.get_voice(vid)


@app.delete("/api/voices/{vid}")
def delete_voice(vid: int):
    db.delete_voice(vid)
    log.info("voices: xoá giọng id %s", vid)
    return {"ok": True}


def _ctx(sid: int):
    s = db.get_session(sid)
    if not s:
        raise HTTPException(404, "Không có session")
    c = db.get_candidate(s["candidate_id"])
    p = db.get_position(c["position_id"])
    return s, c, p


def _brain() -> str:
    """Tên đường sinh gợi ý, để UI ghi "nháp từ ..." cho đúng."""
    return llm.CLAUDE_MODEL if llm.PROVIDER == "claude" else "gemini-direct"


async def _run_assist(sid: int, trigger: str, trigger_text: str | None = None):
    ls = LIVE.get(sid)
    s, c, p = _ctx(sid)
    # snapshot() chứ không phải ls.segments: câu ứng viên vừa nói xong thường
    # chưa được chốt thành segment, bỏ nó là gợi ý bám vào câu trả lời cũ.
    segs = ls.snapshot() if ls else s["transcript"]
    if not segs:
        raise HTTPException(400, "Chưa có transcript")
    # Không có câu nào của ứng viên thì model sẽ TỰ BỊA tiền đề: buổi 13 (11/09)
    # nó sinh ra "8 năm làm tài chính của anh" trong khi transcript, CV và JD đều
    # không có con số đó. Chặn ở đây, đỡ luôn $0.03-0.05 tiền panel.
    if not any(x.get("role") == "candidate" for x in segs):
        raise HTTPException(400, "Ứng viên chưa nói gì - chưa có gì để gợi ý. "
                                 "Nếu vai bị gán sai, bấm \"Đổi vai\" rồi thử lại.")
    t = ls.clock() if ls else (segs[-1]["t"] + segs[-1].get("dur", 0))
    st = db.get_settings()
    models = st.get("assist_models") or panel.DEFAULT_MODELS
    synth = st.get("assist_synth", panel.DEFAULT_SYNTH)
    # Có LITELLM_KEY và chọn model ngoài Gemini thì chạy panel nhiều model; không thì gọi thẳng
    # model của LLM_PROVIDER (stream từng câu hỏi).
    use_panel = bool(panel.key()) and any(m != "gemini-direct" for m in models)
    if ls:
        ls.last_assist_t = ls.clock()           # nút bấm cũng đếm vào debounce của wake phrase
        await ls.emit({"type": "assist_pending", "trigger": trigger, "panel": models if use_panel else [_brain()]})

    async def on_draft(a, m):
        if ls:
            await ls.emit({"type": "assist_draft", "t": t, "trigger": trigger, "from": m, "response": a.model_dump()})
    async def on_stream(d: dict):
        # Bản nháp dựng dần trong lúc model còn đang viết: câu hỏi đầu tiên
        # tới màn hình sau vài giây thay vì chờ cả câu trả lời.
        if ls:
            await ls.emit({"type": "assist_draft", "t": t, "trigger": trigger, "from": _brain(), "response": d})
    try:
        if use_panel:
            a, meta = await panel.assist_panel(p, _for_llm(c, p), segs, trigger_text, models, synth, on_draft)
        else:
            a = await llm.assist(p, _for_llm(c, p), segs, trigger_text, on_draft=on_stream)
            meta = {"models": {_brain(): {"ok": True}}, "synth": ""}
    except Exception as e:
        log.exception("assist")
        if ls:
            await ls.emit({"type": "error", "text": f"Trợ lý lỗi: {e}"})
        raise HTTPException(500, str(e))
    resp = a.model_dump()
    resp["panel"] = meta
    aid = db.add_assist(sid, t, trigger, resp)
    ev = {"type": "assist", "id": aid, "t": t, "trigger": trigger, "response": resp}
    if ls:
        await ls.emit(ev)
    return ev


async def _persister(ls: live.InterviewSession):
    """Ghi transcript xuống DB sau mỗi segment final, để crash giữa buổi không mất gì."""
    q = ls.subscribe()
    try:
        while True:
            ev = await q.get()
            if ev["type"] == "final":
                db.update_session(ls.id, transcript=ls.segments, roles=ls.roles)
            elif ev["type"] == "ended":
                return
    finally:
        ls.unsubscribe(q)


async def _start_live(sid: int, p: dict, source: str) -> live.InterviewSession:
    st = db.get_settings()
    ls = live.InterviewSession(sid, st.get("lang", "vi-VN"), p.get("vocab") or [], st.get("wake_phrases", []),
                               engine=st.get("stt_engine", "soniox"))
    ls.on_wake = lambda text: _run_assist(sid, "voice", text)
    LIVE[sid] = ls
    asyncio.create_task(_persister(ls))
    # Chỉ nguồn tách giọng (mic phòng, replay): extension biết vai theo nguồn vật lý, Recall theo
    # tên người tham dự - đoán thêm ở đó chỉ có hại.
    if source in ("mic", "replay"):
        asyncio.create_task(rolefix.run(ls, lambda: _cand_of(sid)))
    return ls


async def _cand_of(sid: int) -> dict:
    return (await asyncio.to_thread(_ctx, sid))[1]


@app.post("/api/candidates/{cid}/sessions")
async def create_session(cid: int, body: SessionIn, request: Request = None):
    c = db.get_candidate(cid)
    if not c:
        raise HTTPException(404)
    engine = db.get_settings().get("stt_engine", "soniox")
    if engine not in live.ENGINES:            # setting cũ còn "muse" thì rơi về soniox
        engine = "soniox"
    if not os.getenv("SONIOX_API_KEY"):
        raise HTTPException(500, "Chưa có SONIOX_API_KEY")
    p = db.get_position(c["position_id"])
    meta = {"engine": engine}
    # Kiểm giọng mẫu TRƯỚC khi tạo buổi, để sai định dạng không để lại một
    # buổi rỗng trong DB. Chỉ có nghĩa với luồng diarize (mic phòng, replay):
    # extension và Recall đã biết vai theo nguồn.
    prime = _prime_pcm(body.prime) if body.prime and body.source in ("mic", "replay") else None
    # Giọng đã lưu của những người phỏng vấn có mặt. Ai nói mà không khớp giọng
    # nào trong danh sách này thì là ứng viên.
    known, host_name, found = [], None, []
    ids = sorted(set(body.voices or []) | ({body.host} if body.host else set()))
    # Không có cột model thì không biết vector lưu bằng model nào - so nhầm model là gọi sai tên.
    if ids and body.source in ("mic", "replay", "ext") and db.NEW_COLS_OK:
        found = db.get_voices(ids)
        if len(found) != len(ids):
            raise HTTPException(400, "Có giọng đã lưu không còn tồn tại - tải lại trang rồi chọn lại")
        meta["people"] = [{"id": r["id"], "name": r["name"], "host": r["id"] == body.host} for r in found]
        # Đang tải/nạp model (server vừa thức dậy) vẫn tính là có: ws_audio/ws_ext chờ
        # model xong rồi mới mở mic, thay vì tắt so giọng cả buổi như trước.
        meta["voice"] = voice.available() or voice.pending()
        host_name = next((r["name"] for r in found if r["id"] == body.host), None)
        if meta["voice"]:
            # Extension: giọng lưu chỉ để so trên luồng TAB (người trong Meet). Chủ
            # phòng nói vào mic, và Meet không bao giờ phát lại giọng của chính
            # mình - nạp giọng chủ phòng vào đây chỉ thêm cơ hội gọi nhầm ứng viên
            # có giọng na ná thành chủ phòng.
            known = [{"key": str(r["id"]), "name": r["name"], "host": r["id"] == body.host,
                      "emb": voice.as_vector(r["embedding"])} for r in found
                     if not (body.source == "ext" and r["id"] == body.host)]
    if prime:
        meta["prime_s"] = round(len(prime) / (live.RATE * 2), 1)
        # Có model thì so giọng từng câu, mở mic ngay; không thì lùi về mồi giọng
        # cho Soniox và phải chờ nó xử lý xong mẫu. Browser đọc cờ này để báo đúng.
        meta["voice"] = voice.available() or voice.pending()
    # Ai bấm tạo buổi = người phỏng vấn. Dùng để gán vai ở đường Recall mà
    # KHÔNG bắt ai cấu hình gì - app đã biết danh tính qua cookie OAuth.
    u = auth.user_of(request) if request is not None else None
    if u and not u.get("service"):
        meta["created_by"] = {"email": u.get("email", ""), "name": u.get("name", "")}
    if body.source == "recall":
        if not recall.key():
            raise HTTPException(400, "Chưa có RECALLAI_API_KEY")
        pub = os.getenv("PUBLIC_WS_BASE")
        if not pub:
            raise HTTPException(400, "Chưa set PUBLIC_WS_BASE (URL wss public để bot đẩy audio về)")
        if not body.meeting_url:
            raise HTTPException(400, "Thiếu meeting_url")
    if body.source in ("recall", "ext"):
        # Cả hai đều KHÔNG mang được cookie phiên: bot Recall không đăng nhập
        # được, còn extension chạy ở origin chrome-extension:// nên cookie
        # samesite=lax bị chặn, mà websocket lại không đặt được header.
        # -> token riêng từng buổi.
        meta["ws_token"] = secrets.token_urlsafe(24)
    sid = db.create_session(cid, source=body.source, meta=meta)
    ls = await _start_live(sid, p, body.source)
    ls.prime_pcm = prime                   # add_stream("mic") dùng rồi tự bỏ
    ls.known = known
    if body.source == "ext":
        ls.stream_defaults = {"mic": ("interviewer", host_name), "tab": ("candidate", None)}
        # Có cả chủ phòng lẫn người phỏng vấn cùng: luồng MIC cũng tách giọng. Người
        # phỏng vấn cùng có thể ngồi cạnh chủ phòng, nói chung mic - trước đây mọi câu
        # trên mic đều mang tên chủ phòng. Chủ phòng là người chắc chắn có mặt trên mic.
        host_row = next((r for r in found if r["id"] == body.host), None) if ids else None
        if known and host_row is not None and meta.get("voice"):
            ls.stream_known = {"tab": known, "mic": known + [
                {"key": str(host_row["id"]), "name": host_row["name"], "host": True,
                 "emb": voice.as_vector(host_row["embedding"])}]}
            ls.known_only = {"mic"}           # ứng viên ở trong Meet, không nói vào mic của mình
    if body.source == "recall":
        region = db.get_settings().get("recall_region", "us-west-2")
        try:
            recv = f"{pub.rstrip('/')}/ws/recall/{sid}?t={meta['ws_token']}"
            bot = await recall.create_bot(body.meeting_url, recv, region)
        except Exception as e:
            LIVE.pop(sid, None)
            db.delete_session(sid)
            raise HTTPException(502, str(e))
        meta = {**meta, "bot_id": bot.get("id"), "region": region, "meeting_url": body.meeting_url}
        db.update_session(sid, meta=meta)
    elif body.source == "replay":
        path = _mock_path(body.file)
        await ls.add_stream("mic", diarize=True)
        asyncio.create_task(_replay(ls, path, body.speed))
        # Gộp chứ không ghi đè: bản cũ thay cả meta và làm mất created_by.
        meta = {**meta, "file": str(path.name), "speed": body.speed}
        db.update_session(sid, meta=meta)
    # Token của bot Recall do server tự ghép vào URL receiver, browser không cần
    # thấy. Extension thì ngược lại: chính browser là bên phải chuyển token cho nó.
    hide = set() if body.source == "ext" else {"ws_token"}
    return {"id": sid, "source": body.source,
            "meta": {k: v for k, v in meta.items() if k not in hide}}


def _mock_path(name: str | None) -> Path:
    if not name:
        raise HTTPException(400, "Thiếu file")
    for d in (HERE / "mock", ROOT / "bench", ROOT):
        cand = (d / Path(name).name)
        if cand.exists() and cand.suffix == ".wav":
            return cand
    raise HTTPException(404, f"Không thấy {name}")


@app.get("/api/mock-files")
def mock_files():
    out = []
    for d in (HERE / "mock", ROOT / "bench"):
        for f in sorted(d.glob("*.wav")):
            with wave.open(str(f)) as w:
                out.append({"file": f.name, "seconds": round(w.getnframes() / w.getframerate()), "dir": d.name})
    return out


async def _replay(ls: live.InterviewSession, path: Path, speed: float):
    """Đẩy wav vào như mic thật, theo thời gian thực (hoặc nhanh hơn `speed` lần)."""
    with wave.open(str(path)) as w:
        rate, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        pcm = w.readframes(w.getnframes())
    if ch == 2:
        pcm = audioop.tomono(pcm, sw, 0.5, 0.5)
    if sw != 2:
        pcm = audioop.lin2lin(pcm, sw, 2)
    if rate != live.RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, live.RATE, None)
    chunk = live.RATE * 2 // 10                          # 100ms
    await ls.emit({"type": "replay", "state": "start", "seconds": len(pcm) / (live.RATE * 2)})
    st = ls.streams["mic"]
    for i in range(0, len(pcm), chunk):
        if ls.id not in LIVE:
            return
        # Engine rt xử lý ~thời gian thực; đẩy quá xa trước là bị đóng (Soniox 1006, Muse 1008 ở 5s).
        while st.sent_s - st.proc_s > st.max_ahead_s and not st.closed:
            await asyncio.sleep(0.05)
        await ls.feed(pcm[i:i + chunk], "mic")
        await asyncio.sleep(0.1 / max(speed, 0.1))
    await ls.emit({"type": "replay", "state": "done"})


@app.get("/api/live-sessions")
def live_sessions():
    """Buổi đang chạy, cho trang chủ: vào thẳng hoặc kết thúc mà khỏi lần theo
    vị trí -> ứng viên. Kèm các buổi còn "live" trong DB nhưng không còn trong RAM
    (server khởi động lại giữa buổi - Render free tự tắt khi rảnh): chúng không
    nghe được nữa, chỉ còn kết thúc để chấm phần transcript đã lưu, hoặc xoá."""
    out = []
    for s in db.live_sessions():
        c = db.get_candidate(s["candidate_id"])
        p = db.get_position(c["position_id"]) if c else None
        if not c or not p:
            continue
        ls = LIVE.get(s["id"])
        # Bao lâu rồi không có âm thanh: extension đã tắt thu (hoặc đóng tab Meet) mà
        # buổi chưa được kết thúc thì trang chủ nói rõ, thay vì hiện "đang chạy" trơn.
        idle = None
        if ls and ls.streams:
            idle = round(min(time.monotonic() - st.last_audio for st in ls.streams.values()))
        out.append({"id": s["id"], "source": s["source"], "started_at": s["started_at"],
                    "candidate": {"id": c["id"], "name": c["name"]},
                    "position": {"id": p["id"], "title": p["title"]},
                    "running": ls is not None,
                    "clock": ls.clock() if ls else None,
                    "audio": bool(ls and ls.streams), "idle_s": idle,
                    "n_segments": len(ls.segments) if ls else (s.get("n_segments") or 0)})
    return out


@app.get("/api/sessions/{sid}")
def get_session(sid: int):
    s, c, p = _ctx(sid)
    ls = LIVE.get(sid)
    if ls:
        s["transcript"] = ls.segments
        s["roles"] = ls.roles
        s["live"] = True
        s["clock"] = ls.clock()
    p["criteria"] = p.get("criteria") or {}
    s["candidate"] = {"id": c["id"], "name": c["name"]}
    s["position"] = {"id": p["id"], "title": p["title"], "rubric": p.get("rubric"), "criteria": p["criteria"]}
    s["screening"] = _screen_brief(c, p)
    if s.get("evaluation"):
        s["evaluation"] = _decorate_eval(s["evaluation"], p)
    return s


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: int):
    ls = LIVE.pop(sid, None)
    if ls:
        await ls.end()
    db.delete_session(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/assist")
async def assist(sid: int, body: dict | None = None):
    body = body or {}
    return await _run_assist(sid, body.get("trigger", "button"), body.get("text"))


class RolesIn(BaseModel):
    roles: dict | None = None
    swap: bool = False


@app.post("/api/sessions/{sid}/roles")
async def set_roles(sid: int, body: RolesIn):
    ls = LIVE.get(sid)
    if ls:
        if body.swap:
            ls.swap_roles()
        elif body.roles is not None:
            ls.set_roles(body.roles)
        ls.roles_locked = True        # người đang phỏng vấn đã quyết: so giọng và rolefix thôi đổi vai
        db.update_session(sid, transcript=ls.segments, roles=ls.roles)
        await ls.emit({"type": "roles", "roles": ls.roles, "segments": ls.segments, "auto": False})
        return {"roles": ls.roles}
    s = db.get_session(sid)
    roles = dict(s["roles"])
    if body.swap:
        inv = {"interviewer": "candidate", "candidate": "interviewer"}
        roles = {k: inv.get(v, v) for k, v in roles.items()}
    elif body.roles is not None:
        roles = body.roles
    for seg in s["transcript"]:
        seg["role"] = roles.get(seg["speaker"])
    db.update_session(sid, transcript=s["transcript"], roles=roles)
    return {"roles": roles}


class SegRoleIn(BaseModel):
    role: str
    t: float                    # thời điểm câu: kiểm đúng câu, phòng transcript đã đổi từ lúc trang vẽ


@app.post("/api/sessions/{sid}/segments/{i}/role")
async def set_segment_role(sid: int, i: int, body: SegRoleIn):
    """Sửa vai MỘT câu. Khác /roles (theo nhãn Soniox, khoá cả buổi): nhãn Soniox hay tráo giữa buổi, nên
    sửa theo nhãn đè lên cả những câu so giọng / nội dung đã gán đúng. Ở đây chỉ câu được bấm đổi;
    buổi đang chạy thì giọng và nội dung vẫn tiếp tục gán các câu khác, câu này thành mốc cho model."""
    if body.role not in ("interviewer", "candidate"):
        raise HTTPException(400, "Vai phải là interviewer hoặc candidate")
    ls = LIVE.get(sid)
    segs = ls.segments if ls else (await asyncio.to_thread(db.get_session, sid) or {}).get("transcript")
    if segs is None:
        raise HTTPException(404, "Không tìm thấy buổi")
    if not (0 <= i < len(segs)) or abs(float(segs[i].get("t", -1)) - body.t) > 0.05:
        raise HTTPException(409, "Transcript đã thay đổi - tải lại trang rồi sửa lại")
    if ls:
        ls.set_seg_role(i, body.role)
    else:
        seg = segs[i]
        seg["role"], seg["manual"] = body.role, True
        if body.role == "candidate":
            seg.pop("who", None)
    await asyncio.to_thread(db.update_session, sid, transcript=list(segs))   # bản chụp: buổi live vẫn đang thêm câu
    return {"i": i, "role": segs[i]["role"], "who": segs[i].get("who")}


@app.post("/api/sessions/{sid}/end")
async def end_session(sid: int, evaluate: bool = True):
    s, c, p = _ctx(sid)
    ls = LIVE.pop(sid, None)
    if ls:
        # Báo NGAY cho mọi nơi đang mở buổi (trang web, panel extension): dừng mic/thu
        # và hiện "đang chấm" - 'ended' chỉ tới sau khi chấm xong, vài chục giây nữa.
        await ls.emit({"type": "ending"})
        await ls.end()
        if s.get("source") in ("mic", "replay"):
            try:
                await rolefix.final_pass(ls, c)     # câu cuối buổi chưa kịp gán vai: gán trước khi chấm
            except Exception as e:
                log.warning("session %s: gán vai cuối buổi lỗi (%s)", sid, e)
        try:
            ls.annotate_evidence()                  # bằng chứng vai từng câu vào transcript (không có vector)
        except Exception as e:
            log.warning("session %s: ghi bằng chứng vai lỗi (%s)", sid, e)
        segs = ls.segments
        roles = ls.roles
        meta = s.get("meta") or {}
        if meta.get("bot_id"):
            try:
                await recall.leave_bot(meta["bot_id"], meta.get("region", "ap-northeast-1"))
            except Exception as e:
                log.warning("leave bot: %s", e)
    else:
        segs, roles = s["transcript"], s["roles"]
    upd = dict(transcript=segs, roles=roles, status="ended", ended_at=time.time())
    if ls and ls.role_stats["calls"]:
        upd["meta"] = {**(s.get("meta") or {}), "roles": dict(ls.role_stats)}
    db.update_session(sid, **upd)
    out = {"id": sid, "n_segments": len(segs)}
    if evaluate and segs:
        try:
            ev = _finalize_eval((await llm.evaluate(p, _for_llm(c, p), segs)).model_dump(), p, c)
            db.update_session(sid, evaluation=ev)
            out["evaluation"] = _decorate_eval(ev, p)
        except Exception as e:
            # Buổi ĐÃ kết thúc và transcript đã lưu - việc "kết thúc" thành công, chỉ bước
            # chấm hỏng. Vẫn 200 + `error`, và vẫn phát 'ended': không thì trang web/panel kẹt
            # ở "đang chấm…", còn extension báo nhầm "chưa kết thúc được buổi". Chấm lại
            # được ở trang buổi.
            out["error"] = _llm_fail(e, "Chấm điểm").detail
    if ls:
        await ls.emit({"type": "ended", "evaluation": out.get("evaluation"),
                       **({"error": out["error"]} if out.get("error") else {})})
    return out


@app.post("/api/sessions/{sid}/evaluate")
async def evaluate_session(sid: int):
    s, c, p = _ctx(sid)
    if not s["transcript"]:
        raise HTTPException(400, "Transcript trống")
    try:
        ev = _finalize_eval((await llm.evaluate(p, _for_llm(c, p), s["transcript"])).model_dump(), p, c)
    except Exception as e:
        raise _llm_fail(e, "Chấm điểm")
    db.update_session(sid, evaluation=ev)
    return _decorate_eval(ev, p)


# --------------------------------------------------------------- websockets ---
def _ws_token_ok(sid: int, ws: WebSocket) -> bool:
    """Token riêng của buổi, truyền qua query `?t=`.

    Dùng cho mọi client KHÔNG mang được cookie phiên lẫn header: bot Recall,
    extension Chrome, và panel của extension chạy trên origin meet.google.com.
    Đánh đổi đã biết: query lọt nguyên văn vào access log của uvicorn/Caddy,
    nên token chỉ sống bằng đúng một buổi.
    """
    want = ((db.get_session(sid) or {}).get("meta") or {}).get("ws_token") or ""
    got = ws.query_params.get("t", "")
    return bool(want and secrets.compare_digest(got, want))


@app.websocket("/ws/session/{sid}/events")
async def ws_events(ws: WebSocket, sid: int):
    # Cookie phiên đi kèm handshake khi người dùng mở giao diện app. Panel của
    # extension thì không có cookie (origin meet.google.com) nên rơi sang
    # ws_token, y như đường /ext mà nó đi kèm.
    if not (auth.user_of_ws(ws) or _ws_token_ok(sid, ws)):
        await ws.close(code=4401)
        return
    await ws.accept()
    ls = LIVE.get(sid)
    if not ls:
        await ws.send_text(json.dumps({"type": "error", "text": "Session không còn chạy"}))
        await ws.close()
        return
    q = ls.subscribe()
    await ws.send_text(json.dumps({"type": "snapshot", "segments": ls.segments, "roles": ls.roles,
                                   "clock": ls.clock()}, ensure_ascii=False))

    async def pump():
        while True:
            ev = await q.get()
            await ws.send_text(json.dumps(ev, ensure_ascii=False))
    t = asyncio.create_task(pump())
    try:
        while True:                                   # chỉ để phát hiện browser ngắt
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        t.cancel()
        ls.unsubscribe(q)


VOICE_WAIT_S = 180      # Render free vừa thức dậy: tải ~28MB + nạp model trên CPU nhỏ


async def _preparing(ls, ws, text: str):
    """Báo bước chuẩn bị cho cả trang đang mở mic (qua ws audio) lẫn mọi trang đang xem."""
    try:
        await ws.send_text(json.dumps({"type": "preparing", "text": text}))
    except Exception:
        pass
    await ls.emit({"type": "preparing", "text": text})


async def _wait_voice(ls, ws):
    """Buổi có so giọng thì CHỜ model sẵn sàng rồi mới mở luồng - "sẵn sàng hết mới
    bắt đầu". Mở mic khi model còn đang tải thì các câu đầu không so được (buổi tạo
    lúc đó còn tắt hẳn so giọng), hoặc so trễ tới mức audio cần cắt đã trôi khỏi bộ
    đệm 120s. Không có giọng nào để so thì không chờ gì cả."""
    if not (ls.known or ls.prime_pcm) or voice.ready():
        return
    voice.ensure_model_async()              # lỡ lần khởi động trước hỏng giữa chừng
    t0 = time.monotonic()
    while voice.pending() and time.monotonic() - t0 < VOICE_WAIT_S:
        st = voice.status()
        await _preparing(ls, ws, "Đang tải model nhận diện giọng (~28MB)…" if st == "đang tải model"
                         else "Đang nạp model nhận diện giọng vào bộ nhớ…")
        await asyncio.sleep(1.0)
    if not voice.ready():
        log.warning("session %s: model giọng chưa sẵn sàng sau %.0fs (%s)", ls.id,
                    time.monotonic() - t0, voice.status())
        await _preparing(ls, ws, f"Không nạp được model nhận diện giọng ({voice.status()}) - "
                                 "buổi vẫn chạy, vai theo nhãn Soniox")


@app.websocket("/ws/session/{sid}/audio")
async def ws_audio(ws: WebSocket, sid: int):
    """Browser gửi PCM16 16kHz mono. Frame text 'stop' = hết."""
    if not auth.user_of_ws(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    ls = LIVE.get(sid)
    if not ls:
        await ws.close(code=4004)
        return
    if "mic" not in ls.streams:
        await _wait_voice(ls, ws)
        await _preparing(ls, ws, "Đang kết nối Soniox…")
        try:
            await ls.add_stream("mic", diarize=True)
        except Exception as e:
            await ls.emit({"type": "error", "text": f"Không mở được Soniox: {e}"})
            await ws.close()
            return
    await ws.send_text(json.dumps({"type": "ready"}))
    await ls.emit({"type": "ready", "stream": "mic"})
    n = 0
    try:
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            if m.get("bytes"):
                n += len(m["bytes"])
                await ls.feed(m["bytes"], "mic")
            elif m.get("text") == "stop":
                break
    except WebSocketDisconnect:
        pass
    log.info("session %s: mic ngắt sau %.0fs audio", sid, n / (live.RATE * 2))


EXT_STREAMS = {"mic": ("interviewer", "Người phỏng vấn (mic)"),
               "tab": ("candidate", "Ứng viên (tiếng trong tab)")}


@app.websocket("/ws/session/{sid}/ext")
async def ws_ext(ws: WebSocket, sid: int):
    """Extension Chrome đẩy HAI luồng audio tách biệt của cùng một buổi.

    mic = getUserMedia (người phỏng vấn ngồi trước máy)
    tab = chrome.tabCapture (tất cả tiếng phát ra từ tab Meet, tức đầu bên kia)

    Vì là hai nguồn vật lý riêng nên KHÔNG cần diarization và vai không bao giờ
    gán nhầm - đây là điểm hơn hẳn mic phòng lẫn đường Recall. Đổi lại, nếu
    phòng có nhiều hơn một người ở đầu bên kia thì tất cả họ chung nhãn
    "candidate"; app vốn chỉ có hai vai nên chấp nhận được.

    Mỗi luồng một kết nối:  /ws/session/{sid}/ext?stream=mic&t=<ws_token>
    """
    stream = ws.query_params.get("stream", "")
    if stream not in EXT_STREAMS:
        await ws.close(code=4003)
        return
    # Giống /ws/recall: LUÔN kiểm token, không phụ thuộc auth.enabled(). Cookie
    # phiên là samesite=lax nên không tới được từ origin chrome-extension://,
    # còn websocket thì không đặt được header x-service-token.
    if not _ws_token_ok(sid, ws):
        log.warning("session %s: ext ws bị chặn (token sai, stream=%s)", sid, stream)
        await ws.close(code=4401)
        return
    ls = LIVE.get(sid)
    if not ls:
        await ws.close(code=4004)
        return
    await ws.accept()
    role, label = EXT_STREAMS[stream]
    if stream not in ls.streams:
        ls.roles[stream] = role          # gán vai TRƯỚC khi mở, để segment đầu tiên đã có vai
        # Có người phỏng vấn cùng (giọng đã lưu) thì luồng tab phải tách giọng: trong
        # Meet giờ có thể là họ HOẶC ứng viên, không còn mặc định một người. Không
        # chọn ai thì giữ nguyên như cũ - tab là ứng viên, khỏi tốn diarization.
        # KHÔNG chờ model giọng ở đây (khác ws_audio): luồng mic của extension chạy
        # ngay, luồng tab mà đứng chờ thì tiếng ứng viên trong lúc chờ mất sạch (gặp
        # thật 25/09). Model chưa xong thì add_stream thu ngay rồi bật so giọng sau.
        diarize = bool(ls.known_for(stream)) and (stream == "tab" or stream in ls.stream_known)
        try:
            await ls.add_stream(stream, diarize=diarize)
        except Exception as e:
            await ls.emit({"type": "error", "text": f"Không mở được Soniox ({stream}): {e}"})
            await ws.close()
            return
        await ls.emit({"type": "participant", "id": stream, "name": label, "role": role})
    await ws.send_text(json.dumps({"type": "ready", "stream": stream, "role": role}))
    await ls.emit({"type": "ready", "stream": stream})
    n = 0
    try:
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            if m.get("bytes"):
                n += len(m["bytes"])
                await ls.feed(m["bytes"], stream)
            elif m.get("text") == "stop":
                break
    except WebSocketDisconnect:
        pass
    log.info("session %s: ext/%s ngắt sau %.0fs audio", sid, stream, n / (live.RATE * 2))


def _recall_role(part: dict, name: str, cre_email: str, cre_name: str, me: str) -> str:
    """Vai của một participant trong buổi qua Recall.

    KHÔNG dùng `is_host`: đo trên cuộc họp thật 11/09, Meet báo is_host đúng cho
    CẢ HAI người -> cả hai thành interviewer, transcript sai vai toàn bộ.

    Chuỗi ưu tiên, dừng ở cái đầu tiên khớp. Chỉ người KHỚP thành interviewer,
    còn lại là candidate - nên trạng thái "ai cũng interviewer" là không thể xảy ra.
    """
    # Chuẩn hoá TẠI ĐÂY, không tin caller đã lower: hợp đồng ngầm kiểu đó vỡ lúc
    # có caller thứ hai (đã suýt vỡ trong chính test của hàm này).
    email = (part.get("email") or "").strip().lower()
    nm = (name or "").strip().lower()
    cre_email = (cre_email or "").strip().lower()
    cre_name = (cre_name or "").strip().lower()
    me = (me or "").strip().lower()
    if cre_email and email and email == cre_email:
        return "interviewer"                      # chắc nhất, nhưng Meet guest thì email=None
    if cre_name and nm and (cre_name in nm or nm in cre_name):
        return "interviewer"                      # tên hiển thị Google vs tên trong Meet
    if me and me in nm:
        return "interviewer"                      # interviewer_name trong Cài đặt, để đè tay
    return "candidate"


@app.websocket("/ws/recall/{sid}")
async def ws_recall(ws: WebSocket, sid: int):
    """Recall.ai bot đẩy audio_separate_raw.data: 1 message / người / chunk. Mỗi người 1 Soniox stream."""
    ls = LIVE.get(sid)
    if not ls:
        await ws.close(code=4004)
        return
    # LUÔN kiểm, không phụ thuộc auth.enabled(): route này bắt buộc public
    # (PUBLIC_WS_BASE) nên tắt auth mà bỏ kiểm là ai cũng đẩy audio vào được.
    # Không có ws_token (buổi không phải nguồn recall) -> cũng chặn, đúng ý.
    if not _ws_token_ok(sid, ws):
        log.warning("session %s: recall ws bị chặn (token sai)", sid)
        await ws.close(code=4401)
        return
    await ws.accept()
    st = db.get_settings()
    me = (st.get("interviewer_name") or "").strip().lower()
    creator = ((db.get_session(sid) or {}).get("meta") or {}).get("created_by") or {}
    cre_email = (creator.get("email") or "").strip().lower()
    cre_name = (creator.get("name") or "").strip().lower()
    names: dict[str, str] = {}
    shape_logged = False
    n_skip = 0
    try:
        while True:
            raw = await ws.receive_text()
            # Một message xấu KHÔNG được giết cả kết nối: handler chết thì Recall
            # reconnect rồi chết lại thành vòng lặp (đã gặp 07/09).
            try:
                m = json.loads(raw)
                if m.get("event") != "audio_separate_raw.data":
                    continue
                # Payload lồng HAI lớp: data.data.{buffer,participant}.
                # Đọc thiếu một tầng thì participant.id ra None và buffer KeyError.
                outer = m.get("data") or {}
                inner = outer.get("data")
                d = inner if isinstance(inner, dict) else outer   # phòng Recall làm phẳng
                if not shape_logged:
                    log.info("session %s: recall payload keys=%s participant keys=%s",
                             sid, sorted(d), sorted(d.get("participant") or {}))
                    shape_logged = True

                part = d.get("participant") or {}
                raw_pid, buf = part.get("id"), d.get("buffer")
                if raw_pid is None or not buf:
                    n_skip += 1
                    if n_skip <= 3:      # log vài lần rồi thôi, đừng ngập journal
                        log.warning("session %s: bỏ message recall thiếu participant.id/buffer, keys=%s",
                                    sid, sorted(d))
                    continue
                pid = str(raw_pid)
                if pid not in ls.streams:
                    name = part.get("name") or f"participant {pid}"
                    names[pid] = name
                    role = _recall_role(part, name, cre_email, cre_name, me)
                    # Trần cứng MỘT interviewer mỗi buổi. Cần thật, không phải
                    # phòng xa: interviewer_name='Linh' khớp cả "Linh Nguyen"
                    # lẫn "Luke Linh", nên tiêu chí khớp tự nó không đảm bảo
                    # duy nhất. Ai khớp trước giữ vai; còn lại là candidate,
                    # sai thì bấm "Đổi vai".
                    if role == "interviewer" and "interviewer" in ls.roles.values():
                        log.warning("session %s: %r cũng khớp interviewer nhưng vai đã có chủ -> candidate",
                                    sid, name)
                        role = "candidate"
                    ls.roles[pid] = role
                    await ls.add_stream(pid, diarize=False)
                    await ls.emit({"type": "participant", "id": pid, "name": name, "role": role})
                    db.update_session(sid, meta={**(db.get_session(sid).get("meta") or {}), "participants": names})
                await ls.feed(base64.b64decode(buf), pid)   # base64 PCM 16k mono S16LE
            except WebSocketDisconnect:
                raise
            except Exception as e:
                log.warning("session %s: lỗi xử lý message recall (%s: %s) - bỏ qua, giữ kết nối",
                            sid, type(e).__name__, e)
    except WebSocketDisconnect:
        log.info("session %s: recall bot ngắt", sid)
