"""Nhóm cuộc họp (theo chủ đề / phòng ban): chủ nhóm, thành viên, quyền xem các cuộc họp trong nhóm.

Bản web (AUTH_REQUIRED=1):
- Chủ nhóm và thành viên xem được mọi cuộc họp của nhóm, tạo và tham gia cuộc họp trong nhóm.
- Chỉ chủ nhóm quản lý nhóm: đổi tên, thêm / bớt thành viên, thư mục Drive, xóa nhóm.
- Xóa nhóm không xóa cuộc họp: các cuộc họp thành "không thuộc nhóm" và vẫn thuộc người đã tạo.

Chạy trên máy (không đăng nhập): một người dùng, mọi nhóm đều là của người đó, không có chia sẻ.
"""
import copy
import re
import time
from typing import Any, Dict, List, Optional

from meeting import auth, db

NAME_MAX = 80
MEMBERS_MAX = 100
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_CACHE: Dict[int, Optional[Dict[str, Any]]] = {}     # gid -> nhóm (None = không có); xóa mỗi khi nhóm đổi


def _col():
    return db._get_db()["meeting_groups"]


def _email(e: Any) -> str:
    return str(e or "").strip().lower()


def clean_name(name: Any) -> str:
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    if not n:
        raise ValueError("Tên nhóm không được để trống")
    return n[:NAME_MAX]


def clean_members(members: Any, owner: Optional[str]) -> List[str]:
    """Email thành viên: chữ thường, bỏ trùng, bỏ chủ nhóm; bản web chỉ nhận email thuộc tên miền công ty."""
    out: List[str] = []
    bad: List[str] = []
    for m in members or []:
        e = _email(m)
        if not e:
            continue
        if not _EMAIL.fullmatch(e) or (auth.ENABLED and not auth.domain_ok(e)):
            bad.append(e)
        elif e != _email(owner) and e not in out:
            out.append(e)
    if bad:
        raise ValueError("Email không hợp lệ hoặc không thuộc công ty: " + ", ".join(bad[:5]))
    if len(out) > MEMBERS_MAX:
        raise ValueError(f"Một nhóm tối đa {MEMBERS_MAX} thành viên")
    return out


def create_group(name: Any, owner: Optional[str]) -> Dict[str, Any]:
    doc = {"id": db._next_id("groups"), "name": clean_name(name), "owner": _email(owner) or None, "members": [],
           "drive": {}, "recording_drive_days": None, "created_at": time.time()}
    _col().insert_one(dict(doc))
    _CACHE.pop(doc["id"], None)
    return doc


def get_group(gid: Any) -> Optional[Dict[str, Any]]:
    try:
        gid = int(gid)
    except (TypeError, ValueError):
        return None
    if gid not in _CACHE:
        _CACHE[gid] = db._strip(_col().find_one({"id": gid}))
    g = _CACHE[gid]
    return copy.deepcopy(g) if g else None


def role_of(group: Optional[Dict[str, Any]], email: Optional[str]) -> Optional[str]:
    """"owner" | "member" | None. Chạy trên máy: luôn là chủ."""
    if not group:
        return None
    if not auth.ENABLED:
        return "owner"
    e = _email(email)
    if not e:
        return None
    if _email(group.get("owner")) == e:
        return "owner"
    return "member" if e in (group.get("members") or []) else None


def group_ids_for(email: Optional[str]) -> List[int]:
    """Mã các nhóm người này có vai trò (để lọc danh sách cuộc họp)."""
    if not auth.ENABLED:
        return [g["id"] for g in _col().find({}, {"_id": 0, "id": 1})]
    e = _email(email)
    if not e:
        return []
    return [g["id"] for g in _col().find({"$or": [{"owner": e}, {"members": e}]}, {"_id": 0, "id": 1})]


def list_groups_for(email: Optional[str]) -> List[Dict[str, Any]]:
    """Các nhóm người này là chủ hoặc thành viên, kèm vai trò và số cuộc họp, nhóm tạo trước đứng trước."""
    e = _email(email)
    q: Dict[str, Any] = {} if not auth.ENABLED else {"$or": [{"owner": e}, {"members": e}]}
    rows = [db._strip(g) for g in _col().find(q).sort("created_at", 1)] if (e or not auth.ENABLED) else []
    counts: Dict[int, int] = {}
    if rows:
        for r in db._get_db()["meetings"].aggregate([
                {"$match": {"group_id": {"$in": [g["id"] for g in rows]}}},
                {"$group": {"_id": "$group_id", "n": {"$sum": 1}}}]):
            counts[r["_id"]] = r["n"]
    return [{**g, "role": role_of(g, e), "meeting_count": counts.get(g["id"], 0)} for g in rows]


def update_group(gid: Any, name: Any = None, members: Any = None,
                 recording_drive_days: Any = None) -> Dict[str, Any]:
    g = get_group(gid)
    if g is None:
        raise KeyError("Không tìm thấy nhóm")
    upd: Dict[str, Any] = {}
    if name is not None:
        upd["name"] = clean_name(name)
    if members is not None:
        upd["members"] = clean_members(members, g.get("owner"))
    if recording_drive_days is not None:
        days = int(recording_drive_days)
        if days < 0 or days > 3650:
            raise ValueError("Số ngày giữ ghi âm phải từ 1 đến 3650 (0 = theo mặc định của hệ thống)")
        upd["recording_drive_days"] = days or None
    if upd:
        _col().update_one({"id": g["id"]}, {"$set": upd})
        _CACHE.pop(g["id"], None)
    return get_group(g["id"])


def update_drive(gid: int, **fields: Any) -> None:
    """Ghi các trường của thư mục Drive của nhóm (folder_id, folder_url, permissions...)."""
    if fields:
        _col().update_one({"id": int(gid)}, {"$set": {f"drive.{k}": v for k, v in fields.items()}})
        _CACHE.pop(int(gid), None)


def delete_group(gid: Any) -> bool:
    g = get_group(gid)
    if g is None:
        return False
    db._get_db()["meetings"].update_many({"group_id": g["id"]}, {"$unset": {"group_id": ""}})
    ok = _col().delete_one({"id": g["id"]}).deleted_count > 0
    _CACHE.pop(g["id"], None)
    return ok


def set_meeting_group(mid: Any, gid: Optional[Any]) -> bool:
    col = db._get_db()["meetings"]
    if gid is None:
        res = col.update_one({"id": int(mid)}, {"$unset": {"group_id": ""}})
    else:
        res = col.update_one({"id": int(mid)}, {"$set": {"group_id": int(gid)}})
    return res.matched_count > 0


def group_meeting_ids(gid: Any) -> List[int]:
    return [m["id"] for m in db._get_db()["meetings"].find({"group_id": int(gid)}, {"_id": 0, "id": 1})]
