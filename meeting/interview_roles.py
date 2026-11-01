"""Phân quyền cho chế độ Phỏng vấn: admin -> owner -> user (người phỏng vấn).

- admin: quản trị viên chung (AUTH_ADMIN_EMAILS) hoặc trong INTERVIEW_ADMIN_EMAILS (mặc định phuc.bh@urbox.vn).
  Admin gán ai là owner.
- owner: tạo vị trí, tải JD, đặt tiêu chí, tải CV, chấm điểm (người được admin gán, lưu user_settings.interview_role).
- user: người đăng nhập mặc định = người phỏng vấn. Xem vị trí được thêm vào (members) + buổi được giao + gợi ý
  câu hỏi, KHÔNG tạo/sửa vị trí hay tiêu chí.

Bản chạy trên máy (AUTH_REQUIRED=0): không có đăng nhập -> coi như admin (một người dùng).
"""
import os
from typing import Any, Dict, List, Optional

from meeting import auth, user_prefs

INTERVIEW_ADMINS = {e.strip().lower() for e in (os.getenv("INTERVIEW_ADMIN_EMAILS") or "phuc.bh@urbox.vn").split(",") if e.strip()}
ROLES = ("admin", "owner", "user")


def _email(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("email") or "").strip().lower()
    return str(user or "").strip().lower()


def is_admin(user: Any) -> bool:
    if not auth.ENABLED:                 # bản chạy trên máy: một người dùng, toàn quyền
        return True
    return auth.is_admin(user if isinstance(user, dict) else {"email": _email(user)}) or _email(user) in INTERVIEW_ADMINS


def role_of(user: Any) -> str:
    if is_admin(user):
        return "admin"
    email = _email(user)
    if email and (user_prefs.get(email).get("interview_role") == "owner"):
        return "owner"
    return "user"


def is_owner(user: Any) -> bool:
    """Được cấu hình (tạo vị trí, sửa tiêu chí, chấm): admin hoặc owner."""
    return role_of(user) in ("admin", "owner")


def set_role(target_email: str, role: str) -> Dict[str, Any]:
    """Admin gán vai cho một người: 'owner' hoặc 'user' (gỡ owner). Không gán được 'admin' (admin theo email cấu hình)."""
    email = _email(target_email)
    if not email:
        raise ValueError("Thiếu email")
    if role not in ("owner", "user"):
        raise ValueError("Vai trò chỉ nhận 'owner' hoặc 'user'")
    user_prefs.update(email, interview_role=("owner" if role == "owner" else ""))
    return {"email": email, "role": role_of(email)}


def people() -> List[Dict[str, Any]]:
    """Danh sách người dùng đã có cài đặt + vai trò, để admin gán owner."""
    from meeting import db
    out = []
    for d in db._get_db()["user_settings"].find({}, {"_id": 0, "email": 1, "name": 1, "interview_role": 1}):
        email = str(d.get("email") or "").lower()
        if not email:
            continue
        out.append({"email": email, "name": d.get("name") or email.split("@")[0], "role": role_of(email)})
    out.sort(key=lambda x: (0 if x["role"] == "admin" else 1 if x["role"] == "owner" else 2, x["email"]))
    return out
