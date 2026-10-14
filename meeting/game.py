"""Trò chơi có thưởng trong cuộc họp (14/10/2026): trợ lý thiết kế trò chơi và tự làm người dẫn (MC).

"Jarvis ơi, tạo một trò chơi có thưởng để mọi người tương tác với em" -> trợ lý soạn trò chơi (sản phẩm loại "game"),
đưa lên màn hình trình chiếu và dẫn: mở màn, luật chơi, phần thưởng; đọc từng câu hỏi, đếm ngược, công bố đáp án kèm giải
thích, bảng điểm; cuối cùng vinh danh người thắng trên bục và quay số may mắn cho một người chơi khác.

Hai thể thức:
- "classic" (Đấu trường tri thức): trả lời đúng được 100 điểm, càng nhanh càng được thêm (tối đa 50).
- "golden_bell" (Rung chuông vàng): trả lời sai hoặc không trả lời là bị loại, người trụ lại cuối cùng thắng; cả phòng
  cùng bị loại ở một câu thì "cứu trợ" cho những người vừa bị loại ở câu đó.

Hai cách chơi, cùng một bảng điểm:
- Nói đáp án ("đáp án B", "câu C", "bê", "đúng" / "sai"): ứng dụng biết ai nói nhờ nhận ra giọng (tên theo hồ sơ giọng
  nói, hoặc Người nói N), người trong Google Meet cũng chơi được qua bot. Chỉ nhận câu trả lời trong lúc đếm ngược (mic
  của máy đang đọc câu hỏi gửi khoảng lặng nên trợ lý không tự trả lời).
- Quét mã QR trên màn hình để chơi trên điện thoại (/play/<mã>, không cần đăng nhập; mã ngẫu nhiên của từng lượt chơi).
Phần thưởng chỉ hiện và đọc lên: ban tổ chức trao quà, ứng dụng không phát voucher thật.
"""
import asyncio
import logging
import random
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from meeting import retrieval

log = logging.getLogger("meeting.game")

FORMATS = ("classic", "golden_bell")
LETTERS = "ABCD"
ANSWER_S = {"choice": 20, "truefalse": 12}
GRACE_S = 2.0                  # câu nói xong sát giờ: Soniox chốt chữ chậm 1-2 giây
REVEAL_S = 5.0
LOBBY_MAX_S = 15 * 60
MAX_PLAYERS = 300
MAX_QUESTIONS = 12
KEEP_ENDED_S = 30 * 60         # lượt đã xong: điện thoại vẫn xem được hạng và người thắng trong 30 phút
GAMES: Dict[str, Any] = {}     # mã chơi trên điện thoại -> lượt đang mở (GameRun) hoặc kết quả lượt đã xong (EndedGame)

GAME_SYSTEM = """Bạn là người thiết kế và dẫn trò chơi (MC) cho cuộc họp của UrBox (nền tảng quà tặng, e-voucher, loyalty tại
Việt Nam). Thiết kế MỘT trò chơi đố vui có thưởng để mọi người trong phòng cùng chơi: trả lời bằng giọng nói ("đáp án B")
hoặc bấm trên điện thoại.
Trả về DUY NHẤT một JSON, không kèm chữ nào khác:
{"title": "tên trò chơi ngắn, vui, ấn tượng",
 "format": "classic" hoặc "golden_bell",
 "theme": "chủ đề, 1 câu ngắn",
 "intro": "lời mở màn của MC: 2-3 câu sôi nổi, xưng em, gọi mọi người là cả nhà hoặc anh chị",
 "rules": ["3 luật chơi ngắn, mỗi luật tối đa 15 từ"],
 "prize": "phần thưởng theo yêu cầu của người dùng; không có thì 'Phần quà bí mật từ UrBox'",
 "questions": [{"q": "câu hỏi tối đa 25 từ", "options": ["phương án", "phương án", "phương án", "phương án"],
                "answer": 0, "explain": "1 câu giải thích vui", "type": "choice"},
               {"q": "...", "options": ["Đúng", "Sai"], "answer": 1, "explain": "...", "type": "truefalse"}],
 "outro": "lời kết chúc mừng người thắng: 1-2 câu"}
Quy tắc:
- 6 câu (người dùng yêu cầu số câu khác thì theo, tối đa 12), dễ trước khó sau, xen 1-2 câu Đúng/Sai.
- Mỗi phương án tối đa 8 từ, chỉ một đáp án đúng; "answer" là vị trí phương án đúng (0 là A). Đổi chỗ đáp án đúng giữa các câu.
- Nội dung: ưu tiên điều đã nói trong cuộc họp và dữ liệu được cung cấp (chính xác, kiểm chứng được), thêm kiến thức
  chung chắc chắn đúng (quà tặng, voucher, văn hóa Việt Nam, công nghệ). KHÔNG bịa số liệu, khách hàng, sản phẩm của
  UrBox nếu không có trong dữ liệu.
- "golden_bell" (Rung chuông vàng: sai là bị loại, ai trụ lại cuối cùng thắng) khi người dùng nói rung chuông vàng, loại
  trực tiếp, sinh tồn; còn lại "classic" (đúng và nhanh được nhiều điểm).
- Vui vẻ, lịch sự; không đùa về ngoại hình, tôn giáo, chính trị. Tiền dạng 200.000đ, không dùng gạch dài."""


# --------------------------------------------------------------- thiết kế ---
def _clean(v: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "").replace(chr(0x2014), "-")).strip()[:n]


def normalize(data: Any) -> Dict[str, Any]:
    """JSON trợ lý trả về -> trò chơi dùng được (bỏ câu hỏng, đáp án ngoài khoảng). Không có câu nào: ValueError."""
    if not isinstance(data, dict):
        raise ValueError("AI chưa soạn được trò chơi")
    qs = []
    for q in (data.get("questions") or [])[:MAX_QUESTIONS]:
        if not isinstance(q, dict):
            continue
        text = _clean(q.get("q") or q.get("question"), 220)
        opts = [_clean(o, 80) for o in (q.get("options") or []) if _clean(o, 80)][:4]
        try:
            ans = int(q.get("answer"))
        except (TypeError, ValueError):
            continue
        if not text or len(opts) < 2 or not 0 <= ans < len(opts):
            continue
        kind = "truefalse" if len(opts) == 2 and q.get("type") != "choice" else "choice"
        qs.append({"q": text, "options": opts, "answer": ans, "explain": _clean(q.get("explain"), 240), "type": kind})
    if not qs:
        raise ValueError("Trò chơi chưa có câu hỏi hợp lệ")
    fmt = data.get("format") if data.get("format") in FORMATS else "classic"
    return {"title": _clean(data.get("title"), 80) or "Đố vui có thưởng", "format": fmt,
            "theme": _clean(data.get("theme"), 160), "intro": _clean(data.get("intro"), 400),
            "rules": [_clean(r, 120) for r in (data.get("rules") or []) if _clean(r, 120)][:4],
            "prize": _clean(data.get("prize"), 160) or "Phần quà bí mật từ UrBox",
            "questions": qs, "outro": _clean(data.get("outro"), 300)}


# --------------------------------------------------------------- nhận đáp án bằng giọng ---
_LETTER = {"a": 0, "b": 1, "bê": 1, "be": 1, "bi": 1, "c": 2, "xê": 2, "xe": 2, "cê": 2, "si": 2, "d": 3, "đê": 3,
           "dê": 3, "đi": 3, "di": 3}
_NUM = {"1": 0, "một": 0, "2": 1, "hai": 1, "3": 2, "ba": 2, "4": 3, "bốn": 3, "tư": 3}
_PREFIX = r"(?:đáp án|câu|chọn|phương án|em chọn|tôi chọn|mình chọn|chị chọn|anh chọn)"
_TRUE = {"đúng", "đúng rồi", "chuẩn", "có"}
_FALSE = {"sai", "sai rồi", "không đúng", "không"}


def parse_answer(text: str, n_options: int) -> Optional[int]:
    """Câu nói -> vị trí phương án (0 = A), None nếu không phải câu trả lời. Câu dài là lời bàn tán, không tính."""
    low = re.sub(r"[^\w\s]", " ", (text or "").lower())
    words = low.split()
    if not words or len(words) > 8:
        return None
    if n_options == 2:
        phrase = " ".join(words)
        for w in (phrase, " ".join(words[-2:]), words[-1], words[0]):
            if w in _TRUE:
                return 0
            if w in _FALSE:
                return 1
    m = re.search(_PREFIX + r"\s+(?:số\s+)?(\w+)", low)
    if m:
        tok = m.group(1)
        idx = _LETTER.get(tok, _NUM.get(tok))
        return idx if idx is not None and idx < n_options else None
    if len(words) <= 3:                       # nói gọn: "B", "bê ạ", "C nhé"
        for w in words:
            if w in _LETTER and _LETTER[w] < n_options:
                return _LETTER[w]
    return None


def command_of(text: str) -> Optional[str]:
    """Lệnh điều khiển trò chơi gọi kèm tên trợ lý: start / next / scores / stop."""
    low = retrieval.fold(text or "")
    if re.search(r"\b(dung|ket thuc|huy|thoi)\b.*\b(tro choi|game|choi)\b|\b(tro choi|game)\b.*\b(dung|ket thuc)\b", low):
        return "stop"
    if re.search(r"\b(bat dau|choi thoi|vao choi|bat dau choi|start)\b", low):
        return "start"
    if re.search(r"\b(cau tiep|cau sau|qua cau|chuyen cau|cau ke tiep|next)\b", low):
        return "next"
    if re.search(r"\b(bang diem|xep hang|ai dang dan dau)\b", low):
        return "scores"
    return None


def _sent(text: str) -> str:
    """Câu kết thúc bằng dấu câu (ghép lời MC không bị dính câu)."""
    text = (text or "").strip()
    return text if not text or text[-1] in ".!?…" else text + "."


def speech_s(text: str) -> float:
    """Ước lượng thời gian trợ lý đọc xong (giọng tiếng Việt khoảng 2,7 từ mỗi giây)."""
    return len((text or "").split()) / 2.7 + 1.0


# --------------------------------------------------------------- một lượt chơi ---
class GameRun:
    """Một lượt chơi trong một cuộc họp. say(text) và emit(event) do phiên họp truyền vào."""

    def __init__(self, session: Any, artifact_id: int, game: Dict[str, Any]):
        self.s = session
        self.artifact_id = artifact_id
        self.g = game
        self.code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        self.phase = "lobby"
        self.qi = -1
        self.opened_at = self.deadline = 0.0
        self.answers: Dict[str, Dict[str, Any]] = {}
        self.players: Dict[str, Dict[str, Any]] = {}
        self.reveal: Optional[Dict[str, Any]] = None
        self.winners: List[Dict[str, Any]] = []
        self.lucky: Optional[Dict[str, Any]] = None
        self.task: Optional[asyncio.Task] = None
        self._go = asyncio.Event()
        self._skip = asyncio.Event()
        self._last_emit = 0.0
        self._soon: Optional[asyncio.Task] = None
        self.started_at = time.time()
        self.finished = False

    # ---------------------------------------------------------- trạng thái ---
    @property
    def active(self) -> bool:
        return not self.finished

    def question(self) -> Optional[Dict[str, Any]]:
        qs = self.g["questions"]
        return qs[self.qi] if 0 <= self.qi < len(qs) else None

    def _name(self, key: str) -> str:
        p = self.players.get(key) or {}
        if p.get("via") == "voice" and p.get("sid") is not None:
            prof = self.s.speakers.profile(p["sid"]) if hasattr(self.s, "speakers") else None
            if prof is not None and getattr(prof, "label", None):
                return prof.label
        return p.get("name") or "Người chơi"

    def leaderboard(self, n: int = 10) -> List[Dict[str, Any]]:
        rows = [{"key": k, "name": self._name(k), "points": p["points"], "correct": p["correct"], "alive": p["alive"],
                 "via": p["via"]} for k, p in self.players.items()]
        rows.sort(key=lambda r: (-int(r["alive"]) if self.g["format"] == "golden_bell" else 0, -r["points"],
                                 -r["correct"], r["name"]))
        return rows[:n]

    def public(self, pid: Optional[str] = None) -> Dict[str, Any]:
        q = self.question()
        out: Dict[str, Any] = {
            "artifact_id": self.artifact_id, "code": self.code, "title": self.g["title"], "format": self.g["format"],
            "theme": self.g["theme"], "rules": self.g["rules"], "prize": self.g["prize"], "phase": self.phase,
            "qi": self.qi, "total": len(self.g["questions"]), "now": time.time(), "opened_at": self.opened_at,
            "deadline": self.deadline, "players": len(self.players), "ended": self.finished,
            "alive": sum(1 for p in self.players.values() if p["alive"]),
            "leaderboard": self.leaderboard(), "winners": self.winners, "lucky": self.lucky}
        if q is not None:
            out["question"] = {"q": q["q"], "options": q["options"], "type": q["type"],
                               "time": ANSWER_S[q["type"]]}
            out["answered"] = [self._name(k) for k in self.answers][:40]
            out["n_answered"] = len(self.answers)
        if self.phase in ("reveal", "scores", "final", "lucky") and self.reveal is not None:
            out["reveal"] = self.reveal
        if pid is not None:
            p = self.players.get(pid)
            me = {"joined": p is not None}
            if p is not None:
                rank = next((i + 1 for i, r in enumerate(self.leaderboard(len(self.players))) if r["key"] == pid), None)
                a = self.answers.get(pid)
                me.update(name=p["name"], points=p["points"], correct=p["correct"], alive=p["alive"], rank=rank,
                          choice=a["choice"] if a else None, gain=p.get("last_gain"))
            out["me"] = me
        return out

    async def emit(self, force: bool = True) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < 0.7:
            self.emit_soon()                         # gộp các cập nhật dồn dập, gửi bản mới nhất sau chốc lát
            return
        self._last_emit = now
        await self.s.emit({"type": "game_state", "game": self.public()})

    def emit_soon(self, delay: float = 0.6) -> None:
        """Gửi trạng thái sau delay giây (gọi được từ hàm thường đang chạy trong event loop)."""
        if self._soon is not None and not self._soon.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _later():
            await asyncio.sleep(delay)
            await self.emit()
        self._soon = asyncio.ensure_future(_later())

    async def say(self, text: str, mood: str = "happy") -> None:
        await self.s._say(text, mood=mood)

    # ---------------------------------------------------------- người chơi ---
    def _player(self, key: str, name: str, via: str, sid: Optional[int] = None) -> Optional[Dict[str, Any]]:
        p = self.players.get(key)
        if p is None:
            if len(self.players) >= MAX_PLAYERS:
                return None
            alive = self.g["format"] != "golden_bell" or self.qi <= 0     # Rung chuông vàng: vào muộn thì chỉ xem
            p = self.players[key] = {"name": _clean(name, 30) or "Người chơi", "via": via, "sid": sid, "points": 0,
                                     "correct": 0, "alive": alive, "joined_at": time.time(), "last_gain": None}
        return p

    def join(self, name: str) -> str:
        """Người chơi trên điện thoại. Trả về mã người chơi (giữ trong trình duyệt của họ)."""
        if self.finished or self.phase in ("final", "lucky"):
            raise ValueError("Trò chơi đã kết thúc")
        name = _clean(name, 30)
        if not name:
            raise ValueError("Nhập tên để chơi")
        pid = "p" + secrets.token_hex(6)
        if self._player(pid, name, "phone") is None:
            raise ValueError("Đã đủ người chơi")
        self.emit_soon(0.2)                           # màn hình hiện người vừa vào
        return pid

    def _take(self, key: str, choice: int, via: str) -> bool:
        q = self.question()
        p = self.players.get(key)
        if q is None or p is None or self.phase != "answering" or key in self.answers or not p["alive"]:
            return False
        if not 0 <= choice < len(q["options"]) or time.time() > self.deadline + GRACE_S:
            return False
        self.answers[key] = {"choice": choice, "at": time.time(), "via": via}
        return True

    def answer_phone(self, pid: str, choice: int) -> bool:
        ok = self._take(pid, int(choice), "phone")
        if ok:
            self.emit_soon(0.2)
        return ok

    def on_segment(self, seg: Dict[str, Any]) -> None:
        """Câu vừa chốt trong cuộc họp: đang đếm ngược mà là câu trả lời thì ghi cho người nói."""
        q = self.question()
        if q is None or self.phase != "answering":
            return
        choice = parse_answer(seg.get("text") or "", len(q["options"]))
        if choice is None:
            return
        sid = seg.get("speaker_key")
        key = f"v{sid}"
        if self._player(key, seg.get("speaker_label") or "Người nói", "voice", sid) is None:
            return
        if self._take(key, choice, "voice"):
            self.emit_soon(0.2)

    # ---------------------------------------------------------- điều khiển ---
    def start(self) -> None:
        self._go.set()

    def skip(self) -> None:
        self._skip.set()

    async def stop(self, reason: str = "") -> None:
        if self.task is not None and not self.task.done() and self.task is not asyncio.current_task():
            self.task.cancel()
        self._close(reason or "Trò chơi đã dừng")
        await self.emit()

    def _close(self, reason: str = "") -> None:
        if not self.finished:
            self.finished = True
            if self.phase != "final":
                self.phase = "stopped"
            GAMES[self.code] = EndedGame(self)      # điện thoại không mất màn kết quả ngay khi trò chơi khép lại

    async def _wait(self, seconds: float, until: Optional[asyncio.Event] = None) -> None:
        """Chờ seconds giây; bấm Câu tiếp (hoặc sự kiện until) thì thôi chờ."""
        ev = until or self._skip
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass
        if ev is self._skip:
            self._skip.clear()

    # ---------------------------------------------------------- dẫn trò chơi ---
    async def run(self) -> None:
        GAMES[self.code] = self
        try:
            await self._lobby()
            for i in range(len(self.g["questions"])):
                self.qi = i
                await self._ask(i)
                await self._reveal(i)
                if self.g["format"] == "golden_bell" and self.players and \
                        sum(1 for p in self.players.values() if p["alive"]) <= 1 and i >= 1:
                    break                            # Rung chuông vàng: còn một người là xong
                if i in (2, 4) and i < len(self.g["questions"]) - 1:
                    await self._scores()
            await self._final()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("meeting.game: dẫn trò chơi lỗi")
        finally:
            self._close()
            await self._save_results()
            try:
                await self.emit()
            except Exception:
                pass

    async def _lobby(self) -> None:
        g = self.g
        await self.emit()
        how = ("Ai sai là bị loại, người trụ lại cuối cùng thắng." if g["format"] == "golden_bell"
               else "Đúng và càng nhanh thì càng nhiều điểm.")
        # luật chơi đã hiện trên màn hình: lời mở màn ngắn (khoảng 20 giây) để không lấn sang câu hỏi đầu tiên
        text = " ".join(x for x in (
            _sent(g["intro"] or "Chào cả nhà, mình cùng chơi một trò chơi nhỏ nhé!"),
            _sent(f"Trò chơi hôm nay là: {g['title']}"), how,
            "Cả nhà nói đáp án to và rõ, hoặc quét mã QR trên màn hình để chơi trên điện thoại.",
            _sent(f"Phần thưởng là {g['prize']}"), "Sẵn sàng thì gọi em bắt đầu nhé!") if x)
        await self.say(text)
        await self._wait(LOBBY_MAX_S, until=self._go)

    async def _ask(self, i: int) -> None:
        q = self.g["questions"][i]
        self.answers = {}
        self.reveal = None
        self.phase = "reading"
        self.opened_at = self.deadline = 0.0
        await self.emit()
        opts = ". ".join(f"{LETTERS[k]}: {o}" for k, o in enumerate(q["options"])) if q["type"] == "choice" \
            else "Đúng hay Sai?"
        text = f"Câu {i + 1}. {_sent(q['q'])} {_sent(opts)}"
        await self.say(text, mood="thinking")
        await asyncio.sleep(speech_s(text))
        self.phase = "answering"
        t = ANSWER_S[q["type"]]
        self.opened_at = time.time()
        self.deadline = self.opened_at + t
        await self.emit()
        end = self.deadline
        while time.time() < end:
            if self._skip.is_set():
                self._skip.clear()
                break
            phone = [k for k, p in self.players.items() if p["via"] == "phone" and p["alive"]]
            alive = [k for k, p in self.players.items() if p["alive"]]
            if alive and all(k in self.answers for k in alive) and len(phone) == len(alive):
                break                                # mọi người (chơi trên điện thoại) đã trả lời: chốt sớm
            await asyncio.sleep(0.25)
        self.deadline = min(self.deadline, time.time())
        self.phase = "closing"
        await self.emit()
        await asyncio.sleep(GRACE_S)                  # câu trả lời nói sát giờ còn đang được chép

    async def _reveal(self, i: int) -> None:
        q = self.g["questions"][i]
        t = ANSWER_S[q["type"]]
        counts = [0] * len(q["options"])
        gains, out_now = [], []
        for key, a in sorted(self.answers.items(), key=lambda kv: kv[1]["at"]):
            counts[a["choice"]] += 1
        for key, p in self.players.items():
            p["last_gain"] = None
            if not p["alive"]:
                continue
            a = self.answers.get(key)
            if a is not None and a["choice"] == q["answer"]:
                bonus = round(50 * max(0.0, 1 - (a["at"] - self.opened_at) / t))
                gain = 100 + bonus
                p["points"] += gain
                p["correct"] += 1
                p["last_gain"] = gain
                gains.append({"key": key, "name": self._name(key), "points": gain, "at": a["at"]})
            elif self.g["format"] == "golden_bell" and (a is not None or p["joined_at"] < self.opened_at):
                out_now.append(key)                   # sai, hoặc đã vào chơi mà không trả lời
        revived = False
        if self.g["format"] == "golden_bell" and out_now:
            if all(not self.players[k]["alive"] or k in out_now for k in self.players):
                revived = True                        # cả phòng cùng sai: cứu trợ những người vừa bị loại
            else:
                for k in out_now:
                    self.players[k]["alive"] = False
        gains.sort(key=lambda g: g["at"])
        self.reveal = {"answer": q["answer"], "counts": counts, "explain": q["explain"], "gains": gains[:10],
                       "fastest": gains[0]["name"] if gains else None, "eliminated": [self._name(k) for k in out_now][:20]
                       if not revived else [], "revived": revived}
        self.phase = "reveal"
        await self.emit()
        right = f"{LETTERS[q['answer']]}: {q['options'][q['answer']]}" if q["type"] == "choice" else q["options"][q["answer"]]
        if gains:
            who = ", ".join(g["name"] for g in gains[:3])
            cheer = f"Chính xác! {gains[0]['name']} nhanh nhất." if len(gains) == 1 else f"Chúc mừng {who}! {gains[0]['name']} nhanh nhất."
        elif self.answers:
            cheer = "Tiếc quá, chưa ai đúng câu này."
        else:
            cheer = "Câu này chưa ai trả lời."
        extra = ""
        if self.g["format"] == "golden_bell":
            alive = sum(1 for p in self.players.values() if p["alive"])
            extra = (" Cả phòng cùng sai nên em cứu trợ, mọi người quay lại cuộc chơi!" if revived
                     else f" Còn {alive} người trụ lại." if self.players else "")
        lead = "Đáp án đúng là" if q["type"] == "choice" else "Đáp án là:"
        text = f"{lead} {_sent(right)} {_sent(q['explain'])} {cheer}{extra}".replace("  ", " ")
        await self.say(text, mood="happy" if gains else "neutral")
        await self._wait(speech_s(text) + REVEAL_S)

    async def _scores(self) -> None:
        self.phase = "scores"
        await self.emit()
        top = self.leaderboard(3)
        if top and top[0]["points"] > 0:
            text = "Bảng điểm tạm thời: dẫn đầu là " + ", ".join(f"{r['name']} {r['points']} điểm" for r in top) + "."
            await self.say(text)
            await self._wait(speech_s(text) + 3)
        else:
            await self._wait(4)

    async def _final(self) -> None:
        board = self.leaderboard(len(self.players) or 1)
        if self.g["format"] == "golden_bell":
            win = [r for r in board if r["alive"]] or board[:1]
        else:
            best = board[0]["points"] if board else 0
            win = [r for r in board if r["points"] == best and best > 0][:3]
        self.winners = [{"name": r["name"], "points": r["points"], "correct": r["correct"]} for r in win]
        self.phase = "final"
        await self.emit()
        if self.winners:
            names = ", ".join(w["name"] for w in self.winners)
            text = (f"Và người chiến thắng là... {names}! {self.g['outro'] or 'Xin chúc mừng!'} "
                    f"Phần thưởng {self.g['prize']} xin mời nhận quà từ ban tổ chức.")
        else:
            text = f"Trò chơi khép lại rồi. {self.g['outro'] or 'Cảm ơn cả nhà đã tham gia!'}"
        await self.say(text, mood="happy")
        await self._wait(speech_s(text) + 2)
        pool = [r for r in board if r["name"] not in {w["name"] for w in self.winners} and
                (r["correct"] > 0 or self.players.get(r["key"], {}).get("via") == "phone")]
        if len(pool) >= 2:                           # quay số may mắn cho một người chơi khác người thắng
            pick = random.choice(pool)
            self.lucky = {"names": [r["name"] for r in pool][:40], "winner": pick["name"], "at": time.time()}
            self.phase = "lucky"
            await self.say("Còn một phần quà may mắn nữa! Em quay số cho những người đã chơi nhé.")
            await self.emit()
            await asyncio.sleep(6.5)                 # màn hình quay tên khoảng 6 giây
            await self.say(f"Người may mắn là {pick['name']}! Chúc mừng!", mood="happy")
            await asyncio.sleep(3)
        self.phase = "final"
        await self.emit()

    async def _save_results(self) -> None:
        try:
            from meeting import db
            res = {"played_at": self.started_at, "players": len(self.players), "winners": self.winners,
                   "lucky": (self.lucky or {}).get("winner"), "leaderboard": self.leaderboard(20),
                   "questions_played": max(0, self.qi + 1)}
            await asyncio.to_thread(db.update_artifact_meta, self.artifact_id, {"results": res})
        except Exception as e:
            log.info("meeting.game: không lưu được kết quả trò chơi: %s", e)


class EndedGame:
    """Kết quả lượt chơi đã xong: điện thoại chỉ còn xem hạng và người thắng. Không giữ phiên họp trong bộ nhớ."""
    finished = True

    def __init__(self, run: GameRun):
        self.code, self.ended_at = run.code, time.time()
        self._state = run.public()
        self._me = {k: run.public(k)["me"] for k, p in run.players.items() if p["via"] == "phone"}

    def public(self, pid: Optional[str] = None) -> Dict[str, Any]:
        out = {**self._state, "now": time.time()}
        if pid is not None:
            out["me"] = self._me.get(pid) or {"joined": False}
        return out

    def join(self, name: str) -> str:
        raise ValueError("Trò chơi đã kết thúc")

    def answer_phone(self, pid: str, choice: int) -> bool:
        return False


def by_code(code: str) -> "Optional[GameRun | EndedGame]":
    """Lượt chơi theo mã trên điện thoại: đang mở, hoặc đã xong chưa quá KEEP_ENDED_S (chỉ còn xem kết quả)."""
    now = time.time()
    for k in [k for k, g in GAMES.items() if isinstance(g, EndedGame) and now - g.ended_at > KEEP_ENDED_S]:
        GAMES.pop(k, None)
    return GAMES.get(str(code or "")[:20])
