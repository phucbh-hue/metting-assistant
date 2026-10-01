"""MCP server thật "urbox-meeting-data" cho Meeting Assistant.

Cung cấp qua giao thức MCP (stdio hoặc streamable-http):
- 5 tool dữ liệu nội bộ, cùng tên/tham số với mock in-process (meeting/mcp.py):
  query_employee_directory, query_jira_issues, query_system_architecture,
  query_meeting_history, update_jira_issue_status
  -> gọi thẳng meeting.mcp.call_tool(...) nên hành vi giống hệt bản mock.
- 2 tool tri thức (KB): search_knowledge, read_document (mcp_server/kb_search.py)
- Resource kb://{name} cho từng tài liệu markdown trong mcp_server/kb/

Dữ liệu: mặc định MEETING_DB=mock (Mongomock in-memory, seed dữ liệu mẫu khi khởi động), nên mọi
cập nhật Jira chỉ sống trong tiến trình server. Đặt MEETING_DB khác "mock" (ví dụ MEETING_DB=mongo)
để server dùng MONGODB_URL trong .env như ứng dụng chính.

Chạy: python -m mcp_server                       (stdio)
      python -m mcp_server --transport http --port 8765   (http://127.0.0.1:8765/mcp)
"""
import os
import sys
from pathlib import Path
from typing import Any, Dict

# Mặc định chạy trên DB in-memory - phải đặt TRƯỚC khi import meeting.*
os.environ.setdefault("MEETING_DB", "mock")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
    _SDK_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore
    _SDK_V2 = False

from meeting import mcp as meeting_mcp  # noqa: E402
from mcp_server import kb_search  # noqa: E402

SERVER_NAME = "urbox-meeting-data"
INSTRUCTIONS = (
    "Dữ liệu nội bộ UrBox cho trợ lý cuộc họp: danh bạ nhân sự, ticket Jira, kiến trúc hệ thống, "
    "biên bản họp cũ và kho tri thức (quy trình, chính sách, FAQ). Với câu hỏi về quy trình/chính sách, "
    "gọi search_knowledge trước rồi read_document để đọc toàn văn tài liệu phù hợp nhất."
)

mcp = _Server(SERVER_NAME, instructions=INSTRUCTIONS,
              log_level=os.getenv("MCP_LOG_LEVEL", "WARNING").upper())  # type: ignore[arg-type]

meeting_mcp.seed_mock_data()


def _call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return meeting_mcp.call_tool(name, {k: v for k, v in arguments.items() if v is not None})


# ------------------------------------------------------------ DATA TOOLS ---
@mcp.tool()
def query_employee_directory(query: str, department: str = "") -> Dict[str, Any]:
    """Tra cứu danh bạ nhân sự UrBox theo tên, email, chức vụ hoặc kỹ năng; có thể lọc theo phòng ban.

    Dùng để xác định ai phụ trách chuyên môn hoặc đối chiếu danh tính người nói.
    query: từ khóa (ví dụ 'Tuấn', 'DevOps', 'Kafka'); để trống để lấy toàn bộ danh bạ.
    department: lọc phòng ban (ví dụ 'Infrastructure', 'Quality Assurance').
    """
    return _call("query_employee_directory", {"query": query, "department": department})


@mcp.tool()
def query_jira_issues(issue_key: str = "", assignee: str = "", status: str = "") -> Dict[str, Any]:
    """Tra cứu ticket Jira: tiến độ, người làm, hạn chót, mức ưu tiên.

    issue_key: mã ticket (ví dụ 'URBOX-103').
    assignee: tên hoặc email người được giao (ví dụ 'Quân').
    status: 'To Do', 'In Progress', 'In Review', 'Done'. Bỏ trống cả 3 để lấy tất cả ticket.
    """
    return _call("query_jira_issues", {"issue_key": issue_key, "assignee": assignee, "status": status})


@mcp.tool()
def query_system_architecture(system_name: str) -> Dict[str, Any]:
    """Tra cứu kiến trúc, tech stack, schema database và API endpoint của hệ thống UrBox.

    system_name: tên hệ thống (ví dụ 'Core Loyalty', 'Meeting Assistant'). Không khớp thì trả về tất cả hệ thống.
    """
    return _call("query_system_architecture", {"system_name": system_name})


@mcp.tool()
def query_meeting_history(keyword: str) -> Dict[str, Any]:
    """Tra cứu quyết định trong biên bản các cuộc họp trước theo từ khóa (ví dụ 'Redis', 'database')."""
    return _call("query_meeting_history", {"keyword": keyword})


@mcp.tool()
def update_jira_issue_status(issue_key: str, new_status: str, comment: str = "") -> Dict[str, Any]:
    """Cập nhật trạng thái ticket Jira (và thêm ghi chú) khi cuộc họp đã chốt quyết định.

    issue_key: mã ticket (ví dụ 'URBOX-102'). new_status: 'In Progress', 'In Review', 'Done'...
    comment: ghi chú từ cuộc họp (tùy chọn). Đây là thao tác GHI dữ liệu.
    """
    return _call("update_jira_issue_status", {"issue_key": issue_key, "new_status": new_status, "comment": comment})


# -------------------------------------------------------------- KB TOOLS ---
@mcp.tool()
def search_knowledge(query: str, top_k: int = 5) -> Dict[str, Any]:
    """Tìm trong kho tri thức nội bộ UrBox (quy trình, chính sách, kiến trúc, FAQ đối tác, onboarding).

    query: câu hỏi hoặc từ khóa tiếng Việt (có dấu hoặc không dấu), ví dụ 'đổi trả voucher'.
    top_k: số tài liệu tối đa trả về (1-20, mặc định 5).
    Kết quả: danh sách {title, path, uri, snippet, score} xếp theo độ liên quan; dùng path với read_document để đọc toàn văn.
    """
    return kb_search.search(query, top_k)


@mcp.tool()
def read_document(path: str) -> Dict[str, Any]:
    """Đọc toàn văn markdown một tài liệu trong kho tri thức.

    path: đường dẫn lấy từ kết quả search_knowledge (ví dụ 'chinh-sach-doi-tra-voucher.md' hoặc 'kb://chinh-sach-doi-tra-voucher').
    Chỉ đọc được tài liệu nằm trong thư mục KB.
    """
    try:
        return kb_search.read_document(path)
    except (ValueError, FileNotFoundError) as e:
        return {"error": str(e)}


# -------------------------------------------------------------- RESOURCES ---
@mcp.resource("kb://{name}", name="kb-document", mime_type="text/markdown",
              description="Toàn văn một tài liệu trong kho tri thức UrBox (name = tên file không có .md)")
def kb_document(name: str) -> str:
    return kb_search.read_document(name)["content"]


def _register_static_resources():
    """Đăng ký từng tài liệu KB thành resource tĩnh để client thấy được qua resources/list."""
    try:
        docs = kb_search.list_documents()
    except FileNotFoundError:
        return
    for d in docs:
        def _make(doc_name: str):
            def _read() -> str:
                return kb_search.read_document(doc_name)["content"]
            return _read
        mcp.resource(f"kb://{d['name']}", name=d["name"], title=d["title"], mime_type="text/markdown",
                     description=d["title"])(_make(d["name"]))


_register_static_resources()


def run(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765, path: str = "/mcp"):
    if transport in ("http", "streamable-http"):
        if _SDK_V2:
            mcp.run(transport="streamable-http", host=host, port=port, streamable_http_path=path)
        else:  # mcp 1.x: host/port nằm trong settings
            mcp.settings.host = host  # type: ignore[attr-defined]
            mcp.settings.port = port  # type: ignore[attr-defined]
            mcp.settings.streamable_http_path = path  # type: ignore[attr-defined]
            mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
