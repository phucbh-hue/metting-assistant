"""Thinking Engine & AI Assistant Orchestration.

Xử lý khi trợ lý được gọi tên (Wake-Word / Mention):
1. Nhận diện lời gọi trợ lý theo tên do người dùng đặt (mặc định "Jarvis") + "trợ lý ơi", "hey assistant".
2. Thinking Loop (ReAct):
   - Phân tích yêu cầu và ngữ cảnh cuộc họp.
   - Gọi Mock MCP Server để tra cứu dữ liệu (Nhân sự, Jira tickets, System specs).
   - Truyền stream thinking log xuống client để người dùng theo dõi quá trình suy nghĩ.
3. Sinh Artifacts đa phương thức (Báo cáo, Diagram Mermaid, Web Sandbox tương tác).
4. Phản hồi trò chuyện bằng lời/văn bản.
"""
import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from meeting import artifacts, db, mcp

log = logging.getLogger("meeting.llm")

AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.6-flash")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
PROVIDER = (os.getenv("LLM_PROVIDER") or "claude").strip().lower()

# Cụm gọi chung luôn dùng được, bất kể trợ lý đặt tên gì
GENERIC_WAKE = [r"trợ\s*lý\s*ơi", r"hey\s*assistant", r"@ai(?!\w)", r"bot\s*ơi"]
DEFAULT_ASSISTANT = {"name": "Jarvis", "aliases": []}
ASSISTANT_KEY = "assistant"
_assistant_cache: Dict[str, Any] = {"value": None, "at": 0.0}
_CALL_AFTER = re.compile(r"^\s*(ơi|à|ạ|ê|,|:|!|\?)", re.IGNORECASE)
_LEAD_IN = re.compile(r"^(này|ê|hey|hi|hello|alo|ok|okay|ờ|ừ|à|thì|vậy|nhờ|cho\s+(anh|chị|em|mình|tôi)\s+hỏi)$",
                      re.IGNORECASE)
# Tên đứng đầu câu mà không có "ơi"/dấu phẩy: chỉ tính là lời gọi khi phần sau là một yêu cầu
_REQUEST_CUE = re.compile(
    r"(tra\s*cứu|tóm\s*tắt|lập|vẽ|liệt\s*kê|thiết\s*kế|giúp|hãy|kiểm\s*tra|tìm|nhắc|ghi|báo\s*cáo|cập\s*nhật|soạn|"
    r"viết|tạo|xem|phân\s*tích|so\s*sánh|đọc|dịch|cho\s+(anh|chị|em|mình|tôi|bọn)|\?)", re.IGNORECASE)


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip(" ,.;:!?\"'")


def assistant_config(refresh: bool = False) -> Dict[str, Any]:
    """Tên gọi trợ lý (lưu ở collection settings). Có cache 60 giây."""
    import time
    now = time.monotonic()
    if not refresh and _assistant_cache["value"] is not None and now - _assistant_cache["at"] < 60:
        return _assistant_cache["value"]
    cfg = dict(DEFAULT_ASSISTANT)
    try:
        raw = db.get_setting(ASSISTANT_KEY, "")
        if raw:
            data = json.loads(raw)
            name = _clean_name(data.get("name", ""))
            if name:
                cfg = {"name": name, "aliases": [_clean_name(a) for a in data.get("aliases", []) if _clean_name(a)]}
    except Exception as e:
        log.warning("meeting.llm: đọc cấu hình trợ lý lỗi: %s", e)
    _assistant_cache.update({"value": cfg, "at": now})
    return cfg


def validate_assistant_config(name: str, aliases: List[str]) -> Dict[str, Any]:
    name = _clean_name(name)
    if not (2 <= len(name) <= 30) or not re.search(r"[^\W\d_]", name):
        raise ValueError("Tên gọi cần 2-30 ký tự và có chữ cái")
    seen, clean = {name.casefold()}, []
    for a in aliases or []:
        a = _clean_name(a)
        if not a or a.casefold() in seen:
            continue
        if not (2 <= len(a) <= 30) or not re.search(r"[^\W\d_]", a):
            raise ValueError(f"Tên gọi khác '{a}' cần 2-30 ký tự và có chữ cái")
        seen.add(a.casefold())
        clean.append(a)
    if len(clean) > 6:
        raise ValueError("Tối đa 6 tên gọi khác")
    return {"name": name, "aliases": clean}


def set_assistant_config(name: str, aliases: List[str]) -> Dict[str, Any]:
    cfg = validate_assistant_config(name, aliases)
    db.set_setting(ASSISTANT_KEY, json.dumps(cfg, ensure_ascii=False))
    _assistant_cache.update({"value": cfg, "at": 0.0})
    return assistant_config(refresh=True)


def assistant_terms() -> List[str]:
    cfg = assistant_config()
    return [cfg["name"]] + list(cfg.get("aliases", []))


def _name_pattern(name: str) -> "re.Pattern":
    core = r"\s+".join(re.escape(w) for w in name.split())
    return re.compile(rf"(?<!\w){core}(?!\w)", re.IGNORECASE)


def _clean_command(rest: str) -> str:
    # Bỏ dấu câu và tiếng gọi ("ơi", "à"...) ở đầu câu lệnh: "Jarvis ơi, liệt kê..." -> "liệt kê..."
    return re.sub(r"^[,:;.!?\-\s]*((ơi|à|ạ|ê|này|nhé)(?!\w)[,:;.!?\-\s]*)?", "", rest, flags=re.IGNORECASE).strip()


def detect_wake_word(text: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, str]]:
    """Phát hiện câu GỌI trợ lý và trích xuất yêu cầu theo sau.

    Tên trợ lý được coi là lời gọi khi đứng đầu câu ("Bông, tóm tắt giúp anh") hoặc đi kèm tiếng gọi
    ("..., Bông ơi, ..."). Nhắc tên giữa câu ("tôi nghĩ Bông làm được") không kích hoạt.
    Trả về (tên đã gọi, yêu cầu) hoặc None. Yêu cầu có thể rỗng khi người nói ngừng sau khi gọi tên.
    """
    if not text:
        return None
    cfg = cfg or assistant_config()
    names = sorted({cfg["name"], *cfg.get("aliases", [])}, key=len, reverse=True)
    for name in names:
        for m in _name_pattern(name).finditer(text):
            before = re.sub(r"[\W_]+", " ", text[:m.start()]).strip()
            rest = text[m.end():]
            called = _CALL_AFTER.match(rest) is not None
            at_start = not before or _LEAD_IN.match(before) is not None
            only_punct = not re.sub(r"[\W_]+", "", rest)
            if called or (at_start and (only_punct or _REQUEST_CUE.search(rest))):
                return cfg["name"], _clean_command(rest)
    for pattern in GENERIC_WAKE:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return cfg["name"], _clean_command(text[m.end():])
    return None


TOOL_LABELS = {
    "query_employee_directory": "danh bạ nhân sự",
    "query_jira_issues": "ticket Jira",
    "query_system_architecture": "tài liệu kiến trúc hệ thống",
    "query_meeting_history": "biên bản các cuộc họp trước",
    "update_jira_issue_status": "cập nhật ticket Jira",
}

# ---------------------------------------------------------------- lệnh trình chiếu ---
_SLIDE = r"(?:slide|slides|slai|xlai|trang)"
_NUM_WORDS = {"một": 1, "hai": 2, "ba": 3, "bốn": 4, "tư": 4, "năm": 5, "sáu": 6, "bảy": 7, "tám": 8,
              "chín": 9, "mười": 10}
_STAGE_PATTERNS = [
    ("open", re.compile(r"(mở|bật|vào|chuyển sang)\s*(chế độ\s*|màn hình\s*|màn\s*)?(toàn màn hình|sân khấu|trình chiếu|trình bày)"
                        r"|phóng to", re.I)),
    ("close", re.compile(r"(thoát|tắt|đóng)\s*(chế độ\s*|màn hình\s*|màn\s*)?(toàn màn hình|sân khấu|trình chiếu|trình bày)"
                         r"|thu nhỏ", re.I)),
    ("prompt", re.compile(r"nhắc\s*(bài|ý|lời|mình|anh|chị|em|giúp)|(nói|trình bày)\s*(gì|cái gì)\s*tiếp|"
                          r"ý\s*tiếp\s*theo|quên\s*(bài|ý|mất)", re.I)),
    ("analyze", re.compile(r"(nhận xét|phân tích|đánh giá|góp ý)\s*(nhanh\s*|giúp\s*|giùm\s*)*(về\s*)?"
                           r"(cuộc họp|buổi họp|nội dung|từ đầu|cho mình|cho anh|cho chị|cho em)", re.I)),
    ("back", re.compile(r"(quay|trở)\s*(lại|về)\s*(phần|nội dung|bài|bản|màn|cái)\s*(trình bày|trình chiếu|lúc nãy)?\s*"
                        r"(trước|cũ|lúc nãy|ban nãy)", re.I)),
]


def stage_intent(command: str) -> Optional[Dict[str, Any]]:
    """Nhận lệnh điều khiển màn hình trình bày từ câu nói (không cần gọi LLM)."""
    c = re.sub(r"\s+", " ", (command or "").lower()).strip(" .!?,")
    if not c:
        return None
    for action, pat in _STAGE_PATTERNS:
        if pat.search(c):
            return {"action": action}
    m = re.search(rf"{_SLIDE}\s*(?:số\s*)?(\d{{1,2}}|{'|'.join(_NUM_WORDS)})(?!\w)", c)
    if m and re.search(r"(đến|tới|sang|qua|mở|về|xem|chuyển|quay|cho)", c):
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS[m.group(1)]
        return {"action": "goto", "slide": max(0, n - 1)}
    m = re.search(rf"(?:mở|xem|chuyển|quay|tới|đến|sang)\s*(?:lại\s*)?(?:sang\s*|đến\s*|tới\s*)?(?:{_SLIDE}|phần)\s*"
                  r"(?:về|nói về|có|liên quan)\s+(.+)", c)
    if m:
        return {"action": "topic", "query": m.group(1).strip()}
    m = re.search(r"^(?:chuyển|sang|qua|tới|đến|mở)\s*(?:sang\s*|đến\s*|tới\s*)?phần\s+(?!trước|sau|tiếp)(.+)", c)
    if m:
        return {"action": "topic", "query": m.group(1).strip()}
    if re.search(rf"{_SLIDE}\s*(trước|lúc nãy|ban nãy)|(quay|trở|lùi)\s*(lại|về)?\s*(một\s*)?{_SLIDE}", c):
        return {"action": "prev"}
    if re.search(rf"(chuyển|sang|qua|tới|tiếp)\s*(sang\s*)?(một\s*)?{_SLIDE}|{_SLIDE}\s*(tiếp|kế|sau)|"
                 r"^(tiếp|tiếp đi|tiếp tục|next|chuyển tiếp)$|(chuyển|sang|qua|tới)\s*(sang\s*)?(phần|mục)\s*(tiếp theo|tiếp|sau|kế)", c):
        return {"action": "next"}
    return None


_EDIT_CMD = re.compile(r"^(sửa|chỉnh|đổi|thêm|bớt|xóa|xoá|bỏ|cập nhật|viết lại|rút gọn|làm ngắn|làm rõ)(?!\w)", re.I)
_EDIT_TARGET = re.compile(r"(slide|trang|này|đó|bản|biểu đồ|sơ đồ|giao diện|biên bản|nội dung|ý|tiêu đề|dòng)", re.I)


def is_edit_command(command: str) -> bool:
    """Câu lệnh sửa nội dung đang trình chiếu, ví dụ "sửa slide này thêm số liệu doanh thu"."""
    c = (command or "").strip()
    return bool(_EDIT_CMD.search(c) and _EDIT_TARGET.search(c))


def _extract_json(text: str) -> Dict[str, Any]:
    """Trích xuất và parse JSON an toàn từ phản hồi của LLM."""
    if not text:
        return {}
    clean = re.sub(r"```(?:json)?\s*", "", text)
    clean = re.sub(r"```\s*", "", clean).strip()

    # 1. Thử parse trực tiếp toàn bộ
    try:
        return json.loads(clean)
    except Exception:
        pass

    # 2. Tìm khối JSON từ dấu { đầu tiên đến } cuối cùng
    s = clean.find("{")
    e = clean.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(clean[s:e+1])
        except Exception:
            pass

    # 3. Quét các khối nhỏ hơn
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", clean):
        try:
            d = json.loads(m.group(0))
            if any(k in d for k in ("artifact_needed", "chat_response", "thought", "needs_tool", "tool")):
                return d
        except Exception:
            pass

    return {}


# ==============================================================================
# THINKING & TOOL EXECUTION PROMPT
# ==============================================================================
ORCHESTRATOR_SYSTEM = """Bạn là {{NAME}} - Trợ lý Cuộc họp AI Đa phương thức (Meeting Copilot) của UrBox.
Người trong cuộc họp gọi bạn bằng tên "{{NAME}}".
Bạn có khả năng suy nghĩ đa bước (Thinking), kết nối Mock Database MCP để tra cứu thông tin
nội bộ doanh nghiệp (nhân viên, tickets Jira, kiến trúc hệ thống), và sinh ra các sản phẩm
cụ thể: Biên bản họp (minutes), Sơ đồ (diagram), hoặc Thiết kế Web tương tác (web_design).

Các công cụ MCP sẵn có:
1. `query_employee_directory`: Tra cứu thông tin nhân sự (kỹ năng, phòng ban, email).
2. `query_jira_issues`: Tra cứu tickets, backlog, tiến độ, blocker.
3. `query_system_architecture`: Tra cứu specs, DB schemas, API endpoints.
4. `query_meeting_history`: Tra cứu quyết định cuộc họp cũ.
5. `update_jira_issue_status`: Cập nhật trạng thái ticket khi có quyết định.

Khi nhận câu lệnh, bạn hãy thực hiện theo chu trình:
1. <thinking>: Phân tích câu lệnh, xác định xem có cần tra cứu dữ liệu MCP nào không.
2. Nếu cần tra cứu dữ liệu, hãy gọi tool dưới dạng:
   TOOL_CALL: {"tool": "tên_tool", "arguments": {...}}
3. Sau khi có dữ liệu hoặc nếu không cần tool:
   - Quyết định output: 'slides' (bộ slide trình bày) | 'web_design' | 'diagram' | 'minutes' | chỉ trả lời.
   - Trả về JSON chuẩn cấu trúc:
     {
       "thought": "Tóm tắt suy nghĩ logic",
       "insights": [{"kind": "risk|todo|improve|good", "text": "Nhận xét cụ thể 1 câu"}],
       "artifact_needed": "slides" | "web_design" | "diagram" | "minutes" | null,
       "artifact_prompt": "Mô tả chi tiết để generator sinh artifact",
       "chat_response": "Câu trả lời bằng giọng văn đàm thoại tiếng Việt tự nhiên, lễ phép, chuyên nghiệp"
     }
Bạn đang đóng vai một người trợ lý có khuôn mặt, nói chuyện trực tiếp với mọi người: chat_response ngắn gọn
(tối đa 3 câu), đọc lên được thành tiếng, không dùng bảng hay ký hiệu markdown phức tạp.
"""


async def think_and_act(meeting_id: int, prompt: str, segments: List[Dict[str, Any]],
                        trigger: str = "voice_wake_word",
                        on_thinking: Optional[Callable[[str], Any]] = None,
                        on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None,
                        on_insights: Optional[Callable[[List[Dict[str, str]]], Any]] = None) -> Dict[str, Any]:
    """Quy trình Thinking & ReAct hoàn chỉnh của AI khi được kích hoạt."""
    t0 = asyncio.get_event_loop().time()

    # Chuẩn bị tóm tắt ngữ cảnh cuộc họp gần nhất
    recent_lines = [f"{s.get('speaker_label', 'Unknown')}: {s.get('text', '')}" for s in segments[-25:]]
    context_text = "\n".join(recent_lines) if recent_lines else "(Chưa có nội dung)"

    thinking_trace = []

    async def _emit_thought(text: str):
        thinking_trace.append(text)
        if on_thinking:
            await on_thinking(text)

    await _emit_thought(f"Để mình xem yêu cầu: \"{prompt}\"")

    # Bước 1: Quyết định xem có cần gọi tool MCP không
    tool_check_prompt = f"""
Ngữ cảnh cuộc họp gần nhất:
{context_text}

Câu lệnh người dùng:
"{prompt}"

Công cụ MCP có sẵn:
- query_employee_directory(query: str, department?: str)
- query_jira_issues(issue_key?: str, assignee?: str, status?: str)
- query_system_architecture(system_name: str)
- query_meeting_history(keyword: str)

Nếu câu lệnh cần tra cứu dữ liệu trên, hãy trả về DUY NHẤT một JSON dạng:
{{"needs_tool": true, "tool": "tên_tool", "arguments": {{...}}}}
Nếu không cần tool, trả về:
{{"needs_tool": false}}
"""
    mcp_data = {}
    tool_calls_record = []

    try:
        from meeting.artifacts import _call_llm
        check_raw = await _call_llm(
            "Bạn là bộ điều phối tool MCP. Trả về DUY NHẤT định dạng JSON.",
            tool_check_prompt,
            max_tokens=600
        )
        tool_decision = _extract_json(check_raw)
        if tool_decision.get("needs_tool"):
                tool_name = tool_decision.get("tool")
                tool_args = tool_decision.get("arguments", {})
                await _emit_thought(f"Mình đang tra cứu {TOOL_LABELS.get(tool_name, tool_name)}...")
                if on_tool:
                    await on_tool({"tool": tool_name, "args": tool_args})

                res = mcp.call_tool(tool_name, tool_args)
                mcp_data = {tool_name: res}
                tool_calls_record.append({"tool": tool_name, "args": tool_args, "result": res})
                await _emit_thought("Đã có dữ liệu, mình đang đối chiếu với nội dung cuộc họp.")
    except Exception as e:
        log.warning("meeting.llm tool check error: %s", e)

    # Bước 2: Tổng hợp và quyết định hành động sinh Artifacts
    orchestrate_prompt = f"""
## Ngữ cảnh cuộc họp:
{context_text}

## Dữ liệu từ Database MCP (nếu có):
{json.dumps(mcp_data, ensure_ascii=False, indent=2)}

## Câu lệnh người dùng:
"{prompt}"

Hãy suy nghĩ và trả về JSON:
{{
  "thought": "Suy nghĩ logic",
  "insights": [{{"kind": "risk|todo|improve|good", "text": "Tối đa 3 nhận xét cụ thể bạn thấy khi phân tích (tên người, số liệu, hạn chót)"}}],
  "artifact_needed": "slides" | "web_design" | "diagram" | "minutes" | null,
  "artifact_prompt": "Mô tả cụ thể yêu cầu cho sản phẩm cần tạo",
  "chat_response": "Lời đáp ngắn (tối đa 3 câu) để đọc thành tiếng, tiếng Việt tự nhiên"
}}
"""
    await _emit_thought("Mình đang tổng hợp...")

    plan_raw = await _call_llm(ORCHESTRATOR_SYSTEM.replace("{{NAME}}", assistant_config()["name"]),
                               orchestrate_prompt, max_tokens=1500)
    plan = _extract_json(plan_raw)
    insights = artifacts.normalize_insights(plan.get("insights"))
    if insights and on_insights:
        await on_insights(insights)

    artifact_kind = plan.get("artifact_needed")
    if not artifact_kind or artifact_kind == "null":
        p_lower = prompt.lower()
        if any(k in p_lower for k in ["slide", "trình chiếu", "thuyết trình", "bài trình bày", "deck"]):
            artifact_kind = "slides"
        elif any(k in p_lower for k in ["thiết kế", "web", "dashboard", "landing page", "giao diện", "html"]):
            artifact_kind = "web_design"
        elif any(k in p_lower for k in ["vẽ sơ đồ", "sơ đồ", "diagram", "flowchart", "sequence"]):
            artifact_kind = "diagram"
        elif any(k in p_lower for k in ["biên bản", "minutes", "tóm tắt cuộc họp"]):
            artifact_kind = "minutes"

    chat_resp = plan.get("chat_response")
    if not chat_resp:
        if artifact_kind == "slides":
            chat_resp = "Mình đã soạn xong bộ slide, đang đưa lên màn hình trình bày."
        elif artifact_kind == "web_design":
            chat_resp = "Dạ em đã tra cứu thông tin và tạo bản thiết kế Web Dashboard theo yêu cầu của anh trong Sandbox rồi ạ!"
        elif artifact_kind == "diagram":
            chat_resp = "Dạ em đã vẽ sơ đồ luồng hệ thống trên Canvas rồi ạ!"
        elif artifact_kind == "minutes":
            chat_resp = "Dạ em đã hoàn thiện biên bản cuộc họp và bảng Action Items rồi ạ!"
        else:
            chat_resp = "Em đã tiếp nhận yêu cầu và xử lý xong ạ!"

    art_prompt = plan.get("artifact_prompt") or prompt

    generated_artifact = None

    # Bước 3: Sinh Artifact tương ứng
    if artifact_kind == "slides":
        await _emit_thought("Mình đang soạn bộ slide...")
        data_text = json.dumps(mcp_data, ensure_ascii=False)[:6000] if mcp_data else ""
        generated_artifact = await artifacts.generate_slides(meeting_id, art_prompt, context_text, data_text)
        await _emit_thought("Bộ slide đã sẵn sàng.")

    elif artifact_kind == "web_design":
        await _emit_thought("🎨 Đang sinh mã nguồn Web Sandbox (HTML5 + Tailwind CSS)...")
        generated_artifact = await artifacts.generate_web_sandbox(meeting_id, art_prompt, context_text)
        await _emit_thought(f"✨ Giao diện Web đã sẵn sàng trong Sandbox (ID: {generated_artifact['id']})!")

    elif artifact_kind == "diagram":
        await _emit_thought("📊 Đang vẽ sơ đồ Mermaid.js...")
        generated_artifact = await artifacts.generate_diagram(meeting_id, art_prompt, context_text)
        await _emit_thought(f"✨ Sơ đồ trực quan đã hoàn thành (ID: {generated_artifact['id']})!")

    elif artifact_kind == "minutes":
        await _emit_thought("📝 Đang lập biên bản cuộc họp và bảng Action Items...")
        generated_artifact = await artifacts.generate_meeting_minutes(meeting_id, segments, "Biên bản cuộc họp")
        await _emit_thought(f"✨ Biên bản cuộc họp đã được lập xong (ID: {generated_artifact['id']})!")

    # Lưu interaction vào DB
    full_thinking = "\n".join(thinking_trace)
    db.record_interaction(
        meeting_id=meeting_id,
        prompt=prompt,
        trigger=trigger,
        thinking=full_thinking,
        tool_calls=tool_calls_record,
        response={"chat_response": chat_resp, "artifact": generated_artifact}
    )

    return {
        "chat_response": chat_resp,
        "thinking": full_thinking,
        "tool_calls": tool_calls_record,
        "insights": insights,
        "artifact": generated_artifact
    }
