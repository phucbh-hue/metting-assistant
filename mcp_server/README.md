# MCP server "urbox-meeting-data"

Ngày: 01/10/2026 - Người phụ trách: phuc.bh@urbox.vn (Engineering)

MCP server thật (giao thức Model Context Protocol, SDK Python `mcp` 2.x) cung cấp dữ liệu nội bộ và kho tri thức (KB) cho Meeting Assistant. Mục tiêu: kiểm thử luồng trợ lý truy vấn dữ liệu qua MCP, để sau này cắm KB thật của công ty theo đúng hợp đồng tool này.

## Thành phần

| File | Vai trò |
|---|---|
| `mcp_server/server.py` | Server MCP (MCPServer/FastMCP) - khai báo tool và resource |
| `mcp_server/__main__.py` | Điểm chạy `python -m mcp_server` (chọn transport) |
| `mcp_server/kb_search.py` | Tìm kiếm KB (BM25, không phụ thuộc SDK MCP) - dùng chung cho server và fallback in-process |
| `mcp_server/kb/*.md` | Tài liệu KB mẫu (dữ liệu giả lập, không chứa thông tin thật) |
| `meeting/mcp_client.py` | Client MCP bất đồng bộ cho trợ lý |
| `meeting/mcp.py` | `call_tool` (mock in-process) và `call_tool_async` (định tuyến sang server thật nếu đã cấu hình) |
| `scripts/mcp_check.py` | Kiểm tra end-to-end qua stdio và HTTP |

### Tool

- `query_employee_directory(query, department="")` - danh bạ nhân sự
- `query_jira_issues(issue_key="", assignee="", status="")` - ticket Jira
- `query_system_architecture(system_name)` - kiến trúc hệ thống
- `query_meeting_history(keyword)` - quyết định họp cũ
- `update_jira_issue_status(issue_key, new_status, comment="")` - cập nhật ticket (thao tác ghi)
- `search_knowledge(query, top_k=5)` - tìm trong KB, trả về `{query, count, results: [{title, path, uri, snippet, score}]}`
- `read_document(path)` - toàn văn markdown một tài liệu KB, từ chối mọi đường dẫn ra ngoài thư mục KB

5 tool dữ liệu gọi thẳng `meeting.mcp.call_tool(...)` nên kết quả giống hệt bản mock in-process.

### Resource

- `kb://{name}` - toàn văn tài liệu (name = tên file không có `.md`), mỗi tài liệu cũng được liệt kê qua `resources/list`.

### Dữ liệu

Server mặc định chạy với `MEETING_DB=mock` (Mongomock in-memory, tự seed dữ liệu mẫu khi khởi động). Cập nhật Jira chỉ tồn tại trong tiến trình server và mất khi tắt. Muốn dùng MongoDB thật như ứng dụng chính, đặt `MEETING_DB` khác `mock` trước khi chạy server (server sẽ đọc `MONGODB_URL` trong `.env`). Khi spawn qua `MCP_SERVER_CMD`, client truyền `MEETING_DB` cho server con từ `MCP_SERVER_DB` (nếu có), nếu không thì lấy `MEETING_DB` hiện tại, mặc định `mock`.

## Cài đặt

```bash
pip install "mcp[cli]"      # đã thêm vào requirements.txt
```

## Chạy server

```bash
# stdio (mặc định) - dùng cho Claude Desktop / Claude Code / client spawn tiến trình
python -m mcp_server

# streamable-http - endpoint http://127.0.0.1:8765/mcp
python -m mcp_server --transport http --port 8765
# tùy chọn: --host 0.0.0.0 --path /mcp ; mức log: MCP_LOG_LEVEL=INFO (mặc định WARNING)
```

Chạy từ thư mục gốc dự án (`meeting-assistant/`) để Python tìm thấy package `meeting` và `mcp_server`.

## Trỏ trợ lý vào server

Đặt MỘT trong hai biến môi trường cho tiến trình Meeting Assistant:

```bash
# Server HTTP đang chạy sẵn
MCP_SERVER_URL=http://127.0.0.1:8765/mcp

# Hoặc để client tự spawn server stdio ("python" được thay bằng đúng interpreter của venv)
MCP_SERVER_CMD="python -m mcp_server"
```

Tùy chọn: `MCP_TIMEOUT_S` (mặc định 30 giây mỗi lần gọi). Nếu đặt cả hai, ưu tiên `MCP_SERVER_URL`.

Trong code, dùng `await meeting.mcp.call_tool_async(name, arguments)`: có cấu hình thì gọi qua MCP server, không có cấu hình (hoặc server lỗi) thì fallback về `call_tool` in-process và ghi log một lần đường đi được dùng. API client trực tiếp:

```python
from meeting import mcp_client
await mcp_client.connected()                 # True/False
await mcp_client.list_tools()                # [{name, description, input_schema}]
await mcp_client.call_tool("search_knowledge", {"query": "đổi trả voucher"})
await mcp_client.close()                     # đóng phiên (dừng server stdio con)
```

Client giữ một phiên MCP cho mỗi event loop (chạy trong một asyncio task nền), nên server stdio chỉ được spawn một lần và tự kết nối lại nếu phiên bị đứt.

Lưu ý: vòng lặp agent trong `meeting/llm.py` hiện vẫn gọi `mcp.call_tool` (đồng bộ) và chỉ cho phép các tool trong `AGENT_TOOLS`. Để trợ lý thực sự đi qua MCP server và dùng KB, cần đổi sang `await mcp.call_tool_async(...)` và thêm `search_knowledge`, `read_document` vào `AGENT_TOOLS`.

## Thêm tài liệu KB

1. Tạo file `mcp_server/kb/<ten-tai-lieu>.md` (UTF-8), dòng đầu là tiêu đề `# ...`, chia mục bằng `##`.
2. Mỗi mục nên tập trung một ý để đoạn trích (snippet) có nghĩa; ngày dạng dd/mm/yyyy, tiền dạng 1.000.000đ.
3. Không đưa dữ liệu cá nhân thật (họ tên đầy đủ, số điện thoại, CCCD, mã voucher) vào KB.
4. Không cần khởi động lại: chỉ mục tự làm mới khi file thay đổi. Đổi thư mục KB bằng `MCP_KB_DIR`.

## Cắm KB thật trong tương lai

Giữ nguyên hợp đồng tool `search_knowledge(query, top_k)` và `read_document(path)` cùng định dạng kết quả, chỉ thay phần cài đặt trong `kb_search.py`:

- `search()`: nhúng câu hỏi (embedding), truy vấn vector store (pgvector, Qdrant, OpenSearch...), có thể kết hợp BM25 (hybrid) và rerank; trả `title`, `path` (id tài liệu), `uri`, `snippet`, `score`.
- `read_document()`: đọc tài liệu theo id từ nguồn gốc (Confluence, Google Drive, wiki...), kiểm soát quyền truy cập theo người hỏi.
- `list_documents()`: liệt kê tài liệu cho resource `kb://`.

Trợ lý và client không cần sửa vì chỉ phụ thuộc tên tool và định dạng kết quả.

## Kiểm thử

```bash
# End-to-end qua stdio
python scripts/mcp_check.py
# Thêm cả streamable-http (tự chạy server nền ở cổng 8765 rồi dừng)
python scripts/mcp_check.py --http
# Unit test (không cần mạng)
python -m unittest tests.test_mcp -v
```

### Claude Code

```bash
claude mcp add urbox-meeting-data --env MEETING_DB=mock --env PYTHONIOENCODING=utf-8 -- "C:/work/cralwer with ai/ASR/interviewer-assistant-AI-circle/.venv/Scripts/python.exe" "C:/work/cralwer with ai/ASR/meeting-assistant/mcp_server/__main__.py"
```

Dùng đường dẫn tuyệt đối tới `mcp_server/__main__.py` vì Claude Code không chạy lệnh trong thư mục dự án (`server.py` tự thêm gốc dự án vào `sys.path`). Hoặc kết nối bản HTTP đang chạy:

```bash
claude mcp add --transport http urbox-meeting-data http://127.0.0.1:8765/mcp
```

### Claude Desktop

Thêm vào `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "urbox-meeting-data": {
      "command": "C:/work/cralwer with ai/ASR/interviewer-assistant-AI-circle/.venv/Scripts/python.exe",
      "args": ["C:/work/cralwer with ai/ASR/meeting-assistant/mcp_server/__main__.py"],
      "env": { "MEETING_DB": "mock", "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

Có thể kiểm tra trực quan bằng MCP Inspector: `npx @modelcontextprotocol/inspector` rồi nhập lệnh stdio ở trên.
