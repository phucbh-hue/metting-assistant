# UrBox Meeting Copilot (Trợ Lý Cuộc Họp Thông Minh Đa Phương Thức)

Nền tảng trợ lý cuộc họp thông minh thế hệ mới, tích hợp bóc băng thời gian thực (Soniox STT), nhận diện sinh trắc học giọng nói (CAM++ 192D), suy luận danh tính người lạ qua ngữ cảnh đối thoại, tư duy đa bước với Mock Database MCP, tự động lập biên bản cuộc họp, vẽ sơ đồ và sinh Web Sandbox tương tác với khả năng Co-Design đàm thoại hai chiều.

Toàn bộ dữ liệu được lưu trữ độc lập trên **MongoDB Atlas Cloud**, tách biệt hoàn toàn khỏi dữ liệu phỏng vấn tuyển dụng.

---

## 1. Kiến Trúc Hệ Thống & Luồng Nghiệp Vụ Chặt Chẽ

### 1.1 Vòng Đời Cuộc Họp (Meeting State Machine)

Hệ thống kiểm soát trạng thái nghiêm ngặt qua 3 giai đoạn:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Chưa vào phòng / Hub Dashboard                           │
│ - Xem danh sách cuộc họp cũ, thống kê thời lượng, bộ lọc   │
│ - Nút Mic trên Top bar BỊ KHÓA HOÀN TOÀN (chống bật mic vô tội vạ) │
└──────────────┬──────────────────────────────────────────────┘
               │ Bấm "+ Tạo cuộc họp mới" hoặc "Vào phòng họp"
               ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. Phòng Họp Trực Tiếp (State: 'live')                      │
│ - Có ID cuộc họp, Tiêu đề, Host, Mục tiêu/Agenda           │
│ - Nút "Bật Mic Phòng" KÍCH HOẠT (thu âm PCM16 16kHz)        │
│ - Soniox STT bóc băng thời gian thực (< 30ms)               │
│ - CAM++ nhận diện người quen / AI đoán danh tính người lạ   │
│ - Wake-word "Jarvis" gọi trợ lý tra cứu Jira/MCP            │
│ - Co-Design Web Sandbox & Sơ đồ tương tác                  │
└──────────────┬──────────────────────────────────────────────┘
               │ Bấm "🏁 Kết Thúc & Lưu Trữ Cuộc Họp"
               ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Cuộc Họp Đã Lưu Trữ (State: 'ended' / Archived)          │
│ - Mic tự động ngắt, WebSocket từ chối nhận thêm audio (4003)│
│ - AI Agent tự động lập Biên bản tổng kết & Action Items    │
│ - Dữ liệu lưu vĩnh viễn vào MongoDB Atlas                   │
│ - Chế độ Read-Only: Xem lại transcript, xuất Markdown / JSON│
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Công Nghệ Cốt Lõi

### 2.1 Sinh Trắc Học Giọng Nói & Suy Luận Người Lạ (Voice Memory Engine)
- **CAM++ (3D-Speaker)**: Trích xuất vector đặc trưng 192 chiều chuẩn hóa L2, chạy hoàn toàn trên CPU bằng `sherpa-onnx` (file `models/speaker.onnx`).
- **Lọc năng lượng RMS (`voiced_s`)**: Khung $30\text{ms}$, ngưỡng $\text{RMS} \ge 300$ (~$-40\text{ dBFS}$) để loại bỏ khoảng lặng và tiếng thở.
- **Single-Mic Consistency Gate**:
  - Không bao giờ xé một người nói thành nhiều người lạ (`Người lạ #1, #2, #3`) hay gắn nhãn thô `Speaker_1`.
  - Các câu thoại ngắn ("Alo", "ừ", "dạ") tự động kế thừa danh tính của người đang nói hoặc Chủ phòng.
- **Semantic Identity Inference Engine (AI Đoán Tên Người Lạ)**:
  - Phân tích câu nói tự giới thiệu (*"Em là Tuấn bên DevOps..."*).
  - Phân tích câu hỏi hướng đích kề cận (*"Tuấn ơi..."* $\to$ trả lời nhận việc).
  - Đối chiếu chéo với Mock Database MCP (ví dụ ticket Jira `URBOX-102` migrate Postgres v18 của Tuấn).
  - Khi độ tin cậy $\ge 85\%$: Tự động cập nhật lùi toàn bộ các câu trước đó và lưu vector giọng vào MongoDB Atlas (`auto_learned = True`).

### 2.2 Mock Database MCP (Model Context Protocol)
Máy chủ MCP giả lập cung cấp các công cụ:
- `query_employee_directory`: Tra cứu hồ sơ nhân viên UrBox (kỹ năng, phòng ban, email).
- `query_jira_issues`: Tra cứu tickets, backlog, blockers, tiến độ Sprint 38.
- `query_system_architecture`: Tra cứu specs, DB schemas, API endpoints các dịch vụ.
- `query_meeting_history`: Tra cứu các quyết định của các cuộc họp trước.
- `update_jira_issue_status`: Cập nhật trạng thái ticket trực tiếp khi cuộc họp chốt phương án.

### 2.3 Multi-Modal Artifacts & Co-Design Đàm Thoại
- **Biên bản cuộc họp (Meeting Minutes)**: Markdown chuẩn hóa với Executive Summary, Key Decisions và bảng Action Items.
- **Vẽ Sơ đồ (Mermaid.js)**: Sinh mã Mermaid và render trực tiếp trên UI (Flowchart, Sequence Diagram, Mindmap).
- **Web Sandbox Tương Tác**: Sinh mã HTML5 + Tailwind CSS hoàn chỉnh, nhúng trong `<iframe>` sandbox an toàn, có nút chuyển đổi Desktop/Mobile viewport và xem mã nguồn HTML.
- **Co-Design Duplex**: Người dùng vừa xem thiết kế vừa góp ý bằng giọng nói hoặc chat (*"Jarvis, đổi nút sang màu tím gradient và thêm cột doanh thu"*), AI cập nhật mã, tăng phiên bản ($v1 \to v2$) và duy trì liên kết lịch sử.

---

## 3. Cấu Trúc Thư Mục

```
meeting-assistant/
├── .env                          # Cấu hình MongoDB Atlas, API keys
├── models/
│   └── speaker.onnx              # CAM++ 192D model (28.2 MB)
├── meeting/
│   ├── __init__.py
│   ├── db.py                     # MongoDB Atlas database client (pymongo)
│   ├── mcp.py                    # Mock Database MCP Server
│   ├── voice.py                  # CAM++ Embedding & MeetingSpeakers clustering
│   ├── identity.py               # Semantic Identity Inference Engine
│   ├── llm.py                    # Wake-word detection & ReAct Thinking Engine
│   ├── artifacts.py              # Minutes, Mermaid Diagram & Web Sandbox Co-Design
│   ├── live.py                   # Parallel Audio Pipeline & Session Manager
│   ├── app.py                    # FastAPI Backend REST & WebSockets
│   └── index.html                # Web Cockpit SPA 3 cột hiện đại
├── test_e2e_meeting.py           # Bộ kiểm thử E2E 7 kịch bản
└── README.md
```

---

## 4. Hướng Dẫn Khởi Chạy

```powershell
cd "C:\work\cralwer with ai\ASR\meeting-assistant"
& "C:\work\cralwer with ai\ASR\interviewer-assistant-AI-circle\.venv\Scripts\python.exe" -m uvicorn meeting.app:app --host 127.0.0.1 --port 8080 --reload
```

Mở trình duyệt: **`http://127.0.0.1:8080`**
1. Xem danh sách cuộc họp tại trang chủ (Hub).
2. Bấm **"+ Tạo Cuộc Họp Mới"** để thiết lập tiêu đề, chủ trì và agenda.
3. Vào phòng họp, bấm **"Bật Mic Phòng"** để bắt đầu ghi âm và bóc băng.
4. Trải nghiệm gọi trợ lý: *"Jarvis ơi, tra cứu ticket của Tuấn và thiết kế trang Dashboard"*.
5. Bấm **"Kết Thúc & Lưu Trữ Cuộc Họp"** để tự động xuất biên bản tổng kết vào MongoDB Atlas.
