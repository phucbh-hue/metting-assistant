"""Semantic Identity Inference Engine (AI suy luận danh tính người nói từ ngữ cảnh hội thoại).

Trong cuộc họp, người nói chưa có mẫu giọng mang nhãn tạm "Người nói N". AI đọc nội dung trò chuyện
(xưng hô, tự giới thiệu, lời chào gọi tên, đối chiếu danh bạ MCP) để đoán tên thật:
1. Độ tin cậy >= 0.70 và tên chưa được dùng cho người khác -> tự đặt tên cho HỒ SƠ người nói
   (mọi câu cũ và mới của người đó đổi theo, tên được lưu vào meeting_speakers nên không bị mất).
   Cuộc họp thật #38-#42: 7/7 gợi ý 0.75-0.80 đều được người dùng bấm xác nhận; chạy lại bằng AI thật, các tên
   0.70-0.72 cũng đúng còn tên sai chỉ 0.60 -> không cần bấm nữa.
2. 0.60 - 0.70 hoặc trùng tên người khác (xác nhận sẽ gộp 2 người) -> gửi gợi ý để chủ phòng bấm xác nhận / bỏ qua.
   Đoán lại ra cùng tên thì cập nhật thẻ gợi ý cũ, ra tên khác thì thay thẻ cũ (không hiện nhiều thẻ trùng nhau).
3. Mẫu giọng chỉ được lưu vào Voice Registry khi người dùng xác nhận (hoặc bật AUTO_ENROLL_VOICES=1),
   vì vector giọng nói là dữ liệu sinh trắc học (Nghị định 13/2023/NĐ-CP).

IdentityEngine tự chạy khi có thông tin mới (tối đa 1 lần mỗi IDENTITY_MIN_INTERVAL_S giây, gom tất cả người nói
chưa định danh vào MỘT lần gọi):
- Có người tự giới thiệu ("tôi là Phúc", "em là Tuấn bên DevOps", "mình tên Lan") -> đoán ngay.
- Có người được gọi tên ("Tuấn ơi", "mời anh Duy Leo", "chào thầy Minh") -> CHỜ một người khác đáp lời rồi mới đoán;
  chưa ai đáp sau 12 giây thì vẫn đoán (câu có thể nhắc người VỪA nói: "cảm ơn anh Tuấn").
  Lỗi cũ: chạy ngay sau câu gọi tên, lúc người kia chưa trả lời nên AI không biết ai là Tuấn, và câu trả lời đến sau
  không kích hoạt lại -> người dùng phải bấm "AI đoán tên".
- Dự phòng: cứ IDENTITY_FALLBACK_SEGMENTS câu mới mà người chưa có tên nói thêm thì chạy lại một lần.
Mỗi tên chỉ tính là thông tin mới ở 3 lần nhắc đầu; tên trợ lý và tên người đã biết không tính.
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from pydantic import AliasChoices, BaseModel, Field

from meeting import artifacts, db, llm, mcp

log = logging.getLogger("meeting.identity")

AUTO_APPLY_T = float(os.getenv("IDENTITY_AUTO_APPLY_T", "0.70"))
SUGGEST_T = 0.60
ENROLL_T = 0.85                # bật AUTO_ENROLL_VOICES: chỉ tự lưu mẫu giọng (sinh trắc học) khi AI chắc chắn từ 85%
MIN_INTERVAL_S = float(os.getenv("IDENTITY_MIN_INTERVAL_S", "20"))
FALLBACK_EVERY_N = int(os.getenv("IDENTITY_FALLBACK_SEGMENTS", "40"))   # không có tên mới: cứ ~40 câu mới chạy lại một lần
NAME_EVIDENCE_MAX = 3          # một tên được nhắc lại nhiều lần thì chỉ 3 lần đầu là thông tin mới
REPLY_SETTLE_S = 4.0           # người được gọi tên vừa đáp lời: chờ thêm vài giây cho hết câu trả lời rồi mới đoán
REPLY_WINDOW = 12              # ... người đáp lời phải nói trong vòng 12 câu kể từ câu gọi tên
REPLY_WAIT_S = 12.0            # chưa ai đáp sau ngần này giây thì vẫn đoán (câu có thể nhắc tới người vừa nói)
TARGET_MIN_S = 2.0             # tự chạy: chỉ đoán cho người đã nói >= 2 giây (hoặc >= 2 câu, hoặc vừa tự giới thiệu)
TRANSCRIPT_LIMIT = 80          # số câu gửi cho AI
EVIDENCE_LIMIT = 24            # cuộc họp dài: trong 80 câu đó giữ tối đa 24 câu có manh mối tên ở phần đầu buổi
# Tự giới thiệu mà không bắt được tên ngay trong câu ("xin tự giới thiệu" ... tên ở câu sau)
SELF_INTRO = re.compile(r"(tự giới thiệu|giới thiệu\s+(về\s+)?bản thân|\bmy name is\b)", re.IGNORECASE)
# Từ xưng hô đứng trước tên ("anh Tuấn", "thầy Minh"); "là" / "tên" đứng trước tên khi tự giới thiệu
_HONORIFIC_WORDS = {"anh", "chị", "em", "bạn", "cô", "chú", "bác", "ông", "bà", "thầy", "sếp", "mr", "ms", "mrs", "dr"}
_CALL_WORDS = {"chào", "mời", "ơn"}      # "chào Leo", "mời Tuấn", "cảm ơn Sarah" (không kèm kính ngữ)
# "<ngôi thứ nhất> là <Tên>", "tên là <Tên>", "mình tên <Tên>" -> người nói tự giới thiệu
_FIRST_PERSON = {"tôi", "em", "mình", "anh", "chị", "tớ", "tui", "con", "cháu", "tao", "tên"}
_EN_INTRO = re.compile(r"\b(?i:my name is|i am|i'm|i’m)\s+([^\W\d_]+(?:\s+[^\W\d_]+){0,2})")
_EN_MENTION = re.compile(r"\b(?i:this is|welcome|thank you|thanks),?\s+([^\W\d_]+(?:\s+[^\W\d_]+){0,2})")
# Từ viết hoa giữa câu nhưng không phải tên người
_CAP_STOP = {"ok", "okay", "dạ", "vâng", "ừ", "ờ", "à", "ạ", "trời", "giời", "chúa", "phật", "mẹ", "má", "bố", "ba",
             "con", "cháu", "urbox", "jira", "ai", "mcp", "slide", "dashboard", "api",
             "google", "facebook", "youtube", "zalo", "tiktok", "excel", "word", "power", "bi", "sprint", "mega", "sale",
             "podcast", "online", "việt", "nam", "hà", "nội", "sài", "gòn", "tp", "hcm", "redis", "postgres", "staging",
             "mc", "ceo", "cto", "cfo", "pm", "po", "ba", "qa", "hr", "it", "devops", "team", "app", "web", "website",
             "email", "file", "pdf", "cloud", "github", "slack", "teams", "zoom", "meet", "n8n", "chatgpt", "claude",
             "gemini", "openai"}
NAME_CUES = re.compile(
    r"(tên\s+(là|tôi|em|mình|anh|chị|tớ)|\b(mình|em|tôi|anh|chị|tớ|tui|con|cháu)\s+là\b|\bơi\b|\bchào\b|"
    r"giới thiệu|cảm ơn\s+(anh|chị|em|bạn|cô|chú)|mời\s+(anh|chị|em|bạn)|\bthưa\b|"
    r"my name|\bi am\b|\bi'm\b|\bthis is\b|\bhi\b|\bhello\b)", re.IGNORECASE)
HONORIFICS = ("anh", "chị", "em", "bạn", "cô", "chú", "bác", "ông", "bà", "thầy", "mr", "mrs", "ms", "dr")


class IdentityClue(BaseModel):
    clue_type: str = Field(default="context", validation_alias=AliasChoices("clue_type", "type"))
    quote: str = Field(default="", validation_alias=AliasChoices("quote", "evidence_quote", "text"))
    speaker_involved: str = Field(default="", validation_alias=AliasChoices("speaker_involved", "speaker"))


class IdentityPrediction(BaseModel):
    unknown_label: str = Field(default="", validation_alias=AliasChoices("unknown_label", "speaker_label", "label"))
    predicted_name: str = Field(default="", validation_alias=AliasChoices("predicted_name", "name"))
    predicted_role: str = Field(default="", validation_alias=AliasChoices("predicted_role", "role"))
    predicted_email: str = Field(default="", validation_alias=AliasChoices("predicted_email", "email"))
    confidence: float = Field(default=0.0)
    reasoning: str = Field(default="")
    evidence: List[Union[IdentityClue, str, Dict[str, Any]]] = Field(default_factory=list)


SYSTEM_PROMPT = """Bạn là chuyên gia phân tích hội thoại tiếng Việt, nhiệm vụ: xác định TÊN THẬT của những người nói
đang mang nhãn tạm (ví dụ "Người nói 2") trong transcript cuộc họp.

NGUYÊN TẮC SUY LUẬN:
1. Tự giới thiệu: "mình tên là Lan Anh", "em là Tuấn bên DevOps" -> chính người nói câu đó.
2. Gọi tên trực tiếp rồi người kia đáp lời: A nói "Tuấn ơi, xong chưa?" và câu kế tiếp của B "Dạ em xong rồi"
   -> B là Tuấn. Lời chào "em chào chị Hương" thì người ĐƯỢC chào (người đáp lại) là Hương, KHÔNG phải người chào.
3. Người dẫn chương trình / người thứ ba nhắc tên ("hôm nay có Hương và Nam") chỉ là gợi ý, cần thêm bằng chứng.
4. Nếu tên/công việc khớp danh bạ nội bộ thì dùng họ tên đầy đủ, chức danh trong danh bạ. Người ngoài danh bạ
   (khách mời, đối tác...) vẫn phải trả về đúng tên được nhắc trong hội thoại.
5. MỖI TÊN CHỈ GÁN CHO MỘT NHÃN. Hai nhãn không thể cùng là một người - nếu phân vân thì hạ độ tin cậy.
6. Không đoán bừa: không có bằng chứng thì confidence <= 0.3 và predicted_name rỗng.
7. predicted_name chỉ là tên người (không kèm "anh", "chị", "bạn", chức danh).

THANG ĐIỂM confidence:
- 0.85 - 1.00: tự giới thiệu rõ ràng, hoặc được gọi tên và đáp lời ngay sau đó.
- 0.60 - 0.84: có manh mối tên nhưng chưa chắc chắn ai đáp lời.
- < 0.60: chỉ phỏng đoán.

Chỉ trả về MỘT JSON hợp lệ, không giải thích thêm:
{"predictions": [{"unknown_label": "Người nói 2", "predicted_name": "...", "predicted_role": "...",
  "predicted_email": "", "confidence": 0.9, "reasoning": "...",
  "evidence": [{"clue_type": "self_intro|direct_address|reply|directory", "quote": "..."}]}]}"""


def _fmt_t(sec: Any) -> str:
    try:
        s = int(float(sec or 0))
    except Exception:
        s = 0
    return f"{s // 60:02d}:{s % 60:02d}"


def format_transcript(segments: List[Dict[str, Any]], limit: int = TRANSCRIPT_LIMIT) -> str:
    """Mỗi câu một dòng; đoạn bị lược bớt ở giữa (seq không liền nhau) đánh dấu [...]."""
    lines, prev = [], None
    for s in segments[-limit:]:
        seq = s.get("seq")
        if isinstance(prev, int) and isinstance(seq, int) and seq > prev + 1:
            lines.append("[...]")
        lines.append(f"[{_fmt_t(s.get('t_start'))}] {s.get('speaker_label') or 'Không rõ'}: {s.get('text', '')}")
        prev = seq
    return "\n".join(lines)


def name_cues(text: str, skip: Set[str] = frozenset()) -> Tuple[Set[str], Set[str]]:
    """(tên tự giới thiệu, tên được gọi / nhắc tới) trong một câu, theo cách xưng hô tiếng Việt (viết thường).

    "tôi là Bùi Hồng Phúc", "mình tên Lan" -> tự giới thiệu (chính người nói câu này);
    "anh Tuấn", "Tuấn ơi", "đây là chị Hương", "Thank you, John" -> gọi / nhắc tên người khác.
    skip: các từ không phải tên người (tên trợ lý, tên người đã biết), viết thường."""
    def is_name(w: str, inner: bool = False) -> bool:
        """inner: chữ thứ 2 trở đi của tên - "Anh", "Em" viết hoa lúc đó là tên đệm/tên ("Lan Anh", "Đức Anh")."""
        lw = w.lower()
        return (w[:1].isalpha() and w[0].isupper() and len(w) > 1 and lw not in _CAP_STOP
                and lw not in skip and (inner or lw not in _HONORIFIC_WORDS))

    def run_after(words: List[str]) -> str:
        run: List[str] = []
        for x in words:
            if not is_name(x, inner=bool(run)):
                break
            run.append(x.lower())
        return " ".join(run)

    toks = re.findall(r"[^\W\d_]+|[.!?…,;:]", text or "")
    intro: Set[str] = set()
    named: Set[str] = set()
    for i, w in enumerate(toks):
        lw = w.lower()
        if lw in _HONORIFIC_WORDS or lw in _CALL_WORDS or lw in ("là", "tên"):   # anh Tuấn / chào Leo / là Phúc
            name = run_after(toks[i + 1:i + 5])
            if name:
                prev = toks[i - 1].lower() if i else ""
                (intro if lw in ("là", "tên") and prev in _FIRST_PERSON else named).add(name)
        elif lw == "ơi":                                                         # Tuấn ơi / Lan Anh ơi
            run: List[str] = []
            for j in range(i - 1, max(-1, i - 5), -1):
                if not is_name(toks[j], inner=j > 0 and is_name(toks[j - 1])):
                    break
                run.insert(0, toks[j].lower())
            if run:
                named.add(" ".join(run))
    for rx, out in ((_EN_INTRO, intro), (_EN_MENTION, named)):
        for m in rx.finditer(text or ""):
            name = run_after(m.group(1).split())
            if name:
                out.add(name)
    return intro, named - intro


def assistant_words() -> Set[str]:
    cfg = llm.assistant_config()
    return {w.lower() for n in [cfg.get("name", "")] + list(cfg.get("aliases") or []) for w in str(n).split()}


def pick_segments(segments: List[Dict[str, Any]], limit: int = TRANSCRIPT_LIMIT, evidence: int = EVIDENCE_LIMIT,
                  skip: Set[str] = frozenset()) -> List[Dict[str, Any]]:
    """Các câu gửi cho AI: cả cuộc họp nếu ngắn. Cuộc họp dài thì các câu gần nhất cộng các câu có manh mối tên ở phần
    trước (kèm câu ngay sau để thấy ai đáp lời): người tự giới thiệu đầu buổi rồi nói tiếp ở phút 40 vẫn được nhận ra."""
    segments = list(segments)
    if len(segments) <= limit:
        return segments
    cut = len(segments) - (limit - evidence)
    keep: List[int] = []
    for i in range(cut):
        text = segments[i].get("text") or ""
        intro, named = name_cues(text, skip)
        if intro or named or SELF_INTRO.search(text):
            keep += [i, i + 1]
    keep = sorted({i for i in keep if i < cut})[:evidence]
    start = max(cut - (evidence - len(keep)), keep[-1] + 1 if keep else 0)   # chỗ trống còn lại cho các câu gần nhất
    return [segments[i] for i in keep] + segments[start:]


def clean_name(name: str) -> str:
    n = re.sub(r"[\"'“”‘’()\[\]]", "", (name or "")).strip(" .,:;-").strip()
    parts = n.split()
    while len(parts) > 1 and parts[0].casefold() in HONORIFICS:
        parts = parts[1:]
    n = " ".join(parts)[:60]
    return "" if not n or n.casefold() in ("không rõ", "unknown", "người nói", "người lạ") else n


def _evidence_dicts(ev: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for c in ev or []:
        if hasattr(c, "model_dump"):
            out.append(c.model_dump())
        elif isinstance(c, str):
            out.append({"clue_type": "context", "quote": c, "speaker_involved": ""})
        elif isinstance(c, dict):
            out.append({"clue_type": str(c.get("clue_type") or c.get("type") or "context"),
                        "quote": str(c.get("quote") or c.get("text") or ""),
                        "speaker_involved": str(c.get("speaker_involved") or c.get("speaker") or "")})
    return out[:6]


async def predict_identities(segments: List[Dict[str, Any]], target_labels: List[str],
                             meeting: Optional[Dict[str, Any]] = None,
                             known_participants: Optional[List[Dict[str, Any]]] = None) -> List[IdentityPrediction]:
    """Gọi LLM một lần để đoán tên cho tất cả nhãn trong target_labels."""
    if not target_labels or not segments:
        return []
    meeting = meeting or {}
    directory = mcp.call_tool("query_employee_directory", {"query": ""}).get("employees", [])
    dir_lines = "\n".join(f"- {e.get('name')} | {e.get('role')} | {e.get('department')} | {e.get('email')}"
                          for e in directory) or "(trống)"
    known = "\n".join(f"- {p.get('label')}" + (f" ({p.get('role')})" if p.get("role") else "")
                      for p in (known_participants or [])) or "(chưa có)"
    expected = ", ".join(x for x in (meeting.get("expected_attendees") or []) if isinstance(x, str)) or "(không có)"
    agenda = "; ".join(x for x in (meeting.get("agenda") or []) if isinstance(x, str)) or "(không có)"
    cfg = llm.assistant_config()
    bot = ", ".join(x for x in [cfg.get("name", "")] + list(cfg.get("aliases") or []) if x) or "(không có)"
    picked = pick_segments(segments, skip=assistant_words())
    prompt = f"""## Cuộc họp: {meeting.get('title', '')}
Chương trình: {agenda}
Người tham dự dự kiến: {expected}

## Tên gọi trợ lý AI của cuộc họp (KHÔNG phải người tham dự; "{cfg.get('name', '')} ơi ..." là đang ra lệnh cho trợ lý):
{bot}

## Người nói đã biết tên trong buổi họp:
{known}

## Danh bạ nội bộ (MCP):
{dir_lines}

## Transcript (mới nhất ở cuối{"; [...] là đoạn được lược bớt" if len(picked) < len(segments) else ""}):
{format_transcript(picked)}

## Nhãn cần xác định tên:
{json.dumps(target_labels, ensure_ascii=False)}

Trả về JSON {{"predictions": [...]}} với một phần tử cho mỗi nhãn ở trên."""
    artifacts.set_meeting(meeting.get("id"), "đoán tên")
    raw = await artifacts._call_llm(SYSTEM_PROMPT, prompt, max_tokens=1500)
    data = llm._extract_json(raw)
    items = data.get("predictions", []) if isinstance(data, dict) else []
    out = []
    for it in items:
        try:
            out.append(IdentityPrediction(**it))
        except Exception as e:
            log.debug("meeting.identity: bỏ qua dự đoán lỗi định dạng %s: %s", it, e)
    return out


class IdentityEngine:
    """Đoán tên người nói cho một MeetingSession (debounce, gom một lần gọi LLM)."""

    def __init__(self, session: Any):
        self.session = session
        self._running = False
        self._last_run = 0.0
        self._new_since = 0
        self._intro_since = False         # có người tự giới thiệu từ lần chạy trước
        self._mentions: Dict[str, int] = {}
        self._awaiting: Dict[str, Dict[str, Any]] = {}              # tên vừa được gọi -> người gọi, vị trí câu, lúc gọi
        self._intro_sids: Set[int] = set()                          # hồ sơ có câu tự giới thiệu
        self._seen: Dict[int, int] = {}                             # số câu của từng hồ sơ ở lần chạy trước
        self._timer: Optional[asyncio.Task] = None
        self._timer_due = 0.0
        self._dismissed: Set[Tuple[int, str]] = set()
        self._suggestions: Dict[int, Dict[str, Any]] = {}
        self._clock = time.monotonic

    # --------------------------------------------------------------- state ---
    def reset(self):
        """Hồ sơ người nói vừa được dựng lại: bỏ các gợi ý cũ và mọi trạng thái gắn với sid cũ."""
        self._suggestions.clear()
        self._dismissed.clear()
        self._awaiting.clear()
        self._intro_sids.clear()
        self._seen.clear()

    def _sid(self, sid: Optional[int]) -> Optional[int]:
        p = self.session.speakers.profile(sid) if sid is not None else None
        return p.sid if p is not None else None

    def targets(self, everyone: bool = False) -> List[Any]:
        """Người nói chưa có tên. Tự chạy thì bỏ qua người mới nói một câu ngắn (chưa đủ để đoán, đỡ tốn lượt gọi AI);
        bấm "AI đoán tên" (everyone=True) thì đoán cho tất cả."""
        intro = {self._sid(s) for s in self._intro_sids}
        return [p for p in self.session.speakers.visible_profiles()
                if not p.name and not p.locked
                and (everyone or p.n_segments >= 2 or p.speech_s >= TARGET_MIN_S or p.sid in intro)]

    def pending_suggestions(self) -> List[Dict[str, Any]]:
        out = []
        for iid, s in list(self._suggestions.items()):
            p = self.session.speakers.profile(s["sid"])
            if p is None or p.name:
                self._suggestions.pop(iid, None)
                continue
            out.append({**s, "sid": p.sid, "label": p.label})
        return out

    # ------------------------------------------------------------ trigger ---
    def _skip_words(self) -> Set[str]:
        skip = assistant_words()
        for p in self.session.speakers.visible_profiles():
            if p.name:
                skip |= {w.lower() for w in str(p.name).split()}
        return skip

    def cues(self, text: str) -> Tuple[Set[str], Set[str]]:
        """(tên tự giới thiệu, tên được gọi) trong câu mà còn là thông tin mới: bỏ tên trợ lý, tên người nói đã biết,
        và tên đã được nhắc từ 3 lần trở lên."""
        intro, named = name_cues(text, self._skip_words())
        return ({n for n in intro if self._mentions.get(n, 0) < NAME_EVIDENCE_MAX},
                {n for n in named if self._mentions.get(n, 0) < NAME_EVIDENCE_MAX})

    def fresh_names(self, text: str) -> Set[str]:
        """Tên người được nhắc theo cách xưng hô ("anh Tuấn", "Tuấn ơi", "tôi là Bùi Hồng Phúc") mà còn là thông tin mới."""
        intro, named = self.cues(text)
        return intro | named

    def notify(self, seg: Dict[str, Any]):
        self._new_since += 1
        text = seg.get("text") or ""
        intro, named = self.cues(text)
        for n in intro | named:
            self._mentions[n] = self._mentions.get(n, 0) + 1
        sid = self._sid(seg.get("speaker_key"))
        if intro or SELF_INTRO.search(text):
            self._intro_since = True
            if sid is not None:
                self._intro_sids.add(sid)
        for n in named:      # gọi / nhắc tên người khác: chờ người đó đáp lời (tối đa REPLY_WAIT_S) rồi mới đoán
            self._awaiting[n] = {"caller": sid, "idx": len(self.session.segments), "at": self._clock(),
                                 "waited": False}
        self._maybe_schedule()

    def _replied(self) -> Set[str]:
        """Tên vừa được gọi mà đã có một người chưa có tên (khác người gọi) nói sau đó."""
        segs, out = self.session.segments, set()
        for n, a in list(self._awaiting.items()):
            if len(segs) - a["idx"] > REPLY_WINDOW:
                self._awaiting.pop(n, None)
                continue
            caller = self._sid(a["caller"])
            for s in segs[a["idx"]:]:
                p = self.session.speakers.profile(self.session.speakers.sid_of(s.get("seq")))
                if p is not None and p.sid != caller and not p.name and not p.locked:
                    out.add(n)
                    break
        return out

    def _grown(self, targets: List[Any]) -> bool:
        return any(p.n_segments - self._seen.get(p.sid, 0) >= 3 for p in targets)

    def _due(self) -> Optional[float]:
        """Thời điểm (theo _clock) nên đoán tên lần tới; None nếu chưa có thông tin mới."""
        if not artifacts.llm_available() or self._running:
            return None
        targets = self.targets()
        if not targets:
            return None
        now, due = self._clock(), []
        if self._intro_since:
            due.append(now + 2.0)
        if self._replied():
            due.append(now + REPLY_SETTLE_S)
        waiting = [a["at"] for a in self._awaiting.values() if not a["waited"]]
        if waiting:     # chưa ai đáp: có thể câu nhắc tới người VỪA nói ("cảm ơn anh Tuấn", "người vừa nói là thầy Trung")
            due.append(min(waiting) + REPLY_WAIT_S)
        if self._new_since >= FALLBACK_EVERY_N and self._grown(targets):
            due.append(now + 2.0)
        if not due:
            return None
        return max(min(due), self._last_run + MIN_INTERVAL_S)

    def _maybe_schedule(self):
        due = self._due()
        if due is None:
            return
        if self._timer is not None:
            if self._timer_due <= due + 0.5:      # đã hẹn đoán sớm hơn
                return
            self._timer.cancel()                  # người được gọi vừa đáp lời: đoán sớm hơn lịch chờ
        self._timer_due = due
        self._timer = asyncio.create_task(self._delayed(max(0.0, due - self._clock())))

    async def _delayed(self, delay: float):
        me = asyncio.current_task()
        try:
            await asyncio.sleep(delay)
            if self._timer is me:
                self._timer = None
            await self.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("meeting.identity: suy luận nền lỗi: %s", e)
        finally:
            if self._timer is me:
                self._timer = None

    # ---------------------------------------------------------------- run ---
    async def run(self, force: bool = False) -> List[Dict[str, Any]]:
        """force=True (nút "AI đoán tên", lúc kết thúc cuộc họp): đoán ngay cho mọi người chưa có tên."""
        if self._running or not artifacts.llm_available():
            return []
        targets = self.targets(everyone=force)
        if not targets:
            return []
        if force and self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._running = True
        replied = self._replied()
        for n, a in list(self._awaiting.items()):
            if n in replied:
                self._awaiting.pop(n, None)
            else:
                a["waited"] = True     # đã đoán khi người đó chưa đáp; đáp lời sau thì đoán lại
        self._new_since, self._intro_since = 0, False
        try:
            label_map = {p.label: p.sid for p in targets}
            known = [p.to_dict(with_vector=False) for p in self.session.speakers.visible_profiles() if p.name]
            preds = await predict_identities(self.session.segments, list(label_map),
                                             meeting=self.session.meeting, known_participants=known)
            return await self._apply(preds, label_map)
        finally:
            self._running = False
            self._last_run = self._clock()
            for p in targets:
                self._seen[p.sid] = p.n_segments
            self._maybe_schedule()    # thông tin mới đến trong lúc đang đoán

    async def _apply(self, preds: List[IdentityPrediction], label_map: Dict[str, int]) -> List[Dict[str, Any]]:
        from meeting import live  # tránh import vòng ở cấp module
        results = []
        used = {p.name.casefold() for p in self.session.speakers.active_profiles() if p.name}
        for pred in sorted(preds, key=lambda x: -float(x.confidence or 0)):
            sid = label_map.get((pred.unknown_label or "").strip())
            name = clean_name(pred.predicted_name)
            conf = max(0.0, min(1.0, float(pred.confidence or 0)))
            if sid is None or not name or conf < SUGGEST_T:
                continue
            p = self.session.speakers.profile(sid)
            if p is None or p.name or p.locked or (p.sid, name.casefold()) in self._dismissed:
                continue
            evidence = _evidence_dicts(pred.evidence)
            conflict = name.casefold() in used
            auto = conf >= AUTO_APPLY_T and not conflict
            prev_iid = next((i for i, x in self._suggestions.items() if self._sid(x["sid"]) == p.sid), None)
            prev = self._suggestions.get(prev_iid) if prev_iid is not None else None
            if not auto and prev is not None and prev["suggested_name"].casefold() == name.casefold():
                # Lỗi cũ (#40): mỗi lần đoán lại thêm một thẻ "Duy Leo" giống hệt. Cùng gợi ý thì cập nhật thẻ cũ.
                if conf > prev["confidence"]:
                    prev.update(confidence=conf, reasoning=pred.reasoning, evidence=evidence, conflict=conflict)
                    await self.session.emit({**prev, "update": True})
                results.append({"action": "suggested", "inference_id": prev_iid, "sid": p.sid, "old_label": p.label,
                                "new_name": name, "role": pred.predicted_role, "confidence": prev["confidence"],
                                "reasoning": prev["reasoning"], "conflict": conflict})
                continue
            if prev is not None:          # đoán ra tên khác, hoặc tự đặt tên: thẻ gợi ý cũ không còn đúng
                await self._resolve(prev_iid, "superseded")
            iid = await asyncio.to_thread(
                db.save_identity_inference, self.session.id, p.label, name, conf, pred.reasoning, evidence,
                pred.predicted_role, "auto_applied" if auto else "pending", p.sid)
            if auto:
                used.add(name.casefold())
                res = await self.session.rename_speaker(
                    p.sid, name, role=pred.predicted_role, email=pred.predicted_email, origin="ai",
                    confidence=conf, save_voice=live.AUTO_ENROLL_AI and conf >= ENROLL_T,
                    consent_by="ai_identity_inference")
                log.info("meeting.identity: tự đặt tên %s -> %s (%.0f%%)", res["old_label"], name, conf * 100)
                results.append({"action": "applied", "inference_id": iid, "sid": p.sid, "old_label": res["old_label"],
                                "new_name": name, "role": pred.predicted_role, "confidence": conf,
                                "reasoning": pred.reasoning, "updated_segments": res["updated_segments"]})
            else:
                payload = {"type": "identity_suggestion", "inference_id": iid, "sid": p.sid, "label": p.label,
                           "suggested_name": name, "role": pred.predicted_role, "email": pred.predicted_email,
                           "confidence": conf, "reasoning": pred.reasoning, "evidence": evidence,
                           "conflict": conflict}
                self._suggestions[iid] = payload
                await self.session.emit(payload)
                results.append({"action": "suggested", "inference_id": iid, "sid": p.sid, "old_label": p.label,
                                "new_name": name, "role": pred.predicted_role, "confidence": conf,
                                "reasoning": pred.reasoning, "conflict": conflict})
        return results

    # ----------------------------------------------------------- resolve ---
    async def _resolve(self, iid: int, status: str):
        self._suggestions.pop(iid, None)
        await asyncio.to_thread(db.set_inference_status, iid, status)
        await self.session.emit({"type": "suggestion_resolved", "inference_id": iid, "status": status})

    async def accept(self, iid: int, save_voice: bool = False, name: Optional[str] = None) -> Dict[str, Any]:
        inf = self._suggestions.get(iid) or await asyncio.to_thread(db.get_inference, iid)
        if not inf:
            raise KeyError(f"Không tìm thấy gợi ý {iid}")
        sid = inf.get("sid", inf.get("speaker_key"))
        if sid is None:
            p = self.session.speakers.find_by_label(inf.get("label") or inf.get("unknown_label") or "")
            sid = p.sid if p else None
        if sid is None:
            raise KeyError("Gợi ý không còn gắn với người nói nào")
        final_name = clean_name(name or inf.get("suggested_name") or inf.get("predicted_name") or "")
        res = await self.session.rename_speaker(sid, final_name, role=inf.get("role") or inf.get("predicted_role") or "",
                                                email=inf.get("email") or "", origin="manual",
                                                confidence=float(inf.get("confidence") or 1.0), save_voice=save_voice)
        await asyncio.to_thread(db.set_inference_status, iid, "accepted")
        self._suggestions.pop(iid, None)
        await self.session.emit({"type": "suggestion_resolved", "inference_id": iid, "status": "accepted"})
        return res

    async def dismiss(self, iid: int) -> Dict[str, Any]:
        inf = self._suggestions.pop(iid, None) or await asyncio.to_thread(db.get_inference, iid) or {}
        sid = inf.get("sid", inf.get("speaker_key"))
        nm = (inf.get("suggested_name") or inf.get("predicted_name") or "").casefold()
        if sid is not None and nm:
            self._dismissed.add((int(sid), nm))
        await asyncio.to_thread(db.set_inference_status, iid, "dismissed")
        await self.session.emit({"type": "suggestion_resolved", "inference_id": iid, "status": "dismissed"})
        return {"success": True}
