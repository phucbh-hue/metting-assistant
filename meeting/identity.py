"""Semantic Identity Inference Engine (AI suy luận danh tính người nói từ ngữ cảnh hội thoại).

Trong cuộc họp, người nói chưa có mẫu giọng mang nhãn tạm "Người nói N". AI đọc nội dung trò chuyện
(xưng hô, tự giới thiệu, lời chào gọi tên, đối chiếu danh bạ MCP) để đoán tên thật:
1. Độ tin cậy >= 0.85 và tên chưa được dùng cho người khác -> tự đặt tên cho HỒ SƠ người nói
   (mọi câu cũ và mới của người đó đổi theo, tên được lưu vào meeting_speakers nên không bị mất).
2. 0.60 - 0.85 hoặc trùng tên người khác -> gửi gợi ý để chủ phòng bấm xác nhận / bỏ qua.
3. Mẫu giọng chỉ được lưu vào Voice Registry khi người dùng xác nhận (hoặc bật AUTO_ENROLL_VOICES=1),
   vì vector giọng nói là dữ liệu sinh trắc học (Nghị định 13/2023/NĐ-CP).

IdentityEngine chỉ gọi LLM khi có thông tin mới: câu mới nhắc tới tên người chưa biết theo cách xưng hô tiếng Việt
("anh Tuấn", "thầy Minh", "Tuấn ơi", "tôi là Phúc", tối đa 3 lần đầu mỗi tên) hoặc có câu tự giới thiệu; ngoài ra cứ
IDENTITY_FALLBACK_SEGMENTS câu mới mới chạy lại một lần. Tối đa 1 lần mỗi IDENTITY_MIN_INTERVAL_S giây, gom tất cả
người nói chưa định danh vào MỘT lần gọi. Phát lại 5 cuộc họp thật (#31, #36, #37, #38, #40): 84 lượt gọi còn 25.
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

AUTO_APPLY_T = 0.85
SUGGEST_T = 0.60
MIN_INTERVAL_S = float(os.getenv("IDENTITY_MIN_INTERVAL_S", "20"))
FALLBACK_EVERY_N = int(os.getenv("IDENTITY_FALLBACK_SEGMENTS", "40"))   # không có tên mới: cứ ~40 câu mới chạy lại một lần
NAME_EVIDENCE_MAX = 3          # một tên được nhắc lại nhiều lần thì chỉ 3 lần đầu là thông tin mới
SELF_INTRO = re.compile(r"(tên\s+(là|tôi|em|mình|anh|chị|tớ)|giới thiệu|my name|\bi am\b|\bi'm\b|\bthis is\b)",
                        re.IGNORECASE)
# Từ xưng hô đứng trước tên ("anh Tuấn", "thầy Minh"); "là" / "tên" đứng trước tên khi tự giới thiệu
_HONORIFIC_WORDS = {"anh", "chị", "em", "bạn", "cô", "chú", "bác", "ông", "bà", "thầy", "sếp", "mr", "ms", "mrs", "dr"}
# Từ viết hoa giữa câu nhưng không phải tên người
_CAP_STOP = {"ok", "okay", "dạ", "vâng", "ừ", "ờ", "à", "ạ", "trời", "giời", "chúa", "phật", "mẹ", "má", "bố", "ba",
             "con", "cháu", "urbox", "jira", "ai", "mcp", "slide", "dashboard", "api",
             "google", "facebook", "youtube", "zalo", "tiktok", "excel", "word", "power", "bi", "sprint", "mega", "sale",
             "podcast", "online", "việt", "nam", "hà", "nội", "sài", "gòn", "tp", "hcm", "redis", "postgres", "staging"}
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


def format_transcript(segments: List[Dict[str, Any]], limit: int = 80) -> str:
    lines = []
    for s in segments[-limit:]:
        lines.append(f"[{_fmt_t(s.get('t_start'))}] {s.get('speaker_label') or 'Không rõ'}: {s.get('text', '')}")
    return "\n".join(lines)


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
    prompt = f"""## Cuộc họp: {meeting.get('title', '')}
Chương trình: {agenda}
Người tham dự dự kiến: {expected}

## Người nói đã biết tên trong buổi họp:
{known}

## Danh bạ nội bộ (MCP):
{dir_lines}

## Transcript (mới nhất ở cuối):
{format_transcript(segments)}

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
        self._cue_since = False           # có thông tin mới (tên mới / tự giới thiệu) từ lần chạy trước
        self._mentions: Dict[str, int] = {}
        self._timer: Optional[asyncio.Task] = None
        self._dismissed: Set[Tuple[int, str]] = set()
        self._suggestions: Dict[int, Dict[str, Any]] = {}

    # --------------------------------------------------------------- state ---
    def reset(self):
        """Hồ sơ người nói vừa được dựng lại: bỏ các gợi ý cũ (gắn với sid cũ)."""
        self._suggestions.clear()
        self._dismissed.clear()

    def targets(self) -> List[Any]:
        return [p for p in self.session.speakers.visible_profiles() if not p.name and not p.locked]

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
    def fresh_names(self, text: str) -> Set[str]:
        """Tên người được nhắc theo cách xưng hô ("anh Tuấn", "Tuấn ơi", "tôi là Bùi Hồng Phúc") mà còn là thông tin mới.

        Bỏ tên trợ lý, tên người nói đã biết, và tên đã được nhắc từ 3 lần trở lên."""
        cfg = llm.assistant_config()
        skip = {w.lower() for n in [cfg.get("name", "")] + list(cfg.get("aliases") or []) for w in str(n).split()}
        for p in self.session.speakers.visible_profiles():
            if p.name:
                skip |= {w.lower() for w in str(p.name).split()}

        def is_name(w: str) -> bool:
            lw = w.lower()
            return (w[:1].isalpha() and w[0].isupper() and len(w) > 1 and lw not in _CAP_STOP
                    and lw not in skip and lw not in _HONORIFIC_WORDS)

        toks = re.findall(r"[^\W\d_]+|[.!?…,;:]", text or "")
        found: Set[str] = set()
        for i, w in enumerate(toks):
            lw = w.lower()
            if lw in _HONORIFIC_WORDS or lw in ("là", "tên"):          # anh Tuấn / là Bùi Hồng Phúc
                run = []
                for x in toks[i + 1:i + 5]:
                    if not is_name(x):
                        break
                    run.append(x.lower())
                if run:
                    found.add(" ".join(run))
            elif lw == "ơi":                                           # Tuấn ơi
                run = []
                for x in reversed(toks[max(0, i - 4):i]):
                    if not is_name(x):
                        break
                    run.insert(0, x.lower())
                if run:
                    found.add(" ".join(run))
        return {n for n in found if self._mentions.get(n, 0) < NAME_EVIDENCE_MAX}

    def notify(self, seg: Dict[str, Any]):
        self._new_since += 1
        text = seg.get("text") or ""
        names = self.fresh_names(text)
        for n in names:
            self._mentions[n] = self._mentions.get(n, 0) + 1
        if names or SELF_INTRO.search(text):
            self._cue_since = True
        if not artifacts.llm_available() or self._running or self._timer is not None:
            return
        if not (self._cue_since or self._new_since >= FALLBACK_EVERY_N):
            return
        if not self.targets():
            return
        delay = max(2.0, MIN_INTERVAL_S - (time.monotonic() - self._last_run))
        self._timer = asyncio.create_task(self._delayed(delay))

    async def _delayed(self, delay: float):
        try:
            await asyncio.sleep(delay)
            self._timer = None
            await self.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("meeting.identity: suy luận nền lỗi: %s", e)
        finally:
            self._timer = None

    # ---------------------------------------------------------------- run ---
    async def run(self, force: bool = False) -> List[Dict[str, Any]]:
        if self._running or not artifacts.llm_available():
            return []
        targets = self.targets()
        if not targets:
            return []
        self._running = True
        self._new_since, self._cue_since = 0, False
        try:
            label_map = {p.label: p.sid for p in targets}
            known = [p.to_dict(with_vector=False) for p in self.session.speakers.visible_profiles() if p.name]
            preds = await predict_identities(self.session.segments, list(label_map),
                                             meeting=self.session.meeting, known_participants=known)
            return await self._apply(preds, label_map)
        finally:
            self._running = False
            self._last_run = time.monotonic()

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
            iid = await asyncio.to_thread(
                db.save_identity_inference, self.session.id, p.label, name, conf, pred.reasoning, evidence,
                pred.predicted_role, "auto_applied" if auto else "pending", p.sid)
            if auto:
                used.add(name.casefold())
                res = await self.session.rename_speaker(
                    p.sid, name, role=pred.predicted_role, email=pred.predicted_email, origin="ai",
                    confidence=conf, save_voice=live.AUTO_ENROLL_AI, consent_by="ai_identity_inference")
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
