"""Tầng dữ liệu MongoDB cho app phỏng vấn (thay cho db.py Postgres/SQLite của repo gốc).

Dùng CHUNG MongoDB với Meeting: collection interview_positions / interview_candidates / interview_sessions /
interview_assists / interview_settings, và GIỌNG dùng chung registry `voices` của Meeting (kế thừa vân tay giọng).
Giữ nguyên CHỮ KÝ và SHAPE trả về như db.py gốc để app.py + index.html của repo chạy không cần sửa.

Lưu ý quan trọng: `prune_voices` là NO-OP (registry giọng dùng chung với Meeting, không được xoá theo model).
"""
import time
from typing import Any, Dict, List, Optional

from meeting import db as mdb

PG = False
NEW_COLS_OK = True
JSON_COLS = ("rubric", "vocab", "criteria", "screening", "transcript", "roles", "evaluation", "meta", "response")


def _c(name: str):
    return mdb._get_db()[name]


def _strip(d):
    if d:
        d.pop("_id", None)
    return d


def row(r):
    return _strip(dict(r)) if r is not None else None


def rows(rs):
    return [row(r) for r in rs]


def init():
    try:
        g = mdb._get_db()
        g["interview_positions"].create_index("id", unique=True)
        g["interview_candidates"].create_index([("position_id", 1)])
        g["interview_candidates"].create_index("id", unique=True)
        g["interview_sessions"].create_index([("candidate_id", 1)])
        g["interview_sessions"].create_index("id", unique=True)
        g["interview_assists"].create_index([("session_id", 1)])
    except Exception:
        pass


def ping():
    mdb._get_db()["interview_positions"].find_one({}, {"_id": 1})


# --- quyền xem / chia sẻ -----------------------------------------------------
# Mỗi vị trí có `owner` (email người tạo) và `members` (danh sách email được CHIA SẺ để XEM).
# - Xem vị trí (và ứng viên/buổi của nó): admin / chủ vị trí / người trong members.
# - Cấu hình (sửa/xoá/tiêu chí/ứng viên/chia sẻ): admin hoặc CHÍNH chủ vị trí. Member chỉ xem.
# Bản chạy trên máy (auth tắt) -> is_admin()=True nên không lọc gì.
def _email(u):
    if isinstance(u, dict):
        return str(u.get("email") or "").strip().lower()
    return str(u or "").strip().lower()


def _members(p):
    return {_email(m) for m in (p.get("members") or [])} if p else set()


def can_view(user, p) -> bool:
    from meeting import interview_roles as ir
    if ir.is_admin(user):                 # admin + bản chạy máy: xem hết
        return True
    if not p:
        return False
    em = _email(user)
    return bool(em) and (_email(p.get("owner")) == em or em in _members(p))


def can_config(user, p) -> bool:
    from meeting import interview_roles as ir
    if ir.is_admin(user):
        return True
    em = _email(user)
    return bool(em) and bool(p) and _email(p.get("owner")) == em


def _pos_raw(pid):
    if pid is None:
        return None
    return _strip(_c("interview_positions").find_one({"id": int(pid)}))


def owns_position(pid, user) -> bool:
    """Dùng ở middleware: được cấu hình vị trí này không (admin hoặc chủ vị trí)."""
    return can_config(user, _pos_raw(pid))


def candidate_pid(cid):
    c = _strip(_c("interview_candidates").find_one({"id": int(cid)}, {"position_id": 1}))
    return (c or {}).get("position_id")


def set_members(pid, members) -> list:
    mem = sorted({_email(m) for m in (members or []) if _email(m)})
    _c("interview_positions").update_one({"id": int(pid)}, {"$set": {"members": mem}})
    return mem


# --- positions ---------------------------------------------------------------
def _pos_defaults(p):
    """Giao diện repo (vốn chạy trên Postgres có cột default []) luôn đọc p.rubric/p.vocab là MẢNG.
    Vị trí mới tạo trên Mongo chưa có các field này -> đặt mặc định, tránh 'Cannot read ... length/join'."""
    if p is not None:
        p.setdefault("rubric", [])
        p.setdefault("vocab", [])
    return p


def list_positions(viewer=None):
    """viewer=None (gọi nội bộ) -> không lọc. Có viewer -> chỉ vị trí người đó được xem."""
    from meeting import interview_roles as ir
    allow_all = viewer is None or ir.is_admin(viewer)
    em = _email(viewer)
    out = []
    for p in _c("interview_positions").find({}).sort("created_at", -1):
        p = _pos_defaults(_strip(p))
        if not allow_all and _email(p.get("owner")) != em and em not in _members(p):
            continue
        p["n_candidates"] = _c("interview_candidates").count_documents({"position_id": p["id"]})
        out.append(p)
    return out


def get_position(pid):
    return _pos_defaults(_strip(_c("interview_positions").find_one({"id": int(pid)})))


def create_position(**f):
    f.setdefault("created_at", time.time())
    f.setdefault("members", [])           # danh sách email được chia sẻ (view-only)
    f.setdefault("rubric", [])            # UI repo đọc p.rubric/p.vocab là mảng
    f.setdefault("vocab", [])
    if f.get("owner"):
        f["owner"] = _email(f["owner"])
    pid = mdb._next_id("interview_position")
    f["id"] = pid
    _c("interview_positions").insert_one(dict(f))
    return pid


def update_position(pid, **f):
    if f:
        _c("interview_positions").update_one({"id": int(pid)}, {"$set": f})


def delete_position(pid):
    pid = int(pid)
    for c in _c("interview_candidates").find({"position_id": pid}, {"id": 1}):
        delete_candidate(c["id"])
    _c("interview_positions").delete_one({"id": pid})


# --- candidates --------------------------------------------------------------
def _latest_eval_doc(cid):
    return _c("interview_sessions").find_one(
        {"candidate_id": int(cid), "evaluation": {"$ne": None}}, sort=[("started_at", -1)])


def list_candidates(pid):
    out = []
    for c in _c("interview_candidates").find({"position_id": int(pid)}).sort("created_at", 1):
        c = _strip(c)
        c["n_sessions"] = _c("interview_sessions").count_documents({"candidate_id": c["id"]})
        ev = _latest_eval_doc(c["id"])
        c["evaluation"] = (ev or {}).get("evaluation")
        out.append(c)
    return out


def get_candidate(cid):
    return _strip(_c("interview_candidates").find_one({"id": int(cid)}))


def create_candidate(**f):
    f.setdefault("created_at", time.time())
    cid = mdb._next_id("interview_candidate")
    f["id"] = cid
    _c("interview_candidates").insert_one(dict(f))
    return cid


def update_candidate(cid, **f):
    if f:
        _c("interview_candidates").update_one({"id": int(cid)}, {"$set": f})


def delete_candidate(cid):
    cid = int(cid)
    for s in _c("interview_sessions").find({"candidate_id": cid}, {"id": 1}):
        delete_session(s["id"])
    _c("interview_candidates").delete_one({"id": cid})


# --- sessions ----------------------------------------------------------------
def list_sessions(cid):
    out = []
    for s in _c("interview_sessions").find({"candidate_id": int(cid)}).sort("started_at", -1):
        out.append({"id": s["id"], "candidate_id": s["candidate_id"], "source": s.get("source"),
                    "status": s.get("status"), "started_at": s.get("started_at"), "ended_at": s.get("ended_at"),
                    "has_eval": s.get("evaluation") is not None, "n_segments": len(s.get("transcript") or [])})
    return out


def latest_evaluation(cid):
    s = _latest_eval_doc(cid)
    return {"id": s["id"], "started_at": s.get("started_at"), "evaluation": s.get("evaluation")} if s else None


def live_sessions():
    out = []
    for s in _c("interview_sessions").find({"status": "live"}).sort("started_at", -1):
        out.append({"id": s["id"], "candidate_id": s["candidate_id"], "source": s.get("source"),
                    "started_at": s.get("started_at"), "n_segments": len(s.get("transcript") or [])})
    return out


def get_session(sid):
    s = _strip(_c("interview_sessions").find_one({"id": int(sid)}))
    if s:
        s["assists"] = [_strip(a) for a in _c("interview_assists").find({"session_id": int(sid)}).sort("t", 1)]
    return s


def create_session(candidate_id, source="mic", meta=None):
    sid = mdb._next_id("interview_session")
    cand = get_candidate(candidate_id) or {}
    _c("interview_sessions").insert_one({
        "id": sid, "candidate_id": int(candidate_id), "position_id": cand.get("position_id"),
        "source": source, "status": "live", "started_at": time.time(), "ended_at": None,
        "transcript": [], "roles": {}, "evaluation": None, "meta": meta or {}})
    return sid


def update_session(sid, **f):
    if f:
        _c("interview_sessions").update_one({"id": int(sid)}, {"$set": f})


def delete_session(sid):
    sid = int(sid)
    _c("interview_assists").delete_many({"session_id": sid})
    _c("interview_sessions").delete_one({"id": sid})


def add_assist(session_id, t, trigger, response):
    aid = mdb._next_id("interview_assist")
    _c("interview_assists").insert_one({"id": aid, "session_id": int(session_id), "t": t,
                                        "trigger": trigger, "response": response})
    return aid


def evaluated_sessions(pid):
    """Buổi chấm mới nhất của TỪNG ứng viên trong vị trí + candidate_name."""
    out, seen = [], set()
    cand_ids = [c["id"] for c in _c("interview_candidates").find({"position_id": int(pid)}, {"id": 1})]
    names = {c["id"]: c.get("name") for c in _c("interview_candidates").find({"position_id": int(pid)}, {"id": 1, "name": 1})}
    for s in _c("interview_sessions").find(
            {"candidate_id": {"$in": cand_ids}, "evaluation": {"$ne": None}}).sort("started_at", -1):
        if s["candidate_id"] in seen:
            continue
        seen.add(s["candidate_id"])
        s = _strip(s)
        s["candidate_name"] = names.get(s["candidate_id"]) or f"Ứng viên {s['candidate_id']}"
        out.append(s)
    return out


# --- settings ----------------------------------------------------------------
DEFAULT_SETTINGS = {
    "wake_phrases": ["anh nghĩ sao", "em nghĩ sao", "trợ lý ơi", "hỏi gì tiếp"],
    "interviewer_name": "",
    "lang": "vi-VN",
    "recall_region": "ap-northeast-1",
    "stt_engine": "soniox",
    "assist_models": ["gpt-5.5", "vertex_ai/claude-sonnet-5", "gemini-direct"],
    "assist_synth": "vertex_ai/claude-sonnet-5",
}


def get_settings():
    d = {r["key"]: r.get("value") for r in _c("interview_settings").find({})}
    return {**DEFAULT_SETTINGS, **d}


def set_settings(d):
    for k, v in d.items():
        _c("interview_settings").update_one({"key": k}, {"$set": {"key": k, "value": v}}, upsert=True)


# --- voices (DÙNG CHUNG registry `voices` của Meeting) ------------------------
_VOICE_PUB = ("id", "name", "n_samples", "consent_by", "consent_at", "created_at", "updated_at")


def _voice_row(v, with_embedding=False):
    if not v:
        return None
    v = _strip(dict(v))
    v["model"] = v.get("emb_model") or v.get("model")       # repo đọc trường "model"
    if not with_embedding:
        v.pop("embedding", None)
        v = {k: v.get(k) for k in _VOICE_PUB}
    return v


def list_voices():
    return [_voice_row(v) for v in _c("voices").find({}).sort("name", 1)]


def get_voice(vid, with_embedding=False):
    return _voice_row(_c("voices").find_one({"id": int(vid)}), with_embedding)


def get_voices(ids):
    ids = [int(i) for i in ids]
    if not ids:
        return []
    return [_voice_row(v, True) for v in _c("voices").find({"id": {"$in": ids}})]


def create_voice(name, embedding, model, consent_by=""):
    # Gộp theo tên vào registry Meeting (dùng chung); model -> emb_model.
    return mdb.save_voice(name=name, embedding=embedding, consent_by=consent_by, auto_learned=False,
                          n_samples=1, emb_model=model, mode="merge")


def prune_voices(model) -> int:
    return 0          # NO-OP: registry dùng chung với Meeting, không xoá theo model


def update_voice(vid, **f):
    if not f:
        return
    f["updated_at"] = time.time()
    if "model" in f:
        f["emb_model"] = f.pop("model")
    _c("voices").update_one({"id": int(vid)}, {"$set": f})


def delete_voice(vid):
    _c("voices").delete_one({"id": int(vid)})
