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
from typing import Any, Dict, List, Optional, Tuple

from meeting import db, mcp

log = logging.getLogger("meeting.artifacts")

AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.6-flash")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
PROVIDER = (os.getenv("LLM_PROVIDER") or "claude").strip().lower()

_anthropic_client = None
_gemini_client = None


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


async def generate_diagram(meeting_id: int, prompt_request: str,
                           context_text: str = "") -> Dict[str, Any]:
    user_prompt = f"Yêu cầu vẽ sơ đồ: {prompt_request}\n\nNgữ cảnh cuộc họp liên quan:\n{context_text}"
    raw = await _call_llm(DIAGRAM_SYSTEM, user_prompt, max_tokens=2500)

    # Bóc tách mã mermaid
    m = re.search(r"```mermaid\s*(.*?)\s*```", raw, re.DOTALL)
    mermaid_code = m.group(1).strip() if m else raw.strip()

    title = f"Sơ đồ: {prompt_request[:50]}"
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
4. Trả về mã trong khối ```html ... ```."""


async def generate_web_sandbox(meeting_id: int, prompt_request: str,
                               context_text: str = "") -> Dict[str, Any]:
    user_prompt = f"Yêu cầu thiết kế giao diện web: {prompt_request}\n\nÝ tưởng & dữ liệu trong cuộc họp:\n{context_text}"
    raw = await _call_llm(WEB_SYSTEM, user_prompt, max_tokens=4000)

    m = re.search(r"```html\s*(.*?)\s*```", raw, re.DOTALL)
    html_code = m.group(1).strip() if m else raw.strip()

    title = f"Giao diện: {prompt_request[:50]}"
    aid = db.save_artifact(
        meeting_id=meeting_id,
        kind="web_design",
        title=title,
        content=html_code,
        prompt_trigger=prompt_request
    )
    return {"id": aid, "kind": "web_design", "title": title, "content": html_code}


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
    deck = normalize_deck(_json_from_text(await _call_llm(SLIDES_SYSTEM, user_prompt, max_tokens=3500)))
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
