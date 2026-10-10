"""Chế độ BD (bản 3.19, giai đoạn 4): khung chat gợi ý cho đội kinh doanh ngay trong cuộc họp với khách.

- Chỉ chạy khi nhóm của cuộc họp bật "Nhóm BD" (groups.bd_mode).
- Mọi thứ chỉ gửi qua kênh riêng /ws/meeting/<id>/bd tới bảng BD (mở trên máy của người trong nhóm), không qua kênh sự
  kiện chung của phòng họp (màn hình trình chiếu) và không đọc thành tiếng: khách không thấy, không nghe.
- Bảng BD là một khung chat. Mỗi lượt là một câu hỏi và câu trả lời gợi ý:
  - "auto": khách hỏi trong cuộc họp, trợ lý tự nhận ra (chờ DEBOUNCE_S giây cho khách nói hết; lọc bằng luật đơn giản:
    dấu hỏi, "bao nhiêu", "thế nào", "giá", "phí"..., hoặc khách nói dài; AI bỏ qua lời chào, câu xác nhận);
  - "typed": đội BD gõ vào khung chat (hiểu được câu hỏi nối tiếp nhờ vài lượt trước);
  - "marked": đội BD bấm vào một câu trong lời nói để hỏi trợ lý (khi trợ lý bỏ sót).
- Mỗi câu hỏi MỘT câu trả lời gộp tài liệu của nhóm (K1..) và các cuộc họp trước (M1..), đánh dấu nguồn ngay sau ý dùng
  nguồn, kèm ghi chú cho đội BD (điều đã nói / hứa / báo giá ở buổi trước, chỗ khác với tài liệu, thông tin còn thiếu).
- Câu hỏi hiện ngay trên khung chat (đang tìm câu trả lời), câu trả lời tới sau. Nhiều câu hỏi chạy song song (tự nhận ra
  tối đa MAX_INFLIGHT cùng lúc, mỗi MIN_GAP_S giây một lượt; câu gõ / đánh dấu không giới hạn).
- Người trên bảng BD đánh dấu ai là Khách / Đội mình (meetings.bd.roles): chỉ tự nhận câu hỏi của khách.
- "Tra thêm trên mạng" (10/10/2026): đội BD bấm ở một câu trả lời thì trợ lý tra internet cho câu hỏi đó (chỉ khi bấm:
  mỗi lượt mất khoảng 15-30 giây và tính phí), tóm tắt thành khối "Trên mạng" có nguồn W1, W2... gắn vào cùng lượt
  (turn["web"]). Thông tin trên mạng chưa được kiểm chứng, không thay cho giá / chính sách trong tài liệu của nhóm.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from meeting import artifacts, db, groups, retrieval

log = logging.getLogger("meeting.bd")

DEBOUNCE_S = 1.5
MIN_GAP_S = 6.0
MAX_INFLIGHT = 2
WINDOW_S = 90.0
WINDOW_LINES = 14
LONG_WORDS = 25
TURNS_LOAD = 80
HISTORY_TURNS = 3
K_SOURCES = 5
M_SOURCES = 5
_ASK = re.compile(r"\?|\b(bao nhieu|bao lau|bao gio|the nao|nhu nao|ra sao|khi nao|o dau|tai sao|vi sao|lam sao|duoc khong|"
                  r"co duoc|co the|co phai|phai khong|hay khong|gia|phi|chi phi|bang gia|chiet khau|uu dai|hop dong|"
                  r"thanh toan|bao hanh|doi tra|tich hop|ho tro|cam ket|thoi gian|deadline|so sanh|khac gi|loi ich|van de|"
                  r"kho khan|lo ngai|phan nan|chua)\b")
_CODE = re.compile(r"\[\s*([KM]\d{1,2})\s*\]")
_WCODE = re.compile(r"\[\s*(W\d{1,2})\s*\]")
W_SOURCES = 6
W_SEARCHES = 3                  # số lượt tìm tối đa mỗi lần tra (công cụ của Claude): nhanh hơn, rẻ hơn
WEB_TIMEOUT_S = 180

BD_SYSTEM = """Bạn là trợ lý ngồi cạnh đội kinh doanh (BD) của UrBox trong cuộc họp với khách hàng, trả lời trong một khung chat chỉ
đội BD thấy. Mỗi lượt có một CÂU HỎI (khách vừa hỏi trong cuộc họp, hoặc đội BD hỏi trên khung chat) và các nguồn:
- TÀI LIỆU CỦA NHÓM, mã K1, K2... (bảng giá, chính sách, FAQ, hồ sơ năng lực...);
- CÁC CUỘC HỌP TRƯỚC của nhóm, mã M1, M2... (biên bản và lời nói, có tên buổi và ngày).
Trả về DUY NHẤT một JSON, không kèm chữ nào khác:
{"question": "câu hỏi viết lại ngắn gọn, rõ ý; rỗng nếu thực ra không có câu hỏi hay băn khoăn nào cần trả lời",
 "answer": "câu trả lời gợi ý đội BD nói được ngay: 2-5 câu, xưng 'bên em', lịch sự, cụ thể. Đánh dấu nguồn ngay sau ý dùng nguồn, ví dụ: Phí tích hợp là 5.000.000đ [K1].",
 "notes": ["ghi chú cho đội BD (khách không nghe), tối đa 3, mỗi ghi chú có mã nguồn nếu có, ví dụ: Buổi 02/10 anh Bình đã báo 4.000.000đ [M1], nên giải thích phần chênh lệch."],
 "confidence": "cao|vừa|thấp"}
Quy tắc:
- Chỉ dùng thông tin trong nguồn và trong cuộc họp; không bịa giá, chính sách, cam kết. Thiếu thông tin thì nói trong answer
  là bên em sẽ kiểm tra và gửi lại anh chị sau, confidence "thấp".
- Tài liệu và cuộc họp trước nói khác nhau (giá, thời hạn...) thì answer theo tài liệu, ghi rõ khác biệt trong notes.
- Notes dùng cho: điều đã nói, hứa, báo giá, thống nhất ở buổi trước; điều đã hứa mà chưa làm; điều cần hỏi thêm khách.
- Câu tự nhận ra từ lời nói (nguồn câu hỏi: KHÁCH NÓI) mà thực ra không phải câu hỏi hay băn khoăn của khách (lời chào, câu
  xác nhận, câu của đội mình) thì để "question" rỗng.
- Câu đội BD hỏi trên khung chat thì luôn trả lời; câu hỏi nối tiếp thì hiểu theo HỘI THOẠI TRƯỚC TRÊN KHUNG CHAT.
- Tiền theo 1.000.000đ, ngày dd/mm/yyyy, không dùng gạch dài."""

BD_WEB_SYSTEM = """Bạn là trợ lý ngồi cạnh đội kinh doanh (BD) của UrBox trong cuộc họp với khách hàng. Đội BD vừa bấm "Tra thêm
trên mạng" cho một CÂU HỎI. Bên dưới là kết quả tra cứu trên internet (nguồn mã W1, W2...) và câu trả lời trước đó lấy
từ tài liệu nội bộ của nhóm (nếu có).
Trả về DUY NHẤT một JSON, không kèm chữ nào khác:
{"answer": "2-4 câu, tối đa 80 từ, nêu điều tìm được trên mạng liên quan câu hỏi, cụ thể (con số, thời điểm). Đánh dấu nguồn ngay sau ý, ví dụ: Tỷ giá USD bán ra ngày 10/10/2026 khoảng 26.070đ [W1].",
 "notes": ["ghi chú cho đội BD, tối đa 3 ghi chú, mỗi ghi chú tối đa 25 từ: thời điểm và độ tin cậy của số liệu, chỗ khác với tài liệu nội bộ, điều cần kiểm tra trước khi nói với khách"],
 "confidence": "cao|vừa|thấp"}
Quy tắc:
- Chỉ dùng thông tin trong kết quả tra cứu; không bịa. Gắn mã W cho ý có trong đoạn trích của nguồn đó, hoặc khi phần
  tóm tắt của công cụ tìm kiếm ghi rõ ý đó lấy từ trang nào (tên báo, tên miền trùng với nguồn W). Kết quả không trả lời
  được câu hỏi thì nói thẳng là chưa tìm thấy trên mạng, confidence "thấp".
- Đây là thông tin công khai trên internet, UrBox chưa xác nhận: không biến nó thành giá, chính sách hay cam kết của
  UrBox. Giá, chính sách của UrBox luôn theo tài liệu nội bộ; trên mạng nói khác thì ghi trong notes.
- Ghi rõ thời điểm của số liệu; nguồn không ghi thời điểm thì nói "chưa rõ thời điểm". Ưu tiên nguồn chính thức (trang
  của doanh nghiệp, cơ quan nhà nước) hơn báo và trang tổng hợp.
- Tiền theo 1.000.000đ, ngày dd/mm/yyyy, không dùng gạch dài."""


def enabled_for(meeting: Optional[Dict[str, Any]]) -> bool:
    gid = (meeting or {}).get("group_id")
    g = groups.get_group(gid) if gid else None
    return bool(g and g.get("bd_mode"))


def _col():
    return db._get_db()["bd_turns"]


def load_turns(mid: int, limit: int = TURNS_LOAD) -> List[Dict[str, Any]]:
    """Các lượt hỏi đáp của bảng BD (cũ trước, mới sau), bỏ các câu trợ lý thấy không phải câu hỏi."""
    rows = list(_col().find({"meeting_id": int(mid), "status": {"$ne": "none"}}, {"_id": 0})
                .sort([("at", -1), ("_id", -1)]).limit(limit))       # _id: thứ tự ghi khi hai câu hỏi cùng một lúc
    return rows[::-1]


def _src(code: str, p: Dict[str, Any]) -> Dict[str, Any]:
    out = {"code": code, "kind": p.get("kind"), "title": p.get("title"), "ref": p.get("ref")}
    for k in ("meeting_id", "doc_id"):
        if p.get(k) is not None:
            out[k] = p[k]
    return out


def _clean(v: Any, n: int) -> str:
    return str(v or "").replace(chr(0x2014), "-").strip()[:n]


class BDAssistant:
    def __init__(self, s: Any):
        self.s = s
        self.subs: Set[asyncio.Queue] = set()
        self.roles: Dict[str, str] = {str(k): v for k, v in ((s.meeting.get("bd") or {}).get("roles") or {}).items()}
        self.inflight = 0
        self.auto_inflight = 0
        self.last_run = 0.0
        self.last_seq = 0
        self.last_query: Set[str] = set()
        self._timer: Optional[asyncio.Task] = None
        self._tasks: Set[asyncio.Task] = set()
        self._web_ids: Set[str] = set()                 # lượt đang tra trên mạng (bấm hai lần không tra hai lần)

    # ------------------------------------------------------------ kênh riêng ---
    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    async def emit(self, ev: Dict[str, Any]) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def status(self) -> Dict[str, Any]:
        return {"type": "bd_status", "busy": self.inflight}

    async def set_role(self, sid: int, role: str) -> Dict[str, str]:
        if role not in ("client", "team", ""):
            raise ValueError("Vai trò là client, team hoặc để trống")
        if role:
            self.roles[str(int(sid))] = role
        else:
            self.roles.pop(str(int(sid)), None)
        self.s.meeting.setdefault("bd", {})["roles"] = dict(self.roles)
        await asyncio.to_thread(db.update_meeting, self.s.id, {"bd.roles": dict(self.roles)}, True)
        await self.emit({"type": "bd_roles", "roles": dict(self.roles)})
        return dict(self.roles)

    # ------------------------------------------------------------ tác vụ nền ---
    def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def drain(self, timeout: float = 60.0) -> None:
        """Chờ các câu trả lời đang chạy xong (dùng khi test / kết thúc)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            busy = [t for t in self._tasks if t is not asyncio.current_task() and t is not self._timer and not t.done()]
            if not busy:
                return
            await asyncio.wait(busy, timeout=max(0.01, end - time.monotonic()))

    # ------------------------------------------------------------ lời nói trong cuộc họp ---
    def note(self, seg: Dict[str, Any]) -> None:
        """Câu mới trong cuộc họp: chờ người nói nói hết rồi xem khách có hỏi gì không."""
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = self._spawn(self._later(DEBOUNCE_S))

    async def _later(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self.check()

    def _role(self, seg: Dict[str, Any]) -> str:
        return self.roles.get(str(seg.get("speaker_key")), "")

    def _candidates(self, segs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Câu có thể là câu hỏi / băn khoăn của khách (bỏ câu của đội mình khi đã đánh dấu vai trò)."""
        marked = bool(self.roles)
        out = []
        for g in segs:
            if marked and self._role(g) == "team":
                continue
            text = g.get("text") or ""
            if _ASK.search(retrieval.fold(text)) or len(text.split()) >= LONG_WORDS:
                out.append(g)
        return out

    def _window(self) -> List[Dict[str, Any]]:
        segs = [g for g in self.s.segments if (g.get("text") or "").strip()]
        if not segs:
            return []
        end = float(segs[-1].get("t_end") or 0)
        return [g for g in segs if float(g.get("t_end") or 0) >= end - WINDOW_S][-WINDOW_LINES:]

    def _lines(self, segs: List[Dict[str, Any]]) -> str:
        name = {"client": "Khách", "team": "Đội mình"}
        out = []
        for g in segs:
            r = name.get(self._role(g), "")
            out.append(f"[{retrieval.clock(g.get('t_start'))}] {g.get('speaker_label') or 'Không rõ'}"
                       f"{f' ({r})' if r else ''}: {g.get('text', '').strip()}")
        return "\n".join(out)

    async def check(self) -> None:
        """Khách vừa hỏi / băn khoăn thì mở một lượt trên khung chat (giữ giới hạn tần suất)."""
        now = time.monotonic()
        # chỉ xét câu trong 90 giây gần nhất: server khởi động lại giữa buổi thì không xét lại cả buổi họp
        fresh = [g for g in self._window() if int(g.get("seq") or 0) > self.last_seq]
        if not fresh or not self.s.is_live():
            return
        if self.auto_inflight >= MAX_INFLIGHT or now - self.last_run < MIN_GAP_S:
            wait = max(MIN_GAP_S - (now - self.last_run), 1.0)
            self._timer = self._spawn(self._later(wait))   # thử lại sau, không bỏ lỡ câu hỏi vừa rồi
            return
        self.last_seq = max(int(g.get("seq") or 0) for g in fresh)
        cands = self._candidates(fresh)
        if not cands:
            return
        last = cands[-1]
        same = [g for g in cands[-3:] if g.get("speaker_key") == last.get("speaker_key")]   # khách hỏi trong vài câu liền
        question = " ".join((g.get("text") or "").strip() for g in same)[:500]
        q = set(retrieval.terms(question))
        if q and self.last_query and len(q & self.last_query) / max(1, len(q | self.last_query)) > 0.8:
            return                                    # gần như trùng câu vừa trả lời
        self.last_query, self.last_run = q, now
        # tác vụ riêng: câu mới tới chỉ hủy bộ hẹn giờ, không cắt ngang lượt đang ghi lên khung chat
        self._spawn(self.open_turn("auto", question, seg=last))

    # ------------------------------------------------------------ một lượt hỏi đáp ---
    async def open_turn(self, source: str, question: str, seg: Optional[Dict[str, Any]] = None,
                        by: Optional[str] = None) -> Dict[str, Any]:
        """Ghi câu hỏi lên khung chat ngay (đang tìm câu trả lời), câu trả lời chạy nền rồi cập nhật cùng lượt đó."""
        question = re.sub(r"\s+", " ", question or "").strip()[:500]
        turn = {"id": uuid.uuid4().hex[:12], "meeting_id": self.s.id, "source": source, "question": question,
                "asked": question, "speaker": (seg or {}).get("speaker_label") or "", "seq": (seg or {}).get("seq"),
                "t": (seg or {}).get("t_start"), "status": "pending", "answer": "", "notes": [], "sources": [],
                "confidence": "", "by": by, "at": time.time()}
        await asyncio.to_thread(_col().insert_one, dict(turn))
        await self.emit({"type": "bd_turn", "turn": dict(turn)})       # bản sao: lượt còn được cập nhật khi có câu trả lời
        self._spawn(self._answer(turn))
        return dict(turn)

    async def ask(self, question: str, by: Optional[str]) -> Optional[Dict[str, Any]]:
        """Đội BD hỏi trên khung chat."""
        question = re.sub(r"\s+", " ", question or "").strip()
        return await self.open_turn("typed", question, by=by) if question else None

    async def mark(self, seq: int, by: Optional[str]) -> Dict[str, Any]:
        """Đội BD bấm vào một câu trong lời nói để hỏi trợ lý (trợ lý bỏ sót câu hỏi đó)."""
        seg = next((g for g in self.s.segments if g.get("seq") == seq), None)
        if seg is None or not (seg.get("text") or "").strip():
            raise KeyError("Không tìm thấy câu này trong cuộc họp")
        return await self.open_turn("marked", seg["text"], seg=seg, by=by)

    def _history(self, before: float) -> str:
        """Vài lượt hỏi đáp gần nhất trên khung chat (để hiểu câu hỏi nối tiếp)."""
        rows = list(_col().find({"meeting_id": self.s.id, "status": "done", "at": {"$lt": before}}, {"_id": 0})
                    .sort([("at", -1), ("_id", -1)]).limit(HISTORY_TURNS))[::-1]
        return "\n".join(f"- Hỏi: {r.get('question')}\n  Đáp: {_CODE.sub('', r.get('answer') or '')[:400]}" for r in rows)

    async def _answer(self, turn: Dict[str, Any]) -> None:
        auto = turn["source"] == "auto"
        self.inflight += 1
        if auto:
            self.auto_inflight += 1
        await self.emit(self.status())
        try:
            artifacts.set_meeting(self.s.id, "BD gợi ý")
            gid = self.s.meeting.get("group_id")
            idx = await asyncio.to_thread(retrieval.group_index, gid) if gid else retrieval.Index([])
            history = "" if auto else await asyncio.to_thread(self._history, turn["at"])
            search = f"{turn['question']} {history[-300:] if history else ''}".strip()
            kb = idx.search(search, k=K_SOURCES, kinds={"kb"})
            hist = idx.search(search, k=M_SOURCES, kinds={"minutes", "talk"}, exclude_meeting=self.s.id)
            refs = {f"K{i + 1}": p for i, p in enumerate(kb)}
            refs.update({f"M{i + 1}": p for i, p in enumerate(hist)})
            src = "\n\n".join(f"[{c}] {p.get('title')} - {p.get('ref')}:\n{p.get('text', '')[:1400]}" for c, p in refs.items())
            label = {"auto": "KHÁCH NÓI, trợ lý tự nhận ra từ lời nói", "typed": "ĐỘI BD HỎI TRÊN KHUNG CHAT",
                     "marked": "ĐỘI BD ĐÁNH DẤU CÂU NÀY TRONG LỜI NÓI"}[turn["source"]]
            prompt = (f"Cuộc họp: {self.s.title}\n\nLỜI NÓI GẦN NHẤT (90 giây):\n{self._lines(self._window()) or '(chưa có)'}\n\n"
                      + (f"HỘI THOẠI TRƯỚC TRÊN KHUNG CHAT:\n{history}\n\n" if history else "")
                      + f"CÂU HỎI ({label}): {turn['question']}\n\n"
                      + f"NGUỒN:\n{src or '(nhóm chưa có tài liệu, chưa có cuộc họp trước liên quan)'}")
            data = artifacts._json_from_text(await artifacts._call_llm(BD_SYSTEM, prompt, max_tokens=1000))
            data = data if isinstance(data, dict) else {}
            question = re.sub(r"\s+", " ", str(data.get("question") or "")).strip()[:300]
            answer = _clean(data.get("answer"), 1500)
            if auto and (not question or not answer):
                upd = {"status": "none"}                   # trợ lý thấy không phải câu hỏi: gỡ khỏi khung chat
            elif not answer:
                upd = {"status": "error", "answer": "Chưa có câu trả lời, anh chị hỏi lại giúp em."}
            else:
                notes = [_clean(x, 400) for x in (data.get("notes") or []) if _clean(x, 400)][:3]
                used = list(dict.fromkeys(_CODE.findall(" ".join([answer] + notes))))
                upd = {"status": "done", "question": question or turn["question"], "answer": answer, "notes": notes,
                       "sources": [_src(c, refs[c]) for c in used if c in refs][:8],
                       "confidence": _clean(data.get("confidence"), 10).lower()}
        except Exception as e:
            log.warning("meeting.bd: trả lời câu hỏi lỗi: %s", e)
            upd = {"status": "error", "answer": f"Chưa trả lời được: {e}"}
        finally:
            self.inflight -= 1
            if auto:
                self.auto_inflight -= 1
        upd["done_at"] = time.time()
        turn.update(upd)
        await asyncio.to_thread(_col().update_one, {"id": turn["id"]}, {"$set": upd})
        await self.emit({"type": "bd_turn", "turn": dict(turn)})
        await self.emit(self.status())

    # ------------------------------------------------------------ tra thêm trên mạng ---
    async def search_web(self, turn_id: str, by: Optional[str]) -> Dict[str, Any]:
        """Đội BD bấm "Tra thêm trên mạng" ở một lượt: tra internet cho câu hỏi đó, chạy nền, kết quả vào turn["web"]."""
        turn = await asyncio.to_thread(_col().find_one, {"meeting_id": self.s.id, "id": str(turn_id)}, {"_id": 0})
        if turn is None or turn.get("status") == "none":
            raise KeyError("Không tìm thấy câu hỏi này trên khung chat")
        if turn.get("status") == "pending":
            raise ValueError("Trợ lý đang trả lời câu này, anh chị chờ xong rồi tra thêm trên mạng")
        if turn["id"] in self._web_ids:
            return turn                                    # đang tra rồi: không tra (và tính phí) thêm lần nữa
        self._web_ids.add(turn["id"])
        web = {"status": "pending", "by": by, "at": time.time()}
        turn["web"] = web
        await asyncio.to_thread(_col().update_one, {"id": turn["id"]}, {"$set": {"web": web}})
        await self.emit({"type": "bd_turn", "turn": dict(turn)})
        self._spawn(self._web(turn))
        return dict(turn)

    async def _web(self, turn: Dict[str, Any]) -> None:
        from meeting import websearch
        self.inflight += 1
        await self.emit(self.status())
        question = turn.get("question") or turn.get("asked") or ""
        asked = turn.get("web") or {}
        web: Dict[str, Any] = {"status": "error", "by": asked.get("by"), "at": asked.get("at")}
        try:
            artifacts.set_meeting(self.s.id, "BD tra cứu web")
            res = await asyncio.wait_for(artifacts.web_search_tool(question, max_uses=W_SEARCHES, basic=True), WEB_TIMEOUT_S)
            if res.get("error"):
                raise RuntimeError(res["error"])
            refs = {f"W{i + 1}": s for i, s in enumerate((res.get("sources") or [])[:W_SOURCES])}
            if not refs:
                raise RuntimeError("không tìm được trang nào phù hợp trên mạng")
            src = "\n\n".join(
                f"[{c}] {s.get('title') or ''} - {s.get('domain') or s.get('url')}"
                + (f" (đăng/cập nhật: {s['published']})" if s.get("published") else "")
                + f":\n{(s.get('excerpt') or '(không có đoạn trích)')[:1500]}" for c, s in refs.items())
            internal = _CODE.sub("", turn.get("answer") or "").strip() if turn.get("status") == "done" else ""
            prompt = (f"Thời điểm hiện tại: {time.strftime('%H:%M %d/%m/%Y')}\nCuộc họp: {self.s.title}\n\n"
                      f"CÂU HỎI: {question}\n\n"
                      f"CÂU TRẢ LỜI TỪ TÀI LIỆU NỘI BỘ: {internal or '(chưa có)'}\n\n"
                      + (f"TÓM TẮT CỦA CÔNG CỤ TÌM KIẾM (không có mã nguồn):\n{res['summary'][:2500]}\n\n" if res.get("summary") else "")
                      + f"KẾT QUẢ TRA CỨU TRÊN MẠNG:\n{src}")
            raw = await artifacts._call_llm(BD_WEB_SYSTEM, prompt, max_tokens=3000)
            data = artifacts._json_from_text(raw)
            data = data if isinstance(data, dict) else {}
            if not data.get("answer"):                     # JSON bị cắt giữa chừng: vẫn lấy được câu trả lời đã viết xong
                m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', raw or "")
                data = {"answer": json.loads(f'"{m.group(1)}"')} if m else {}
            answer = _clean(data.get("answer"), 1500)
            if not answer:
                raise RuntimeError("chưa tổng hợp được kết quả tra cứu")
            notes = [_clean(x, 400) for x in (data.get("notes") or []) if _clean(x, 400)][:3]
            used = [c for c in dict.fromkeys(_WCODE.findall(" ".join([answer] + notes))) if c in refs]

            def _ws(c: str) -> Dict[str, Any]:
                s = refs[c]
                url = str(s.get("url") or "")
                return {"code": c, "title": _clean(s.get("title"), 160), "domain": s.get("domain") or websearch.domain_of(url),
                        "url": url if re.match(r"https?://", url, re.I) else ""}
            web.update({"status": "done", "answer": answer, "notes": notes,
                        "confidence": _clean(data.get("confidence"), 10).lower(),
                        "sources": [_ws(c) for c in (used or list(refs))], "engine": res.get("engine") or ""})
        except Exception as e:
            log.warning("meeting.bd: tra cứu trên mạng lỗi: %s", e)
            web["error"] = _clean(e, 300) or "lỗi không rõ"
        finally:
            self.inflight -= 1
            self._web_ids.discard(turn["id"])
        web["done_at"] = time.time()
        turn["web"] = web
        await asyncio.to_thread(_col().update_one, {"id": turn["id"]}, {"$set": {"web": web}})
        await self.emit({"type": "bd_turn", "turn": dict(turn)})
        await self.emit(self.status())


def assistant(s: Any) -> Optional[BDAssistant]:
    """Trợ lý BD của phiên họp (tạo khi cần); nhóm không bật BD thì None."""
    if not enabled_for(s.meeting):
        return None
    if getattr(s, "bd", None) is None:
        s.bd = BDAssistant(s)
    return s.bd


def on_segment(s: Any, seg: Dict[str, Any]) -> None:
    a = assistant(s)
    if a is not None and s.is_live():
        a.note(seg)
