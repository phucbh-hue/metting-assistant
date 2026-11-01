"""Chế độ Phỏng vấn: lưu trữ + nghiệp vụ (port từ interviewer-assistant-AI-circle/interview/app.py + db.py).

Ba nhóm dữ liệu trong Mongo:
  interview_positions   vị trí tuyển dụng: JD, yêu cầu, khung tiêu chí (rubric), persona, vocab
  interview_candidates  ứng viên: CV, ghi chú, bản sàng lọc CV (screening)
  interview_sessions    buổi phỏng vấn: transcript, bản đánh giá (evaluation)

Nguyên tắc giữ từ bản gốc: độ phù hợp % và điểm tổng LUÔN tính bằng code (score_match), không tin số model;
kết quả của model được căn theo đúng tên + thứ tự tiêu chí (_align); việc AI dài chạy NỀN, trạng thái persist ở
DB để chịu được chuyển trang / restart. Mọi truy vấn DB bọc trong asyncio.to_thread (Atlas ở Singapore, mỗi câu
là một vòng mạng; chạy thẳng trên event loop thì audio các buổi họp/phỏng vấn khựng).
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

from meeting import db
from meeting import interview_llm as L

log = logging.getLogger("meeting.interview")

READ_TIMEOUT_S = 150.0
SCREEN_TIMEOUT_S = 240.0
EVAL_TIMEOUT_S = 240.0
SCREEN_PARALLEL = 3


def _pos():
    return db._get_db()["interview_positions"]


def _cand():
    return db._get_db()["interview_candidates"]


def _sess():
    return db._get_db()["interview_sessions"]


# ------------------------------------------------------------------ làm sạch ---
def _lines(xs, cap: int = 25) -> List[str]:
    out, seen = [], set()
    for x in xs or []:
        s = " ".join(str(x or "").split())
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:cap]


def clean_rubric(rubric: list) -> list:
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


def clean_criteria(d: dict) -> dict:
    return {"source": "hr" if d.get("source") == "hr" else "ai", "file": str(d.get("file") or "")[:200],
            "must_have": _lines(d.get("must_have")), "nice_to_have": _lines(d.get("nice_to_have")),
            "at": time.time()}


# --------------------------------------------------------------- CRUD vị trí ---
_POS_FIELDS = ("title", "jd", "requirements", "background", "persona", "rubric", "vocab", "criteria", "members")


def create_position(owner: str, **f) -> int:
    pid = db._next_id("interview_position")
    now = time.time()
    doc = {"id": pid, "owner": (owner or "").strip().lower(), "title": str(f.get("title") or "").strip(),
           "jd": str(f.get("jd") or ""), "requirements": str(f.get("requirements") or ""),
           "background": str(f.get("background") or ""), "persona": str(f.get("persona") or ""),
           "rubric": clean_rubric(f.get("rubric") or []), "vocab": _lines(f.get("vocab"), 120),
           "criteria": {}, "members": _lines(f.get("members"), 50), "created_at": now, "updated_at": now}
    _pos().insert_one(dict(doc))
    return pid


def get_position(pid: int) -> Optional[dict]:
    p = db._strip(_pos().find_one({"id": int(pid)}))
    if p:
        p["criteria"] = p.get("criteria") or {}
    return p


def list_positions(user: Optional[dict]) -> List[dict]:
    """Admin: tất cả. Owner: vị trí mình tạo. Người phỏng vấn: vị trí được thêm vào members."""
    q = _visible_query(user)
    rows = [db._strip(x) for x in _pos().find(q).sort("created_at", -1)]
    return rows


def _visible_query(user: Optional[dict]) -> dict:
    from meeting import interview_roles
    if interview_roles.is_admin(user):
        return {}
    email = str((user or {}).get("email") or "").lower()
    return {"$or": [{"owner": email}, {"members": email}]}


def can_edit_position(user: Optional[dict], p: Optional[dict]) -> bool:
    """Chỉ admin hoặc owner (người tạo vị trí) được sửa vị trí / tiêu chí / ứng viên."""
    from meeting import interview_roles
    if interview_roles.is_admin(user):
        return True
    email = str((user or {}).get("email") or "").lower()
    return bool(p) and email and email == str(p.get("owner") or "").lower()


def can_view_position(user: Optional[dict], p: Optional[dict]) -> bool:
    from meeting import interview_roles
    if interview_roles.is_admin(user) or not p:
        return bool(p)
    email = str((user or {}).get("email") or "").lower()
    return bool(email) and (email == str(p.get("owner") or "").lower() or email in (p.get("members") or []))


def update_position(pid: int, **f) -> None:
    upd = {k: v for k, v in f.items() if k in _POS_FIELDS and v is not None}
    if "rubric" in upd:
        upd["rubric"] = clean_rubric(upd["rubric"])
    if "criteria" in upd:
        upd["criteria"] = clean_criteria(upd["criteria"])
    if "vocab" in upd:
        upd["vocab"] = _lines(upd["vocab"], 120)
    if "members" in upd:
        upd["members"] = _lines(upd["members"], 50)
    upd["updated_at"] = time.time()
    _pos().update_one({"id": int(pid)}, {"$set": upd})


def delete_position(pid: int) -> None:
    for c in list_candidates(pid):
        cancel_screening(c["id"])
        _cand().delete_one({"id": c["id"]})
    _sess().delete_many({"position_id": int(pid)})
    _pos().delete_one({"id": int(pid)})


# -------------------------------------------------------------- CRUD ứng viên ---
def create_candidate(position_id: int, **f) -> int:
    cid = db._next_id("interview_candidate")
    now = time.time()
    doc = {"id": cid, "position_id": int(position_id), "name": str(f.get("name") or "").strip(),
           "cv": str(f.get("cv") or ""), "notes": str(f.get("notes") or ""),
           "screening": {}, "created_at": now, "updated_at": now}
    _cand().insert_one(dict(doc))
    return cid


def get_candidate(cid: int) -> Optional[dict]:
    return db._strip(_cand().find_one({"id": int(cid)}))


def list_candidates(pid: int) -> List[dict]:
    return [db._strip(x) for x in _cand().find({"position_id": int(pid)}).sort("created_at", 1)]


def update_candidate(cid: int, **f) -> None:
    upd = {k: v for k, v in f.items() if k in ("name", "cv", "notes", "screening") and v is not None}
    upd["updated_at"] = time.time()
    _cand().update_one({"id": int(cid)}, {"$set": upd})


def delete_candidate(cid: int) -> None:
    cancel_screening(cid)
    _sess().delete_many({"candidate_id": int(cid)})
    _cand().delete_one({"id": int(cid)})


# --------------------------------------------------------------- CRUD buổi ---
def create_session(candidate_id: int, position_id: int, source: str = "ext", **f) -> int:
    sid = db._next_id("interview_session")
    now = time.time()
    doc = {"id": sid, "candidate_id": int(candidate_id), "position_id": int(position_id), "source": source,
           "status": "live", "started_at": now, "ended_at": None, "transcript": [], "roles": {},
           "evaluation": None, "interviewers": _lines(f.get("interviewers"), 20),
           "token": f.get("token") or "", "meta": f.get("meta") or {}}
    _sess().insert_one(dict(doc))
    return sid


def get_session(sid: int) -> Optional[dict]:
    return db._strip(_sess().find_one({"id": int(sid)}))


def list_sessions(candidate_id: Optional[int] = None, position_id: Optional[int] = None) -> List[dict]:
    q: Dict[str, Any] = {}
    if candidate_id is not None:
        q["candidate_id"] = int(candidate_id)
    if position_id is not None:
        q["position_id"] = int(position_id)
    return [db._strip(x) for x in _sess().find(q).sort("started_at", -1)]


def update_session(sid: int, **f) -> None:
    _sess().update_one({"id": int(sid)}, {"$set": f})


def append_segment(sid: int, seg: dict) -> None:
    _sess().update_one({"id": int(sid)}, {"$push": {"transcript": seg}})


def evaluated_sessions(pid: int) -> List[dict]:
    out = []
    for s in _sess().find({"position_id": int(pid), "evaluation": {"$ne": None}}).sort("ended_at", -1):
        s = db._strip(s)
        c = get_candidate(s["candidate_id"])
        s["candidate_name"] = (c or {}).get("name") or f"Ứng viên {s['candidate_id']}"
        out.append(s)
    return out


# ------------------------------------------------- dấu vân tay / căn tên / điểm ---
def crit_sig(p: dict) -> str:
    """Dấu vân tay bộ tiêu chí (KHÔNG gồm tỷ trọng: match tính lại lúc đọc)."""
    cr = p.get("criteria") or {}
    basis = [[c.get("name"), c.get("description")] for c in p.get("rubric") or []]
    blob = json.dumps([basis, cr.get("must_have") or [], cr.get("nice_to_have") or []], ensure_ascii=False)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def cv_sig(c: dict) -> str:
    return hashlib.sha1(((c.get("cv") or "") + "\x00" + (c.get("notes") or "")).encode()).hexdigest()[:12]


def fresh_screening(c: dict, p: dict) -> Optional[dict]:
    s = c.get("screening") or {}
    if s.get("result") and s.get("sig") == crit_sig(p) and s.get("cv_sig") == cv_sig(c):
        return s
    return None


def _req_status(s) -> str:
    s = str(s or "").strip().lower().replace("-", "_").replace(" ", "_")
    return s if s in L.REQ_STATUS else {"yes": "met", "đạt": "met", "no": "not_met",
                                        "không_đạt": "not_met", "một_phần": "partial"}.get(s, "unknown")


def _align(items: list, key: str, wants: list, blank) -> list:
    """Xếp kết quả model theo đúng danh sách gốc + đúng tên gốc (trùng tên -> giống nhất -> cùng thứ tự)."""
    got: Dict[int, int] = {}
    used: set = set()
    for k, w in enumerate(wants):
        nw = L.norm_name(w)
        for i, x in enumerate(items):
            if i not in used and L.norm_name(x.get(key)) == nw:
                got[k] = i
                used.add(i)
                break
    pairs = sorted(((L.name_sim(w, x.get(key)), k, i) for k, w in enumerate(wants) if k not in got
                    for i, x in enumerate(items) if i not in used), reverse=True)
    for sim, k, i in pairs:
        if sim < L.NAME_SIM_MIN:
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


def _align_reqs(ev: dict, p: dict) -> None:
    cr = p.get("criteria") or {}
    for key in ("must_have", "nice_to_have"):
        xs = [dict(x, status=_req_status(x.get("status"))) for x in ev.get(key) or []]
        want = cr.get(key) or []
        ev[key] = _align(xs, "requirement", want, _blank_req) if want and xs else []


def clean_screening(r: dict, p: dict) -> dict:
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


def screen_view(c: dict, p: dict, full: bool = True) -> dict:
    """Trạng thái sàng lọc cho trình duyệt; độ phù hợp % tính LẠI mỗi lần đọc theo tỷ trọng hiện tại."""
    raw = c.get("screening") or {}
    s = dict(raw)
    if c["id"] in SCREENING:
        s["status"] = "running"
    elif not s:
        return {"status": "none"}
    elif s.get("status") == "running":
        s.update(status="error", error="Bị ngắt giữa chừng (server vừa khởi động lại) - bấm Chấm lại")
    r = s.get("result")
    if r:
        m = L.score_match(p.get("rubric") or [], {x.get("name"): x.get("score") for x in r.get("criteria") or []})
        s.update(match=m["match"], coverage=m["coverage"], avg=m["avg"], recommendation=r.get("recommendation"))
        must = r.get("must_have") or []
        s["must"] = {k: sum(1 for x in must if x.get("status") == k) for k in L.REQ_STATUS}
        s["stale"] = fresh_screening({**c, "screening": raw}, p) is None
    if not full:
        s.pop("result", None)
    return s


def finalize_eval(ev: dict, p: dict, c: dict) -> dict:
    """Bản chấm vừa sinh: xếp theo đúng tiêu chí + chụp lại bản sàng lọc CV đã dùng (trước/sau)."""
    rub = p.get("rubric") or []
    if rub:
        ev["competencies"] = _align(ev.get("competencies") or [], "name", [x["name"] for x in rub],
                                    lambda n: {"name": n, "score": 0, "evidence": [], "cv_check": "",
                                               "assessment": "(AI chưa chấm tiêu chí này)"})
    _align_reqs(ev, p)
    s = fresh_screening(c, p)
    if s:
        r = s["result"]
        ev["cv_snapshot"] = {"scores": {x.get("name"): x.get("score") or 0 for x in r.get("criteria") or []},
                             "must_have": {x.get("requirement"): x.get("status") for x in r.get("must_have") or []},
                             "at": s.get("at")}
    return ev


def decorate_eval(ev: dict, p: dict) -> dict:
    """Điểm tổng + độ phù hợp tính BẰNG CODE lúc đọc, theo tỷ trọng hiện tại (đè overall_score của model)."""
    ev = dict(ev)
    rub = p.get("rubric") or []
    scores = {x.get("name"): x.get("score") for x in ev.get("competencies") or []}
    m = L.score_match(rub, scores)
    if m["avg"] is None and scores:
        m = L.score_match([], scores)
    if m["avg"] is not None:
        ev["overall_score"] = m["avg"]
    snap = ev.get("cv_snapshot")
    cv = None
    if snap:
        cm = L.score_match(rub, snap.get("scores") or {})
        cv = {"match": cm["match"], "coverage": cm["coverage"], "scores": snap.get("scores") or {},
              "must_have": snap.get("must_have") or {}}
    ev["match"] = {"final": m["match"], "coverage": m["coverage"], "cv": cv}
    return ev


def position_detail(pid: int) -> Optional[dict]:
    """Vị trí + danh sách ứng viên (đã rút gọn: không kèm cả CV / cả bản chấm)."""
    p = get_position(pid)
    if not p:
        return None
    cands = list_candidates(pid)
    for c in cands:
        c["screening"] = screen_view(c, p, full=False)
        c["has_cv"] = bool((c.get("cv") or "").strip())
        c.pop("cv", None)
    # đính kèm buổi + bản chấm (rút gọn) theo ứng viên
    evs = {s["candidate_id"]: s for s in evaluated_sessions(pid)}
    for c in cands:
        s = evs.get(c["id"])
        if s and s.get("evaluation"):
            ev = decorate_eval(s["evaluation"], p)
            c["evaluation"] = {"session_id": s["id"], "overall_score": ev.get("overall_score"),
                               "recommendation": ev.get("recommendation"), "match": {"final": ev["match"]["final"]}}
    p["candidates"] = cands
    p["criteria_sig"] = crit_sig(p)
    p["criteria_job"] = crit_job_view(pid)
    return p


# ------------------------------------------------- việc AI chạy NỀN: tiêu chí ---
CRIT_JOBS: Dict[int, dict] = {}       # pid -> {"kind","status","started_at","file","error","task"}


def crit_job_view(pid: int) -> Optional[dict]:
    j = CRIT_JOBS.get(int(pid))
    return {k: j[k] for k in ("kind", "status", "started_at", "file", "error") if k in j} if j else None


def _start_crit_job(pid: int, kind: str, work, file: str = "") -> Optional[dict]:
    cur = CRIT_JOBS.get(int(pid))
    if cur and cur["status"] == "running":
        raise RuntimeError("AI đang xử lý tiêu chí của vị trí này - đợi xong rồi thử lại")
    job = {"kind": kind, "status": "running", "started_at": time.time(), "file": file}
    CRIT_JOBS[int(pid)] = job

    async def run():
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("vị trí %s: %s lỗi", pid, kind)
            job.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")
        else:
            if CRIT_JOBS.get(int(pid)) is job:
                CRIT_JOBS.pop(int(pid), None)

    job["task"] = asyncio.create_task(run())
    return crit_job_view(pid)


async def build_criteria(pid: int) -> Optional[dict]:
    """AI đề xuất khung tiêu chí từ JD (chạy nền)."""
    p = get_position(pid)
    if not p:
        raise RuntimeError("Không tìm thấy vị trí")

    async def work():
        b = await asyncio.wait_for(L.build_position(p), READ_TIMEOUT_S)
        upd = {"rubric": clean_rubric([c.model_dump() for c in b.competencies]), "vocab": b.vocab,
               "criteria": {"source": "ai", "file": "", "must_have": _lines(b.must_have),
                            "nice_to_have": _lines(b.nice_to_have), "at": time.time()}}
        now = get_position(pid) or {}
        if not (now.get("persona") or "").strip():
            upd["persona"] = b.persona
        await asyncio.to_thread(update_position, pid, **upd)
        await _screen_missing(pid)
    return _start_crit_job(pid, "build", work)


async def import_criteria(pid: int, doc) -> Optional[dict]:
    """Nhập bộ tiêu chí HR từ file (chạy nền), GIỮ NGUYÊN tiêu chí + tỷ trọng HR đặt."""
    p = get_position(pid)
    if not p:
        raise RuntimeError("Không tìm thấy vị trí")

    async def work():
        b = await asyncio.wait_for(L.import_criteria(p, doc), READ_TIMEOUT_S)
        upd = {"rubric": clean_rubric([c.model_dump() for c in b.competencies]), "vocab": b.vocab,
               "criteria": {"source": "hr", "file": doc.name, "must_have": _lines(b.must_have),
                            "nice_to_have": _lines(b.nice_to_have), "at": time.time()}}
        now = get_position(pid) or {}
        if not (now.get("background") or "").strip() and b.background:
            upd["background"] = b.background
        if not (now.get("persona") or "").strip():
            upd["persona"] = b.persona
        await asyncio.to_thread(update_position, pid, **upd)
        await _screen_missing(pid)
    return _start_crit_job(pid, "import", work, file=doc.name)


# --------------------------------------------- việc AI chạy NỀN: sàng lọc CV ---
SCREENING: Dict[int, asyncio.Task] = {}
_screen_active = 0


async def _screen_slot():
    global _screen_active
    while _screen_active >= SCREEN_PARALLEL:
        await asyncio.sleep(0.5)
    _screen_active += 1


async def _screen(cid: int):
    global _screen_active
    slot = False
    try:
        await _screen_slot()
        slot = True
        c = await asyncio.to_thread(get_candidate, cid)
        p = await asyncio.to_thread(get_position, c["position_id"]) if c else None
        if not c or not p:
            return
        sig, cvs, t0 = crit_sig(p), cv_sig(c), time.monotonic()
        try:
            r = await asyncio.wait_for(L.screen_cv(p, c), SCREEN_TIMEOUT_S)
            upd = {"status": "done", "at": time.time(), "sig": sig, "cv_sig": cvs,
                   "seconds": round(time.monotonic() - t0, 1), "result": clean_screening(r.model_dump(), p)}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = "AI trả lời quá lâu" if isinstance(e, (TimeoutError, asyncio.TimeoutError)) else f"{type(e).__name__}: {str(e)[:240]}"
            log.warning("sàng lọc CV ứng viên %s lỗi: %s", cid, err)
            cur = ((await asyncio.to_thread(get_candidate, cid)) or {}).get("screening") or {}
            upd = {**cur, "status": "error", "at": time.time(), "error": err}
        if SCREENING.get(cid) is not asyncio.current_task():
            return
        await asyncio.to_thread(update_candidate, cid, screening=upd)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("sàng lọc CV ứng viên %s: lỗi ngoài model", cid)
    finally:
        if slot:
            _screen_active -= 1
        if SCREENING.get(cid) is asyncio.current_task():
            SCREENING.pop(cid, None)


def cancel_screening(cid: int) -> None:
    t = SCREENING.pop(int(cid), None)
    if t and not t.done():
        t.cancel()


def spawn_screening(cid: int, force: bool = False) -> bool:
    """Chấm (hoặc chấm lại) một CV. force: huỷ lượt cũ; không force thì bỏ qua nếu đang chạy."""
    cid = int(cid)
    c = get_candidate(cid)
    if not c or not (c.get("cv") or "").strip():
        return False
    old = SCREENING.get(cid)
    if old and not old.done():
        if not force:
            return False
        old.cancel()

    async def run():
        await _screen(cid)
    SCREENING[cid] = asyncio.create_task(run())
    return True


async def _screen_missing(pid: int) -> None:
    """Sau khi tiêu chí đổi: chấm những CV chưa có bản còn khớp (stale)."""
    p = await asyncio.to_thread(get_position, pid)
    if not p:
        return
    for c in await asyncio.to_thread(list_candidates, pid):
        if (c.get("cv") or "").strip() and fresh_screening(c, p) is None:
            spawn_screening(c["id"], force=False)


# --------------------------------------------------- chấm sau buổi + so sánh ---
async def evaluate_session(sid: int) -> dict:
    """Chấm lại cả buổi: CV + transcript -> Evaluation (đồng bộ, người bấm đang chờ)."""
    s = await asyncio.to_thread(get_session, sid)
    if not s:
        raise RuntimeError("Không tìm thấy buổi phỏng vấn")
    p = await asyncio.to_thread(get_position, s["position_id"])
    c = await asyncio.to_thread(get_candidate, s["candidate_id"])
    if not p or not c:
        raise RuntimeError("Buổi thiếu vị trí hoặc ứng viên")
    ev = await asyncio.wait_for(L.evaluate(p, c, s.get("transcript") or []), EVAL_TIMEOUT_S)
    data = finalize_eval(ev.model_dump(), p, c)
    await asyncio.to_thread(update_session, sid, evaluation=data)
    return decorate_eval(data, p)


async def compare_candidates(pid: int, refresh: bool = False) -> dict:
    p = await asyncio.to_thread(get_position, pid)
    if not p:
        raise RuntimeError("Không tìm thấy vị trí")
    sessions = await asyncio.to_thread(evaluated_sessions, pid)
    if len(sessions) < 2:
        raise RuntimeError(f"Cần ít nhất 2 ứng viên đã có đánh giá (hiện có {len(sessions)})")
    for s in sessions:
        s["evaluation"] = decorate_eval(s["evaluation"], p)
    names = [s["candidate_name"] for s in sessions]
    comps = [c["name"] for c in (p.get("rubric") or [])] or sorted(
        {c["name"] for s in sessions for c in s["evaluation"].get("competencies", [])})
    matrix = []
    for cn in comps:
        rowd = {"competency": cn, "scores": {}}
        for s in sessions:
            sc = next((c["score"] for c in s["evaluation"].get("competencies", [])
                       if L.norm_name(c["name"]) == L.norm_name(cn)), None)
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
            "must_checked": bool(ev.get("must_have")),
            "must_open": sorted(({"requirement": x.get("requirement"), "status": x.get("status")}
                                 for x in ev.get("must_have") or [] if x.get("status") != "met"),
                                key=lambda x: {"not_met": 0, "partial": 1}.get(x["status"], 2))}
    key = json.dumps([[s["id"], _hash(s["evaluation"])] for s in sessions]
                     + [crit_sig(p), [c.get("weight") for c in p.get("rubric") or []]])
    cache = p.get("compare_cache") or {}
    narrative = cache.get("narrative") if (not refresh and cache.get("key") == key) else None
    if narrative is None:
        cmp = await L.compare(p, [{"candidate_name": s["candidate_name"], "evaluation": s["evaluation"]} for s in sessions])
        narrative = cmp.model_dump()
        await asyncio.to_thread(lambda: _pos().update_one(
            {"id": int(pid)}, {"$set": {"compare_cache": {"key": key, "narrative": narrative, "at": time.time()}}}))
    return {"position": {"id": pid, "title": p["title"]}, "candidates": names, "matrix": matrix,
            "overall": overall, "narrative": narrative}


def _hash(x) -> str:
    return hashlib.sha1(json.dumps(x, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:10]


def screen_brief(c: dict, p: dict) -> Optional[dict]:
    """Phần sàng lọc CV người phỏng vấn cần NGAY trong buổi (chỉ bản còn khớp CV + tiêu chí)."""
    s = fresh_screening(c, p)
    if not s:
        return None
    r = s["result"]
    m = L.score_match(p.get("rubric") or [], {x.get("name"): x.get("score") for x in r.get("criteria") or []})
    return {"match": m["match"], "coverage": m["coverage"], "questions": r.get("questions") or [],
            "requirements": [x for x in r.get("must_have") or [] if x.get("status") != "met"],
            "gaps": r.get("gaps") or [], "red_flags": r.get("red_flags") or []}
