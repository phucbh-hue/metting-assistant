"""Cài đặt riêng của từng người (bản web), lưu trong Atlas (`user_settings`, mỗi email một bản ghi).

- llm_provider: gói AI riêng dùng cho các cuộc họp người đó tạo ("claude-cli" | "codex-cli" | None = API công ty).
- calendar_autojoin: bot tự vào các cuộc họp có link Meet trên lịch của người đó.
- skip_events: các buổi trên lịch người đó bấm "Bỏ qua buổi này".
- series_groups: chuỗi cuộc họp lặp lại -> nhóm (buổi sau tự vào đúng nhóm).
"""
from typing import Any, Dict

from meeting import db


def _col():
    return db._get_db()["user_settings"]


def _key(email: str) -> str:
    return (email or "").strip().lower()


def get(email: str) -> Dict[str, Any]:
    if not _key(email):
        return {}
    return db._strip(_col().find_one({"email": _key(email)})) or {}


def update(email: str, **fields: Any) -> Dict[str, Any]:
    if not _key(email):
        raise ValueError("Thiếu email người dùng")
    if fields:
        _col().update_one({"email": _key(email)}, {"$set": fields}, upsert=True)
    return get(email)


def users_with(**match: Any) -> list:
    """Email những người có cài đặt khớp (ví dụ calendar_autojoin=True)."""
    return [d["email"] for d in _col().find(match, {"_id": 0, "email": 1})]
