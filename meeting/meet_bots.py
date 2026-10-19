"""Bot Recall vào Google Meet theo lịch của từng người (bản web, bản 3.18).

Người dùng bật "Tự cho bot vào các cuộc họp có link Meet trên lịch của tôi" thì cứ mỗi phút:
- Hẹn bot cho các buổi bắt đầu trong 30 phút tới (hoặc đang diễn ra) có link Meet, chưa từ chối, chưa bấm Bỏ qua.
  Một link Meet ở một giờ bắt đầu chỉ một bot, dù nhiều người cùng bật (người hẹn trước là người tạo cuộc họp).
- Buổi bị hủy, đổi giờ, bỏ qua, tắt công tắc mà bot chưa vào: xóa bot (người khác cùng buổi vẫn bật thì chuyển cho họ).
- Quanh giờ vào (2 phút trước tới 20 phút sau) đọc trạng thái bot mỗi phút để hiện "đang chờ cho vào", "không vào được".
- Bot rời Meet (kết nối âm thanh đóng quá 2 phút và Recall báo xong): kết thúc cuộc họp, lập biên bản, lưu Drive.

Âm thanh: Recall nối vào /ws/recall/<rid>/?token=<mã ngẫu nhiên của từng buổi>. Gói tin đầu tiên tạo cuộc họp (tiêu đề,
người tham dự, chương trình lấy từ lịch; nhóm theo chuỗi lặp lại), mỗi người trong Meet một luồng nhận dạng riêng.

Nhiều người dùng Copilot cùng một cuộc họp (bản 3.21): vẫn một bot. Cuộc họp nhận âm thanh thuộc người hẹn bot; mỗi người
còn lại (cùng bật bot, hoặc dán link mời khi bot đã có) có cuộc họp riêng nhận bản sao lời thoại: phòng độc lập (trợ lý, sản
phẩm AI chỉ người đó thấy), biên bản lưu vào Drive của người đó. Ai đã xem được một cuộc họp của bot qua nhóm (thành viên,
chủ nhóm) thì dùng chung cuộc họp đó: một biên bản trong thư mục Drive của chủ nhóm. Một người kết thúc cuộc họp của mình
thì chỉ cuộc họp đó dừng; người hẹn bot kết thúc sớm thì cuộc họp riêng của người khác nhận âm thanh thay, bot ở lại.
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
SWITCH_WAIT_S = 30               # cuộc họp nhận âm thanh vừa kết thúc: chờ chuyển sang cuộc họp riêng khác tối đa ngần này
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


_MEET_RE = re.compile(r"^(?:https?://)?meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:[/?#].*)?$", re.I)
_CODE_RE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$", re.I)
MANUAL_HOURS = 4                 # bot mời bằng link: coi như buổi họp dài tối đa 4 giờ (sau đó không đọc trạng thái nữa)


def normalize_meet_url(text: str) -> str:
    """Link Google Meet (hoặc chỉ mã abc-defg-hij) -> https://meet.google.com/abc-defg-hij. Sai dạng: ValueError."""
    t = (text or "").strip()
    m = _MEET_RE.match(t)
    code = m.group(1) if m else (t if _CODE_RE.match(t) else "")
    if not code:
        raise ValueError("Link Google Meet chưa đúng dạng (ví dụ: https://meet.google.com/abc-defg-hij)")
    return f"https://meet.google.com/{code.lower()}"


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
    """Báo trạng thái bot cho phòng họp đang mở (cuộc họp nhận âm thanh và các cuộc họp riêng)."""
    from meeting import live
    if rec.get("meeting_id"):
        s = live.SESSIONS.get(int(rec["meeting_id"]))
        if s is not None:
            await s.set_meet_bot(public(rec))
    for c in _copies(rec):
        s = live.SESSIONS.get(int(c["meeting_id"]))
        if s is not None:
            await s.set_meet_bot(dict(public(rec), meeting_id=int(c["meeting_id"]), shared_by=_owner_name(rec)))


# -------------------------------------------------- bot dùng chung: cuộc họp riêng ---
def _copies(rec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cuộc họp riêng đang nhận lời thoại: [{email, name, meeting_id, at}]."""
    return [c for c in ((rec or {}).get("copies") or []) if isinstance(c, dict) and c.get("meeting_id")]


def _target(c: Dict[str, Any]) -> Dict[str, Any]:
    return {"meeting_id": int(c["meeting_id"]), "email": c["email"], "name": c.get("name") or ""}


def _name_of(rec: Dict[str, Any], email: str) -> str:
    for x in list(rec.get("joiners") or []) + _copies(rec):
        if x.get("email") == email and x.get("name"):
            return x["name"]
    return user_prefs.get(email).get("name") or (email or "").split("@")[0]


def _owner_name(rec: Dict[str, Any]) -> str:
    return rec.get("owner_name") or _name_of(rec, rec.get("owner") or "")


def targets_for(rid: str, mid: int) -> List[Dict[str, Any]]:
    """Cuộc họp riêng nhận lời thoại từ cuộc họp `mid` (cuộc họp nhận âm thanh hiện tại của bot `rid`)."""
    rec = _col().find_one({"rid": str(rid or "")[:32]})
    if not rec or rec.get("meeting_id") != mid:
        return []
    return [_target(c) for c in _copies(rec)]


def _refresh(rec: Dict[str, Any]) -> None:
    """Báo phiên của cuộc họp nhận âm thanh danh sách cuộc họp riêng mới."""
    from meeting import live
    if rec.get("meeting_id"):
        s = live.SESSIONS.get(int(rec["meeting_id"]))
        if s is not None:
            s.set_copy_targets([_target(c) for c in _copies(rec)])


def _can_view(email: str, m: Dict[str, Any]) -> bool:
    g = groups.get_group(m["group_id"]) if m.get("group_id") else None
    return auth.can_access({"email": email}, m, g)


def _covered(email: str, rec: Dict[str, Any], src: Dict[str, Any]) -> bool:
    """Người này đã xem được một cuộc họp còn nhận lời thoại của bot (qua nhóm: thành viên, chủ nhóm)."""
    metas = [src] + [m for m in (db.get_meeting(c["meeting_id"]) for c in _copies(rec)) if m]
    return any(m.get("status") != "ended" and _can_view(email, m) for m in metas)


def meeting_of(rec: Optional[Dict[str, Any]], email: str) -> Optional[int]:
    """Cuộc họp của bot này mà người này mở được: cuộc họp riêng của mình, hoặc cuộc họp nhận âm thanh (của mình, hoặc
    xem được qua nhóm)."""
    if not rec:
        return None
    email = (email or "").strip().lower()
    for c in _copies(rec):
        if c["email"] == email:
            return int(c["meeting_id"])
    mid = rec.get("meeting_id")
    if not mid:
        return None
    if rec.get("owner") == email:
        return int(mid)
    m = db.get_meeting(mid)
    return int(mid) if m and _can_view(email, m) else None


def public_for(rec: Optional[Dict[str, Any]], email: str) -> Optional[Dict[str, Any]]:
    """Trạng thái bot nhìn từ phía một người: cuộc họp người đó mở được, ai mời bot (khi không phải chính mình)."""
    out = public(rec)
    if out is None:
        return None
    out["meeting_id"] = meeting_of(rec, email)
    if rec.get("owner") != (email or "").strip().lower():
        out["shared_by"] = _owner_name(rec)
    return out


def _mapped_group(email: str, rec: Dict[str, Any]) -> Optional[int]:
    """Nhóm người này chọn cho buổi họp (gắn chuỗi lặp lại trên lịch, hoặc chọn lúc dán link), nếu vẫn thuộc nhóm đó."""
    gid = (user_prefs.get(email).get("series_groups") or {}).get(rec.get("series") or rec.get("event_id") or "")
    for j in rec.get("joiners") or []:
        if j.get("email") == email and j.get("group_id"):
            gid = j["group_id"]
    if not gid:
        return None
    g = groups.get_group(gid)
    return int(g["id"]) if g is not None and groups.role_of(g, email) in ("owner", "member") else None


def _wants_copy(email: str, rec: Dict[str, Any]) -> bool:
    """Người cùng dùng bot muốn có cuộc họp riêng: tự dán link mời, bot mời bằng link, hoặc vẫn bật bot cho buổi này.
    Đã kết thúc / thôi nhận ở buổi này thì không tạo lại (trừ khi tự dán link mời lần nữa)."""
    if email in (rec.get("detached") or []):
        return False
    if any(j.get("email") == email for j in rec.get("joiners") or []):
        return True
    return bool(rec.get("manual")) or _wants(email, rec)


def _create_copy(rec: Dict[str, Any], src: Dict[str, Any], email: str, existing: Optional[int] = None) -> int:
    """Cuộc họp riêng (chạy trong thread): cùng tiêu đề, chương trình, giờ bắt đầu với cuộc họp nhận âm thanh; chủ là
    người này; nhóm theo lựa chọn của chính người này. existing: phòng họp người đó đang mở (họp kết hợp)."""
    meet = {"url": rec["meet_url"], "rid": rec["rid"], "event_id": rec.get("event_id") or "", "copy_of": int(src["id"])}
    if existing:
        db.update_meeting(int(existing), {f"meet.{k}": v for k, v in meet.items()}, True)
        return int(existing)
    mid = db.create_meeting(src.get("title") or rec.get("title") or "Google Meet", description=src.get("description") or "",
                            agenda=src.get("agenda") or [], expected_attendees=src.get("expected_attendees") or [],
                            source="meet", owner=email, meeting_type=src.get("meeting_type") or "Google Meet")
    db.update_meeting(mid, {"started_at": src.get("started_at") or time.time(), "meet": meet}, True)
    gid = _mapped_group(email, rec)
    if gid:
        groups.set_meeting_group(mid, gid)
    return mid


async def _ensure_copy(rec: Dict[str, Any], email: str, name: str = "", existing: Optional[int] = None) -> Optional[int]:
    """Cuộc họp riêng cho một người cùng dùng bot (đã có thì trả về). None: không cần (người hẹn bot, đã xem được qua
    nhóm thì dùng chung, không còn muốn, bot chưa có cuộc họp)."""
    from meeting import live
    email = (email or "").strip().lower()
    async with _locks.setdefault("c" + rec["rid"], asyncio.Lock()):
        fresh = _col().find_one({"rid": rec["rid"]})
        if not fresh or fresh.get("status") not in ACTIVE or not fresh.get("meeting_id") or fresh.get("owner") == email:
            return None
        rec = strip(fresh, True)
        mine = next((c for c in _copies(rec) if c["email"] == email), None)
        if mine is not None:
            return int(mine["meeting_id"])
        if not await asyncio.to_thread(_wants_copy, email, rec):
            return None
        src = await asyncio.to_thread(db.get_meeting, rec["meeting_id"])
        if src is None or src.get("status") == "ended":
            return None
        if existing is None and await asyncio.to_thread(_covered, email, rec, src):
            return None               # xem được qua nhóm: dùng chung, một biên bản trong thư mục Drive của chủ nhóm
        mid = await asyncio.to_thread(_create_copy, rec, src, email, existing)
        entry = {"email": email, "name": name or await asyncio.to_thread(_name_of, rec, email), "meeting_id": mid,
                 "at": time.time()}
        copies = _copies(rec) + [entry]

        def persist():
            _col().update_one({"rid": rec["rid"]}, {"$set": {"copies": copies, "updated_at": time.time()}})
        c = live.SESSIONS.get(mid)
        if c is not None:                              # phòng đang mở (họp kết hợp): nhận dấu cuộc họp riêng ngay
            c.meeting.setdefault("meet", {}).update(url=rec["meet_url"], rid=rec["rid"], copy_of=int(src["id"]))
        s = await live.get_session(int(rec["meeting_id"]))
        if s is not None:                              # chép bù các câu đã có (người vào sau) rồi mới gửi câu mới
            await s.attach_copy(_target(entry), backfill=existing is None, persist=persist)
        else:
            await asyncio.to_thread(persist)
        rec["copies"] = copies
    log.info("meeting.meet_bots: cuộc họp riêng %s của %s cho bot '%s'", mid, email, rec["title"])
    await _notify(rec)
    return mid


async def _make_copies(rec: Dict[str, Any]) -> None:
    """Cuộc họp nhận âm thanh vừa tạo: cuộc họp riêng cho những người cùng dùng bot. Người đã chọn nhóm cho buổi này làm
    trước, để thành viên nhóm đó dùng chung cuộc họp của nhóm thay vì mỗi người một bản."""
    rec = strip(_col().find_one({"rid": rec["rid"]}), True) or rec
    joined = {j["email"]: j for j in rec.get("joiners") or [] if j.get("email")}
    people = [e for e in dict.fromkeys(list(rec.get("also") or []) + list(joined)) if e and e != rec.get("owner")]
    mapped = {e: await asyncio.to_thread(_mapped_group, e, rec) for e in people}
    for e in sorted(people, key=lambda e: mapped[e] is None):
        try:
            await _ensure_copy(rec, e, existing=(joined.get(e) or {}).get("meeting_id"))
        except Exception:
            log.exception("meeting.meet_bots: tạo cuộc họp riêng cho %s lỗi", e)


async def _finish_idle(mid: int) -> None:
    """Cuộc họp không còn nhận lời thoại từ bot: kết thúc và lập biên bản, trừ khi trong phòng còn bật mic."""
    from meeting import live
    s = await live.get_session(int(mid))
    if s is None or not s.is_live():
        return
    if s.audio_owner is None:
        log.info("meeting.meet_bots: bot rời Meet, kết thúc cuộc họp %s", mid)
        await s.finish(generate_minutes=True)
    else:
        await s.emit({"type": "toast", "level": "info",
                      "text": "Bot đã rời Google Meet. Mic trong phòng vẫn bật nên cuộc họp chưa kết thúc."})


async def _detach_copy(rec: Dict[str, Any], mid: int, finish: bool = False) -> Dict[str, Any]:
    """Một cuộc họp riêng thôi nhận lời thoại (chủ của nó kết thúc, bỏ qua buổi này, cho bot rời): bot ở lại cho người
    khác. finish: kết thúc luôn cuộc họp đó (lập biên bản)."""
    from meeting import live
    copies = _copies(rec)
    gone = [c for c in copies if int(c["meeting_id"]) == int(mid)]
    if not gone:
        return rec
    rest = [c for c in copies if int(c["meeting_id"]) != int(mid)]
    detached = list(dict.fromkeys(list(rec.get("detached") or []) + [c["email"] for c in gone]))
    rec = _update(rec, copies=rest, detached=detached)
    _refresh(rec)
    s = live.SESSIONS.get(int(mid))
    if s is not None:
        await s.set_meet_bot(dict(public(rec), meeting_id=int(mid), status="ended", shared_by=_owner_name(rec),
                                  status_msg="Cuộc họp này đã thôi nhận lời thoại từ bot"))
    await _make_copies(rec)                          # ai trước đây xem qua nhóm của cuộc họp này: có bản riêng
    if finish:
        await _finish_idle(int(mid))
    return strip(_col().find_one({"rid": rec["rid"]}), True) or rec


async def _promote(rec: Dict[str, Any]) -> bool:
    """Cuộc họp nhận âm thanh thôi nhận (người hẹn bot kết thúc, cho bot rời, bỏ qua buổi này): cuộc họp riêng còn mở của
    người khác nhận âm thanh thay, bot ở lại trong Google Meet. Không còn cuộc họp riêng nào mở: False."""
    from meeting import live
    copies = _copies(rec)
    for c in copies:
        m = await asyncio.to_thread(db.get_meeting, c["meeting_id"])
        if not m or m.get("status") == "ended":
            continue
        old_mid, old_owner = rec.get("meeting_id"), rec.get("owner")
        rest = [x for x in copies if int(x["meeting_id"]) != int(c["meeting_id"])]
        await asyncio.to_thread(db.update_meeting, int(c["meeting_id"]), {"meet.copy_of": None}, True)
        rec = _update(rec, owner=c["email"], owner_name=c.get("name") or "", meeting_id=int(c["meeting_id"]),
                      copies=rest, also=[x for x in (rec.get("also") or []) if x != c["email"]],
                      detached=list(dict.fromkeys(list(rec.get("detached") or []) + ([old_owner] if old_owner else []))),
                      group_id=m.get("group_id"))
        old = live.SESSIONS.get(int(old_mid)) if old_mid else None
        if old is not None:
            old.set_copy_targets([])
            await old.set_meet_bot(dict(public(rec), meeting_id=int(old_mid), status="ended",
                                        status_msg="Cuộc họp này đã thôi nhận lời thoại, bot ở lại cho người khác"))
        s = live.SESSIONS.get(int(c["meeting_id"]))
        if s is not None:
            s.meeting.setdefault("meet", {})["copy_of"] = None
            s.set_copy_targets([_target(x) for x in rest])
        for x in rest:                               # các cuộc họp riêng còn lại chép theo cuộc họp mới
            await asyncio.to_thread(db.update_meeting, int(x["meeting_id"]), {"meet.copy_of": int(c["meeting_id"])}, True)
            o = live.SESSIONS.get(int(x["meeting_id"]))
            if o is not None:
                o.meeting.setdefault("meet", {})["copy_of"] = int(c["meeting_id"])
        log.info("meeting.meet_bots: bot '%s' chuyển sang cuộc họp %s của %s", rec["title"], c["meeting_id"], c["email"])
        await _notify(rec)
        await _make_copies(rec)                      # ai trước đây xem qua nhóm của cuộc họp vừa thôi nhận: có bản riêng
        return True
    return False


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
        manual = _col().find_one({"manual": True, "meet_url": ev["meet_url"], "status": {"$in": list(ACTIVE)},
                                  "end": {"$gt": now}})
        if manual is not None:                    # đã có người dán link mời bot vào đúng cuộc họp này: không gọi bot thứ hai
            if manual["owner"] != email and email not in (manual.get("also") or []):
                _col().update_one({"rid": manual["rid"]}, {"$addToSet": {"also": email}})
                manual.setdefault("also", []).append(email)
            await _copy_if_needed(manual, email, owner_name)
            return strip(manual)
        rec = _col().find_one({"key": key, "status": {"$ne": "cancelled"}}, sort=[("created_at", -1)])
        if rec is not None:
            if rec["owner"] != email and email not in (rec.get("also") or []):
                _col().update_one({"rid": rec["rid"]}, {"$addToSet": {"also": email}})
                rec.setdefault("also", []).append(email)
            if rec.get("status") == "pending" and not rec.get("bot_id"):
                return strip(await _create(strip(rec, True), owner_name, now))
            await _copy_if_needed(rec, email, owner_name)
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


async def _copy_if_needed(rec: Dict[str, Any], email: str, owner_name: str = "") -> None:
    """Người cùng dùng bot vừa được ghi nhận (bật bot muộn, bỏ "Bỏ qua"): bot đang chép lời thì tạo cuộc họp riêng ngay,
    chép bù phần đã nói."""
    if not rec.get("meeting_id") or rec.get("status") not in ACTIVE or rec.get("owner") == email:
        return
    if any(c["email"] == email for c in _copies(rec)) or email in (rec.get("detached") or []):
        return
    try:
        await _ensure_copy(rec, email, owner_name)
    except Exception:
        log.exception("meeting.meet_bots: tạo cuộc họp riêng cho %s lỗi", email)


async def retry(email: str, ev: Dict[str, Any], owner_name: str = "") -> Dict[str, Any]:
    """Mời bot vào lại (lần trước không ai cho vào, bot lỗi): bản ghi cũ thôi, hẹn bot mới vào ngay."""
    old = _col().find_one({"key": key_of(ev["meet_url"], ev["start"]), "status": {"$in": ["failed", "ended"]}},
                          sort=[("created_at", -1)])
    keep, carry = None, {}
    if old is not None:
        _col().update_one({"rid": old["rid"]}, {"$set": {"status": "cancelled", "status_msg": "Đã mời bot vào lại",
                                                         "updated_at": time.time()}})
        m = db.get_meeting(old["meeting_id"]) if old.get("meeting_id") else None
        if m and m.get("status") != "ended":
            keep = int(old["meeting_id"])              # cuộc họp còn mở: bot mới chép tiếp vào đúng cuộc họp đó
            # cùng các cuộc họp riêng còn mở của người khác; chủ bot mới là chủ cuộc họp đó (người bấm có cuộc họp riêng)
            carry = {"copies": [c for c in _copies(old) if (db.get_meeting(c["meeting_id"]) or {}).get("status") == "live"],
                     "joiners": old.get("joiners") or [], "detached": old.get("detached") or [],
                     "owner": old["owner"], "owner_name": old.get("owner_name") or ""}
    rec = await ensure_bot(email, ev, owner_name)
    if keep and not rec.get("meeting_id"):
        fields: Dict[str, Any] = {"meeting_id": keep, **carry}
        if carry["owner"] != rec["owner"]:
            fields["also"] = [x for x in dict.fromkeys(list(rec.get("also") or []) + [rec["owner"]]) if x != carry["owner"]]
        _col().update_one({"rid": rec["rid"]}, {"$set": fields})
        rec.update(fields)
        _refresh(rec)
    return rec


async def join_now(email: str, owner_name: str, meet_url: str, title: str = "", group_id: Optional[int] = None,
                   meeting_id: Optional[int] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Mời bot vào ngay một cuộc họp Google Meet không có trên lịch (dán link). Không cần kết nối Lịch Google.

    meeting_id: bot chép vào cuộc họp đang mở trong ứng dụng (họp kết hợp: mic trong phòng + người online qua Meet);
    không có thì gói âm thanh đầu tiên tạo cuộc họp mới (tên `title`, nhóm `group_id`). Người gọi kiểm tra quyền.
    Bot của người khác đã ở trong cuộc họp này: không gọi bot thứ hai, người này có cuộc họp riêng nhận cùng lời thoại
    (phòng đang mở thì chính phòng đó)."""
    now = now or time.time()
    url = normalize_meet_url(meet_url)
    code = meet_code(url)
    lock = _locks.setdefault("join:" + code, asyncio.Lock())
    async with lock:
        cur = _col().find_one({"meet_url": url, "status": {"$in": list(ACTIVE)}, "end": {"$gt": now},
                               "start": {"$lte": now + recall.SCHEDULE_LEAD_S}}, sort=[("created_at", -1)])
        if cur is not None:
            if cur["owner"] == email:
                return strip(cur)                 # bấm lại: dùng bot đang có
            mine = next((c for c in _copies(cur) if c["email"] == email), None)
            if mine is None:
                joiner = {"email": email, "name": owner_name, "group_id": group_id,
                          "meeting_id": int(meeting_id) if meeting_id else None}
                joiners = [j for j in cur.get("joiners") or [] if j.get("email") != email] + [joiner]
                _col().update_one({"rid": cur["rid"]}, {"$addToSet": {"also": email},
                                                        "$set": {"joiners": joiners, "updated_at": now,
                                                                 "detached": [x for x in cur.get("detached") or []
                                                                              if x != email]}})
                cur = _col().find_one({"rid": cur["rid"]})
                if cur.get("meeting_id"):            # bot đã chép lời: có cuộc họp riêng ngay (chưa thì lúc bắt đầu)
                    await _ensure_copy(strip(cur, True), email, owner_name, existing=meeting_id)
                    cur = _col().find_one({"rid": cur["rid"]})
            return strip(cur)
        rec = {"rid": secrets.token_hex(8), "token": secrets.token_urlsafe(24), "key": key_of(url, now), "owner": email,
               "also": [], "owner_name": owner_name, "event_id": f"manual:{code}:{int(now)}", "series": "", "manual": True,
               "meet_url": url, "title": (title or "").strip()[:200] or f"Google Meet {code}", "start": now,
               "end": now + MANUAL_HOURS * 3600, "attendees": [], "description": "", "group_id": group_id,
               "bot_id": "", "status": "pending", "status_msg": "Đang mời bot", "code": "", "sub_code": "",
               "meeting_id": int(meeting_id) if meeting_id else None, "ws_open": False, "ws_closed_at": None,
               "polled_at": 0.0, "created_at": now, "updated_at": now}
        _col().insert_one(dict(rec))
        rec = await _create(rec, owner_name, now)
        await _notify(rec)
        return strip(rec)


async def leave(rid: str, email: str, admin: bool = False) -> Dict[str, Any]:
    """Cho bot rời (hoặc hủy bot đang xin vào). Người mời, người cùng tham dự, hoặc quản trị viên. Không có: KeyError.

    Bot dùng chung: người có cuộc họp riêng bấm thì chỉ cuộc họp đó thôi nhận lời thoại; người hẹn bot bấm thì cuộc họp
    riêng còn mở của người khác nhận thay. Bot chỉ rời Google Meet khi không còn ai dùng (hoặc quản trị viên cho rời)."""
    email = (email or "").strip().lower()
    rec = _col().find_one({"rid": str(rid or "")[:32]})
    mine = next((c for c in _copies(rec) if c["email"] == email), None) if rec else None
    if rec is None or not (admin or rec["owner"] == email or email in (rec.get("also") or []) or mine):
        raise KeyError(rid)
    rec = strip(rec, True)
    if rec.get("status") not in ACTIVE:
        return strip(rec)
    if mine is not None:
        return strip(await _detach_copy(rec, int(mine["meeting_id"])))
    if rec.get("meeting_id") and (rec["owner"] == email or email in (rec.get("also") or [])) and await _promote(rec):
        return strip(_col().find_one({"rid": rec["rid"]}))
    if rec.get("bot_id"):
        await recall.remove(rec["bot_id"])
    rec = _update(rec, status="ended" if rec.get("meeting_id") else "cancelled", left_at=time.time(),
                  status_msg="Đã cho bot rời cuộc họp")
    await _notify(rec)
    for c in _copies(rec):                          # quản trị viên cho bot rời hẳn: cuộc họp riêng không còn lời thoại
        await _finish_idle(int(c["meeting_id"]))
    return strip(rec)


def manual_for(email: str, now: Optional[float] = None, hours: float = 12) -> List[Dict[str, Any]]:
    """Bot anh chị mời bằng link (hoặc dùng chung khi dán link mà bot của người khác đã ở đó) trong `hours` giờ gần đây,
    mới nhất trước, để hiện trên trang chủ."""
    now = now or time.time()
    rows = _col().find({"status": {"$ne": "cancelled"}, "created_at": {"$gte": now - hours * 3600},
                        "$or": [{"manual": True, "owner": email}, {"manual": True, "also": email},
                                {"joiners.email": email}]}).sort("created_at", -1).limit(5)
    return [dict(public_for(r, email), title=r.get("title"), meet_url=r.get("meet_url"), manual=True) for r in rows]


async def _retry_pending_manual(now: float) -> None:
    """Bot mời bằng link mà Recall đang bận lúc mời: thử lại ở lượt sau (bot theo lịch đã có ensure_bot làm việc này)."""
    for rec in [strip(r, True) for r in _col().find({"manual": True, "status": "pending", "bot_id": "",
                                                     "end": {"$gt": now}})]:
        rec = await _create(rec, rec.get("owner_name") or "", now)
        await _notify(rec)


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
    await _retry_pending_manual(now)
    await reconcile(fetched, now)
    await watch(now)


async def reconcile(fetched: Dict[str, List[Dict[str, Any]]], now: float) -> None:
    """Bot chưa vào mà buổi họp không còn hợp lệ với người hẹn: hủy (hoặc chuyển cho người cùng tham dự).
    Bot mời bằng link (manual) không theo lịch nên không đối chiếu ở đây."""
    for rec in [strip(r, True) for r in _col().find({"status": {"$in": list(NOT_JOINED)}, "manual": {"$ne": True}})]:
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
    for rec in [strip(r, True) for r in _col().find({"status": {"$in": ["joining", "waiting", "in_call"]},
                                                     "manual": {"$ne": True}})]:
        for c in _copies(rec):                       # người có cuộc họp riêng bấm Bỏ qua: chỉ cuộc họp đó thôi nhận
            if rec["event_id"] in (user_prefs.get(c["email"]).get("skip_events") or []):
                rec = await _detach_copy(rec, int(c["meeting_id"]), finish=True)
        prefs = user_prefs.get(rec["owner"])
        if rec["event_id"] not in (prefs.get("skip_events") or []) or rec.get("left_at"):
            continue
        old = rec.get("meeting_id")
        if old and await _promote(rec):              # người hẹn bot bỏ qua: cuộc họp riêng của người khác nhận thay
            await _finish_idle(int(old))
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
        no_audio = ("Bot đã vào cuộc họp nhưng chưa gửi được âm thanh về server (quản trị viên kiểm tra PUBLIC_BASE_URL "
                    "và server có nhận kết nối WebSocket từ Recall không)")
        if ph == "in_call" and not rec.get("ws_open") and not rec.get("meeting_id") and now - rec["start"] > 180:
            msg = no_audio
        if ph in ("ended", "failed") and not rec.get("ws_open"):
            # "Bot đã xong" che mất lý do: chưa từng vào thì nói vì sao (chưa ai cho vào, Meet chặn khách...)
            if not st["joined"]:
                msg = recall.not_joined_text(st)
            elif not rec.get("meeting_id"):
                msg = no_audio
        rec = _update(rec, polled_at=now, code=st["code"], sub_code=st["sub_code"], status_msg=msg,
                      status=ph if ph in ("scheduled", "joining", "waiting", "in_call") else rec["status"])
        if ph in ("ended", "failed") and not rec.get("ws_open"):
            await finish(rec, failed=not st["joined"])
        else:
            await _notify(rec)


async def finish(rec: Dict[str, Any], failed: bool = False) -> None:
    """Bot đã rời: kết thúc cuộc họp (lập biên bản, lưu Drive) nếu trong phòng không ai còn bật mic; xóa media trên Recall.
    failed: bot chưa từng vào cuộc họp. Khi đó cuộc họp đang mở trong ứng dụng (bot mời từ phòng họp) vẫn giữ nguyên."""
    rec = _update(rec, status="failed" if failed else "ended")
    await _notify(rec)
    mid = rec.get("meeting_id")
    if mid and not failed:
        await _finish_idle(int(mid))
        for c in _copies(rec):                       # cuộc họp riêng của từng người: kết thúc, biên bản của riêng họ
            await _finish_idle(int(c["meeting_id"]))
    try:
        await recall.delete_media(rec["bot_id"])
    except recall.RecallError as e:
        log.info("meeting.meet_bots: xóa media bot %s lỗi: %s", rec.get("bot_id"), e)


async def on_meeting_ended(mid: int) -> None:
    """Người dùng bấm Kết thúc cuộc họp trong ứng dụng (hoặc xóa cuộc họp đang diễn ra). Cuộc họp riêng: chỉ cuộc họp đó
    thôi nhận lời thoại. Cuộc họp nhận âm thanh: cuộc họp riêng còn mở của người khác nhận thay, bot ở lại; không còn ai
    thì bot rời Google Meet."""
    rec = _col().find_one({"meeting_id": int(mid), "status": {"$in": list(ACTIVE)}})
    if rec is None:
        rec = _col().find_one({"copies.meeting_id": int(mid), "status": {"$in": list(ACTIVE)}})
        if rec is not None:
            await _detach_copy(strip(rec, True), int(mid))
        return
    if not rec.get("bot_id"):
        return
    rec = strip(rec, True)
    if await _promote(rec):
        return
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
        await _make_copies(rec)                      # những người khác cùng dùng bot: mỗi người một cuộc họp riêng
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
    checked = stalled = 0.0
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            item = parse_audio(msg.get("text") or (msg.get("bytes") or b"").decode("utf-8", "replace"))
            if item is None or not item["participant"]["id"]:
                continue
            if s is None or s.disposed or not s.is_live():
                # chưa có cuộc họp, hoặc cuộc họp nhận âm thanh vừa kết thúc trong ứng dụng: người hẹn bot kết thúc sớm
                # thì cuộc họp riêng của người khác nhận thay (đọc lại bản ghi bot, tối đa mỗi giây một lần)
                now = time.monotonic()
                if s is not None and now - checked < 1.0:
                    continue
                checked = now
                fresh = _col().find_one({"rid": rec["rid"]})
                if not fresh or fresh.get("status") not in ACTIVE:
                    break                              # bot đang rời: không nhận nữa
                rec = strip(fresh, True)
                if s is not None and not s.disposed and rec.get("meeting_id") == s.id:
                    stalled = stalled or now
                    if now - stalled > SWITCH_WAIT_S:
                        break                          # không có cuộc họp nào nhận thay
                    continue
                stalled = 0.0
                mid = await meeting_for(rec)
                s = await live.get_session(mid)
                if s is None:
                    break
                await s.set_meet_bot(public(rec))
                if not s.is_live():
                    continue
                checked = 0.0                          # đã nối: cuộc họp này kết thúc thì đọc lại bản ghi ngay
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
