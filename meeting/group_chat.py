"""Trò chuyện / báo cáo của chủ nhóm BD trên toàn bộ cuộc họp của nhóm (bản 3.19, giai đoạn 4).

Ví dụ: "Báo cáo các câu phàn nàn của khách", "Khách đang gặp vấn đề gì?", "Mình đã hứa gì với Vinmart?".
1. Xếp hạng các cuộc họp của nhóm theo mức liên quan (BM25 trên tiêu đề, biên bản, lời nói). Lấy tối đa MAX_MEETINGS cuộc
   họp (ít hơn thì lấy hết), luôn kèm RECENT buổi gần nhất.
2. Đọc SONG SONG từng cuộc họp (tối đa PARALLEL lời gọi cùng lúc): rút ý liên quan câu hỏi, kèm trích dẫn và giờ.
3. Tổng hợp thành câu trả lời / báo cáo Markdown có nguồn [M1].. (tên buổi, ngày, link) và đoạn tài liệu [K1].. nếu có.
Chạy nền: tin trả lời hiện ngay ở trạng thái "đang đọc 3/8 cuộc họp...", giao diện tự cập nhật. Lưu 50 tin gần nhất.
"""
import asyncio
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from meeting import artifacts, db, group_kb, retrieval

log = logging.getLogger("meeting.group_chat")

MAX_MEETINGS = 8
RECENT = 2
PARALLEL = 4
KEEP = 50
MINUTES_CHARS = 6000
TALK_CHARS = 24000
NONE = "KHÔNG CÓ"
_TASKS: Dict[str, asyncio.Task] = {}

MAP_SYSTEM = f"""Bạn đọc MỘT cuộc họp (biên bản và lời nói) của một nhóm tại UrBox để giúp trả lời câu hỏi của chủ nhóm.
Ghi lại MỌI thông tin trong cuộc họp này liên quan tới câu hỏi: gạch đầu dòng ngắn, mỗi ý ghi người nói và giờ [mm:ss] nếu có;
trích nguyên văn ngắn khi quan trọng (phàn nàn, yêu cầu, cam kết, con số, hạn chót). Không suy diễn ngoài nội dung.
Không có gì liên quan thì trả lời đúng một dòng: {NONE}"""

REDUCE_SYSTEM = """Bạn là trợ lý phân tích cho chủ nhóm tại UrBox. Dựa vào GHI CHÚ rút từ từng cuộc họp (mã M1, M2...) và
đoạn TÀI LIỆU của nhóm (mã K1, K2...) nếu có, trả lời câu hỏi bằng tiếng Việt, định dạng Markdown:
- Mở đầu bằng kết luận 1-3 câu trả lời thẳng câu hỏi.
- Sau đó chi tiết theo chủ đề hoặc theo thời gian (dùng bảng khi liệt kê nhiều mục: vấn đề, khách / người nói, buổi, mức độ).
- Câu hỏi dạng báo cáo thì viết như báo cáo: tiêu đề, các mục, đề xuất việc cần làm.
- Mỗi ý ghi nguồn dạng [M1] hoặc [K2].
Không bịa thông tin ngoài ghi chú và tài liệu; thiếu thông tin thì nói rõ. Ngày dd/mm/yyyy, tiền theo 1.000.000đ, không
dùng gạch dài."""


def _col():
    return db._get_db()["group_chats"]


def messages(gid: int) -> List[Dict[str, Any]]:
    d = _col().find_one({"group_id": int(gid)}, {"_id": 0}) or {}
    return d.get("messages") or []


def _push(gid: int, *msgs: Dict[str, Any]) -> None:
    _col().update_one({"group_id": int(gid)},
                      {"$push": {"messages": {"$each": list(msgs), "$slice": -KEEP}}, "$set": {"updated_at": time.time()}},
                      upsert=True)


def _set(gid: int, mid: str, **fields: Any) -> None:
    _col().update_one({"group_id": int(gid), "messages.id": mid},
                      {"$set": {f"messages.$.{k}": v for k, v in fields.items()}})


def clear(gid: int) -> None:
    _col().delete_one({"group_id": int(gid)})


def pick_meetings(gid: int, question: str) -> List[Dict[str, Any]]:
    """Các cuộc họp nên đọc để trả lời: liên quan nhất theo BM25, luôn kèm vài buổi gần nhất; mới nhất trước."""
    ms = db.find_meetings({"group_id": int(gid)}, limit=200, sort="started_at")
    if len(ms) <= MAX_MEETINGS:
        return ms
    idx = retrieval.group_index(gid)
    best: Dict[int, float] = {}
    for p in idx.search(question, k=200, kinds={"minutes", "talk"}):
        best[p["meeting_id"]] = best.get(p["meeting_id"], 0.0) + p["score"]
    chosen = [m["id"] for m in ms[:RECENT]]
    for mid, _ in sorted(best.items(), key=lambda kv: kv[1], reverse=True):
        if len(chosen) >= MAX_MEETINGS:
            break
        if mid not in chosen:
            chosen.append(mid)
    for m in ms:                                   # câu hỏi không khớp từ nào: lấy thêm các buổi gần nhất
        if len(chosen) >= MAX_MEETINGS:
            break
        if m["id"] not in chosen:
            chosen.append(m["id"])
    by_id = {m["id"]: m for m in ms}
    return sorted((by_id[i] for i in chosen), key=lambda m: m.get("started_at") or 0, reverse=True)


def meeting_text(m: Dict[str, Any], question: str) -> str:
    """Biên bản + lời nói của một cuộc họp; lời nói quá dài thì giữ các khung liên quan nhất (theo thứ tự thời gian)."""
    art = retrieval.latest_minutes(m["id"])
    minutes = ((art or {}).get("content") or "")[:MINUTES_CHARS]
    wins = retrieval.transcript_windows(db.get_segments(m["id"]))
    total = sum(len(t) for _, t in wins)
    if total > TALK_CHARS:
        idx = retrieval.Index([{"kind": "talk", "text": t, "i": i} for i, (_, t) in enumerate(wins)])
        q = set(retrieval.terms(question))
        ranked = sorted(range(len(wins)), key=lambda i: idx.score(i, q), reverse=True)
        keep, n = set(), 0
        for i in ranked:
            if n + len(wins[i][1]) > TALK_CHARS:
                continue
            keep.add(i)
            n += len(wins[i][1])
        wins = [w for i, w in enumerate(wins) if i in keep]
    talk = "\n".join(t for _, t in wins)
    return (f"CUỘC HỌP: {retrieval.meeting_title(m)}\n\nBIÊN BẢN:\n{minutes or '(chưa có biên bản)'}\n\n"
            f"LỜI NÓI{' (các đoạn liên quan nhất)' if total > TALK_CHARS else ''}:\n{talk or '(không có)'}")


async def answer(gid: int, msg_id: str, question: str, owner: Optional[str]) -> None:
    artifacts.set_meeting(None, "trò chuyện nhóm")
    artifacts.set_owner(owner)
    try:
        ms = await asyncio.to_thread(pick_meetings, gid, question)
        if not ms:
            await asyncio.to_thread(_set, gid, msg_id, status="done", progress="", at=time.time(),
                                    text="Nhóm chưa có cuộc họp nào để trả lời.")
            return
        total, done = len(ms), 0
        await asyncio.to_thread(_set, gid, msg_id, progress=f"Đang đọc {total} cuộc họp (0/{total})...")
        sem = asyncio.Semaphore(PARALLEL)

        async def read(m: Dict[str, Any]) -> str:
            nonlocal done
            async with sem:
                text = await asyncio.to_thread(meeting_text, m, question)
                out = await artifacts._call_llm(MAP_SYSTEM, f"CÂU HỎI CỦA CHỦ NHÓM: {question}\n\n{text}", max_tokens=1500)
            done += 1
            await asyncio.to_thread(_set, gid, msg_id, progress=f"Đang đọc {total} cuộc họp ({done}/{total})...")
            return out

        notes = await asyncio.gather(*(read(m) for m in ms), return_exceptions=True)
        used: List[Dict[str, Any]] = []
        blocks: List[str] = []
        for m, note in zip(ms, notes):
            if isinstance(note, Exception):
                log.warning("meeting.group_chat: đọc cuộc họp %s lỗi: %s", m["id"], note)
                continue
            note = (note or "").strip()
            if not note or note.upper().startswith(NONE):
                continue
            code = f"M{len(used) + 1}"
            used.append({"code": code, "meeting_id": m["id"], "title": m.get("title") or "Cuộc họp",
                         "date": retrieval.fmt_date(m.get("started_at"))})
            blocks.append(f"[{code}] {retrieval.meeting_title(m)}:\n{note}")
        kb = []
        if group_kb.signature(gid)[0]:
            idx = await asyncio.to_thread(retrieval.group_index, gid)
            kb = idx.search(question, k=5, kinds={"kb"})
        kb_text = "\n\n".join(f"[K{i + 1}] {p['title']} - {p['ref']}:\n{p['text'][:1200]}" for i, p in enumerate(kb))
        await asyncio.to_thread(_set, gid, msg_id, progress="Đang tổng hợp câu trả lời...")
        prompt = (f"CÂU HỎI CỦA CHỦ NHÓM: {question}\n\nĐã đọc {total} cuộc họp, {len(used)} cuộc họp có thông tin liên quan.\n\n"
                  f"GHI CHÚ TỪ CÁC CUỘC HỌP:\n{chr(10).join(blocks) if blocks else '(không cuộc họp nào có thông tin liên quan)'}"
                  + (f"\n\nTÀI LIỆU CỦA NHÓM:\n{kb_text}" if kb_text else ""))
        text = await artifacts._call_llm(REDUCE_SYSTEM, prompt, max_tokens=3500)
        sources = used + [{"code": f"K{i + 1}", "doc_id": p.get("doc_id"), "title": p["title"], "ref": p["ref"]}
                          for i, p in enumerate(kb)]
        await asyncio.to_thread(_set, gid, msg_id, status="done", progress="", text=text.strip(), sources=sources,
                                read=total, at=time.time())
    except Exception as e:
        log.warning("meeting.group_chat: trả lời câu hỏi của nhóm %s lỗi: %s", gid, e)
        await asyncio.to_thread(_set, gid, msg_id, status="error", progress="", text=f"Chưa trả lời được: {e}",
                                at=time.time())


def prepare(gid: int, question: str, owner: Optional[str]) -> Dict[str, Any]:
    """Ghi câu hỏi và một tin trả lời đang chờ. Trả về tin trả lời (kèm câu hỏi để chạy phần trả lời)."""
    question = re.sub(r"\s+", " ", question or "").strip()[:1000]
    if not question:
        raise ValueError("Câu hỏi trống")
    if any(m.get("status") == "pending" for m in messages(gid)):
        raise ValueError("Đang trả lời câu hỏi trước, chờ xong rồi hỏi tiếp")
    now = time.time()
    q = {"id": uuid.uuid4().hex[:12], "role": "user", "text": question, "by": owner, "at": now}
    a = {"id": uuid.uuid4().hex[:12], "role": "assistant", "status": "pending", "text": "", "question": question,
         "progress": "Đang tìm các cuộc họp liên quan...", "sources": [], "at": now}
    _push(gid, q, a)
    return a


def start(gid: int, msg: Dict[str, Any], owner: Optional[str]) -> "asyncio.Task":
    """Chạy phần trả lời ở nền (đọc song song các cuộc họp rồi tổng hợp)."""
    t = asyncio.create_task(answer(gid, msg["id"], msg["question"], owner))
    _TASKS[msg["id"]] = t
    t.add_done_callback(lambda _t, k=msg["id"]: _TASKS.pop(k, None))
    return t
