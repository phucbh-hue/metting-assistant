"""Thinking Engine & AI Assistant Orchestration.

Xử lý khi trợ lý được gọi tên (Wake-Word / Mention):
1. Nhận diện Wake-word ("Jarvis", "Mira", "Trợ lý ơi", "AI ơi", "Hey Assistant").
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

WAKE_WORDS = [
    r"\bjarvis\b",
    r"\bmira\b",
    r"\btrợ\s*lý\s*ơi\b",
    r"\bai\s*ơi\b",
    r"\bhey\s*assistant\b",
    r"@ai\b",
    r"\bbot\s*ơi\b"
]


def detect_wake_word(text: str) -> Optional[Tuple[str, str]]:
    """Phát hiện từ khóa gọi tên trợ lý và trích xuất câu lệnh theo sau.

    Trả về (wake_word_matched, command_text) hoặc None.
    """
    for pattern in WAKE_WORDS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            wake_word = m.group(0)
            command = text[m.end():].strip()
            # Bỏ dấu câu và tiếng gọi ("ơi", "à"...) ở đầu câu lệnh: "Jarvis ơi, liệt kê..." -> "liệt kê..."
            command = re.sub(r"^[,:;.!?\-\s]*((ơi|à|ạ|này|nhé)\b[,:;.!?\-\s]*)?", "", command, flags=re.IGNORECASE).strip()
            return wake_word, command
    return None


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
ORCHESTRATOR_SYSTEM = """Bạn là Jarvis / Mira - Trợ lý Cuộc họp AI Đa phương thức (Meeting Copilot).
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
   - Quyết định output: 'web_design' | 'diagram' | 'minutes' | 'chat_response'.
   - Trả về JSON chuẩn cấu trúc:
     {
       "thought": "Tóm tắt suy nghĩ logic",
       "artifact_needed": "web_design" | "diagram" | "minutes" | null,
       "artifact_prompt": "Mô tả chi tiết để generator sinh artifact",
       "chat_response": "Câu trả lời bằng giọng văn đàm thoại tiếng Việt tự nhiên, lễ phép, chuyên nghiệp"
     }
"""


async def think_and_act(meeting_id: int, prompt: str, segments: List[Dict[str, Any]],
                        trigger: str = "voice_wake_word",
                        on_thinking: Optional[Callable[[str], Any]] = None,
                        on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, Any]:
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

    await _emit_thought(f"🧠 Đang phân tích yêu cầu: '{prompt}'...")

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
                await _emit_thought(f"🔍 Đang truy vấn Mock MCP Tool `{tool_name}`: {json.dumps(tool_args, ensure_ascii=False)}...")
                if on_tool:
                    await on_tool({"tool": tool_name, "args": tool_args})

                res = mcp.call_tool(tool_name, tool_args)
                mcp_data = {tool_name: res}
                tool_calls_record.append({"tool": tool_name, "args": tool_args, "result": res})
                await _emit_thought(f"✅ Đã nhận dữ liệu từ MCP Database ({len(str(res))} bytes).")
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
  "artifact_needed": "web_design" | "diagram" | "minutes" | null,
  "artifact_prompt": "Mô tả cụ thể yêu cầu thiết kế web/diagram/minutes",
  "chat_response": "Lời đáp thoại tự nhiên bằng tiếng Việt"
}}
"""
    await _emit_thought("⚡ Đang tổng hợp giải pháp và khởi tạo sản phẩm...")

    plan_raw = await _call_llm(ORCHESTRATOR_SYSTEM, orchestrate_prompt, max_tokens=1500)
    plan = _extract_json(plan_raw)

    artifact_kind = plan.get("artifact_needed")
    if not artifact_kind or artifact_kind == "null":
        p_lower = prompt.lower()
        if any(k in p_lower for k in ["thiết kế", "web", "dashboard", "landing page", "giao diện", "html"]):
            artifact_kind = "web_design"
        elif any(k in p_lower for k in ["vẽ sơ đồ", "sơ đồ", "diagram", "flowchart", "sequence"]):
            artifact_kind = "diagram"
        elif any(k in p_lower for k in ["biên bản", "minutes", "tóm tắt cuộc họp"]):
            artifact_kind = "minutes"

    chat_resp = plan.get("chat_response")
    if not chat_resp:
        if artifact_kind == "web_design":
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
    if artifact_kind == "web_design":
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
        "artifact": generated_artifact
    }
