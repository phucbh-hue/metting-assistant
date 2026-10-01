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


async def co_design_refine(meeting_id: int, artifact_id: int, user_feedback: str) -> Dict[str, Any]:
    """Cập nhật bản thiết kế theo đàm thoại trực tiếp với người dùng (Co-Design Loop)."""
    art = db.get_artifact(artifact_id)
    if not art:
        raise ValueError(f"Không tìm thấy artifact id={artifact_id}")

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
