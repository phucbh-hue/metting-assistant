"""Lưu trữ cơ sở dữ liệu MongoDB cho Meeting Assistant AI.

Tách biệt hoàn toàn khỏi Database của dự án Interview để tránh trùng dữ liệu:
- Sử dụng MONGODB_URL từ .env (hỗ trợ MongoDB Atlas Cloud hoặc Local MongoDB).
- Cơ sở dữ liệu: meeting_assistant.
- Hỗ trợ auto-fallback sang mongomock nếu chưa bật MongoDB để server không bao giờ bị crash.

Các Collections:
- voices: Hồ sơ sinh trắc học giọng nói (vector 192 số chuẩn hóa L2, tên, vai trò, phòng ban, consent)
- meetings: Phiên họp (tiêu đề, trạng thái, thời gian, từ vựng STT)
- meeting_segments: Từng câu thoại theo thời gian thực (start, end, speaker_id, text, confidence, is_inferred)
- identity_inferences: Các phán đoán suy luận danh tính người lạ từ nội dung đối thoại
- ai_artifacts: Các sản phẩm do AI sinh ra (Minutes, Web design, Diagram, Image) kèm versioning
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
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
import pymongo

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("meeting.db")

MONGODB_URL = os.getenv("MONGODB_URL", "").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "meeting_assistant").strip()

_client: Optional[pymongo.MongoClient] = None
_db = None
_is_mock = False


def _get_db():
    global _client, _db, _is_mock
    if _db is not None:
        return _db

    # 1. Thử kết nối tới MONGODB_URL nếu được cấu hình
    if MONGODB_URL:
        try:
            client = pymongo.MongoClient(
                MONGODB_URL,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=5000
            )
            # Test ping
            client.admin.command("ping")
            _client = client
            _db = client[MONGODB_DB_NAME]
            _is_mock = False
            log.info("meeting.db: Đã kết nối thành công tới MongoDB (%s / db: %s)",
                     MONGODB_URL.split("@")[-1] if "@" in MONGODB_URL else MONGODB_URL,
                     MONGODB_DB_NAME)
            return _db
        except Exception as e:
            log.warning("meeting.db: Không thể kết nối tới MONGODB_URL (%s). Đang dùng in-memory mongomock làm fallback.", e)

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
    try:
        import mongomock
        _client = mongomock.MongoClient()
        _db = _client[MONGODB_DB_NAME]
        _is_mock = True
        log.info("meeting.db: Đang sử dụng Mongomock (In-Memory MongoDB) - Sẵn sàng nhận MONGODB_URL thật!")
        return _db
    except Exception as e:
        log.error("meeting.db: Lỗi khởi tạo MongoDB: %s", e)
        raise


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
    # Tạo các index cần thiết
    try:
        db["voices"].create_index("id", unique=True)
        db["voices"].create_index("name")
        db["meetings"].create_index("id", unique=True)
        db["meeting_segments"].create_index([("meeting_id", 1), ("t_start", 1)])
        db["identity_inferences"].create_index("meeting_id")
        db["ai_artifacts"].create_index([("meeting_id", 1), ("version", -1)])
        db["mcp_mock_data"].create_index([("collection", 1), ("key", 1)], unique=True)
        db["settings"].create_index("key", unique=True)
    except Exception as e:
        log.warning("meeting.db init indexes: %s", e)
    log.info("meeting.db: Khởi tạo collections MongoDB hoàn tất (%s)", "Mock" if _is_mock else "Live MongoDB")


# ----------------------------------------------------------------- VOICES ---
def list_voices() -> List[Dict[str, Any]]:
    db = _get_db()
    docs = list(db["voices"].find().sort("name", 1))
    out = []
    for d in docs:
        d["id"] = d.get("id")
        d.pop("_id", None)
        d.pop("embedding", None)  # Bỏ vector lớn khi liệt kê
        out.append(d)
    return out


def get_voice(vid: int) -> Optional[Dict[str, Any]]:
    d = _get_db()["voices"].find_one({"id": vid})
    if d:
        d.pop("_id", None)
    return d


def get_voice_by_name(name: str) -> Optional[Dict[str, Any]]:
    clean = name.strip()
    if not clean:
        return None
    pattern = f"^{re.escape(clean)}$"
    d = _get_db()["voices"].find_one({"name": {"$regex": pattern, "$options": "i"}})
    if d:
        d.pop("_id", None)
    return d


def save_voice(name: str, embedding: List[float], role: str = "", department: str = "", email: str = "",
               consent_by: str = "", auto_learned: bool = False, n_samples: int = 1) -> int:
    db = _get_db()
    now = time.time()
    existing = get_voice_by_name(name)
    if existing:
        vid = existing["id"]
        update_fields: Dict[str, Any] = {
            "embedding": embedding,
            "n_samples": n_samples,
            "updated_at": now
        }
        if role:
            update_fields["role"] = role
        if department:
            update_fields["department"] = department
        if email:
            update_fields["email"] = email
        db["voices"].update_one({"id": vid}, {"$set": update_fields})
        return vid
    else:
        vid = _next_id("voices")
        doc = {
            "id": vid,
            "name": name.strip(),
            "email": email.strip(),
            "role": role.strip(),
            "department": department.strip(),
            "embedding": embedding,
            "n_samples": n_samples,
            "consent_by": consent_by,
            "consent_at": now,
            "auto_learned": bool(auto_learned),
            "created_at": now,
            "updated_at": now
        }
        db["voices"].insert_one(doc)
        return vid


def delete_voice(vid: int) -> bool:
    res = _get_db()["voices"].delete_one({"id": vid})
    return res.deleted_count > 0


# ----------------------------------------------------------------- MEETINGS ---
def create_meeting(title: str, description: str = "", host_id: Optional[int] = None,
                   meeting_type: str = "Technical Review", agenda: Optional[List[str]] = None,
                   expected_attendees: Optional[List[str]] = None,
                   vocab: Optional[List[str]] = None, source: str = "mic") -> int:
    db = _get_db()
    now = time.time()
    mid = _next_id("meetings")
    doc = {
        "id": mid,
        "title": title.strip() or "Cuộc họp kỹ thuật",
        "description": description.strip(),
        "meeting_type": meeting_type.strip() or "Technical Review",
        "host_id": host_id,
        "agenda": agenda or [],
        "expected_attendees": expected_attendees or [],
        "vocab": vocab or [],
        "source": source,
        "status": "live",
        "started_at": now,
        "ended_at": None,
        "summary": "",
        "action_items": [],
        "meta": {}
    }
    db["meetings"].insert_one(doc)
    return mid


def get_meeting(mid: Any) -> Optional[Dict[str, Any]]:
    try:
        mid_val = int(mid)
    except Exception:
        mid_val = mid
    d = _get_db()["meetings"].find_one({"id": mid_val})
    if d:
        d.pop("_id", None)
    return d


def list_meetings(limit: int = 50) -> List[Dict[str, Any]]:
    docs = list(_get_db()["meetings"].find().sort("started_at", -1).limit(limit))
    for d in docs:
        d.pop("_id", None)
    return docs


def list_meetings_with_stats(limit: int = 50) -> List[Dict[str, Any]]:
    """Liệt kê danh sách cuộc họp kèm thống kê chi tiết (thời lượng, số câu, artifacts)."""
    db = _get_db()
    docs = list(db["meetings"].find().sort("started_at", -1).limit(limit))
    out = []
    for d in docs:
        d.pop("_id", None)
        mid = d["id"]
        seg_count = db["meeting_segments"].count_documents({"meeting_id": mid})
        art_count = db["ai_artifacts"].count_documents({"meeting_id": mid})
        d["segment_count"] = seg_count
        d["artifact_count"] = art_count
        if d.get("ended_at") and d.get("started_at"):
            d["duration_s"] = int(d["ended_at"] - d["started_at"])
        elif d.get("started_at"):
            d["duration_s"] = int(time.time() - d["started_at"])
        else:
            d["duration_s"] = 0
        out.append(d)
    return out


def update_meeting(mid: int, updates: Dict[str, Any]) -> bool:
    db = _get_db()
    clean_updates = {k: v for k, v in updates.items() if k not in ("_id", "id")}
    res = db["meetings"].update_one(
        {"id": mid},
        {"$set": clean_updates}
    )
    return res.modified_count > 0


def delete_meeting(mid: int) -> bool:
    """Xóa hoàn toàn cuộc họp và toàn bộ dữ liệu liên quan."""
    db = _get_db()
    db["meeting_segments"].delete_many({"meeting_id": mid})
    db["identity_inferences"].delete_many({"meeting_id": mid})
    db["ai_artifacts"].delete_many({"meeting_id": mid})
    db["ai_interactions"].delete_many({"meeting_id": mid})
    res = db["meetings"].delete_one({"id": mid})
    return res.deleted_count > 0


def end_meeting(mid: int) -> bool:
    now = time.time()
    res = _get_db()["meetings"].update_one(
        {"id": mid},
        {"$set": {"status": "ended", "ended_at": now}}
    )
    return res.modified_count > 0


# -------------------------------------------------------- MEETING SEGMENTS ---
def add_segment(meeting_id: int, t_start: float, t_end: float, speaker_label: str, text: str,
                speaker_id: Optional[int] = None, confidence: float = 1.0, is_inferred: bool = False,
                raw_embedding: Optional[List[float]] = None) -> int:
    db = _get_db()
    sid = _next_id("segments")
    doc = {
        "id": sid,
        "meeting_id": meeting_id,
        "t_start": t_start,
        "t_end": t_end,
        "speaker_label": speaker_label,
        "speaker_id": speaker_id,
        "text": text,
        "confidence": confidence,
        "is_inferred": bool(is_inferred),
        "raw_embedding": raw_embedding
    }
    db["meeting_segments"].insert_one(doc)
    return sid


def get_segments(meeting_id: int) -> List[Dict[str, Any]]:
    docs = list(_get_db()["meeting_segments"].find({"meeting_id": meeting_id}).sort("t_start", 1))
    for d in docs:
        d.pop("_id", None)
    return docs


def update_segments_speaker(meeting_id: int, old_label: str, new_name: str,
                            new_speaker_id: Optional[int] = None, is_inferred: bool = True) -> int:
    """Cập nhật lùi (retroactive update) tên người nói cho toàn bộ các câu trong buổi."""
    res = _get_db()["meeting_segments"].update_many(
        {"meeting_id": meeting_id, "speaker_label": old_label},
        {"$set": {
            "speaker_label": new_name,
            "speaker_id": new_speaker_id,
            "is_inferred": bool(is_inferred)
        }}
    )
    return res.modified_count


# ---------------------------------------------------- IDENTITY INFERENCES ---
def save_identity_inference(meeting_id: int, unknown_label: str, predicted_name: str,
                            confidence: float, reasoning: str, evidence: List[Dict[str, Any]],
                            predicted_role: str = "", status: str = "auto_applied") -> int:
    db = _get_db()
    iid = _next_id("inferences")
    now = time.time()
    doc = {
        "id": iid,
        "meeting_id": meeting_id,
        "unknown_label": unknown_label,
        "predicted_name": predicted_name,
        "predicted_role": predicted_role,
        "confidence": confidence,
        "reasoning": reasoning,
        "evidence": evidence,
        "status": status,
        "created_at": now
    }
    db["identity_inferences"].insert_one(doc)
    return iid


def get_inferences(meeting_id: int) -> List[Dict[str, Any]]:
    docs = list(_get_db()["identity_inferences"].find({"meeting_id": meeting_id}).sort("created_at", -1))
    for d in docs:
        d.pop("_id", None)
    return docs


# ----------------------------------------------------------- AI ARTIFACTS ---
def save_artifact(meeting_id: int, kind: str, title: str, content: str,
                  prompt_trigger: str = "", parent_id: Optional[int] = None) -> int:
    db = _get_db()
    now = time.time()
    # Tìm version cao nhất hiện tại cho title này trong buổi họp
    latest = db["ai_artifacts"].find_one(
        {"meeting_id": meeting_id, "title": title},
        sort=[("version", -1)]
    )
    cur_v = latest.get("version", 0) if latest else 0
    aid = _next_id("artifacts")
    doc = {
        "id": aid,
        "meeting_id": meeting_id,
        "kind": kind,
        "title": title,
        "content": content,
        "version": cur_v + 1,
        "parent_id": parent_id,
        "prompt_trigger": prompt_trigger,
        "created_at": now
    }
    db["ai_artifacts"].insert_one(doc)
    return aid


def get_artifacts(meeting_id: int) -> List[Dict[str, Any]]:
    docs = list(_get_db()["ai_artifacts"].find({"meeting_id": meeting_id}).sort("created_at", -1))
    for d in docs:
        d.pop("_id", None)
    return docs


def get_artifact(artifact_id: int) -> Optional[Dict[str, Any]]:
    d = _get_db()["ai_artifacts"].find_one({"id": artifact_id})
    if d:
        d.pop("_id", None)
    return d


# --------------------------------------------------------- AI INTERACTIONS ---
def record_interaction(meeting_id: int, prompt: str, trigger: str = "voice_wake_word",
                       thinking: str = "", tool_calls: Optional[List[Dict[str, Any]]] = None,
                       response: Optional[Dict[str, Any]] = None, t: float = 0.0) -> int:
    db = _get_db()
    now = time.time()
    iid = _next_id("interactions")
    doc = {
        "id": iid,
        "meeting_id": meeting_id,
        "t": t,
        "trigger": trigger,
        "prompt": prompt,
        "thinking": thinking,
        "tool_calls": tool_calls or [],
        "response": response or {},
        "created_at": now
    }
    db["ai_interactions"].insert_one(doc)
    return iid


# ---------------------------------------------------------- MCP MOCK DATA ---
def get_mcp_item(collection: str, key: str) -> Optional[Dict[str, Any]]:
    d = _get_db()["mcp_mock_data"].find_one({"collection": collection, "key": key})
    return d.get("data") if d else None


def query_mcp_collection(collection: str) -> List[Dict[str, Any]]:
    docs = list(_get_db()["mcp_mock_data"].find({"collection": collection}))
    out = []
    for d in docs:
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
    _get_db()["settings"].update_one(
        {"key": key},
        {"$set": {"value": value}},
        upsert=True
    )
