"""Chế độ BD (bản 3.19, giai đoạn 4): gợi ý cho đội kinh doanh ngay trong cuộc họp với khách.

- Chỉ chạy khi nhóm của cuộc họp bật "Nhóm BD" (groups.bd_mode).
- Gợi ý chỉ gửi qua kênh riêng /ws/meeting/<id>/bd tới bảng BD (mở trên máy của người trong nhóm), không qua kênh sự kiện
  chung của phòng họp (màn hình trình chiếu) và không đọc thành tiếng: khách không thấy, không nghe.
- Có câu mới thì chờ DEBOUNCE_S giây cho người nói nói hết. Lọc bằng luật đơn giản (dấu hỏi, "bao nhiêu", "thế nào", "giá",
  "phí"..., hoặc khách nói dài) để không gọi AI cho mọi câu. Tối đa một lượt mỗi MIN_GAP_S giây, MAX_INFLIGHT lượt cùng lúc.
- Mỗi lượt gọi SONG SONG 2 lời gọi AI: theo tài liệu của nhóm (K1..) và theo các cuộc họp trước (M1..). Lời gọi nào xong
  trước thì hiện trước; không có gì đáng gợi ý thì không hiện.
- Hỏi nhanh: người trong đội BD gõ câu hỏi trên bảng BD, cũng 2 lời gọi song song.
- Người trên bảng BD đánh dấu ai là Khách / Đội mình (meetings.bd.roles): chỉ gợi ý cho câu của khách.
"""
import asyncio
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
CARDS_LOAD = 60
_ASK = re.compile(r"\?|\b(bao nhieu|bao lau|bao gio|the nao|nhu nao|ra sao|khi nao|o dau|tai sao|vi sao|lam sao|duoc khong|"
                  r"co duoc|co the|co phai|phai khong|hay khong|gia|phi|chi phi|bang gia|chiet khau|uu dai|hop dong|"
                  r"thanh toan|bao hanh|doi tra|tich hop|ho tro|cam ket|thoi gian|deadline|so sanh|khac gi|loi ich|van de|"
                  r"kho khan|lo ngai|phan nan|chua)\b")

COMMON = """Bạn là trợ lý ngồi cạnh đội kinh doanh (BD) của UrBox trong cuộc họp với khách hàng, gợi ý để đội BD trả lời khách tốt
nhất. Chỉ đội BD thấy gợi ý này. Trả về DUY NHẤT một JSON, không kèm chữ nào khác:
{"question": "câu khách vừa hỏi hoặc điều khách băn khoăn, viết lại ngắn gọn; rỗng nếu không có gì cần gợi ý",
 "answer": "gợi ý 2-4 câu đội BD nói được ngay (xưng 'bên em', lịch sự, cụ thể)",
 "points": ["ý chính / con số / điều kiện cụ thể, tối đa 4"], "refs": ["mã nguồn đã dùng"], "confidence": "cao|vừa|thấp"}
Quy tắc chung: ưu tiên câu hỏi hoặc băn khoăn MỚI NHẤT của khách; câu do đội mình nói thì không cần gợi ý; nếu có câu hỏi
được đội BD gõ (HỎI NHANH) thì trả lời đúng câu đó. Tiền theo 1.000.000đ, ngày dd/mm/yyyy, không dùng gạch dài."""

KB_SYSTEM = COMMON + """
Nguồn: TÀI LIỆU CỦA NHÓM (mã K1, K2...). Chỉ dùng thông tin trong tài liệu và trong cuộc họp; không bịa giá, chính sách.
Tài liệu không có thông tin thì nói rõ "tài liệu chưa có thông tin này", gợi ý hẹn gửi lại sau, confidence "thấp"."""

HISTORY_SYSTEM = COMMON + """
Nguồn: CÁC CUỘC HỌP TRƯỚC của nhóm (biên bản và lời nói, mã M1, M2...). Nêu lần trước mình đã nói, hứa, báo giá hoặc thống
nhất gì liên quan tới điều khách đang hỏi, và điều cần lưu ý (khác với lần trước, đã hứa mà chưa làm). Không có gì liên
quan thì để "question" rỗng."""


def enabled_for(meeting: Optional[Dict[str, Any]]) -> bool:
    gid = (meeting or {}).get("group_id")
    g = groups.get_group(gid) if gid else None
    return bool(g and g.get("bd_mode"))


def _col():
    return db._get_db()["bd_cards"]


def load_cards(mid: int, limit: int = CARDS_LOAD) -> List[Dict[str, Any]]:
    return list(_col().find({"meeting_id": int(mid)}, {"_id": 0}).sort("at", -1).limit(limit))


def _src(code: str, p: Dict[str, Any]) -> Dict[str, Any]:
    out = {"code": code, "kind": p.get("kind"), "title": p.get("title"), "ref": p.get("ref")}
    for k in ("meeting_id", "doc_id"):
        if p.get(k) is not None:
            out[k] = p[k]
    return out


class BDAssistant:
    def __init__(self, s: Any):
        self.s = s
        self.subs: Set[asyncio.Queue] = set()
        self.roles: Dict[str, str] = {str(k): v for k, v in ((s.meeting.get("bd") or {}).get("roles") or {}).items()}
        self.inflight = 0
        self.last_run = 0.0
        self.last_seq = 0
        self.last_query: Set[str] = set()
        self._timer: Optional[asyncio.Task] = None
        self._tasks: Set[asyncio.Task] = set()

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

    # ------------------------------------------------------------ tự gợi ý ---
    def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def note(self, seg: Dict[str, Any]) -> None:
        """Câu mới trong cuộc họp: chờ người nói nói hết rồi xem có cần gợi ý không."""
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

    def _is_ask(self, segs: List[Dict[str, Any]]) -> bool:
        marked = bool(self.roles)
        for g in segs:
            if marked and self._role(g) == "team":
                continue                              # câu của đội mình: không cần gợi ý
            text = g.get("text") or ""
            if _ASK.search(retrieval.fold(text)) or len(text.split()) >= LONG_WORDS:
                return True
        return False

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
        """Có câu hỏi / băn khoăn mới của khách thì chạy một lượt gợi ý (giữ giới hạn tần suất)."""
        now = time.monotonic()
        # chỉ xét câu trong 90 giây gần nhất: server khởi động lại giữa buổi thì không xét lại cả buổi họp
        fresh = [g for g in self._window() if int(g.get("seq") or 0) > self.last_seq]
        if not fresh or not self.s.is_live():
            return
        if self.inflight >= MAX_INFLIGHT or now - self.last_run < MIN_GAP_S:
            wait = max(MIN_GAP_S - (now - self.last_run), 1.0)
            self._timer = self._spawn(self._later(wait))   # thử lại sau, không bỏ lỡ câu hỏi vừa rồi
            return
        self.last_seq = max(int(g.get("seq") or 0) for g in fresh)
        if not self._is_ask(fresh):
            return
        query = " ".join(g.get("text", "") for g in fresh[-4:])
        q = set(retrieval.terms(query))
        if q and self.last_query and len(q & self.last_query) / max(1, len(q | self.last_query)) > 0.8:
            return                                    # gần như trùng câu vừa gợi ý
        self.last_query, self.last_run = q, now
        # chạy thành tác vụ riêng: câu mới tới chỉ hủy bộ hẹn giờ, không hủy lượt gợi ý đang gọi AI
        self._spawn(self.run(query, typed="", by=None))

    async def ask(self, question: str, by: Optional[str]) -> List[Dict[str, Any]]:
        """Hỏi nhanh từ bảng BD."""
        question = re.sub(r"\s+", " ", question or "").strip()[:500]
        return await self.run(question, typed=question, by=by) if question else []

    def ask_soon(self, question: str, by: Optional[str]) -> None:
        self._spawn(self.ask(question, by))

    # ------------------------------------------------------------ một lượt ---
    async def run(self, query: str, typed: str = "", by: Optional[str] = None) -> List[Dict[str, Any]]:
        artifacts.set_meeting(self.s.id, "BD gợi ý")
        gid = self.s.meeting.get("group_id")
        idx = await asyncio.to_thread(retrieval.group_index, gid) if gid else retrieval.Index([])
        recent = self._window()
        search = f"{query} {typed}".strip()
        kb = idx.search(search, k=6, kinds={"kb"})
        hist = idx.search(search, k=6, kinds={"minutes", "talk"}, exclude_meeting=self.s.id)
        jobs = []
        if kb or typed:
            jobs.append(self._card("kb", KB_SYSTEM, "K", kb, recent, typed, by))
        if hist:
            jobs.append(self._card("history", HISTORY_SYSTEM, "M", hist, recent, typed, by))
        if not jobs:
            return []
        self.inflight += 1
        await self.emit(self.status())
        try:
            res = await asyncio.gather(*jobs, return_exceptions=True)
        finally:
            self.inflight -= 1
            await self.emit(self.status())
        cards = []
        for r in res:
            if isinstance(r, Exception):
                log.warning("meeting.bd: gợi ý lỗi: %s", r)
                await self.emit({"type": "bd_error", "text": f"Chưa gợi ý được: {r}"})
            elif r:
                cards.append(r)
        return cards

    async def _card(self, kind: str, system: str, prefix: str, passages: List[Dict[str, Any]],
                    recent: List[Dict[str, Any]], typed: str, by: Optional[str]) -> Optional[Dict[str, Any]]:
        refs = {f"{prefix}{i + 1}": p for i, p in enumerate(passages)}
        label = "TÀI LIỆU CỦA NHÓM" if kind == "kb" else "CÁC CUỘC HỌP TRƯỚC"
        src = "\n\n".join(f"[{code}] {p.get('title')} - {p.get('ref')}:\n{p.get('text', '')[:1500]}" for code, p in refs.items())
        prompt = (f"Cuộc họp: {self.s.title}\n\nLỜI NÓI GẦN NHẤT (90 giây):\n{self._lines(recent) or '(chưa có)'}\n\n"
                  + (f"HỎI NHANH của đội BD: {typed}\n\n" if typed else "")
                  + f"{label}:\n{src or '(nhóm chưa có tài liệu nào)'}")
        data = artifacts._json_from_text(await artifacts._call_llm(system, prompt, max_tokens=900))
        if not isinstance(data, dict):
            return None
        question = re.sub(r"\s+", " ", str(data.get("question") or "")).strip()[:300]
        answer = str(data.get("answer") or "").replace(chr(0x2014), "-").strip()[:1500]
        if not answer or (not question and not typed):
            return None
        card = {"id": uuid.uuid4().hex[:12], "meeting_id": self.s.id, "kind": kind, "auto": not typed,
                "question": typed or question, "answer": answer,
                "points": [str(x).replace(chr(0x2014), "-").strip()[:300] for x in (data.get("points") or [])
                           if str(x).strip()][:4],
                "confidence": str(data.get("confidence") or "").strip().lower()[:10],
                "sources": [_src(c, refs[c]) for c in dict.fromkeys(str(r).strip("[] ") for r in (data.get("refs") or []))
                            if c in refs][:6],
                "by": by, "at": time.time()}
        await asyncio.to_thread(_col().insert_one, dict(card))
        await self.emit({"type": "bd_card", "card": card})
        return card


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
