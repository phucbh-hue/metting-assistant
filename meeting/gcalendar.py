"""Lịch Google của từng người (bản web, quyền calendar.events.readonly): các buổi sắp tới có link Google Meet để bot
vào họp thay người dùng và lập cuộc họp (tiêu đề, người tham dự, chương trình lấy từ lịch).

Chỉ đọc lịch chính (primary) của người đã kết nối; không sửa hay tạo sự kiện. Kết quả nhớ 2 phút cho mỗi người.
"""
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from meeting import google_oauth

log = logging.getLogger("meeting.gcalendar")

EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
CACHE_S = 120
PAST_S = 15 * 60                 # buổi đã bắt đầu tối đa 15 phút trước (bot vẫn vào được buổi đang diễn ra)
AHEAD_S = 7 * 24 * 3600
FIELDS = ("items(id,status,summary,description,start,end,hangoutLink,conferenceData(entryPoints(entryPointType,uri)),"
          "attendees(email,displayName,responseStatus,self,organizer,resource),recurringEventId,htmlLink,"
          "organizer(email,displayName,self),eventType),nextPageToken")
_MEET = re.compile(r"^https://meet\.google\.com/[a-z]{3,4}-[a-z]{4}-[a-z]{3,4}$")
_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


class CalendarError(Exception):
    """Lỗi đọc lịch (viết cho người dùng đọc)."""


def meet_url(ev: Dict[str, Any]) -> str:
    """Link Google Meet của sự kiện (chỉ nhận đúng dạng https://meet.google.com/abc-defg-hij)."""
    cands = [ev.get("hangoutLink") or ""]
    for ep in ((ev.get("conferenceData") or {}).get("entryPoints") or []):
        if ep.get("entryPointType") == "video":
            cands.append(ep.get("uri") or "")
    for u in cands:
        u = str(u).strip().split("?")[0].rstrip("/").lower()
        if _MEET.match(u):
            return u
    return ""


def _ts(part: Dict[str, Any]) -> Tuple[Optional[float], bool]:
    """(giờ dạng epoch, cả ngày?) của start / end trong sự kiện."""
    part = part or {}
    if part.get("dateTime"):
        try:
            d = datetime.fromisoformat(str(part["dateTime"]).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp(), False
        except ValueError:
            return None, False
    if part.get("date"):
        try:
            return datetime.fromisoformat(str(part["date"])).replace(tzinfo=timezone.utc).timestamp(), True
        except ValueError:
            return None, True
    return None, False


def plain_text(desc: str, limit: int = 2000) -> str:
    """Mô tả sự kiện (có thể là HTML) thành chữ thường, dùng làm chương trình họp."""
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", desc or "")
    t = re.sub(r"(?i)<li[^>]*>", "- ", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:limit]


def normalize(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    start, all_day = _ts(ev.get("start"))
    end, _ = _ts(ev.get("end"))
    if start is None or ev.get("status") == "cancelled":
        return None
    people, me = [], {}
    for a in ev.get("attendees") or []:
        if a.get("resource"):                     # phòng họp, thiết bị: không phải người
            continue
        p = {"name": str(a.get("displayName") or "").strip(), "email": str(a.get("email") or "").strip().lower(),
             "response": a.get("responseStatus") or "needsAction", "organizer": bool(a.get("organizer"))}
        people.append(p)
        if a.get("self"):
            me = p
    org = ev.get("organizer") or {}
    return {
        "id": str(ev.get("id") or ""),
        "series": str(ev.get("recurringEventId") or ""),
        "title": str(ev.get("summary") or "").strip() or "(Không có tiêu đề)",
        "start": start, "end": end or start + 3600, "all_day": all_day,
        "meet_url": meet_url(ev),
        "attendees": people,
        "declined": me.get("response") == "declined",
        "organizer": str(org.get("email") or "").lower(),
        "is_organizer": bool(org.get("self")),
        "description": plain_text(ev.get("description") or ""),
        "link": str(ev.get("htmlLink") or ""),
        "kind": str(ev.get("eventType") or "default"),
    }


async def _get(email: str, params: Dict[str, str]) -> Dict[str, Any]:
    for attempt in (1, 2):
        token = await google_oauth.access_token(email)
        async with httpx.AsyncClient(timeout=30, transport=google_oauth._transport) as c:
            try:
                r = await c.get(EVENTS, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise CalendarError(f"Không kết nối được Google Calendar: {e}")
        if r.status_code == 401 and attempt == 1:          # access token vừa hết hạn: làm mới rồi thử lại
            google_oauth.forget_access(email)
            continue
        break
    if r.status_code < 400:
        return r.json()
    try:
        err = r.json().get("error") or {}
    except ValueError:
        err = {}
    reasons = " ".join(str(e.get("reason") or "") for e in err.get("errors") or []) + " " + str(err.get("status") or "")
    msg = str(err.get("message") or "")
    if r.status_code == 403 and re.search(r"insufficient|ACCESS_TOKEN_SCOPE", reasons + msg, re.I):
        raise google_oauth.NeedReconnect("Chưa cho phép quyền xem Lịch: hãy bấm Kết nối Google lại và đánh dấu ô Lịch")
    if r.status_code == 403 and re.search(r"accessNotConfigured|SERVICE_DISABLED|has not been used", reasons + msg, re.I):
        raise CalendarError("Google Calendar API chưa được bật trên Google Cloud của ứng dụng (báo quản trị viên)")
    raise CalendarError(f"Google Calendar báo lỗi {r.status_code}: {msg[:160]}".rstrip(": "))


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def upcoming(email: str, refresh: bool = False, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Các buổi từ 15 phút trước tới 7 ngày tới trên lịch chính, theo giờ bắt đầu (đã tách từng buổi của chuỗi lặp lại).
    Chưa kết nối / thiếu quyền Lịch: google_oauth.NeedReconnect. Lỗi khác: CalendarError."""
    email = (email or "").lower()
    hit = _CACHE.get(email)
    if hit and not refresh and time.time() - hit[0] < CACHE_S:
        return hit[1]
    st = google_oauth.status(email)
    if st.get("connected") and not st.get("calendar"):
        raise google_oauth.NeedReconnect("Chưa cho phép quyền xem Lịch: hãy bấm Kết nối Google lại và đánh dấu ô Lịch")
    now = now or time.time()
    params = {"timeMin": _iso(now - PAST_S), "timeMax": _iso(now + AHEAD_S), "singleEvents": "true",
              "orderBy": "startTime", "maxResults": "100", "fields": FIELDS}
    out: List[Dict[str, Any]] = []
    for _ in range(5):                            # tối đa 500 buổi trong 7 ngày
        body = await _get(email, params)
        for ev in body.get("items") or []:
            n = normalize(ev)
            if n is not None and n["kind"] not in ("workingLocation", "outOfOffice", "focusTime"):
                out.append(n)
        if not body.get("nextPageToken"):
            break
        params = {**params, "pageToken": body["nextPageToken"]}
    _CACHE[email] = (time.time(), out)
    return out


def cached(email: str) -> List[Dict[str, Any]]:
    """Kết quả đã đọc gần nhất (không gọi Google), dùng để tra một buổi theo mã."""
    hit = _CACHE.get((email or "").lower())
    return hit[1] if hit else []


def forget(email: str) -> None:
    _CACHE.pop((email or "").lower(), None)
