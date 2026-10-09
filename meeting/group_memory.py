"""Nhớ xuyên cuộc họp trong một nhóm (bản 3.19, giai đoạn 3).

Lập biên bản cho cuộc họp thuộc nhóm thì thêm một lời gọi AI: rút các điều đã chốt của buổi này (quyết định, con số,
hạn chót, người phụ trách, chính sách) và so với các điều đã chốt ở tối đa 12 buổi trước trong nhóm. Nói khác điều đã
chốt thì biên bản có mục "Thay đổi so với các buổi trước":
    "Dạ thưa anh chị, theo như mình đã bàn ở buổi 12/09/2026 (Review sprint) thì là A chứ không phải B, anh chị có thể
     xem xét lại nha ạ."
Theo lựa chọn của anh Phúc (09/10/2026): chỉ ghi trong biên bản, không nhắc hay ngắt lời trong lúc họp.

Kết quả lưu ở meetings.memory = {"facts": [...], "changes": [...], "minutes_id", "at"}. Buổi trước chưa có memory (họp
trước bản này, hoặc mới chuyển vào nhóm) thì rút bù từ biên bản của buổi đó, tối đa BACKFILL buổi mỗi lần, chạy song song.
"""
import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

from meeting import artifacts, db, retrieval

log = logging.getLogger("meeting.group_memory")

PREV_MEETINGS = 12
PREV_FACTS_MAX = 150
BACKFILL = 4
KINDS = ("quyết định", "con số", "hạn chót", "người phụ trách", "chính sách")

RULES = """- facts: chỉ những điều ĐÃ CHỐT, con số, hạn chót, người phụ trách, chính sách cụ thể; tối đa 25 điều; topic ngắn gọn,
  cụ thể (ví dụ "Ngày phát hành voucher Tết", "Phí tích hợp cho Vinmart"), value là giá trị đã chốt.
- Ngày theo dd/mm/yyyy, tiền theo 1.000.000đ. Không dùng gạch dài."""

MEMORY_SYSTEM = f"""Bạn là thư ký theo dõi các điều đã chốt của một nhóm họp định kỳ tại UrBox.
Đầu vào: (1) CÁC ĐIỀU ĐÃ CHỐT Ở CÁC BUỔI TRƯỚC, mỗi dòng có mã [P..], ngày và tên buổi; (2) BIÊN BẢN BUỔI HỌP HÔM NAY.
Trả về DUY NHẤT một JSON, không kèm chữ nào khác:
{{"facts": [{{"topic": "...", "value": "...", "kind": "quyết định|con số|hạn chót|người phụ trách|chính sách", "who": "người nêu nếu rõ"}}],
 "changes": [{{"topic": "...", "before": "điều đã chốt trước đây", "ref": "P3", "now": "điều hôm nay nói"}}]}}
Quy tắc:
{RULES}
- changes: chỉ khi hôm nay nói KHÁC rõ ràng với một điều đã chốt ở buổi trước CÙNG chủ đề (ngày khác, con số khác, người
  phụ trách khác, quyết định ngược lại). Thêm chi tiết hay báo tiến độ bình thường không phải là thay đổi. Không chắc thì
  không đưa vào. ref là mã [P..] của điều đã chốt trước đây. Không có thì "changes": []."""

FACTS_SYSTEM = f"""Bạn là thư ký theo dõi các điều đã chốt của một nhóm họp tại UrBox. Đọc BIÊN BẢN và trả về DUY NHẤT một JSON:
{{"facts": [{{"topic": "...", "value": "...", "kind": "quyết định|con số|hạn chót|người phụ trách|chính sách", "who": "người nêu nếu rõ"}}]}}
Quy tắc:
{RULES}"""


def _s(v: Any, n: int = 200) -> str:
    return re.sub(r"\s+", " ", str(v or "")).replace(chr(0x2014), "-").strip()[:n]


def clean_facts(data: Any) -> List[Dict[str, str]]:
    out = []
    for f in (data or {}).get("facts") or [] if isinstance(data, dict) else []:
        if not isinstance(f, dict) or not _s(f.get("topic")) or not _s(f.get("value")):
            continue
        kind = _s(f.get("kind"), 40).lower()
        out.append({"topic": _s(f["topic"], 120), "value": _s(f["value"], 240),
                    "kind": kind if kind in KINDS else "quyết định", "who": _s(f.get("who"), 80)})
    return out[:25]


def previous(gid: int, mid: int, before: Optional[float]) -> List[Dict[str, Any]]:
    """Tối đa PREV_MEETINGS buổi trước (bắt đầu trước buổi này) của nhóm, mới nhất trước."""
    where: Dict[str, Any] = {"group_id": int(gid), "id": {"$ne": int(mid)}}
    if before:
        where["started_at"] = {"$lt": float(before)}
    return db.find_meetings(where, limit=PREV_MEETINGS, sort="started_at")


async def _extract_facts(m: Dict[str, Any]) -> List[Dict[str, str]]:
    art = await asyncio.to_thread(retrieval.latest_minutes, m["id"])
    if not art:
        return []
    prompt = f"Buổi họp: {retrieval.meeting_title(m)}\n\nBIÊN BẢN:\n{art['content'][:14000]}"
    data = artifacts._json_from_text(await artifacts._call_llm(FACTS_SYSTEM, prompt, max_tokens=2000))
    facts = clean_facts(data)
    await asyncio.to_thread(db.update_meeting, m["id"], {"memory": {"facts": facts, "changes": [], "minutes_id": art.get("id"),
                                                                    "at": time.time(), "backfilled": True}}, True)
    return facts


async def backfill(prev: List[Dict[str, Any]]) -> None:
    """Rút bù điều đã chốt cho các buổi trước chưa có (song song, tối đa BACKFILL buổi)."""
    todo = [m for m in prev if not (m.get("memory") or {}).get("at")][:BACKFILL]
    if not todo:
        return
    res = await asyncio.gather(*(_extract_facts(m) for m in todo), return_exceptions=True)
    for m, r in zip(todo, res):
        if isinstance(r, Exception):
            log.warning("meeting.group_memory: rút điều đã chốt của cuộc họp %s lỗi: %s", m["id"], r)
        else:
            m["memory"] = {"facts": r, "at": time.time()}


def changes_section(changes: List[Dict[str, Any]]) -> str:
    lines = ["## Thay Đổi So Với Các Buổi Trước"]
    for c in changes:
        when = f"ở buổi {c['before_date']}" + (f" ({c['before_title']})" if c.get("before_title") else "") if c.get("before_date") \
            else "từ trước"
        lines.append(f"- **{c['topic']}**: Dạ thưa anh chị, theo như mình đã bàn {when} thì là {c['before']} chứ không phải "
                     f"{c['now']}, anh chị có thể xem xét lại nha ạ.")
    return "\n".join(lines)


async def with_changes(mid: int, meeting: Dict[str, Any], minutes_md: str) -> str:
    """Biên bản của cuộc họp thuộc nhóm: rút điều đã chốt, so với các buổi trước; có thay đổi thì ghép mục vào cuối."""
    gid = meeting.get("group_id")
    if not gid or not (minutes_md or "").strip():
        return minutes_md
    with artifacts.purpose("nhớ xuyên cuộc họp"):
        prev = await asyncio.to_thread(previous, gid, mid, meeting.get("started_at"))
        await backfill(prev)
        index: Dict[str, Dict[str, Any]] = {}
        lines = []
        for m in prev:
            for f in (m.get("memory") or {}).get("facts") or []:
                if len(index) >= PREV_FACTS_MAX:
                    break
                code = f"P{len(index) + 1}"
                index[code] = {**f, "meeting_id": m["id"], "date": retrieval.fmt_date(m.get("started_at")),
                               "title": m.get("title") or ""}
                lines.append(f"[{code}] ({index[code]['date']} - {index[code]['title']}) {f['topic']}: {f['value']}")
        prompt = ("CÁC ĐIỀU ĐÃ CHỐT Ở CÁC BUỔI TRƯỚC:\n" + ("\n".join(lines) if lines else "(chưa có)")
                  + f"\n\nBIÊN BẢN BUỔI HỌP HÔM NAY ({retrieval.meeting_title(meeting)}):\n{minutes_md[:14000]}")
        data = artifacts._json_from_text(await artifacts._call_llm(MEMORY_SYSTEM, prompt, max_tokens=2500))
    facts = clean_facts(data)
    changes = []
    for c in (data.get("changes") if isinstance(data, dict) else None) or []:
        if not isinstance(c, dict):
            continue
        ref = index.get(_s(c.get("ref"), 10).strip("[]"))
        topic, before, now = _s(c.get("topic"), 120), _s(c.get("before"), 240), _s(c.get("now"), 240)
        if not ref or not topic or not now:
            continue                                   # chỉ nhận thay đổi đối chiếu được với một điều đã chốt cụ thể
        changes.append({"topic": topic, "before": before or ref["value"], "now": now, "ref": ref["meeting_id"],
                        "before_date": ref["date"], "before_title": ref["title"]})
    await asyncio.to_thread(db.update_meeting, mid, {"memory": {"facts": facts, "changes": changes[:15],
                                                                "at": time.time()}}, True)
    if not changes:
        return minutes_md
    return minutes_md.rstrip() + "\n\n" + changes_section(changes[:15]) + "\n"
