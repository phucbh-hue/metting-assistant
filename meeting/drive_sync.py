"""Lưu biên bản và ghi âm của cuộc họp lên Google Drive (bản web), chạy nền sau khi biên bản xong.

Cấu trúc trong Drive của người giữ (chủ nhóm; cuộc họp không thuộc nhóm thì người tạo):
    Meeting Copilot / <Tên nhóm>        / <dd-mm-yyyy HHhMM - Tiêu đề> / Biên bản - <tiêu đề> (Google Docs), Ghi âm.mp3
    Meeting Copilot / Cuộc họp riêng    / ...
- Mã các thư mục được nhớ (google_tokens.root_id / private_id, group.drive.folder_id, meeting.drive.folder_id) nên đổi
  tên nhóm hay cuộc họp không sinh thư mục mới; thư mục bị xóa trên Drive thì tạo lại.
- Lưu lại (biên bản tạo lại, bấm "Lưu lên Drive") ghi đè đúng tệp cũ, không sinh bản trùng.
- Thư mục nhóm được chia sẻ quyền chỉnh sửa cho thành viên, gỡ khi bớt người.
- Ghi âm trên Drive tự xóa sau group.recording_drive_days (mặc định RECORDING_RETENTION_DAYS) ngày; biên bản giữ mãi.
- Lưu lỗi không bao giờ ảnh hưởng biên bản trong ứng dụng; lỗi tạm thời thì thử lại 3 lần.
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from meeting import auth, db, google_oauth, groups, recap_export, recording
from meeting.gdrive import Drive

log = logging.getLogger("meeting.drive")

ROOT_NAME = "Meeting Copilot"
PRIVATE_NAME = "Cuộc họp riêng"
AUDIO_NAME = "Ghi âm.mp3"
RETRY_DELAYS = (5.0, 30.0, 120.0)
_TASKS: Dict[int, "asyncio.Task"] = {}


def enabled() -> bool:
    return auth.ENABLED and google_oauth.configured()


def keeper_of(meeting: Dict[str, Any], group: Optional[Dict[str, Any]]) -> str:
    """Biên bản nằm trong Drive của ai: chủ nhóm, hoặc người tạo nếu cuộc họp không thuộc nhóm."""
    return str((group or {}).get("owner") or meeting.get("owner") or "").lower()


def _latest_minutes(mid: int) -> Optional[Dict[str, Any]]:
    arts = [a for a in db.get_artifacts(mid) if a.get("kind") == "minutes" and (a.get("content") or "").strip()]
    return max(arts, key=lambda a: (a.get("created_at") or 0, a.get("id") or 0)) if arts else None


async def _status(mid: int, **fields: Any) -> Dict[str, Any]:
    """Ghi trạng thái lưu vào meeting.drive và báo cho phòng họp đang mở (sự kiện drive_status)."""
    await asyncio.to_thread(db.update_meeting, mid, {f"drive.{k}": v for k, v in fields.items()}, True)
    m = await asyncio.to_thread(db.get_meeting, mid) or {}
    drive = m.get("drive") or {}
    from meeting import live
    s = live.SESSIONS.get(mid)
    if s is not None:
        s.meeting["drive"] = drive
        await s.emit({"type": "drive_status", "drive": drive})
    return drive


async def save_meeting(mid: int) -> Dict[str, Any]:
    """Lưu biên bản mới nhất + ghi âm (nếu có) của cuộc họp vào thư mục của nó trên Drive. Trả về meeting.drive."""
    m = await asyncio.to_thread(db.get_meeting, mid)
    if not m:
        raise KeyError("Không tìm thấy cuộc họp")
    g = groups.get_group(m.get("group_id")) if m.get("group_id") else None
    keeper = keeper_of(m, g)
    if not keeper:
        return await _status(mid, status="skipped", error="Cuộc họp cũ chưa có chủ, không biết lưu vào Drive của ai")
    if not (await asyncio.to_thread(google_oauth.status, keeper)).get("drive", True):
        raise google_oauth.NeedReconnect(f"{keeper} chưa cho phép quyền Google Drive: hãy kết nối Google lại và đánh "
                                         "dấu ô Drive")
    minutes = await asyncio.to_thread(_latest_minutes, mid)
    seconds = (await asyncio.to_thread(recording.info, mid)).get("seconds") or 0
    md = m.get("drive") or {}
    if not minutes and not (seconds and not md.get("audio_deleted_at")):
        return await _status(mid, status="skipped", error="Chưa có biên bản hay ghi âm để lưu")
    await _status(mid, status="saving", error="")
    drive = Drive(keeper)
    tok = await asyncio.to_thread(google_oauth.get_doc, keeper)
    root = await drive.ensure_folder(ROOT_NAME, None, tok.get("root_id"))
    if root["id"] != tok.get("root_id"):
        await asyncio.to_thread(google_oauth.update_doc, keeper, root_id=root["id"])
    if g is not None:
        gname = recap_export.clean_title(g["name"], "Nhóm")
        parent = await drive.ensure_folder(gname, root["id"], (g.get("drive") or {}).get("folder_id"))
        if parent.get("name") != gname:                       # nhóm đã đổi tên
            parent = await drive.rename(parent["id"], gname)
        groups.update_drive(g["id"], folder_id=parent["id"], folder_url=parent.get("webViewLink"), keeper=keeper)
    else:
        parent = await drive.ensure_folder(PRIVATE_NAME, root["id"], tok.get("private_id"))
        if parent["id"] != tok.get("private_id"):
            await asyncio.to_thread(google_oauth.update_doc, keeper, private_id=parent["id"])
    same_keeper = md.get("keeper") == keeper
    fname = recap_export.folder_name(m)
    folder = await drive.ensure_folder(fname, parent["id"], md.get("folder_id") if same_keeper else None)
    if same_keeper and md.get("parent_id") and md["parent_id"] != parent["id"] and folder["id"] == md.get("folder_id"):
        folder = await drive.move(folder["id"], parent["id"], md["parent_id"])      # cuộc họp đã chuyển nhóm
    if folder.get("name") != fname:
        folder = await drive.rename(folder["id"], fname)
    out: Dict[str, Any] = {"keeper": keeper, "folder_id": folder["id"], "folder_url": folder.get("webViewLink"),
                           "parent_id": parent["id"]}
    # Thư mục cuộc họp vẫn là thư mục cũ thì ghi đè đúng các tệp cũ; thư mục mới (bị xóa trên Drive, đổi người giữ) thì
    # tạo tệp mới trong đó, không đụng tệp cũ ở chỗ khác
    same = same_keeper and folder["id"] == md.get("folder_id")
    title = recap_export.clean_title(m.get("title"))
    if minutes:
        doc = await drive.put_doc(f"Biên bản - {title}", recap_export.md_to_html(minutes["content"], f"Biên bản - {title}"),
                                  folder["id"], md.get("doc_id") if same else None)
        out.update(doc_id=doc["id"], doc_url=doc.get("webViewLink"), minutes_id=minutes.get("id"))
    if seconds and not md.get("audio_deleted_at") and (not same or md.get("audio_seconds") != seconds
                                                       or not md.get("audio_id")):
        path = await asyncio.to_thread(recap_export.recording_mp3, mid)
        try:
            if path is not None:
                f = await drive.put_file(AUDIO_NAME, "audio/mpeg", str(path), folder["id"],
                                         md.get("audio_id") if same else None)
                out.update(audio_id=f["id"], audio_url=f.get("webViewLink"), audio_seconds=seconds, audio_at=time.time())
        finally:
            recap_export.discard(path)
    if g is not None:
        await sync_group_sharing(g["id"], drive)
    log.info("meeting.drive: đã lưu cuộc họp %s lên Drive của %s", mid, keeper)
    return await _status(mid, **out, status="done", error="", saved_at=time.time())


async def _run(mid: int):
    try:
        for attempt, delay in enumerate((0.0,) + tuple(RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                await save_meeting(mid)
                return
            except google_oauth.NeedReconnect as e:
                await _status(mid, status="need_connect", error=str(e))
                return
            except Exception as e:
                log.warning("meeting.drive: lưu cuộc họp %s lần %d lỗi: %s", mid, attempt + 1, e)
                await _status(mid, status="error", error=str(e)[:300])
    finally:
        _TASKS.pop(mid, None)


def schedule(mid: int) -> bool:
    """Lên lịch lưu (chạy nền). Đang lưu dở thì thôi. Không ở bản web / chưa cấu hình Google thì bỏ qua."""
    if not enabled():
        return False
    t = _TASKS.get(mid)
    if t is not None and not t.done():
        return True
    _TASKS[mid] = asyncio.create_task(_run(mid))
    return True


async def sync_group_sharing(gid: int, drive: Optional[Drive] = None) -> None:
    """Chia sẻ thư mục nhóm (quyền chỉnh sửa) cho đúng danh sách thành viên hiện tại."""
    g = groups.get_group(gid)
    folder = ((g or {}).get("drive") or {}).get("folder_id")
    if not g or not folder or not g.get("owner"):
        return
    drive = drive or Drive(g["owner"])
    perms: List[Dict[str, str]] = list((g.get("drive") or {}).get("permissions") or [])
    have = {p["email"] for p in perms}
    want = set(g.get("members") or [])
    for email in sorted(want - have):
        perms.append({"email": email, "id": await drive.share(folder, email, "writer")})
    for p in [p for p in perms if p["email"] not in want]:
        await drive.unshare(folder, p["id"])
        perms.remove(p)
    groups.update_drive(gid, permissions=perms)


def schedule_sharing(gid: int) -> None:
    if not enabled():
        return

    async def run():
        try:
            await sync_group_sharing(gid)
        except Exception as e:
            log.warning("meeting.drive: đồng bộ chia sẻ nhóm %s lỗi: %s", gid, e)
    asyncio.create_task(run())


async def delete_audio(mid: int) -> bool:
    """Xóa ghi âm của cuộc họp trên Drive (người dùng xóa bản ghi âm / xóa cuộc họp, hoặc quá hạn lưu)."""
    m = await asyncio.to_thread(db.get_meeting, mid) or {}
    md = m.get("drive") or {}
    if not md.get("audio_id") or not md.get("keeper"):
        return False
    try:
        await Drive(md["keeper"]).delete(md["audio_id"])
    except Exception as e:
        log.warning("meeting.drive: xóa ghi âm cuộc họp %s trên Drive lỗi: %s", mid, e)
        return False
    if m:
        await _status(mid, audio_id=None, audio_url=None, audio_deleted_at=time.time())
    return True


async def purge_recordings(now: Optional[float] = None) -> int:
    """Xóa ghi âm trên Drive quá hạn lưu của nhóm (hoặc RECORDING_RETENTION_DAYS)."""
    now = now or time.time()
    rows = await asyncio.to_thread(lambda: list(db._get_db()["meetings"].find(
        {"drive.audio_id": {"$nin": [None, ""]}}, {"_id": 0, "id": 1, "group_id": 1, "drive": 1})))
    n = 0
    for m in rows:
        md = m.get("drive") or {}
        g = groups.get_group(m.get("group_id")) if m.get("group_id") else None
        days = float((g or {}).get("recording_drive_days") or recording.RETENTION_DAYS)
        if days > 0 and md.get("audio_at") and now - float(md["audio_at"]) > days * 86400:
            n += await delete_audio(m["id"])
    if n:
        log.info("meeting.drive: đã xóa %d ghi âm quá hạn trên Drive", n)
    return n
