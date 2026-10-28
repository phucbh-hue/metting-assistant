"""Recall.ai: bot vào Google Meet thay người dùng và gửi âm thanh của từng người trong Meet về server (bản web).

- Xác thực: `Authorization: Token <RECALLAI_API_KEY>`. Mỗi vùng (us-west-2, us-east-1, eu-central-1, ap-northeast-1) một
  bộ key riêng: đặt RECALL_REGION, hoặc để trống thì tự dò bằng một lời gọi chỉ đọc rồi nhớ trong DB.
- Âm thanh: `recording_config.audio_separate_raw` + realtime endpoint WebSocket, sự kiện `audio_separate_raw.data`
  (PCM 16 bit, 16 kHz, mono, base64; kèm participant id / tên / email). Google Meet: tối đa 16 người nói cùng lúc.
- Bot hẹn trước (`join_at`) phải tạo trước ít nhất 10 phút; muộn hơn là bot vào ngay (ad-hoc, có thể báo 507 khi hết
  bot dự phòng: thử lại lượt sau).
- Không giữ bản ghi trên Recall lâu: retention 1 giờ, xóa media ngay khi bot xong (Nghị định 13: chỉ giữ cái cần).
- Bot vào Meet là khách: người trong phòng phải bấm Cho vào. Cuộc họp chỉ cho người trong tổ chức thì bot không vào được
  (`google_meet_organisation_restricted`).
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from meeting import db

log = logging.getLogger("meeting.recall")

REGIONS = ("us-west-2", "us-east-1", "eu-central-1", "ap-northeast-1")
SCHEDULE_LEAD_S = 10 * 60
_transport: Optional[httpx.AsyncBaseTransport] = None        # test thay bằng httpx.MockTransport
_region: Dict[str, str] = {}


class RecallError(Exception):
    """Recall từ chối (viết cho người dùng đọc)."""


class RecallBusy(RecallError):
    """Lỗi tạm thời (mạng, 429, 5xx, hết bot dự phòng 507): thử lại lượt sau."""


class RecallAccount(RecallError):
    """Lỗi của tài khoản Recall (hết credit, key sai): mọi bot đều không vào được cho tới khi quản trị viên xử lý."""


NO_CREDIT = ("Tài khoản Recall.ai của công ty đã hết credit nên bot không vào được Google Meet. Quản trị viên nạp thêm "
             "credit trong trang quản trị Recall.ai (mục Billing), rồi bấm Mời bot vào lại.")
_account_problem: Dict[str, Any] = {}          # lỗi tài khoản gần nhất: hiện cảnh báo trên trang chủ cho mọi người


def account_problem() -> Optional[Dict[str, Any]]:
    """{"text", "at"} khi lần gọi Recall gần nhất báo lỗi tài khoản (hết credit, key sai); None khi bình thường."""
    return dict(_account_problem) if _account_problem else None


def api_key() -> str:
    return (os.getenv("RECALLAI_API_KEY") or os.getenv("RECALL_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(api_key())


def bot_name() -> str:
    return (os.getenv("RECALL_BOT_NAME") or "Trợ lý họp UrBox").strip()[:100]


async def _call(region: str, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30, transport=_transport) as c:
        return await c.request(method, f"https://{region}.recall.ai/api/v1{path}", json=body,
                               headers={"Authorization": f"Token {api_key()}", "Accept": "application/json"})


async def region() -> str:
    """Vùng của API key: RECALL_REGION, vùng đã dò lần trước (DB), hoặc dò bằng lời gọi chỉ đọc ở từng vùng."""
    env = (os.getenv("RECALL_REGION") or "").strip().lower()
    if env in REGIONS:
        return env
    if _region.get("key") == api_key() and _region.get("v"):
        return _region["v"]
    saved = str(db.get_setting("recall_region", "") or "")
    if saved in REGIONS and db.get_setting("recall_region_key", "") == api_key()[-6:]:
        _region.update(key=api_key(), v=saved)
        return saved
    for r in REGIONS:
        try:
            resp = await _call(r, "GET", "/bot/")
        except httpx.HTTPError as e:
            log.info("meeting.recall: dò vùng %s lỗi mạng: %s", r, e)
            continue
        if resp.status_code < 400:
            _region.update(key=api_key(), v=r)
            db.set_setting("recall_region", r)
            db.set_setting("recall_region_key", api_key()[-6:])
            log.info("meeting.recall: API key thuộc vùng %s", r)
            return r
    raise RecallError("API key Recall không dùng được ở vùng nào: kiểm tra RECALLAI_API_KEY (hoặc đặt RECALL_REGION)")


async def _req(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    reg = await region()
    try:
        r = await _call(reg, method, path, body)
    except httpx.HTTPError as e:
        raise RecallBusy(f"Không kết nối được Recall: {e}")
    if r.status_code in (429, 502, 503, 504, 507):
        raise RecallBusy(f"Recall đang bận ({r.status_code}), thử lại sau")
    if r.status_code == 402 or (r.status_code >= 400 and "insufficient_credit" in r.text):
        _account_problem.update(text=NO_CREDIT, at=time.time())
        raise RecallAccount(NO_CREDIT)
    if r.status_code in (401, 403):
        _account_problem.update(text="API key Recall sai hoặc đã bị thu hồi (RECALLAI_API_KEY): báo quản trị viên",
                                at=time.time())
        raise RecallAccount(_account_problem["text"])
    if r.status_code >= 400:
        raise RecallError(f"Recall từ chối ({r.status_code}): {r.text[:300]}")
    if method == "POST" and path == "/bot/":
        _account_problem.clear()                 # tạo được bot: tài khoản đã ổn trở lại
    if not r.content:
        return {}
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"items": data}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def create_bot(meeting_url: str, ws_url: str, join_at: Optional[float] = None, owner_name: str = "",
                     metadata: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Tạo bot: hẹn giờ vào nếu còn ít nhất 10 phút, không thì vào ngay. Trả về bot (có id, status_changes)."""
    now = now or time.time()
    # KHÔNG gửi tin chào vào chat Meet: người dùng thấy ngại khi bot tự nhắn (đồng ý ghi âm đã lấy từ trước). owner_name
    # vẫn nhận để hiện "bot do X mời" trên giao diện.
    body: Dict[str, Any] = {
        "meeting_url": meeting_url,
        "bot_name": bot_name(),
        "recording_config": {
            "audio_separate_raw": {},
            "realtime_endpoints": [{"type": "websocket", "url": ws_url, "events": ["audio_separate_raw.data"]}],
            "retention": {"type": "timed", "hours": 1},
        },
        # mọi người rời đi 60 giây (không phải 2 giây mặc định) mới rời: chủ phòng rớt mạng một lúc không làm hỏng buổi họp
        "automatic_leave": {"everyone_left_timeout": {"timeout": 60}},
        "metadata": {k: str(v)[:500] for k, v in (metadata or {}).items()},
    }
    if join_at and join_at - now >= SCHEDULE_LEAD_S:
        body["join_at"] = _iso(join_at)
    return await _req("POST", "/bot/", body)


async def get_bot(bot_id: str) -> Dict[str, Any]:
    return await _req("GET", f"/bot/{bot_id}/")


async def delete_bot(bot_id: str) -> bool:
    """Xóa bot hẹn trước chưa vào họp. Bot đã được điều đi (sắp vào / đang ở trong) thì Recall từ chối: trả False."""
    try:
        await _req("DELETE", f"/bot/{bot_id}/")
        return True
    except RecallBusy:
        raise
    except RecallError as e:
        log.info("meeting.recall: không xóa được bot %s (%s), sẽ cho rời cuộc họp", bot_id, e)
        return False


async def leave_call(bot_id: str) -> None:
    await _req("POST", f"/bot/{bot_id}/leave_call/")


async def delete_media(bot_id: str) -> None:
    await _req("POST", f"/bot/{bot_id}/delete_media/")


async def remove(bot_id: str) -> None:
    """Bot không cần nữa: chưa vào thì xóa hẳn, đã vào thì cho rời cuộc họp."""
    if not await delete_bot(bot_id):
        try:
            await leave_call(bot_id)
        except RecallError as e:
            log.info("meeting.recall: bot %s không rời được (%s)", bot_id, e)


# ------------------------------------------------------------- trạng thái ---
STATUS_TEXT = {
    "ready": "Bot đã sẵn sàng, chờ tới giờ vào",
    "joining_call": "Bot đang xin vào cuộc họp",
    "in_waiting_room": "Bot đang ở phòng chờ: người trong cuộc họp bấm Cho vào (Admit)",
    "in_call_not_recording": "Bot đã vào cuộc họp",
    "recording_permission_allowed": "Bot đã vào cuộc họp",
    "recording_permission_denied": "Chủ phòng không cho bot ghi lại cuộc họp",
    "in_call_recording": "Bot đang nghe và chép lời",
    "call_ended": "Bot đã rời cuộc họp",
    "done": "Bot đã xong",
    "fatal": "Bot không vào được cuộc họp",
}
SUB_TEXT = {
    "bot_kicked_from_waiting_room": "người trong cuộc họp đã từ chối cho bot vào",
    "timeout_exceeded_waiting_room": "chờ ở phòng chờ quá lâu, chưa ai cho vào",
    "call_ended_by_platform_waiting_room_timeout": "Google Meet chỉ cho chờ 10 phút mà chưa ai cho bot vào",
    "timeout_exceeded_noone_joined": "không có ai vào cuộc họp",
    "timeout_exceeded_everyone_left": "mọi người đã rời cuộc họp",
    "bot_kicked_from_call": "bot bị mời ra khỏi cuộc họp",
    "meeting_not_found": "không tìm thấy cuộc họp theo link Meet",
    "meeting_requires_sign_in": "cuộc họp chỉ cho tài khoản đã đăng nhập vào",
    "google_meet_organisation_restricted": "cuộc họp chỉ cho người trong tổ chức vào, bot là khách nên bị chặn",
    "google_meet_knocking_disabled": "cuộc họp không cho khách xin vào",
    "google_meet_bot_blocked": "Google Meet chặn bot",
    "meeting_not_started": "cuộc họp chưa bắt đầu",
    "meeting_link_expired": "link cuộc họp đã hết hạn",
    "meeting_locked": "cuộc họp đang bị khóa",
}
PHASE = {"ready": "scheduled", "joining_call": "joining", "in_waiting_room": "waiting",
         "in_call_not_recording": "in_call", "recording_permission_allowed": "in_call", "in_call_recording": "in_call",
         "recording_permission_denied": "in_call", "call_ended": "ended", "done": "ended", "fatal": "failed"}


def last_status(bot: Dict[str, Any]) -> Dict[str, Any]:
    """Trạng thái cuối của bot + lý do. Trạng thái cuối thường là "done" (không có sub_code): lý do thật (chờ phòng chờ
    quá lâu, bị từ chối, Meet chặn khách...) nằm ở bước call_ended / fatal ngay trước, nên lấy sub_code gần nhất."""
    changes = [c for c in (bot.get("status_changes") or []) if isinstance(c, dict)]
    last = changes[-1] if changes else {}
    codes = [str(c.get("code") or "").removeprefix("bot.") for c in changes]
    code = codes[-1] if codes else ""
    reason = next((str(c.get("sub_code")) for c in reversed(changes) if c.get("sub_code")), "")
    return {"code": code, "sub_code": str(last.get("sub_code") or "") or (reason if code in ("done", "call_ended") else ""),
            "reason": reason, "message": str(last.get("message") or ""), "at": str(last.get("created_at") or ""),
            "joined": any(c in ("in_call_not_recording", "in_call_recording") for c in codes),
            "waited": "in_waiting_room" in codes}


def not_joined_text(st: Dict[str, Any]) -> str:
    """Bot kết thúc mà chưa từng vào cuộc họp: câu giải thích cho người dùng."""
    why = SUB_TEXT.get(st.get("reason") or "", (st.get("reason") or "").replace("_", " "))
    if not why:
        why = "bot đã chờ ở phòng chờ nhưng chưa ai bấm Cho vào" if st.get("waited") else "chưa vào được cuộc họp"
    return f"Bot không vào được cuộc họp ({why})"


def phase(code: str) -> str:
    return PHASE.get(code, "joining" if code else "scheduled")


def status_text(code: str, sub_code: str = "") -> str:
    text = STATUS_TEXT.get(code, f"Trạng thái bot: {code}" if code else "Bot đã được hẹn")
    sub = SUB_TEXT.get(sub_code, sub_code.replace("_", " ") if sub_code else "")
    return f"{text} ({sub})" if sub and code in ("call_ended", "done", "fatal", "recording_permission_denied") else text
