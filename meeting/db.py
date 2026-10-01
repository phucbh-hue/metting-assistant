"""Lưu trữ cơ sở dữ liệu MongoDB cho Meeting Assistant AI.

Tách biệt hoàn toàn khỏi Database của dự án Interview để tránh trùng dữ liệu:
- Sử dụng MONGODB_URL từ .env (hỗ trợ MongoDB Atlas Cloud hoặc Local MongoDB).
- Cơ sở dữ liệu: meeting_assistant.
- Hỗ trợ auto-fallback sang mongomock nếu chưa bật MongoDB để server không bao giờ bị crash.
- MEETING_DB=mock: ép dùng mongomock (in-memory) - BẮT BUỘC cho test để không ghi vào dữ liệu thật.

Các Collections:
- voices: Hồ sơ sinh trắc học giọng nói (vector 192 số chuẩn hóa L2, tên, vai trò, phòng ban, consent)
- meetings: Phiên họp (tiêu đề, trạng thái, thời gian, từ vựng STT, trạng thái lập biên bản)
- meeting_speakers: Hồ sơ người nói trong từng cuộc họp (sid ổn định, tên, centroid giọng, liên kết voices)
- meeting_segments: Từng câu thoại (seq, start, end, speaker_key, speaker_label, nhãn Soniox, text, vector)
- identity_inferences: Các phán đoán suy luận danh tính người nói từ nội dung đối thoại
- ai_artifacts: Các sản phẩm do AI sinh ra (Minutes, Web design, Diagram) kèm versioning
- ai_interactions: Lịch sử gọi trợ lý, wake-word, thinking logs, MCP tool calls
- mcp_mock_data: Cơ sở dữ liệu giả lập cho MCP (nhân viên, tickets Jira, kiến trúc hệ thống)
- settings: Cấu hình hệ thống key-value
- counters: Auto-increment sequence generator cho ID số
"""
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
import numpy as np
import pymongo

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("meeting.db")

MONGODB_URL = os.getenv("MONGODB_URL", "").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "meeting_assistant").strip()
FORCE_MOCK = os.getenv("MEETING_DB", "").strip().lower() in ("mock", "memory", "mongomock")

_client: Optional[pymongo.MongoClient] = None
_db = None
_is_mock = False

PUBLIC_SEGMENT_FIELDS = {"_id": 0, "raw_embedding": 0}


def _get_db():
    global _client, _db, _is_mock
    if _db is not None:
        return _db

    if FORCE_MOCK:
        import mongomock
        _client = mongomock.MongoClient()
        _db = _client[MONGODB_DB_NAME]
        _is_mock = True
        log.info("meeting.db: MEETING_DB=mock -> dùng Mongomock (In-Memory)")
        return _db

    # 1. Thử kết nối tới MONGODB_URL nếu được cấu hình
    if MONGODB_URL:
        try:
            client = pymongo.MongoClient(
                MONGODB_URL,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10000
            )
            client.admin.command("ping")
            _client = client
            _db = client[MONGODB_DB_NAME]
            _is_mock = False
            log.info("meeting.db: Đã kết nối thành công tới MongoDB (%s / db: %s)",
                     MONGODB_URL.split("@")[-1] if "@" in MONGODB_URL else MONGODB_URL,
                     MONGODB_DB_NAME)
            return _db
        except Exception as e:
            log.warning("meeting.db: Không thể kết nối tới MONGODB_URL (%s). Đang thử fallback.", e)

    # 2. Thử kết nối tới localhost:27017 mặc định
    try:
        client = pymongo.MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=1000)
        client.admin.command("ping")
        _client = client
        _db = client[MONGODB_DB_NAME]
        _is_mock = False
        log.info("meeting.db: Đã kết nối thành công tới Localhost MongoDB (db: %s)", MONGODB_DB_NAME)
        return _db
    except Exception:
        pass

    # 3. Fallback sang mongomock để hệ thống luôn chạy trơn tru kể cả khi chưa bật MongoDB server
    import mongomock
    _client = mongomock.MongoClient()
    _db = _client[MONGODB_DB_NAME]
    _is_mock = True
    log.info("meeting.db: Đang sử dụng Mongomock (In-Memory MongoDB) - dữ liệu sẽ mất khi tắt server!")
    return _db


def is_mock() -> bool:
    _get_db()
    return _is_mock


def get_status() -> Dict[str, Any]:
    _get_db()
    return {
        "engine": "MongoDB",
        "database": MONGODB_DB_NAME,
        "is_mock": _is_mock,
        "url_configured": bool(MONGODB_URL)
    }


def _next_id(seq_name: str) -> int:
    db = _get_db()
    ret = db["counters"].find_one_and_update(
        {"_id": seq_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=pymongo.ReturnDocument.AFTER
    )
    return ret["seq"]


def init():
    db = _get_db()
    try:
        db["voices"].create_index("id", unique=True)
        db["voices"].create_index("name")
        db["meetings"].create_index("id", unique=True)
        db["meetings"].create_index([("started_at", -1)])
        db["meeting_segments"].create_index([("meeting_id", 1), ("t_start", 1)])
        db["meeting_segments"].create_index([("meeting_id", 1), ("seq", 1)])
        db["meeting_segments"].create_index([("meeting_id", 1), ("speaker_key", 1)])
        db["meeting_speakers"].create_index([("meeting_id", 1), ("sid", 1)], unique=True)
        db["identity_inferences"].create_index("meeting_id")
        db["ai_artifacts"].create_index([("meeting_id", 1), ("version", -1)])
        db["mcp_mock_data"].create_index([("collection", 1), ("key", 1)], unique=True)
        db["settings"].create_index("key", unique=True)
    except Exception as e:
        log.warning("meeting.db init indexes: %s", e)
    log.info("meeting.db: Khởi tạo collections MongoDB hoàn tất (%s)", "Mock" if _is_mock else "Live MongoDB")


def _strip(d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if d:
        d.pop("_id", None)
    return d


# ----------------------------------------------------------------- VOICES ---
def list_voices(with_embedding: bool = False) -> List[Dict[str, Any]]:
    proj = {"_id": 0} if with_embedding else {"_id": 0, "embedding": 0}
    return list(_get_db()["voices"].find({}, proj).sort("name", 1))


def get_voice(vid: int) -> Optional[Dict[str, Any]]:
    return _strip(_get_db()["voices"].find_one({"id": vid}))


def get_voice_by_name(name: str) -> Optional[Dict[str, Any]]:
    clean = (name or "").strip()
    if not clean:
        return None
    pattern = f"^{re.escape(clean)}$"
    return _strip(_get_db()["voices"].find_one({"name": {"$regex": pattern, "$options": "i"}}))


def _unit(v: Iterable[float]) -> List[float]:
    arr = np.asarray(list(v), dtype=np.float32)
    n = float(np.linalg.norm(arr))
    return (arr / n).tolist() if n > 0 else arr.tolist()


def save_voice(name: str, embedding: Optional[List[float]], role: str = "", department: str = "", email: str = "",
               consent_by: str = "", auto_learned: bool = False, n_samples: int = 1,
               mode: str = "merge") -> int:
    """Lưu / cập nhật hồ sơ giọng nói theo tên (không phân biệt hoa thường).

    mode="replace": thay hẳn vector (thu mẫu lại trong Voice Studio).
    mode="merge"  : trộn có trọng số với vector cũ theo số mẫu (học thêm từ cuộc họp).
    Hồ sơ thu mẫu thủ công (auto_learned=False) KHÔNG bị dữ liệu tự học ghi đè vector.
    """
    db = _get_db()
    now = time.time()
    emb = _unit(embedding) if embedding is not None else None
    existing = get_voice_by_name(name)
    if existing:
        vid = existing["id"]
        upd: Dict[str, Any] = {"updated_at": now}
        old = existing.get("embedding")
        old_ok = bool(old) and float(np.linalg.norm(np.asarray(old, dtype=np.float32))) > 1e-3
        if emb is not None:
            if mode == "replace" or not old_ok:
                upd.update({"embedding": emb, "n_samples": int(n_samples), "auto_learned": bool(auto_learned)})
                if consent_by:
                    upd.update({"consent_by": consent_by, "consent_at": now})
            elif auto_learned and not existing.get("auto_learned", False):
                pass  # giữ nguyên mẫu thu thủ công
            else:
                n_old = max(1, int(existing.get("n_samples") or 1))
                n_new = max(1, int(n_samples))
                merged = (np.asarray(old, dtype=np.float32) * min(n_old, 50)
                          + np.asarray(emb, dtype=np.float32) * min(n_new, 50))
                upd.update({"embedding": _unit(merged), "n_samples": min(n_old + n_new, 999)})
        if role:
            upd["role"] = role.strip()
        if department:
            upd["department"] = department.strip()
        if email:
            upd["email"] = email.strip()
        db["voices"].update_one({"id": vid}, {"$set": upd})
        return vid

    vid = _next_id("voices")
    db["voices"].insert_one({
        "id": vid,
        "name": name.strip(),
        "email": (email or "").strip(),
        "role": (role or "").strip(),
        "department": (department or "").strip(),
        "embedding": emb,
        "n_samples": int(n_samples),
        "consent_by": consent_by,
        "consent_at": now,
        "auto_learned": bool(auto_learned),
        "created_at": now,
        "updated_at": now
    })
    return vid


def update_voice(vid: int, fields: Dict[str, Any]) -> bool:
    allowed = {k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()
               if k in ("name", "role", "department", "email")}
    if not allowed:
        return False
    allowed["updated_at"] = time.time()
    return _get_db()["voices"].update_one({"id": vid}, {"$set": allowed}).matched_count > 0


def delete_voice(vid: int) -> bool:
    db = _get_db()
    res = db["voices"].delete_one({"id": vid})
    # Bỏ liên kết ở các hồ sơ người nói (giữ lại tên trong lịch sử cuộc họp)
    db["meeting_speakers"].update_many({"voice_id": vid}, {"$set": {"voice_id": None}})
    return res.deleted_count > 0


# ----------------------------------------------------------------- MEETINGS ---
def create_meeting(title: str, description: str = "", host_id: Optional[int] = None,
                   meeting_type: str = "Technical Review", agenda: Optional[List[str]] = None,
                   expected_attendees: Optional[List[str]] = None,
                   vocab: Optional[List[str]] = None, source: str = "mic") -> int:
    db = _get_db()
    now = time.time()
    mid = _next_id("meetings")
    db["meetings"].insert_one({
        "id": mid,
        "title": (title or "").strip() or "Cuộc họp nội bộ",
        "description": (description or "").strip(),
        "meeting_type": (meeting_type or "").strip() or "Technical Review",
        "host_id": host_id,
        "agenda": agenda or [],
        "expected_attendees": expected_attendees or [],
        "vocab": vocab or [],
        "source": source,
        "status": "live",
        "started_at": now,
        "ended_at": None,
        "minutes_status": None,
        "summary": "",
        "action_items": [],
        "meta": {}
    })
    return mid


def get_meeting(mid: Any) -> Optional[Dict[str, Any]]:
    try:
        mid_val = int(mid)
    except Exception:
        mid_val = mid
    return _strip(_get_db()["meetings"].find_one({"id": mid_val}))


def list_meetings(limit: int = 50) -> List[Dict[str, Any]]:
    return list(_get_db()["meetings"].find({}, {"_id": 0}).sort("started_at", -1).limit(limit))


def list_meetings_with_stats(limit: int = 100) -> List[Dict[str, Any]]:
    """Danh sách cuộc họp kèm thống kê (thời lượng, số câu, số người nói, số artifacts)."""
    db = _get_db()
    docs = list(db["meetings"].find({}, {"_id": 0}).sort("started_at", -1).limit(limit))
    ids = [d["id"] for d in docs]
    seg_stats: Dict[int, Dict[str, Any]] = {}
    art_counts: Dict[int, int] = {}
    spk_counts: Dict[int, int] = {}
    try:
        for row in db["meeting_segments"].aggregate([
            {"$match": {"meeting_id": {"$in": ids}}},
            {"$group": {"_id": "$meeting_id", "n": {"$sum": 1}, "labels": {"$addToSet": "$speaker_label"}}}
        ]):
            seg_stats[row["_id"]] = row
        for row in db["ai_artifacts"].aggregate([
            {"$match": {"meeting_id": {"$in": ids}}},
            {"$group": {"_id": "$meeting_id", "n": {"$sum": 1}}}
        ]):
            art_counts[row["_id"]] = row["n"]
        for row in db["meeting_speakers"].aggregate([
            {"$match": {"meeting_id": {"$in": ids}, "merged_into": None, "n_segments": {"$gt": 0}}},
            {"$group": {"_id": "$meeting_id", "n": {"$sum": 1}}}
        ]):
            spk_counts[row["_id"]] = row["n"]
    except Exception as e:  # pragma: no cover - fallback cho backend không hỗ trợ aggregate
        log.warning("list_meetings_with_stats aggregate lỗi (%s), dùng count từng cuộc họp", e)
        for mid in ids:
            seg_stats[mid] = {"n": db["meeting_segments"].count_documents({"meeting_id": mid}), "labels": []}
            art_counts[mid] = db["ai_artifacts"].count_documents({"meeting_id": mid})
    now = time.time()
    for d in docs:
        st = seg_stats.get(d["id"], {})
        d["segment_count"] = st.get("n", 0)
        d["speaker_count"] = spk_counts.get(d["id"]) or len([x for x in st.get("labels", []) if x])
        d["artifact_count"] = art_counts.get(d["id"], 0)
        if d.get("ended_at") and d.get("started_at"):
            d["duration_s"] = int(d["ended_at"] - d["started_at"])
        elif d.get("started_at"):
            d["duration_s"] = int(now - d["started_at"])
        else:
            d["duration_s"] = 0
    return docs


MEETING_EDITABLE = ("title", "description", "meeting_type", "agenda", "expected_attendees", "vocab", "host_id")


def update_meeting(mid: int, updates: Dict[str, Any], internal: bool = False) -> bool:
    """Cập nhật thông tin cuộc họp. API công khai chỉ được sửa các trường MEETING_EDITABLE."""
    clean = {k: v for k, v in updates.items() if k not in ("_id", "id") and (internal or k in MEETING_EDITABLE)}
    if not clean:
        return False
    return _get_db()["meetings"].update_one({"id": mid}, {"$set": clean}).matched_count > 0


def delete_meeting(mid: int) -> bool:
    """Xóa hoàn toàn cuộc họp và toàn bộ dữ liệu liên quan."""
    db = _get_db()
    for col in ("meeting_segments", "meeting_speakers", "identity_inferences", "ai_artifacts", "ai_interactions"):
        db[col].delete_many({"meeting_id": mid})
    return db["meetings"].delete_one({"id": mid}).deleted_count > 0


def end_meeting(mid: int) -> bool:
    return _get_db()["meetings"].update_one(
        {"id": mid, "status": {"$ne": "ended"}},
        {"$set": {"status": "ended", "ended_at": time.time()}}
    ).modified_count > 0


def get_stats() -> Dict[str, Any]:
    db = _get_db()
    meetings = list(db["meetings"].find({}, {"_id": 0, "status": 1, "started_at": 1, "ended_at": 1}))
    ended = [m for m in meetings if m.get("status") == "ended" and m.get("started_at") and m.get("ended_at")]
    total_s = sum(max(0.0, m["ended_at"] - m["started_at"]) for m in ended)
    return {
        "meetings_total": len(meetings),
        "meetings_live": sum(1 for m in meetings if m.get("status") == "live"),
        "meetings_ended": len(ended),
        "total_duration_s": int(total_s),
        "avg_duration_s": int(total_s / len(ended)) if ended else 0,
        "segments_total": db["meeting_segments"].count_documents({}),
        "voices_total": db["voices"].count_documents({}),
    }


# -------------------------------------------------------- MEETING SEGMENTS ---
def add_segment(meeting_id: int, t_start: float, t_end: float, speaker_label: str, text: str,
                speaker_id: Optional[int] = None, confidence: float = 1.0, is_inferred: bool = False,
                raw_embedding: Optional[List[float]] = None, seq: Optional[int] = None,
                speaker_key: Optional[int] = None, raw_speaker: Optional[str] = None,
                epoch: Optional[int] = None, stream: str = "mic", voiced: Optional[float] = None) -> int:
    db = _get_db()
    sid = _next_id("segments")
    db["meeting_segments"].insert_one({
        "id": sid,
        "meeting_id": meeting_id,
        "seq": seq if seq is not None else sid,
        "t_start": t_start,
        "t_end": t_end,
        "speaker_key": speaker_key,
        "speaker_label": speaker_label,
        "speaker_id": speaker_id,
        "text": text,
        "confidence": confidence,
        "is_inferred": bool(is_inferred),
        "raw_speaker": raw_speaker,
        "epoch": epoch,
        "stream": stream,
        "voiced": voiced,
        "raw_embedding": raw_embedding
    })
    return sid


def get_segments(meeting_id: int, with_embedding: bool = False) -> List[Dict[str, Any]]:
    proj = {"_id": 0} if with_embedding else PUBLIC_SEGMENT_FIELDS
    docs = list(_get_db()["meeting_segments"].find({"meeting_id": meeting_id}, proj).sort("t_start", 1))
    # Dữ liệu cũ chưa có seq: dùng id làm khóa
    for d in docs:
        if d.get("seq") is None:
            d["seq"] = d.get("id")
    return docs


def backfill_segment_seq(meeting_id: int) -> int:
    """Dữ liệu cũ (bản 2.5) chưa có trường seq: ghi seq = id để cập nhật theo câu hoạt động được."""
    col = _get_db()["meeting_segments"]
    n = 0
    for d in list(col.find({"meeting_id": meeting_id, "seq": None}, {"_id": 1, "id": 1})):
        col.update_one({"_id": d["_id"]}, {"$set": {"seq": d.get("id")}})
        n += 1
    return n


def max_segment_seq(meeting_id: int) -> int:
    d = _get_db()["meeting_segments"].find_one(
        {"meeting_id": meeting_id, "seq": {"$ne": None}}, {"seq": 1}, sort=[("seq", -1)])
    return int(d["seq"]) if d and d.get("seq") is not None else 0


def max_segment_epoch(meeting_id: int) -> int:
    d = _get_db()["meeting_segments"].find_one(
        {"meeting_id": meeting_id, "epoch": {"$ne": None}}, {"epoch": 1}, sort=[("epoch", -1)])
    return int(d["epoch"]) if d and d.get("epoch") is not None else -1


def update_segments_speaker_by_seqs(meeting_id: int, items: List[Dict[str, Any]]) -> int:
    """items: [{seq, speaker_key, speaker_label, speaker_id, is_inferred?}] - cập nhật từng câu."""
    if not items:
        return 0
    pairs = []
    for it in items:
        upd = {"speaker_key": it.get("speaker_key"), "speaker_label": it.get("speaker_label"),
               "speaker_id": it.get("speaker_id")}
        if "is_inferred" in it:
            upd["is_inferred"] = bool(it["is_inferred"])
        pairs.append(({"meeting_id": meeting_id, "seq": it["seq"]}, {"$set": upd}))
    col = _get_db()["meeting_segments"]
    try:
        return col.bulk_write([pymongo.UpdateOne(f, u) for f, u in pairs], ordered=False).modified_count
    except Exception:
        return sum(col.update_one(f, u).modified_count for f, u in pairs)


def update_segments_speaker(meeting_id: int, old_label: str, new_name: str,
                            new_speaker_id: Optional[int] = None, is_inferred: bool = True) -> int:
    """(Tương thích dữ liệu cũ) Cập nhật lùi tên người nói theo nhãn."""
    res = _get_db()["meeting_segments"].update_many(
        {"meeting_id": meeting_id, "speaker_label": old_label},
        {"$set": {"speaker_label": new_name, "speaker_id": new_speaker_id, "is_inferred": bool(is_inferred)}}
    )
    return res.modified_count


# -------------------------------------------------------- MEETING SPEAKERS ---
def upsert_speakers(meeting_id: int, profiles: List[Dict[str, Any]]):
    if not profiles:
        return
    now = time.time()
    pairs = []
    for p in profiles:
        doc = {k: v for k, v in p.items() if k != "_id"}
        doc.update({"meeting_id": meeting_id, "updated_at": now})
        pairs.append(({"meeting_id": meeting_id, "sid": p["sid"]},
                      {"$set": doc, "$setOnInsert": {"created_at": now}}))
    col = _get_db()["meeting_speakers"]
    try:
        col.bulk_write([pymongo.UpdateOne(f, u, upsert=True) for f, u in pairs], ordered=False)
    except Exception:
        for f, u in pairs:
            col.update_one(f, u, upsert=True)


def list_speakers(meeting_id: int, with_vector: bool = False) -> List[Dict[str, Any]]:
    proj = {"_id": 0} if with_vector else {"_id": 0, "centroid": 0}
    return list(_get_db()["meeting_speakers"].find({"meeting_id": meeting_id}, proj).sort("sid", 1))


# ---------------------------------------------------- IDENTITY INFERENCES ---
def save_identity_inference(meeting_id: int, unknown_label: str, predicted_name: str,
                            confidence: float, reasoning: str, evidence: List[Dict[str, Any]],
                            predicted_role: str = "", status: str = "auto_applied",
                            speaker_key: Optional[int] = None) -> int:
    db = _get_db()
    iid = _next_id("inferences")
    db["identity_inferences"].insert_one({
        "id": iid,
        "meeting_id": meeting_id,
        "speaker_key": speaker_key,
        "unknown_label": unknown_label,
        "predicted_name": predicted_name,
        "predicted_role": predicted_role,
        "confidence": confidence,
        "reasoning": reasoning,
        "evidence": evidence,
        "status": status,
        "created_at": time.time()
    })
    return iid


def get_inference(iid: int) -> Optional[Dict[str, Any]]:
    return _strip(_get_db()["identity_inferences"].find_one({"id": iid}))


def set_inference_status(iid: int, status: str) -> bool:
    return _get_db()["identity_inferences"].update_one(
        {"id": iid}, {"$set": {"status": status, "resolved_at": time.time()}}).matched_count > 0


def get_inferences(meeting_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
    q: Dict[str, Any] = {"meeting_id": meeting_id}
    if status:
        q["status"] = status
    return list(_get_db()["identity_inferences"].find(q, {"_id": 0}).sort("created_at", -1))


# ----------------------------------------------------------- AI ARTIFACTS ---
def save_artifact(meeting_id: int, kind: str, title: str, content: str,
                  prompt_trigger: str = "", parent_id: Optional[int] = None) -> int:
    db = _get_db()
    latest = db["ai_artifacts"].find_one({"meeting_id": meeting_id, "title": title}, sort=[("version", -1)])
    cur_v = latest.get("version", 0) if latest else 0
    aid = _next_id("artifacts")
    db["ai_artifacts"].insert_one({
        "id": aid,
        "meeting_id": meeting_id,
        "kind": kind,
        "title": title,
        "content": content,
        "version": cur_v + 1,
        "parent_id": parent_id,
        "prompt_trigger": prompt_trigger,
        "created_at": time.time()
    })
    return aid


def get_artifacts(meeting_id: int) -> List[Dict[str, Any]]:
    return list(_get_db()["ai_artifacts"].find({"meeting_id": meeting_id}, {"_id": 0}).sort("created_at", -1))


def get_artifact(artifact_id: int) -> Optional[Dict[str, Any]]:
    return _strip(_get_db()["ai_artifacts"].find_one({"id": artifact_id}))


# --------------------------------------------------------- AI INTERACTIONS ---
def record_interaction(meeting_id: int, prompt: str, trigger: str = "voice_wake_word",
                       thinking: str = "", tool_calls: Optional[List[Dict[str, Any]]] = None,
                       response: Optional[Dict[str, Any]] = None, t: float = 0.0) -> int:
    iid = _next_id("interactions")
    _get_db()["ai_interactions"].insert_one({
        "id": iid,
        "meeting_id": meeting_id,
        "t": t,
        "trigger": trigger,
        "prompt": prompt,
        "thinking": thinking,
        "tool_calls": tool_calls or [],
        "response": response or {},
        "created_at": time.time()
    })
    return iid


# ---------------------------------------------------------- MCP MOCK DATA ---
def get_mcp_item(collection: str, key: str) -> Optional[Dict[str, Any]]:
    d = _get_db()["mcp_mock_data"].find_one({"collection": collection, "key": key})
    return d.get("data") if d else None


def query_mcp_collection(collection: str) -> List[Dict[str, Any]]:
    out = []
    for d in _get_db()["mcp_mock_data"].find({"collection": collection}):
        item = d.get("data", {})
        item["_key"] = d.get("key")
        out.append(item)
    return out


def put_mcp_item(collection: str, key: str, data: Dict[str, Any]):
    _get_db()["mcp_mock_data"].update_one(
        {"collection": collection, "key": key},
        {"$set": {"data": data}},
        upsert=True
    )


# --------------------------------------------------------------- SETTINGS ---
def get_setting(key: str, default: str = "") -> str:
    d = _get_db()["settings"].find_one({"key": key})
    return d.get("value", default) if d else default


def set_setting(key: str, value: str):
    _get_db()["settings"].update_one({"key": key}, {"$set": {"value": value}}, upsert=True)
