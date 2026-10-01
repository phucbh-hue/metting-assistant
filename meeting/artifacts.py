"""Multi-Modal Artifacts Generator & Conversational Co-Design Engine.

Chức năng:
1. Biên bản cuộc họp & Action Items (Meeting Minutes) định dạng Markdown / HTML.
2. Vẽ Diagram (Mermaid.js) & Hình ảnh concept.
3. Sinh Web Design tương tác hoàn chỉnh (HTML5 + Tailwind CSS) chạy trong Sandbox.
4. Co-Design đàm thoại hai chiều: Tiếp nhận góp ý của người dùng ("Sửa nút sang màu tím",
   "Thêm card thống kê") để tinh chỉnh trực tiếp mã nguồn web/diagram/minutes theo phiên bản.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from meeting import db, mcp

log = logging.getLogger("meeting.artifacts")

AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.6-flash")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
PROVIDER = (os.getenv("LLM_PROVIDER") or "claude").strip().lower()

_anthropic_client = None
_gemini_client = None

SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_skill_cache: Dict[str, str] = {}


def load_skill(name: str) -> str:
    """Hướng dẫn chuyên môn cho từng loại sản phẩm (meeting/skills/<name>.md), nối vào system prompt."""
    if name not in _skill_cache:
        p = SKILLS_DIR / f"{name}.md"
        _skill_cache[name] = p.read_text(encoding="utf-8").strip() if p.exists() else ""
    return _skill_cache[name]


def with_skill(system: str, name: str) -> str:
    sk = load_skill(name)
    return f"{system}\n\n{sk}" if sk else system


_STATS_RE = re.compile(r"ai\s+nói\s+(nhiều|ít)|thời\s*(gian|lượng)\s*(nói|phát biểu)|tỉ\s*lệ\s*(nói|phát biểu)|"
                       r"thống\s*kê\s*(cuộc họp|phát biểu|người nói)|nói\s*bao\s*(nhiêu|lâu)|talk\s*time", re.I)


def wants_stats(text: str) -> bool:
    """Người dùng có hỏi về thống kê phát biểu (ai nói bao lâu) không. Mặc định sản phẩm nói về NỘI DUNG họp."""
    return bool(_STATS_RE.search(text or ""))


def llm_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("GEMINI_API_KEY"))


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _anthropic_client


def _gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _gemini_client


async def _call_llm(system: str, prompt: str, max_tokens: int = 4000) -> str:
    """Gọi LLM (Claude hoặc Gemini theo LLM_PROVIDER) với fallback sang provider còn lại."""
    return (await _call_llm_raw(system, prompt, max_tokens) or "").replace(chr(0x2014), "-")   # quy ước nội dung: "-" thay cho gạch dài


async def _call_llm_raw(system: str, prompt: str, max_tokens: int = 4000) -> str:
    if not llm_available():
        raise RuntimeError("Chưa cấu hình ANTHROPIC_API_KEY hoặc GEMINI_API_KEY")
    use_claude_first = PROVIDER == "claude" or not os.getenv("GEMINI_API_KEY")
    errors = []
    if use_claude_first and os.getenv("ANTHROPIC_API_KEY"):
        try:
            resp = await _anthropic().messages.create(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}]
            )
            return "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        except Exception as e:
            errors.append(f"Claude: {e}")
            log.warning("meeting.artifacts: Claude error (%s), fallback to Gemini", e)

    if os.getenv("GEMINI_API_KEY"):
        try:
            resp = await asyncio.to_thread(
                _gemini().interactions.create,
                model=AGENT_MODEL,
                system_instruction=system,
                input=prompt
            )
            return resp.output_text
        except Exception as e:
            errors.append(f"Gemini: {e}")
            log.warning("meeting.artifacts: Gemini error (%s)", e)

    if not use_claude_first and os.getenv("ANTHROPIC_API_KEY"):
        resp = await _anthropic().messages.create(
            model=CLAUDE_MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}])
        return "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    raise RuntimeError("; ".join(errors) or "Không gọi được LLM")


# ==============================================================================
# 1. BIÊN BẢN CUỘC HỌP (MEETING MINUTES & ACTION ITEMS)
# ==============================================================================
MINUTES_SYSTEM = """Bạn là Thư ký Cuộc họp Thông minh (Executive Meeting Scribe).
Nhiệm vụ: Phân tích toàn bộ transcript cuộc họp và dữ liệu MCP để soạn thảo một Biên bản Cuộc họp chuyên nghiệp.

Cấu trúc bắt buộc (định dạng Markdown):
# BIÊN BẢN CUỘC HỌP: [Tiêu đề cuộc họp]
**Thời gian:** [Thời gian] | **Chủ trì:** [Host] | **Thành viên tham dự:** [Danh sách]

## 1. Tóm Tắt Điều Hành (Executive Summary)
(2-3 đoạn ngắn nêu bật trọng tâm, các vấn đề nổi cộm và mục tiêu đạt được)

## 2. Các Quyết Định Quan Trọng Đã Thống Nhất (Key Decisions)
- [Quyết định 1]: Chi tiết, lý do, người đề xuất.
- [Quyết định 2]: ...

## 3. Bảng Phân Công Nhiệm Vụ (Action Items Matrix)
| STT | Nhiệm vụ (Action Item) | Người phụ trách (Assignee) | Hạn chót (Deadline) | Mức độ ưu tiên | Ghi chú & Căn cứ |
|---|---|---|---|---|---|
| 1 | ... | ... | ... | Cao / TB / Thấp | Trích dẫn ngắn |

## 4. Vấn Đề Tồn Đọng / Rủi Ro Cần Theo Dõi (Open Questions & Risks)
- ...

Yêu cầu: Viết tiếng Việt chuẩn xác, tên người nói và nhiệm vụ phải đối chiếu đúng với nhân sự công ty."""


def _fmt_date(ts: Any) -> str:
    try:
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


async def generate_meeting_minutes(meeting_id: int, segments: List[Dict[str, Any]],
                                   title: str = "Cuộc họp nội bộ", meeting: Optional[Dict[str, Any]] = None,
                                   speakers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    lines = [f"{s.get('speaker_label', 'Không rõ')}: {s.get('text', '')}" for s in segments]
    transcript_text = "\n".join(lines) if lines else "(Chưa có nội dung)"
    meeting = meeting or {}
    people = "\n".join(
        f"- {p.get('label')}" + (f" ({p.get('role')})" if p.get("role") else "")
        + f": {p.get('n_segments', 0)} câu" for p in (speakers or [])) or "(không rõ)"
    agenda = "\n".join(f"- {a}" for a in (meeting.get("agenda") or []) if isinstance(a, str)) or "(không có)"

    prompt = (f"Tiêu đề: {title}\nThời gian bắt đầu: {_fmt_date(meeting.get('started_at'))}\n"
              f"Loại cuộc họp: {meeting.get('meeting_type', '')}\nChương trình:\n{agenda}\n\n"
              f"Người nói trong buổi họp (nhãn 'Người nói N' là người chưa xác định được tên):\n{people}\n\n"
              f"Transcript cuộc họp:\n{transcript_text}\n\nHãy lập biên bản cuộc họp chi tiết theo cấu trúc. "
              "Ngày tháng ghi theo dd/mm/yyyy.")
    content = await _call_llm(MINUTES_SYSTEM, prompt, max_tokens=3500)

    # Lưu vào DB
    aid = db.save_artifact(
        meeting_id=meeting_id,
        kind="minutes",
        title=f"Biên bản: {title}",
        content=content,
        prompt_trigger="Tự động lập biên bản"
    )
    return {"id": aid, "kind": "minutes", "title": f"Biên bản: {title}", "content": content}


# ==============================================================================
# 2. VẼ DIAGRAM (MERMAID.JS)
# ==============================================================================
DIAGRAM_SYSTEM = """Bạn là Chuyên gia Kiến trúc Hệ thống & Trực quan hóa Sơ đồ (Diagram Specialist).
Nhiệm vụ: Dựa vào yêu cầu và bối cảnh cuộc họp, hãy sinh mã Mermaid.js chuẩn xác, đẹp mắt và hợp lệ.

Hỗ trợ các loại sơ đồ:
- `graph TD` hoặc `graph LR`: Luồng quy trình, kiến trúc dịch vụ, luồng dữ liệu.
- `sequenceDiagram`: Luồng tương tác giữa các service, API calls, webhooks, auth.
- `classDiagram` / `erDiagram`: Thiết kế schema database, thực thể dữ liệu.
- `mindmap`: Sơ đồ tư duy brainstorming ý tưởng cuộc họp.

Quy tắc BẮT BUỘC:
- CHỈ trả về đoạn mã Mermaid nằm trong khối ```mermaid ... ```.
- Tuyệt đối không dùng ký tự đặc biệt làm hỏng cú pháp Mermaid.
- Đặt nhãn rõ ràng bằng tiếng Việt / tiếng Anh kỹ thuật."""


MERMAID_TYPES = ("flowchart", "graph", "sequenceDiagram", "stateDiagram-v2", "stateDiagram", "erDiagram", "gantt",
                 "mindmap", "classDiagram", "journey", "timeline")


def extract_mermaid(raw: str) -> str:
    m = re.search(r"```(?:mermaid)?\s*(.*?)\s*```", raw or "", re.DOTALL)
    code = (m.group(1) if m else (raw or "")).strip()
    return re.sub(r"^mermaid\s*\n", "", code)


def mermaid_problem(code: str) -> str:
    """Kiểm tra cú pháp Mermaid ở mức cơ bản (không render được ở server). Rỗng = tạm ổn."""
    lines = [ln for ln in (code or "").splitlines() if ln.strip() and not ln.strip().startswith("%%")]
    if not lines:
        return "không có mã"
    head = lines[0].strip()
    if not any(head.startswith(t) for t in MERMAID_TYPES):
        return f"dòng đầu phải là loại sơ đồ (flowchart, sequenceDiagram...), đang là '{head[:40]}'"
    if "```" in code:
        return "còn dấu ``` trong mã"
    if head.startswith(("flowchart", "graph")):
        for ln in lines[1:]:
            # nhãn có dấu ngoặc / hai chấm mà không nằm trong ngoặc kép -> Mermaid lỗi
            for lab in re.findall(r"[\[{(]([^\]\})\"]*)[\]})]", ln):
                if re.search(r"[():;,/]", lab) and not lab.startswith('"'):
                    return f"nhãn '{lab[:30]}' chứa ký tự đặc biệt nhưng chưa đặt trong ngoặc kép"
    if head.startswith("sequenceDiagram") and not any(re.search(r"->>|-->>|->|-->", ln) for ln in lines[1:]):
        return "sequenceDiagram không có thông điệp nào"
    if head.startswith("gantt") and not any(ln.strip().startswith("dateFormat") for ln in lines):
        return "gantt thiếu dòng dateFormat"
    return ""


def diagram_title(code: str) -> str:
    m = re.search(r"^\s*title\s*:?\s*(.+)$", code or "", re.M)
    return m.group(1).strip().strip('"')[:60] if m else ""


async def generate_diagram(meeting_id: int, prompt_request: str,
                           context_text: str = "", data_text: str = "") -> Dict[str, Any]:
    user_prompt = (f"Yêu cầu vẽ sơ đồ: {prompt_request}\n\nDữ liệu tra cứu nội bộ:\n{data_text or '(không có)'}\n\n"
                   f"Nội dung cuộc họp:\n{context_text}")
    system = with_skill(DIAGRAM_SYSTEM, "diagram")
    mermaid_code, problem = "", ""
    for attempt in range(2):
        raw = await _call_llm(system, user_prompt if not problem else
                              f"{user_prompt}\n\nMã lần trước bị lỗi: {problem}. Hãy sửa và trả về mã Mermaid hợp lệ.",
                              max_tokens=2500)
        mermaid_code = extract_mermaid(raw)
        problem = mermaid_problem(mermaid_code)
        if not problem:
            break
    if problem:
        raise RuntimeError(f"Sơ đồ chưa hợp lệ: {problem}")

    title = f"Sơ đồ: {diagram_title(mermaid_code) or prompt_request[:50]}"
    aid = db.save_artifact(
        meeting_id=meeting_id,
        kind="diagram",
        title=title,
        content=mermaid_code,
        prompt_trigger=prompt_request
    )
    return {"id": aid, "kind": "diagram", "title": title, "content": mermaid_code}


# ==============================================================================
# 3. SINH WEB DESIGN TƯƠNG TÁC (INTERACTIVE WEB SANDBOX)
# ==============================================================================
WEB_SYSTEM = """Bạn là Senior Frontend UI/UX Designer & Prototyping Specialist.
Nhiệm vụ: Thiết kế giao diện web tương tác hoàn chỉnh (Landing Page, Dashboard, Tool UI, Form)
dựa trên ý tưởng thảo luận trong cuộc họp.

Quy tắc kỹ thuật BẮT BUỘC:
1. Trả về DUY NHẤT một tài liệu HTML5 độc lập, nhúng CDN:
   - Tailwind CSS: `<script src="https://cdn.tailwindcss.com"></script>`
   - Phosphor Icons hoặc Lucide Icons qua CDN (hoặc SVG inline)
   - Font Inter: `<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">`
2. Thiết kế hiện đại, sang trọng (phong cách Stripe / Linear / Vercel), hỗ trợ responsive.
3. Kèm JavaScript nội tuyến (`<script>...</script>`) để xử lý các tương tác:
   - Chuyển tab, mở modal, toggle dark/light mode, bấm nút đổi trạng thái, lọc dữ liệu mẫu.
4. Dùng số liệu thật được cung cấp (ticket, người phụ trách, con số trong cuộc họp); dữ liệu mẫu phải ghi rõ là mẫu.
5. Có thẻ <title> ngắn gọn. Trả về mã trong khối ```html ... ```."""


async def generate_web_sandbox(meeting_id: int, prompt_request: str,
                               context_text: str = "", data_text: str = "") -> Dict[str, Any]:
    user_prompt = (f"Yêu cầu thiết kế giao diện web: {prompt_request}\n\n"
                   f"Dữ liệu tra cứu nội bộ (dùng số liệu thật này nếu liên quan):\n{data_text or '(không có)'}\n\n"
                   f"Ý tưởng & dữ liệu trong cuộc họp:\n{context_text}")
    raw = await _call_llm(WEB_SYSTEM, user_prompt, max_tokens=8000)

    m = re.search(r"```html\s*(.*?)\s*```", raw, re.DOTALL)
    html_code = m.group(1).strip() if m else re.sub(r"^```(?:html)?\s*", "", raw.strip())
    if "<" not in html_code:
        raise RuntimeError("AI chưa tạo được mã giao diện")

    t = re.search(r"<title>\s*([^<]{3,80}?)\s*</title>", html_code, re.I)
    title = f"Giao diện: {(t.group(1) if t else prompt_request)[:50]}"
    aid = db.save_artifact(
        meeting_id=meeting_id,
        kind="web_design",
        title=title,
        content=html_code,
        prompt_trigger=prompt_request
    )
    return db.get_artifact(aid)


def _json_from_text(text: str) -> Any:
    """Lấy object JSON đầu tiên trong phản hồi LLM (bỏ ```json ... ```)."""
    if not text:
        return {}
    clean = re.sub(r"```(?:json)?\s*", "", text)
    clean = re.sub(r"```\s*", "", clean).strip()
    try:
        return json.loads(clean)
    except Exception:
        pass
    s, e = clean.find("{"), clean.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(clean[s:e + 1])
        except Exception:
            pass
    return {}


# ==============================================================================
# 3b. BỘ SLIDE TRÌNH BÀY (PRESENTATION DECK)
# ==============================================================================
SLIDE_LAYOUTS = ("title", "bullets", "two_column", "quote", "metrics")

SLIDES_SYSTEM = """Bạn là chuyên gia soạn slide thuyết trình cho cuộc họp nội bộ UrBox.
Dựa vào yêu cầu, nội dung cuộc họp và dữ liệu tra cứu, soạn một bộ slide NGẮN GỌN, dễ trình bày.

Quy tắc:
- 4-8 slide. Slide đầu dùng layout "title" (bullets[0] là phụ đề). Các slide sau dùng "bullets",
  "two_column" (ý chia 2 cột), "metrics" (mỗi ý dạng "Nhãn: Giá trị") hoặc "quote".
- Mỗi slide tối đa 5 ý, mỗi ý tối đa 14 từ, viết cụ thể bằng tên người, số liệu, mốc thời gian có trong dữ liệu.
- "notes": lời nhắc cho người trình bày (1-3 câu, nói gì ở slide này), dùng để nhắc bài.
- Ngày ghi dd/mm/yyyy, tiền ghi dạng 1.000.000đ. Không bịa số liệu không có trong dữ liệu.

Chỉ trả về MỘT JSON:
{"title": "Tên bộ slide", "slides": [{"title": "...", "layout": "bullets", "bullets": ["...", "..."], "notes": "..."}]}"""

SLIDES_REFINE_SYSTEM = """Bạn cùng người dùng chỉnh bộ slide đang trình chiếu trong cuộc họp.
Áp dụng đúng yêu cầu sửa (thường là cho slide đang xem), giữ nguyên các slide không liên quan, giữ cùng cấu trúc JSON.
Chỉ trả về MỘT JSON:
{"chat_message": "Một câu tiếng Việt cho biết đã sửa gì", "focus_slide": <số thứ tự slide liên quan nhất, bắt đầu từ 1>,
 "deck": {"title": "...", "slides": [{"title": "...", "layout": "...", "bullets": ["..."], "notes": "..."}]}}"""


def normalize_deck(data: Any) -> Optional[Dict[str, Any]]:
    """Chuẩn hóa bộ slide từ LLM; None nếu không hợp lệ."""
    if isinstance(data, dict) and isinstance(data.get("deck"), dict):
        data = data["deck"]
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        return None
    slides = []
    for s in data["slides"][:15]:
        if isinstance(s, str):
            s = {"title": s}
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()[:120]
        bullets = s.get("bullets") or s.get("points") or []
        if isinstance(bullets, str):
            bullets = re.split(r"\n+|•", bullets)
        bullets = [str(b).strip(" -•\t")[:200] for b in bullets if str(b).strip(" -•\t")][:6]
        if not title and not bullets:
            continue
        layout = s.get("layout") if s.get("layout") in SLIDE_LAYOUTS else ("title" if not slides else "bullets")
        slides.append({"title": title or f"Slide {len(slides) + 1}", "layout": layout, "bullets": bullets,
                       "notes": str(s.get("notes") or "").strip()[:600]})
    if not slides:
        return None
    return {"title": str(data.get("title") or slides[0]["title"]).strip()[:120], "slides": slides}


async def generate_slides(meeting_id: int, prompt_request: str, context_text: str = "",
                          data_text: str = "") -> Dict[str, Any]:
    user_prompt = (f"Yêu cầu: {prompt_request}\n\nDữ liệu tra cứu (nếu có):\n{data_text or '(không có)'}\n\n"
                   f"Nội dung cuộc họp gần nhất:\n{context_text or '(chưa có)'}")
    deck = normalize_deck(_json_from_text(await _call_llm(with_skill(SLIDES_SYSTEM, "slides"), user_prompt, max_tokens=3500)))
    if deck is None:
        raise RuntimeError("AI chưa tạo được bộ slide hợp lệ, hãy thử lại với yêu cầu cụ thể hơn")
    title = f"Slide: {deck['title'][:60]}"
    aid = db.save_artifact(meeting_id=meeting_id, kind="slides", title=title,
                           content=json.dumps(deck, ensure_ascii=False), prompt_trigger=prompt_request)
    return db.get_artifact(aid)


# ==============================================================================
# 3c. NHẬN XÉT NHANH (để trợ lý đọc to trong lúc chờ phân tích)
# ==============================================================================
INSIGHT_KINDS = ("risk", "todo", "improve", "good", "info")

INSIGHTS_SYSTEM = """Bạn là trợ lý cuộc họp, vừa đọc transcript. Nêu tối đa 4 nhận xét NGẮN, CỤ THỂ, có ích ngay:
rủi ro hoặc vấn đề, việc chưa có người phụ trách hay hạn chót, điểm cần làm rõ hoặc cải thiện, điều đang làm tốt.
Mỗi nhận xét là MỘT câu nói tự nhiên (tối đa 25 từ), nêu đúng tên người, số liệu, mốc thời gian trong cuộc họp,
không chung chung, không lặp lại transcript. Không có gì đáng nói thì trả về danh sách rỗng.
Chỉ trả về MỘT JSON: {"insights": [{"kind": "risk|todo|improve|good", "text": "..."}]}"""


def normalize_insights(data: Any) -> List[Dict[str, str]]:
    items = data.get("insights") if isinstance(data, dict) else data
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            it = {"kind": "info", "text": it}
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if text:
            out.append({"kind": it.get("kind") if it.get("kind") in INSIGHT_KINDS else "info", "text": text[:300]})
    return out[:4]


async def meeting_insights(segments: List[Dict[str, Any]], meeting: Optional[Dict[str, Any]] = None,
                           focus: str = "") -> List[Dict[str, str]]:
    if not segments:
        return []
    lines = "\n".join(f"{s.get('speaker_label', 'Không rõ')}: {s.get('text', '')}" for s in segments[-120:])
    prompt = (f"Cuộc họp: {(meeting or {}).get('title', '')}\nTrọng tâm: {focus or 'toàn bộ cuộc họp'}\n\n"
              f"Transcript:\n{lines}")
    return normalize_insights(_json_from_text(await _call_llm(INSIGHTS_SYSTEM, prompt, max_tokens=800)))


# ==============================================================================
# 3d. THỐNG KÊ CUỘC HỌP (số liệu thật, tính từ transcript - dùng cho dashboard và báo cáo)
# ==============================================================================
def _words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def meeting_facts(segments: List[Dict[str, Any]], meeting: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Thời lượng nói, số câu, số từ của từng người và nhịp trao đổi theo phút."""
    per: Dict[str, Dict[str, Any]] = {}
    buckets: Dict[int, int] = {}
    end = 0.0
    for s in segments:
        lab = s.get("speaker_label") or "Không rõ"
        t0, t1 = float(s.get("t_start") or 0), float(s.get("t_end") or 0)
        n = _words(s.get("text", ""))
        p = per.setdefault(lab, {"speaker": lab, "segments": 0, "talk_s": 0.0, "words": 0})
        p["segments"] += 1
        p["talk_s"] += max(0.0, t1 - t0)
        p["words"] += n
        buckets[int(t0 // 60)] = buckets.get(int(t0 // 60), 0) + n
        end = max(end, t1)
    total = sum(p["talk_s"] for p in per.values()) or 1.0
    speakers = sorted(per.values(), key=lambda p: -p["talk_s"])
    for p in speakers:
        p["talk_s"] = round(p["talk_s"], 1)
        p["share_pct"] = round(100.0 * p["talk_s"] / total, 1)
    minutes = int(end // 60) + 1 if segments else 0
    return {"title": (meeting or {}).get("title", ""), "duration_min": round(end / 60.0, 1),
            "segments": len(segments), "speakers": speakers,
            "words_per_minute": [{"minute": m + 1, "words": buckets.get(m, 0)} for m in range(minutes)][-60:]}


# ==============================================================================
# 3e. DASHBOARD SỐ LIỆU (kiểu Power BI: thẻ KPI + biểu đồ + bảng)
# ==============================================================================
CHART_TYPES = ("bar", "hbar", "line", "area", "pie", "donut", "stacked_bar", "table")
_CHART_ALIASES = {"column": "bar", "columns": "bar", "horizontal_bar": "hbar", "barh": "hbar", "doughnut": "donut",
                  "stacked": "stacked_bar", "stacked_column": "stacked_bar", "pie_chart": "pie", "spline": "line"}

DASHBOARD_SYSTEM = """Bạn là chuyên gia phân tích dữ liệu, dựng dashboard kiểu Power BI để chiếu ngay trong cuộc họp nội bộ UrBox.
Dựa vào yêu cầu, dữ liệu tra cứu, thống kê cuộc họp và transcript, chỉ trả về MỘT JSON:
{"title": "...", "subtitle": "1 câu: phạm vi dữ liệu",
 "kpis": [{"label": "Ticket đang mở", "value": "4", "unit": "ticket", "delta": "1 quá hạn", "trend": "up|down|flat", "note": "..."}],
 "charts": [
   {"type": "bar|hbar|line|area|pie|donut|stacked_bar", "title": "...", "labels": ["..."],
    "series": [{"name": "...", "data": [3, 1]}], "unit": "...", "note": "...", "sample": false},
   {"type": "table", "title": "...", "columns": ["..."], "rows": [["...", "..."]]}],
 "highlights": ["Nhận xét quan trọng nhất, 1 câu cụ thể"],
 "source": "Nguồn dữ liệu"}
Quy tắc:
- 2-4 KPI, 2-5 biểu đồ; có danh sách (ticket, việc cần làm) thì thêm 1 bảng. Chọn loại biểu đồ hợp dữ liệu:
  so sánh nhóm -> bar/hbar, xu hướng theo thời gian -> line/area, tỉ trọng ít nhóm -> donut.
- "data" chỉ chứa số (không kèm đơn vị, không dấu phân cách). Nhãn ngắn, tối đa 24 ký tự.
- Chỉ dùng số liệu có trong dữ liệu được cung cấp hoặc đếm/cộng trực tiếp từ đó (thống kê cuộc họp là số liệu thật).
  Không tự ước lượng phần trăm hoàn thành, doanh số hay tiến độ khi dữ liệu không nêu con số; KPI không có số thật thì bỏ.
  Nếu buộc phải minh họa vì thiếu dữ liệu, đặt "sample": true và ghi rõ trong note.
- Ngày dd/mm/yyyy, tiền dạng 1.000.000đ trong value của KPI. Tiếng Việt, không dùng gạch dài."""

DASHBOARD_REFINE_SYSTEM = """Bạn cùng người dùng chỉnh dashboard đang chiếu trong cuộc họp (thêm/bớt/đổi loại biểu đồ, đổi KPI...).
Áp dụng đúng yêu cầu, giữ nguyên phần không liên quan, giữ cùng cấu trúc JSON và quy tắc dữ liệu (không bịa số).
Chỉ trả về MỘT JSON: {"chat_message": "Một câu cho biết đã sửa gì", "dashboard": {...cùng cấu trúc...}}"""


def _num(x: Any) -> Optional[float]:
    """'1.000.000' -> 1000000, '3,5' -> 3.5, '12%' -> 12; không phải số -> None."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if x == x and abs(x) != float("inf") else None
    s = re.sub(r"(đồng|vnđ|vnd|đ|%|\s)", "", str(x or "").strip().lower())
    if re.fullmatch(r"-?\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    elif re.fullmatch(r"-?\d+,\d+", s):
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalize_dashboard(data: Any) -> Optional[Dict[str, Any]]:
    """Chuẩn hóa dashboard từ LLM (cắt bớt, ép kiểu số, khớp nhãn với dữ liệu); None nếu không dùng được."""
    if isinstance(data, dict) and isinstance(data.get("dashboard"), dict):
        data = data["dashboard"]
    if not isinstance(data, dict):
        return None
    kpis = []
    for k in (data.get("kpis") or [])[:6]:
        if not isinstance(k, dict):
            continue
        label = str(k.get("label") or "").strip()[:40]
        value = str(k.get("value") if k.get("value") is not None else "").strip()[:24]
        if label and value:
            kpis.append({"label": label, "value": value, "unit": str(k.get("unit") or "").strip()[:16],
                         "delta": str(k.get("delta") or "").strip()[:48],
                         "trend": k.get("trend") if k.get("trend") in ("up", "down", "flat") else "",
                         "note": str(k.get("note") or "").strip()[:140]})
    charts = []
    for c in (data.get("charts") or [])[:8]:
        if not isinstance(c, dict):
            continue
        typ = str(c.get("type") or "bar").strip().lower()
        typ = _CHART_ALIASES.get(typ, typ)
        if typ not in CHART_TYPES:
            typ = "bar"
        item = {"type": typ, "title": str(c.get("title") or "").strip()[:80] or f"Biểu đồ {len(charts) + 1}",
                "note": str(c.get("note") or "").strip()[:180], "sample": bool(c.get("sample")),
                "unit": str(c.get("unit") or "").strip()[:16]}
        if typ == "table":
            cols = [str(x).strip()[:40] for x in (c.get("columns") or [])][:8]
            rows = [[str(v).strip()[:90] for v in r][:max(1, len(cols) or 8)]
                    for r in (c.get("rows") or []) if isinstance(r, list) and r][:40]
            if not rows:
                continue
            item.update({"columns": cols or [f"Cột {i + 1}" for i in range(len(rows[0]))], "rows": rows})
            charts.append(item)
            continue
        raw_series = c.get("series")
        if not raw_series and isinstance(c.get("data"), list):
            raw_series = [{"name": item["title"], "data": c["data"]}]
        series = []
        for s in (raw_series if isinstance(raw_series, list) else [])[:6]:
            if not isinstance(s, dict):
                continue
            vals = [_num(v) for v in (s.get("data") or [])][:60]
            if any(v is not None for v in vals):
                series.append({"name": str(s.get("name") or "").strip()[:40], "data": [v or 0 for v in vals]})
        if not series:
            continue
        labels = [str(x).strip()[:32] for x in (c.get("labels") or [])][:60]
        n = min(len(labels), max(len(s["data"]) for s in series)) if labels else max(len(s["data"]) for s in series)
        labels = labels[:n] if labels else [str(i + 1) for i in range(n)]
        for s in series:
            s["data"] = (s["data"] + [0] * n)[:n]
        if typ in ("pie", "donut"):
            series = series[:1]
        item.update({"labels": labels, "series": series})
        charts.append(item)
    if not kpis and not charts:
        return None
    return {"title": str(data.get("title") or "Dashboard").strip()[:100],
            "subtitle": str(data.get("subtitle") or "").strip()[:180],
            "kpis": kpis, "charts": charts[:6],
            "highlights": [str(h).strip()[:220] for h in (data.get("highlights") or []) if str(h).strip()][:5],
            "source": str(data.get("source") or "").strip()[:200]}


def _data_prompt(prompt_request: str, context_text: str, data_text: str, facts: Optional[Dict[str, Any]]) -> str:
    stats = (f"## Thống kê phát biểu (người dùng có hỏi; số liệu thật, tính tự động):\n"
             f"{json.dumps(facts, ensure_ascii=False)}\n\n") if facts and wants_stats(prompt_request) else ""
    return (f"Yêu cầu: {prompt_request}\n\n{stats}"
            f"## Dữ liệu tra cứu nội bộ:\n{data_text or '(không có)'}\n\n"
            f"## Nội dung cuộc họp (toàn bộ, theo thứ tự thời gian):\n{context_text or '(chưa có)'}")


async def generate_dashboard(meeting_id: int, prompt_request: str, context_text: str = "", data_text: str = "",
                             facts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = await _call_llm(with_skill(DASHBOARD_SYSTEM, "dashboard"), _data_prompt(prompt_request, context_text, data_text, facts),
                          max_tokens=4000)
    dash = normalize_dashboard(_json_from_text(raw))
    if dash is None:
        raise RuntimeError("AI chưa dựng được dashboard hợp lệ, hãy nói rõ cần biểu đồ số liệu gì")
    aid = db.save_artifact(meeting_id=meeting_id, kind="dashboard", title=f"Dashboard: {dash['title'][:60]}",
                           content=json.dumps(dash, ensure_ascii=False), prompt_trigger=prompt_request)
    return db.get_artifact(aid)


async def _refine_dashboard(meeting_id: int, art: Dict[str, Any], user_feedback: str) -> Dict[str, Any]:
    cur = normalize_dashboard(_json_from_text(art.get("content", ""))) or {}
    prompt = (f"## Dashboard hiện tại (JSON):\n{json.dumps(cur, ensure_ascii=False, indent=1)}\n\n"
              f"## Yêu cầu chỉnh sửa:\n\"{user_feedback}\"")
    data = _json_from_text(await _call_llm(DASHBOARD_REFINE_SYSTEM, prompt, max_tokens=4000))
    dash = normalize_dashboard(data)
    if dash is None:
        raise RuntimeError("AI chưa sửa được dashboard, hãy nói rõ cần đổi biểu đồ nào, đổi thế nào")
    new_aid = db.save_artifact(meeting_id=meeting_id, kind="dashboard", title=art.get("title", ""),
                               content=json.dumps(dash, ensure_ascii=False), prompt_trigger=user_feedback,
                               parent_id=art["id"])
    out = db.get_artifact(new_aid)
    out["chat_message"] = str((data or {}).get("chat_message") or "").strip() or f"Đã sửa dashboard: {user_feedback}"
    return out


# ==============================================================================
# 3f. BÁO CÁO NHANH (văn bản chiếu lên màn hình: tóm tắt, gạch đầu dòng, bảng)
# ==============================================================================
REPORT_SYSTEM = """Bạn là trợ lý cuộc họp, viết BÁO CÁO NHANH để chiếu ngay lên màn hình.
Markdown: "# Tiêu đề", một đoạn tóm tắt 2-3 câu, các mục "##" với gạch đầu dòng ngắn, bảng khi có số liệu hoặc danh sách việc.
Viết cụ thể: tên người, số liệu, hạn chót. Chỉ dùng dữ liệu được cung cấp; thiếu thì ghi rõ "chưa có dữ liệu".
Ngày dd/mm/yyyy, tiền 1.000.000đ, không dùng gạch dài. Tối đa khoảng 300 từ. Chỉ trả về nội dung markdown."""


def report_title(markdown: str, fallback: str = "Báo cáo nhanh") -> str:
    m = re.search(r"^\s*#{1,3}\s+(.+)$", markdown or "", re.M)
    return (m.group(1).strip() if m else fallback).strip("* ")[:80] or fallback


def save_report(meeting_id: int, markdown: str, prompt_request: str = "", title: str = "") -> Dict[str, Any]:
    md = (markdown or "").strip()
    aid = db.save_artifact(meeting_id=meeting_id, kind="report", title=f"Báo cáo: {title or report_title(md)}",
                           content=md, prompt_trigger=prompt_request)
    return db.get_artifact(aid)


async def generate_report(meeting_id: int, prompt_request: str, context_text: str = "", data_text: str = "",
                          facts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = await _call_llm(with_skill(REPORT_SYSTEM, "report"), _data_prompt(prompt_request, context_text, data_text, facts),
                          max_tokens=2500)
    md = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", (raw or "").strip())
    if len(md) < 20:
        raise RuntimeError("AI chưa viết được báo cáo")
    return save_report(meeting_id, md, prompt_request)


# ==============================================================================
# 4. TRÒ CHUYỆN CO-DESIGN HAI CHIỀU (CONVERSATIONAL CO-DESIGN REFINEMENT)
# ==============================================================================
REFINE_SYSTEM = """Bạn là AI Co-Designer làm việc trực tiếp cùng người dùng trong cuộc họp.
Người dùng đang quan sát bản thiết kế hiện tại (Web HTML, Sơ đồ Mermaid, hoặc Biên bản) và đưa ra
các yêu cầu chỉnh sửa nhanh bằng giọng nói hoặc chat ("Đổi nút thành màu tím", "Thêm cột doanh thu",
"Cho font chữ to hơn", "Thêm chart biểu đồ").

Nhiệm vụ của bạn:
1. Hiểu chính xác phần cần sửa đổi từ phản hồi của người dùng.
2. Áp dụng các thay đổi vào mã nguồn hiện tại một cách mượt mà và giữ nguyên các phần còn lại.
3. Trả về:
   - Một lời đáp trò chuyện ngắn gọn, thân thiện bằng tiếng Việt (ví dụ: "Em đã đổi nút CTA sang màu tím gradient và bổ sung bảng doanh thu theo ý anh ạ!").
   - Mã nguồn hoàn chỉnh mới sau khi đã cập nhật (trong khối ```html, ```mermaid hoặc ```markdown tương ứng)."""


async def _refine_slides(meeting_id: int, art: Dict[str, Any], user_feedback: str,
                         slide_index: Optional[int]) -> Dict[str, Any]:
    deck = normalize_deck(_json_from_text(art.get("content", ""))) or {"title": art.get("title", ""), "slides": []}
    cur = (slide_index or 0) + 1
    prompt = (f"## Bộ slide hiện tại (JSON):\n{json.dumps(deck, ensure_ascii=False, indent=1)}\n\n"
              f"## Người dùng đang xem slide số {cur}\n\n## Yêu cầu chỉnh sửa:\n\"{user_feedback}\"")
    data = _json_from_text(await _call_llm(SLIDES_REFINE_SYSTEM, prompt, max_tokens=4000))
    new_deck = normalize_deck(data)
    if new_deck is None:
        raise RuntimeError("AI chưa sửa được bộ slide, hãy nói rõ hơn cần sửa slide nào, sửa gì")
    try:
        focus = min(max(int(data.get("focus_slide")) - 1, 0), len(new_deck["slides"]) - 1)
    except Exception:
        focus = min(cur - 1, len(new_deck["slides"]) - 1)
    new_aid = db.save_artifact(meeting_id=meeting_id, kind="slides", title=art.get("title", ""),
                               content=json.dumps(new_deck, ensure_ascii=False), prompt_trigger=user_feedback,
                               parent_id=art["id"])
    out = db.get_artifact(new_aid)
    out.update({"chat_message": str(data.get("chat_message") or "").strip() or f"Đã sửa slide: {user_feedback}",
                "focus_slide": focus})
    return out


async def co_design_refine(meeting_id: int, artifact_id: int, user_feedback: str,
                           slide_index: Optional[int] = None) -> Dict[str, Any]:
    """Cập nhật bản thiết kế theo đàm thoại trực tiếp với người dùng (Co-Design Loop)."""
    art = db.get_artifact(artifact_id)
    if not art:
        raise ValueError(f"Không tìm thấy artifact id={artifact_id}")
    if art.get("kind") == "slides":
        return await _refine_slides(meeting_id, art, user_feedback, slide_index)
    if art.get("kind") == "dashboard":
        return await _refine_dashboard(meeting_id, art, user_feedback)

    kind = art.get("kind", "web_design")
    cur_content = art.get("content", "")
    title = art.get("title", "")

    prompt = f"""
## Loại thiết kế: {kind}
## Tiêu đề: {title}

## Nội dung thiết kế HIỆN TẠI:
{cur_content}

## Yêu cầu chỉnh sửa từ người dùng:
"{user_feedback}"

Hãy cập nhật thiết kế và trả về lời phản hồi ngắn gọn kèm toàn bộ mã nguồn mới đã cập nhật.
"""

    resp_raw = await _call_llm(REFINE_SYSTEM, prompt, max_tokens=6000)

    # Tách tin nhắn trò chuyện và khối mã
    chat_message = "Em đã cập nhật thiết kế theo yêu cầu của bạn!"
    new_code = cur_content

    if kind == "web_design":
        m = re.search(r"```(?:html)?\s*([\s\S]*?)```", resp_raw, re.DOTALL)
        if m:
            new_code = m.group(1).strip()
            chat_message = re.sub(r"```(?:html)?[\s\S]*?```", "", resp_raw, flags=re.DOTALL).strip()
        else:
            m_open = re.search(r"```(?:html)?\s*([\s\S]+)", resp_raw)
            if m_open and ("<!doctype" in m_open.group(1).lower() or "<html" in m_open.group(1).lower()):
                new_code = m_open.group(1).strip()
                chat_message = resp_raw[:m_open.start()].strip()
            elif "<html" in resp_raw.lower() or "<!doctype" in resp_raw.lower():
                s = resp_raw.lower().find("<!doctype")
                if s == -1:
                    s = resp_raw.lower().find("<html")
                if s != -1:
                    new_code = resp_raw[s:].strip()
                    chat_message = resp_raw[:s].strip()
    elif kind == "diagram":
        m = re.search(r"```(?:mermaid)?\s*([\s\S]*?)```", resp_raw, re.DOTALL)
        if m:
            new_code = m.group(1).strip()
            chat_message = re.sub(r"```(?:mermaid)?[\s\S]*?```", "", resp_raw, flags=re.DOTALL).strip()
        else:
            m_open = re.search(r"```(?:mermaid)?\s*([\s\S]+)", resp_raw)
            if m_open:
                new_code = m_open.group(1).strip()
                chat_message = resp_raw[:m_open.start()].strip()
    else:
        m = re.search(r"```(?:markdown)?\s*([\s\S]*?)```", resp_raw, re.DOTALL)
        if m:
            new_code = m.group(1).strip()
            chat_message = re.sub(r"```(?:markdown)?[\s\S]*?```", "", resp_raw, flags=re.DOTALL).strip()
        else:
            new_code = resp_raw.strip()

    if not chat_message:
        chat_message = f"Đã áp dụng thay đổi: '{user_feedback}'"

    # Lưu bản mới vào DB (versioning, liên kết parent_id)
    new_aid = db.save_artifact(
        meeting_id=meeting_id,
        kind=kind,
        title=title,
        content=new_code,
        prompt_trigger=user_feedback,
        parent_id=artifact_id
    )

    return {
        "id": new_aid,
        "parent_id": artifact_id,
        "kind": kind,
        "title": title,
        "content": new_code,
        "chat_message": chat_message,
        "version": art.get("version", 1) + 1
    }
