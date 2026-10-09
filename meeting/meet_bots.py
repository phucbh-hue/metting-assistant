"""Bot Recall vào Google Meet theo lịch của từng người (bản web, bản 3.18).

Người dùng bật "Tự cho bot vào các cuộc họp có link Meet trên lịch của tôi" thì cứ mỗi phút:
- Hẹn bot cho các buổi bắt đầu trong 30 phút tới (hoặc đang diễn ra) có link Meet, chưa từ chối, chưa bấm Bỏ qua.
  Một link Meet ở một giờ bắt đầu chỉ một bot, dù nhiều người cùng bật (người hẹn trước là người tạo cuộc họp).
- Buổi bị hủy, đổi giờ, bỏ qua, tắt công tắc mà bot chưa vào: xóa bot (người khác cùng buổi vẫn bật thì chuyển cho họ).
- Quanh giờ vào (2 phút trước tới 20 phút sau) đọc trạng thái bot mỗi phút để hiện "đang chờ cho vào", "không vào được".
- Bot rời Meet (kết nối âm thanh đóng quá 2 phút và Recall báo xong): kết thúc cuộc họp, lập biên bản, lưu Drive.

Âm thanh: Recall nối vào /ws/recall/<rid>/?token=<mã ngẫu nhiên của từng buổi>. Gói tin đầu tiên tạo cuộc họp (tiêu đề,
người tham dự, chương trình lấy từ lịch; nhóm theo chuỗi lặp lại), mỗi người trong Meet một luồng nhận dạng riêng.
"""
import asyncio
import base64
import binascii
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from meeting import auth, db, gcalendar, google_oauth, groups, recall, user_prefs

log = logging.getLogger("meeting.meet_bots")

TICK_S = 60
LOOKAHEAD_S = 30 * 60
POLL_BEFORE_S = 2 * 60
POLL_AFTER_S = 20 * 60
POLL_EVERY_S = 55
END_GRACE_S = 120
ACTIVE = ("pending", "scheduled", "joining", "waiting", "in_call")
NOT_JOINED = ("pending", "scheduled")
SKIP_KEEP = 300
_locks: Dict[str, asyncio.Lock] = {}
_tick_lock = asyncio.Lock()                  # vòng lặp nền và nút bấm (bật công tắc, bỏ qua) không chạy chồng lên nhau
_task: Optional["asyncio.Task"] = None


def _col():
    return db._get_db()["meet_bots"]


def public_base() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")


def enabled() -> bool:
    """Bot chỉ chạy ở bản web có key Recall và địa chỉ công khai (Recall phải nối được WebSocket vào server)."""
    return auth.ENABLED and recall.enabled() and public_base().startswith(("https://", "http://"))


def ws_url(rec: Dict[str, Any]) -> str:
    base = re.sub(r"^http", "ws", public_base())
    return f"{base}/ws/recall/{rec['rid']}/?token={rec['token']}"          # Recall: phải có "/" trước dấu "?"


def meet_code(url: str) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1].lower()


def key_of(meet_url: str, start: float) -> str:
    return f"{meet_code(meet_url)}|{int(start // 60)}"


def strip(rec: Optional[Dict[str, Any]], with_token: bool = False) -> Optional[Dict[str, Any]]:
    if rec is None:
        return None
    out = {k: v for k, v in rec.items() if k != "_id" and (with_token or k != "token")}
    return out


def public(rec: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Trạng thái bot để hiện trên giao diện (không có token)."""
    if not rec:
        return None
    return {k: rec.get(k) for k in ("rid", "status", "status_msg", "code", "sub_code", "meeting_id", "owner", "start",
                                    "ws_open", "updated_at")}


def find_for_event(email: str, ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Bot của một buổi trên lịch người này (người hẹn hoặc cùng tham dự)."""
    if not ev.get("meet_url"):
        return None
    rec = _col().find_one({"key": key_of(ev["meet_url"], ev["start"])}, sort=[("created_at", -1)])
    return strip(rec)


def for_meeting(mid: int) -> Optional[Dict[str, Any]]:
    return strip(_col().find_one({"meeting_id": int(mid)}, sort=[("created_at", -1)]))


def _update(rec: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    fields["updated_at"] = time.time()
    _col().update_one({"rid": rec["rid"]}, {"$set": fields})
    rec.update(fields)
    return rec


async def _notify(rec: Dict[str, Any]) -> None:
    """Báo trạng thái bot cho phòng họp đang mở."""
    if not rec.get("meeting_id"):
        return
    from meeting import live
    s = live.SESSIONS.get(int(rec["meeting_id"]))
    if s is not None:
        await s.set_meet_bot(public(rec))


# --------------------------------------------------------------- hẹn bot ---
def eligible(ev: Dict[str, Any], skip: List[str], now: float) -> bool:
    return bool(ev.get("meet_url")) and not ev.get("all_day") and not ev.get("declined") \
        and ev.get("id") not in skip and ev["start"] - now <= LOOKAHEAD_S and ev["end"] > now


def _attendee_names(ev: Dict[str, Any]) -> List[str]:
    out = []
    for a in ev.get("attendees") or []:
        n = a.get("name") or (a.get("email") or "").split("@")[0]
        if n and n not in out:
            out.append(n)
    return out[:50]


async def _create(rec: Dict[str, Any], owner_name: str, now: float) -> Dict[str, Any]:
    try:
        bot = await recall.create_bot(rec["meet_url"], ws_url(rec), join_at=rec["start"], owner_name=owner_name,
                                      metadata={"rid": rec["rid"], "owner": rec["owner"]}, now=now)
    except recall.RecallBusy as e:
        return _update(rec, status="pending", status_msg=f"Chờ hẹn bot: {e}")
    except recall.RecallError as e:
        log.warning("meeting.meet_bots: hẹn bot cho %s lỗi: %s", rec["meet_url"], e)
        return _update(rec, status="failed", status_msg=str(e)[:300])
    st = recall.last_status(bot)
    status = recall.phase(st["code"]) if st["code"] else "scheduled"
    log.info("meeting.meet_bots: đã hẹn bot %s cho '%s' (%s)", bot.get("id"), rec["title"], rec["owner"])
    return _update(rec, bot_id=str(bot.get("id") or ""), status=status, code=st["code"],
                   status_msg=recall.status_text(st["code"]) if st["code"] else
                   ("Bot sẽ vào lúc bắt đầu" if rec["start"] - now >= recall.SCHEDULE_LEAD_S else "Bot đang vào"))


async def ensure_bot(email: str, ev: Dict[str, Any], owner_name: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """Một bot cho mỗi (link Meet, giờ bắt đầu). Đã có thì thêm người này vào danh sách cùng tham dự."""
    now = now or time.time()
    key = key_of(ev["meet_url"], ev["start"])
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        rec = _col().find_one({"key": key, "status": {"$ne": "cancelled"}}, sort=[("created_at", -1)])
        if rec is not None:
            if rec["owner"] != email and email not in (rec.get("also") or []):
                _col().update_one({"rid": rec["rid"]}, {"$addToSet": {"also": email}})
            if rec.get("status") == "pending" and not rec.get("bot_id"):
                return strip(await _create(strip(rec, True), owner_name, now))
            return strip(rec)
        prefs = user_prefs.get(email)
        rec = {"rid": secrets.token_hex(8), "token": secrets.token_urlsafe(24), "key": key, "owner": email, "also": [],
               "owner_name": owner_name, "event_id": ev["id"], "series": ev.get("series") or "",
               "meet_url": ev["meet_url"], "title": ev["title"], "start": ev["start"], "end": ev["end"],
               "attendees": ev.get("attendees") or [], "description": ev.get("description") or "",
               "group_id": (prefs.get("series_groups") or {}).get(ev.get("series") or ev["id"]),
               "bot_id": "", "status": "pending", "status_msg": "Đang hẹn bot", "code": "", "sub_code": "",
               "meeting_id": None, "ws_open": False, "ws_closed_at": None, "polled_at": 0.0,
               "created_at": now, "updated_at": now}
        _col().insert_one(dict(rec))
        return strip(await _create(rec, owner_name, now))


async def retry(email: str, ev: Dict[str, Any], owner_name: str = "") -> Dict[str, Any]:
    """Mời bot vào lại (lần trước không ai cho vào, bot lỗi): bản ghi cũ thôi, hẹn bot mới vào ngay."""
    old = _col().find_one({"key": key_of(ev["meet_url"], ev["start"]), "status": {"$in": ["failed", "ended"]}},
                          sort=[("created_at", -1)])
    keep = None
    if old is not None:
        _col().update_one({"rid": old["rid"]}, {"$set": {"status": "cancelled", "status_msg": "Đã mời bot vào lại",
                                                         "updated_at": time.time()}})
        m = db.get_meeting(old["meeting_id"]) if old.get("meeting_id") else None
        if m and m.get("status") != "ended":
            keep = int(old["meeting_id"])              # cuộc họp còn mở: bot mới chép tiếp vào đúng cuộc họp đó
    rec = await ensure_bot(email, ev, owner_name)
    if keep and not rec.get("meeting_id"):
        _col().update_one({"rid": rec["rid"]}, {"$set": {"meeting_id": keep}})
        rec["meeting_id"] = keep
    return rec


def _wants(email: str, rec: Dict[str, Any]) -> bool:
    p = user_prefs.get(email)
    return bool(p.get("calendar_autojoin")) and rec["event_id"] not in (p.get("skip_events") or [])


async def cancel(rec: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Bỏ bot của một buổi: người cùng tham dự khác vẫn bật công tắc và không bỏ qua thì chuyển bot cho họ."""
    for other in rec.get("also") or []:
        p = user_prefs.get(other)
        if _wants(other, rec):
            also = [x for x in rec.get("also") or [] if x != other]
            log.info("meeting.meet_bots: chuyển bot '%s' từ %s sang %s (%s)", rec["title"], rec["owner"], other, reason)
            return _update(rec, owner=other, also=also, owner_name="",
                           group_id=(p.get("series_groups") or {}).get(rec.get("series") or rec["event_id"]))
    if rec.get("bot_id"):
        try:
            await recall.remove(rec["bot_id"])
        except recall.RecallBusy as e:
            log.info("meeting.meet_bots: chưa hủy được bot %s (%s), thử lại lượt sau", rec["bot_id"], e)
            return rec
        except recall.RecallError as e:
            log.warning("meeting.meet_bots: hủy bot %s lỗi: %s", rec["bot_id"], e)
    log.info("meeting.meet_bots: hủy bot '%s' (%s)", rec["title"], reason)
    return _update(rec, status="cancelled", status_msg=reason)


# --------------------------------------------------------------- lịch chạy ---
async def tick(now: Optional[float] = None) -> None:
    now = now or time.time()
    fetched: Dict[str, List[Dict[str, Any]]] = {}
    for email in await asyncio.to_thread(user_prefs.users_with, calendar_autojoin=True):
        prefs = await asyncio.to_thread(user_prefs.get, email)
        try:
            events = await gcalendar.upcoming(email, now=now)
            if prefs.get("calendar_error"):
                await asyncio.to_thread(user_prefs.update, email, calendar_error="")
        except (google_oauth.OAuthError, gcalendar.CalendarError) as e:
            if prefs.get("calendar_error") != str(e):
                await asyncio.to_thread(user_prefs.update, email, calendar_error=str(e))
            log.info("meeting.meet_bots: không đọc được lịch của %s: %s", email, e)
            continue
        fetched[email] = events
        skip = list(prefs.get("skip_events") or [])
        for ev in events:
            if eligible(ev, skip, now):
                try:
                    await ensure_bot(email, ev, prefs.get("name") or "", now)
                except Exception:
                    log.exception("meeting.meet_bots: hẹn bot lỗi")
    await reconcile(fetched, now)
    await watch(now)


async def reconcile(fetched: Dict[str, List[Dict[str, Any]]], now: float) -> None:
    """Bot chưa vào mà buổi họp không còn hợp lệ với người hẹn: hủy (hoặc chuyển cho người cùng tham dự)."""
    for rec in [strip(r, True) for r in _col().find({"status": {"$in": list(NOT_JOINED)}})]:
        owner = rec["owner"]
        prefs = user_prefs.get(owner)
        reason = ""
        if not prefs.get("calendar_autojoin"):
            reason = "Đã tắt tự vào cuộc họp"
        elif rec["event_id"] in (prefs.get("skip_events") or []):
            reason = "Đã bỏ qua buổi này"
        elif owner in fetched:                        # chỉ dựa vào lịch khi vừa đọc được (không hủy vì lỗi mạng)
            ev = next((e for e in fetched[owner] if e["id"] == rec["event_id"]), None)
            if ev is None and rec["start"] > now - gcalendar.PAST_S:
                reason = "Buổi họp đã bị hủy hoặc xóa khỏi lịch"
            elif ev is not None and ev.get("declined"):
                reason = "Đã từ chối buổi họp"
            elif ev is not None and (not ev.get("meet_url") or key_of(ev["meet_url"], ev["start"]) != rec["key"]):
                reason = "Buổi họp đổi giờ hoặc đổi link (sẽ hẹn lại theo giờ mới)"
        if reason:
            rec = await cancel(rec, reason)
            await _notify(rec)
    # Bot đã được điều đi / đang ở trong cuộc họp mà người hẹn bấm Bỏ qua buổi này: bot rời ngay (tắt công tắc chung thì
    # chỉ áp dụng cho các buổi sau, không đuổi bot khỏi buổi đang họp)
    for rec in [strip(r, True) for r in _col().find({"status": {"$in": ["joining", "waiting", "in_call"]}})]:
        prefs = user_prefs.get(rec["owner"])
        if rec["event_id"] not in (prefs.get("skip_events") or []) or rec.get("left_at"):
            continue
        if any(_wants(o, rec) for o in rec.get("also") or []):
            rec = await cancel(rec, "Đã bỏ qua buổi này")     # người cùng tham dự khác vẫn muốn: chuyển bot, bot ở lại
            await _notify(rec)
            continue
        try:
            await recall.leave_call(rec["bot_id"])
        except recall.RecallError as e:
            log.info("meeting.meet_bots: bot %s chưa rời được (%s)", rec["bot_id"], e)
            continue
        rec = _update(rec, left_at=now, status_msg="Đã bỏ qua buổi này: bot rời cuộc họp")
        await _notify(rec)


async def watch(now: float) -> None:
    """Đọc trạng thái bot quanh giờ vào; bot đã rời cuộc họp thì kết thúc cuộc họp trong ứng dụng."""
    for rec in [strip(r, True) for r in _col().find({"status": {"$in": ["scheduled", "joining", "waiting", "in_call"]}})]:
        if not rec.get("bot_id"):
            continue
        near = rec["start"] - POLL_BEFORE_S <= now <= rec["start"] + POLL_AFTER_S
        closed = not rec.get("ws_open") and rec.get("ws_closed_at") and now - rec["ws_closed_at"] >= END_GRACE_S
        stale = now > rec["end"] + 3 * 3600                 # quá 3 giờ sau giờ kết thúc trên lịch mà chưa có tin
        if not (near or closed or stale) or now - float(rec.get("polled_at") or 0) < POLL_EVERY_S:
            continue
        try:
            bot = await recall.get_bot(rec["bot_id"])
        except recall.RecallError as e:
            log.info("meeting.meet_bots: đọc trạng thái bot %s lỗi: %s", rec["bot_id"], e)
            _update(rec, polled_at=now)
            continue
        st = recall.last_status(bot)
        ph = recall.phase(st["code"])
        msg = recall.status_text(st["code"], st["sub_code"])
        if ph == "in_call" and not rec.get("ws_open") and not rec.get("meeting_id") and now - rec["start"] > 180:
            msg = ("Bot đã vào cuộc họp nhưng chưa gửi được âm thanh về server (quản trị viên kiểm tra PUBLIC_BASE_URL "
                   "và server có nhận kết nối WebSocket từ Recall không)")
        rec = _update(rec, polled_at=now, code=st["code"], sub_code=st["sub_code"], status_msg=msg,
                      status=ph if ph in ("scheduled", "joining", "waiting", "in_call") else rec["status"])
        if ph in ("ended", "failed") and not rec.get("ws_open"):
            await finish(rec, failed=ph == "failed" and not st["joined"])
        else:
            await _notify(rec)


async def finish(rec: Dict[str, Any], failed: bool = False) -> None:
    """Bot đã rời: kết thúc cuộc họp (lập biên bản, lưu Drive) nếu trong phòng không ai còn bật mic; xóa media trên Recall."""
    rec = _update(rec, status="failed" if failed and not rec.get("meeting_id") else "ended")
    await _notify(rec)
    mid = rec.get("meeting_id")
    if mid:
        from meeting import live
        s = await live.get_session(int(mid))
        if s is not None and s.is_live():
            if s.audio_owner is None:
                log.info("meeting.meet_bots: bot rời Meet, kết thúc cuộc họp %s", mid)
                await s.finish(generate_minutes=True)
            else:
                await s.emit({"type": "toast", "level": "info",
                              "text": "Bot đã rời Google Meet. Mic trong phòng vẫn bật nên cuộc họp chưa kết thúc."})
    try:
        await recall.delete_media(rec["bot_id"])
    except recall.RecallError as e:
        log.info("meeting.meet_bots: xóa media bot %s lỗi: %s", rec.get("bot_id"), e)


async def on_meeting_ended(mid: int) -> None:
    """Người dùng bấm Kết thúc cuộc họp trong ứng dụng: bot rời Google Meet."""
    rec = _col().find_one({"meeting_id": int(mid), "status": {"$in": list(ACTIVE)}})
    if not rec or not rec.get("bot_id"):
        return
    rec = strip(rec, True)
    try:
        await recall.leave_call(rec["bot_id"])
    except recall.RecallError as e:
        log.info("meeting.meet_bots: bot %s không rời được: %s", rec["bot_id"], e)
    _update(rec, status="ended", status_msg="Đã kết thúc cuộc họp trong ứng dụng, bot rời Meet")


async def locked_tick() -> None:
    async with _tick_lock:
        await tick()


def run_soon() -> None:
    """Chạy một lượt ngay (vừa bật công tắc / bỏ qua một buổi), không chờ tới phút sau."""
    if enabled():
        asyncio.ensure_future(locked_tick())


async def loop() -> None:
    while True:
        try:
            await locked_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("meeting.meet_bots: lượt chạy lỗi")
        await asyncio.sleep(TICK_S)


def start() -> Optional["asyncio.Task"]:
    """Chạy nền khi server khởi động (bản web có key Recall). Kết nối âm thanh mất khi server khởi động lại."""
    global _task
    if not enabled():
        return None
    _col().update_many({"ws_open": True}, {"$set": {"ws_open": False, "ws_closed_at": time.time()}})
    _task = asyncio.create_task(loop())
    return _task


# ---------------------------------------------------------- nhận âm thanh ---
def check_token(rid: str, token: str) -> Optional[Dict[str, Any]]:
    rec = _col().find_one({"rid": str(rid or "")[:32]})
    if not rec or not token or not hmac.compare_digest(str(token), str(rec.get("token") or "")):
        return None
    if rec.get("status") in ("cancelled", "failed", "ended"):
        return None
    return strip(rec, True)


def _agenda(desc: str) -> List[str]:
    items = []
    for ln in (desc or "").splitlines():
        ln = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", ln).strip()
        if ln and not ln.lower().startswith(("http://", "https://", "join ", "tham gia")):
            items.append(ln[:200])
    return items[:10]


async def meeting_for(rec: Dict[str, Any]) -> int:
    """Cuộc họp của buổi này: gói tin âm thanh đầu tiên thì tạo (một lần, kể cả khi Recall nối lại)."""
    lock = _locks.setdefault("m" + rec["rid"], asyncio.Lock())
    async with lock:
        fresh = _col().find_one({"rid": rec["rid"]}) or {}
        if fresh.get("meeting_id"):
            rec["meeting_id"] = fresh["meeting_id"]
            return int(fresh["meeting_id"])
        prefs = user_prefs.get(rec["owner"])
        gid = (prefs.get("series_groups") or {}).get(rec.get("series") or rec["event_id"]) or rec.get("group_id")
        mid = await asyncio.to_thread(
            db.create_meeting, rec["title"], description=rec.get("description") or "", agenda=_agenda(rec.get("description")),
            expected_attendees=_attendee_names(rec), source="meet", owner=rec["owner"], meeting_type="Google Meet")
        g = groups.get_group(gid) if gid else None
        if g is not None and groups.role_of(g, rec["owner"]) in ("owner", "member"):
            groups.set_meeting_group(mid, g["id"])
        await asyncio.to_thread(db.update_meeting, mid, {"meet": {"url": rec["meet_url"], "rid": rec["rid"],
                                                                  "event_id": rec["event_id"]}}, True)
        _update(rec, meeting_id=mid, status="in_call", status_msg=recall.STATUS_TEXT["in_call_recording"])
        log.info("meeting.meet_bots: tạo cuộc họp %s từ Google Meet '%s'", mid, rec["title"])
        return mid


def parse_audio(raw: str) -> Optional[Dict[str, Any]]:
    """Gói tin audio_separate_raw.data của Recall -> {pcm, participant}. Gói khác / hỏng: None."""
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict) or msg.get("event") != "audio_separate_raw.data":
        return None
    d = ((msg.get("data") or {}).get("data") or {})
    try:
        pcm = base64.b64decode(d.get("buffer") or "", validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(pcm) % 2:
        pcm = pcm[:-1]
    p = d.get("participant") or {}
    return {"pcm": pcm, "participant": {"id": str(p.get("id") if p.get("id") is not None else ""),
                                        "name": str(p.get("name") or "").strip()[:100],
                                        "email": str(p.get("email") or "").strip().lower()[:200],
                                        "is_host": bool(p.get("is_host"))}}


async def serve(ws: Any, rid: str) -> None:
    """Recall nối vào đây: nhận âm thanh từng người trong Meet, đưa vào cuộc họp."""
    from starlette.websockets import WebSocketDisconnect
    from meeting import live
    rec = check_token(rid, ws.query_params.get("token", ""))
    if rec is None:
        await ws.close(code=4403)
        return
    await ws.accept()
    rec = _update(rec, ws_open=True, ws_closed_at=None)
    log.info("meeting.meet_bots: bot của '%s' đã nối âm thanh", rec["title"])
    s = None
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            item = parse_audio(msg.get("text") or (msg.get("bytes") or b"").decode("utf-8", "replace"))
            if item is None or not item["participant"]["id"]:
                continue
            if s is None or s.disposed:
                mid = await meeting_for(rec)
                s = await live.get_session(mid)
                if s is None:
                    break
                await s.set_meet_bot(public(rec))
            if not s.is_live():
                break                                  # cuộc họp đã kết thúc trong ứng dụng
            await s.feed_meet(item["participant"], item["pcm"])
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        rec = _update(rec, ws_open=False, ws_closed_at=time.time())
        if s is not None:
            await s.meet_disconnected()
            await s.set_meet_bot(public(rec))
        log.info("meeting.meet_bots: bot của '%s' ngắt kết nối âm thanh", rec["title"])


# -------------------------------------------------------- cài đặt của người dùng ---
def skip_event(email: str, event_id: str, skip: bool) -> List[str]:
    cur = [x for x in (user_prefs.get(email).get("skip_events") or []) if x != event_id]
    if skip:
        cur.append(event_id)
    cur = cur[-SKIP_KEEP:]
    user_prefs.update(email, skip_events=cur)
    return cur


def set_series_group(email: str, series_key: str, gid: Optional[int]) -> Dict[str, Any]:
    cur = dict(user_prefs.get(email).get("series_groups") or {})
    if gid:
        cur[series_key] = int(gid)
    else:
        cur.pop(series_key, None)
    user_prefs.update(email, series_groups=cur)
    # bot đã hẹn mà chưa tạo cuộc họp: cuộc họp sắp tạo vào đúng nhóm
    _col().update_many({"owner": email, "meeting_id": None, "status": {"$in": list(ACTIVE)},
                        "$or": [{"series": series_key}, {"event_id": series_key}]}, {"$set": {"group_id": gid}})
    return cur
