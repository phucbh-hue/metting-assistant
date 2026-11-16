"""Link phòng họp online mà bot Recall vào được: Google Meet, Microsoft Teams, Zoom (bản 3.30.0, 16/11/2026).

Trước đây app chỉ nhận link Google Meet. Recall.ai vào được cả Teams và Zoom bằng cùng một API (`meeting_url`), nên phần
còn lại chỉ là nhận ra link trong lịch (hangoutLink, conferenceData, location, description) và trong ô "Mời bot".
- Google Meet: bỏ tham số truy vấn, chỉ giữ mã abc-defg-hij.
- Teams, Zoom: PHẢI giữ nguyên tham số (context, pwd...) vì thiếu là không vào được.
`code(url)` là mã ngắn, có tiền tố nền tảng, để ghép khóa "một bot cho một buổi" (`meet_bots.key_of`).
"""
import re
from typing import Optional

_MEET = re.compile(r"^(?:https?://)?meet\.google\.com/([a-z]{3,4}-[a-z]{4,5}-[a-z]{3,4})(?:[/?#].*)?$", re.I)
_MEET_CODE = re.compile(r"^[a-z]{3,4}-[a-z]{4,5}-[a-z]{3,4}$", re.I)
_TEAMS = re.compile(r"^(?:https?://)?(teams\.microsoft\.com/(?:l/meetup-join|meet)/[^\s<>\"']+"
                    r"|teams\.live\.com/meet/[^\s<>\"']+)$", re.I)
_ZOOM = re.compile(r"^(?:https?://)?((?:[a-z0-9-]+\.)?zoom\.us/(?:j|s|w|wc|my)/[^\s<>\"']+)$", re.I)
_ZOOM_ID = re.compile(r"/(?:j|s|w|wc)/(\d{9,12})")
_ANY_URL = re.compile(r"https?://[^\s<>\"']+")

LABEL = {"meet": "Google Meet", "teams": "Microsoft Teams", "zoom": "Zoom"}


def platform(url: str) -> str:
    """'meet' | 'teams' | 'zoom' | '' theo link đã chuẩn hóa hoặc chưa."""
    u = (url or "").strip()
    if _MEET.match(u):
        return "meet"
    if _TEAMS.match(u):
        return "teams"
    if _ZOOM.match(u):
        return "zoom"
    return ""


def normalize(text: str) -> str:
    """Link (hoặc riêng mã Meet abc-defg-hij) -> link chuẩn bot vào được. Không nhận ra: ''."""
    t = (text or "").strip().rstrip(".,;)")
    m = _MEET.match(t)
    if m:
        return f"https://meet.google.com/{m.group(1).lower()}"
    if _MEET_CODE.match(t):
        return f"https://meet.google.com/{t.lower()}"
    m = _TEAMS.match(t) or _ZOOM.match(t)
    if m:
        rest = m.group(1)
        host, _, path = rest.partition("/")
        return f"https://{host.lower()}/{path}"
    return ""


def find_in_text(text: Optional[str]) -> str:
    """Link phòng họp đầu tiên trong một đoạn chữ (location / description của sự kiện lịch, lời mời dán vào)."""
    for u in _ANY_URL.findall(text or ""):
        n = normalize(u)
        if n:
            return n
    return ""


def code(url: str) -> str:
    """Mã ngắn kèm nền tảng: 'abc-defg-hij' (Meet, giữ như cũ để không đổi khóa các bản ghi cũ), 'zoom:123456789',
    'teams:<đoạn cuối đường dẫn>'."""
    p = platform(url)
    u = (url or "").strip()
    if p == "meet":
        return u.rstrip("/").rsplit("/", 1)[-1].split("?")[0].lower()
    if p == "zoom":
        m = _ZOOM_ID.search(u)
        return "zoom:" + (m.group(1) if m else u.split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()[:40])
    if p == "teams":
        tail = u.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        return "teams:" + tail.lower()[:48]
    return u.lower()[:60]


def describe(url: str) -> str:
    """Tên nền tảng để hiện cho người dùng ('Google Meet', 'Zoom'...)."""
    return LABEL.get(platform(url), "phòng họp online")
