"""Mock Database MCP (Model Context Protocol) Server & Tools cho Meeting Assistant.

Mô phỏng hạ tầng dữ liệu nội bộ của UrBox:
- Danh bạ nhân sự (Employee Directory)
- Hệ thống quản lý công việc Jira (Tickets, Sprints, Assignees, Blockers)
- Kiến trúc hệ thống & Database Schemas (System Specs)
- Lịch sử biên bản các cuộc họp trước (Meeting History)

Hỗ trợ giao thức MCP:
- list_tools()
- call_tool(name, arguments)
"""
import json
import logging
from typing import Any, Dict, List, Optional

from meeting import db

log = logging.getLogger("meeting.mcp")

# ==============================================================================
# DỮ LIỆU MẪU MÔ PHỎNG HẠ TẦNG URBOX
# ==============================================================================
DEFAULT_EMPLOYEES = [
    {
        "id": "EMP-001",
        "name": "Bùi Hồng Phúc",
        "email": "phuc.bh@urbox.vn",
        "role": "Chief Technology Officer / Lead Architect",
        "department": "Engineering Leadership",
        "skills": ["System Architecture", "High Concurrency", "Distributed Systems", "AI & Voice"],
        "status": "active"
    },
    {
        "id": "EMP-002",
        "name": "Nguyễn Hoàng Nam",
        "email": "nam.nh@urbox.vn",
        "role": "Tech Lead Backend",
        "department": "Core Platform",
        "skills": ["Go", "Python", "Kubernetes", "PostgreSQL", "Kafka"],
        "status": "active"
    },
    {
        "id": "EMP-003",
        "name": "Trần Thu Hằng",
        "email": "hang.tt@urbox.vn",
        "role": "QA Lead",
        "department": "Quality Assurance",
        "skills": ["Automation Testing", "E2E Testing", "Playwright", "Security Audit"],
        "status": "active"
    },
    {
        "id": "EMP-004",
        "name": "Lê Văn Tuấn",
        "email": "tuan.lv@urbox.vn",
        "role": "Senior DevOps / SRE Engineer",
        "department": "Infrastructure & Cloud",
        "skills": ["Terraform", "AWS", "GCP", "Kubernetes", "CI/CD", "PostgreSQL DBA"],
        "status": "active"
    },
    {
        "id": "EMP-005",
        "name": "Đỗ Minh Quân",
        "email": "quan.dm@urbox.vn",
        "role": "Senior Backend Developer",
        "department": "Payment & Loyalty Core",
        "skills": ["Python", "FastAPI", "Postgres", "Redis", "Payment Gateways", "RabbitMQ"],
        "status": "active"
    },
    {
        "id": "EMP-006",
        "name": "Phạm Thùy Linh",
        "email": "linh.pt@urbox.vn",
        "role": "Lead Frontend & UI/UX Designer",
        "department": "Product Experience",
        "skills": ["React", "Next.js", "Tailwind CSS", "Figma", "Design Systems"],
        "status": "active"
    },
    {
        "id": "EMP-007",
        "name": "Nguyễn Thu Trang",
        "email": "trang.nt@urbox.vn",
        "role": "Product Manager",
        "department": "Merchant Solutions",
        "skills": ["Product Roadmap", "Data Analysis", "Agile/Scrum", "UX Research"],
        "status": "active"
    }
]

DEFAULT_JIRA_ISSUES = [
    {
        "key": "URBOX-101",
        "title": "Triển khai cache Redis Cluster cho service Voucher & Gift Card",
        "assignee": "Nguyễn Hoàng Nam",
        "assignee_email": "nam.nh@urbox.vn",
        "status": "In Progress",
        "priority": "High",
        "sprint": "Sprint 38 (Q3/2026)",
        "story_points": 5,
        "due_date": "2026-10-02",
        "description": "Giảm tải DB chính bằng cách cache danh mục voucher và hot deals với TTL 5 phút, tự động invalidate khi admin update."
    },
    {
        "key": "URBOX-102",
        "title": "Migration Database PostgreSQL lên v18 & cấu hình connection pool Neon",
        "assignee": "Lê Văn Tuấn",
        "assignee_email": "tuan.lv@urbox.vn",
        "status": "In Review",
        "priority": "High",
        "sprint": "Sprint 38 (Q3/2026)",
        "story_points": 8,
        "due_date": "2026-09-30",
        "description": "Nâng cấp Neon PostgreSQL lên v18.6, tối ưu pooling min_size=2 max_size=10 tránh lỗi disconnect idle."
    },
    {
        "key": "URBOX-103",
        "title": "Khắc phục memory leak worker background xử lý webhook thanh toán VNPay",
        "assignee": "Đỗ Minh Quân",
        "assignee_email": "quan.dm@urbox.vn",
        "status": "In Progress",
        "priority": "Critical",
        "sprint": "Sprint 38 (Q3/2026)",
        "story_points": 5,
        "due_date": "2026-09-29",
        "description": "Worker retry webhook bị kẹt kết nối HTTPClient không đóng gây tràn RAM sau 8 giờ chạy liên tục."
    },
    {
        "key": "URBOX-104",
        "title": "Thiết kế Landing Page chiến dịch Mega Sale 10.10 kèm hiệu ứng tương tác",
        "assignee": "Phạm Thùy Linh",
        "assignee_email": "linh.pt@urbox.vn",
        "status": "To Do",
        "priority": "Medium",
        "sprint": "Sprint 38 (Q3/2026)",
        "story_points": 5,
        "due_date": "2026-10-05",
        "description": "Xây dựng responsive mockup cho chiến dịch voucher quà tặng 10.10 bằng Tailwind CSS, hỗ trợ dark mode."
    },
    {
        "key": "URBOX-105",
        "title": "Tối ưu hóa thời gian load trang Dashboard đối tác UrBox Merchant Portal",
        "assignee": "Phạm Thùy Linh",
        "assignee_email": "linh.pt@urbox.vn",
        "status": "In Progress",
        "priority": "High",
        "sprint": "Sprint 38 (Q3/2026)",
        "story_points": 3,
        "due_date": "2026-10-01",
        "description": "Lazy load charts và phân trang SSR cho danh sách 50,000 giao dịch quà tặng hàng ngày."
    },
    {
        "key": "URBOX-106",
        "title": "Automation Test E2E kịch bản đổi voucher quà tặng đa thương hiệu",
        "assignee": "Trần Thu Hằng",
        "assignee_email": "hang.tt@urbox.vn",
        "status": "To Do",
        "priority": "Medium",
        "sprint": "Sprint 38 (Q3/2026)",
        "story_points": 5,
        "due_date": "2026-10-04",
        "description": "Viết test script Playwright kiểm thử luồng nhận mã OTP, kích hoạt thẻ quà tặng và check balance."
    }
]

DEFAULT_SYSTEMS = [
    {
        "name": "UrBox Core Loyalty & Voucher Engine",
        "architecture": "Microservices on Kubernetes (AWS EKS & GCP)",
        "stack": ["Go", "Python FastAPI", "React", "PostgreSQL 18", "Redis Cluster", "Kafka"],
        "database_schema": {
            "vouchers": "id (uuid), code, title, value, merchant_id, status (available, redeemed, expired), expiry_date",
            "transactions": "id (uuid), user_id, voucher_id, amount, status, created_at",
            "merchants": "id (int), name, category, commission_rate, api_key"
        },
        "key_endpoints": [
            "POST /api/v1/vouchers/redeem - Đổi voucher",
            "GET /api/v1/merchants/balance - Số dư đối tác",
            "POST /api/v1/webhooks/payment - Webhook giao dịch"
        ]
    },
    {
        "name": "UrBox Meeting Assistant AI Platform",
        "architecture": "FastAPI + Realtime WebSockets + Onnx CPU Embedding",
        "stack": ["Python 3.12", "Soniox stt-rt-v5", "sherpa-onnx CAM++", "Claude 3.5/Sonnet 5", "Gemini 3.6"],
        "database_schema": {
            "voices": "id, name, embedding (192 float vector), role, department, consent_by",
            "meetings": "id, title, status, started_at, ended_at",
            "meeting_segments": "id, meeting_id, t_start, t_end, speaker_label, text, confidence"
        }
    }
]

DEFAULT_MEETING_HISTORY = [
    {
        "meeting_id": "M-PREV-01",
        "date": "2026-09-22",
        "title": "Họp Kiến trúc: Chốt giải pháp AI Transcribe và Voice Identification",
        "decisions": [
            "Quyết định không tin nhãn diarization Soniox mà dùng CAM++ 192-dim vector trích xuất từ audio câu để phân cụm.",
            "Chọn Claude Sonnet 5 và Gemini 3.6 Flash làm hai model chính với fallback tự động.",
            "Dùng Neon PostgreSQL làm database tập trung, hỗ trợ lưu trữ vector sinh trắc học theo NĐ 13/2023."
        ],
        "attendees": ["Bùi Hồng Phúc", "Nguyễn Hoàng Nam", "Lê Văn Tuấn", "Trần Thu Hằng"]
    }
]


def seed_mock_data():
    """Khởi tạo dữ liệu giả lập vào DB nếu chưa có."""
    for emp in DEFAULT_EMPLOYEES:
        db.put_mcp_item("employees", emp["email"], emp)
    for issue in DEFAULT_JIRA_ISSUES:
        db.put_mcp_item("jira_issues", issue["key"], issue)
    for sys_item in DEFAULT_SYSTEMS:
        db.put_mcp_item("systems", sys_item["name"], sys_item)
    for hist in DEFAULT_MEETING_HISTORY:
        db.put_mcp_item("meeting_history", hist["meeting_id"], hist)
    log.info("meeting.mcp: đã nạp dữ liệu Mock MCP vào database thành công")


# ==============================================================================
# MCP TOOL DEFINITIONS (Chuẩn Model Context Protocol)
# ==============================================================================
MCP_TOOLS = [
    {
        "name": "query_employee_directory",
        "description": "Tra cứu danh bạ nhân sự công ty UrBox: tìm kiếm theo tên, phòng ban, chức vụ, email hoặc kỹ năng. Dùng để đối chiếu danh tính người nói hoặc tìm người phụ trách chuyên môn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Từ khóa tìm kiếm: tên nhân viên (ví dụ 'Tuấn', 'Quân', 'Nam'), chức vụ ('DevOps', 'QA') hoặc kỹ năng."
                },
                "department": {
                    "type": "string",
                    "description": "Tùy chọn lọc theo phòng ban (ví dụ 'Infrastructure', 'Quality Assurance', 'Core Platform')."
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "query_jira_issues",
        "description": "Tra cứu công việc, tickets và backlog trên Jira của UrBox. Dùng để kiểm tra tiến độ, blocker, hạn chót hoặc xác định ai đang làm ticket nào.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "Mã ticket cụ thể nếu biết (ví dụ 'URBOX-101', 'URBOX-103')."
                },
                "assignee": {
                    "type": "string",
                    "description": "Tên hoặc email người được giao việc (ví dụ 'Tuấn', 'Quân', 'nam.nh@urbox.vn')."
                },
                "status": {
                    "type": "string",
                    "description": "Trạng thái ticket ('In Progress', 'In Review', 'To Do', 'Done')."
                }
            }
        }
    },
    {
        "name": "query_system_architecture",
        "description": "Tra cứu kiến trúc kỹ thuật, schema database, tech stack và API endpoints của các hệ thống UrBox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "system_name": {
                    "type": "string",
                    "description": "Tên hệ thống cần tra cứu ('Core Loyalty', 'Meeting Assistant', 'Payment', v.v.)."
                }
            },
            "required": ["system_name"]
        }
    },
    {
        "name": "query_meeting_history",
        "description": "Tra cứu biên bản và quyết định của các cuộc họp trước đó để đảm bảo tính nhất quán.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "Từ khóa tìm kiếm trong các quyết định họp cũ (ví dụ 'CAM++', 'database', 'Redis')."
                }
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "update_jira_issue_status",
        "description": "Cập nhật trạng thái hoặc thêm ghi chú vào ticket Jira ngay trong cuộc họp khi có quyết định.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "Mã ticket (ví dụ 'URBOX-102')."
                },
                "new_status": {
                    "type": "string",
                    "description": "Trạng thái mới ('In Progress', 'In Review', 'Done')."
                },
                "comment": {
                    "type": "string",
                    "description": "Ghi chú từ cuộc họp."
                }
            },
            "required": ["issue_key", "new_status"]
        }
    }
]


def list_tools() -> List[Dict[str, Any]]:
    return MCP_TOOLS


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Thực thi tool MCP trên Mock Database."""
    try:
        if name == "query_employee_directory":
            q = arguments.get("query", "").strip().lower()
            dept = arguments.get("department", "").strip().lower()
            items = db.query_mcp_collection("employees")
            if not items:
                items = DEFAULT_EMPLOYEES
            matched = []
            for emp in items:
                name_m = q in emp.get("name", "").lower() or q in emp.get("email", "").lower() or q in emp.get("role", "").lower()
                dept_m = not dept or dept in emp.get("department", "").lower()
                skill_m = any(q in s.lower() for s in emp.get("skills", []))
                if (name_m or skill_m) and dept_m:
                    matched.append(emp)
            return {"count": len(matched), "employees": matched}

        elif name == "query_jira_issues":
            key = arguments.get("issue_key", "").strip().upper()
            assignee = arguments.get("assignee", "").strip().lower()
            status = arguments.get("status", "").strip().lower()
            items = db.query_mcp_collection("jira_issues")
            if not items:
                items = DEFAULT_JIRA_ISSUES
            matched = []
            for it in items:
                if key and it.get("key", "").upper() != key:
                    continue
                if assignee:
                    a_name = it.get("assignee", "").lower()
                    a_mail = it.get("assignee_email", "").lower()
                    if assignee not in a_name and assignee not in a_mail:
                        continue
                if status and status not in it.get("status", "").lower():
                    continue
                matched.append(it)
            return {"count": len(matched), "issues": matched}

        elif name == "query_system_architecture":
            sys_name = arguments.get("system_name", "").strip().lower()
            items = db.query_mcp_collection("systems")
            if not items:
                items = DEFAULT_SYSTEMS
            matched = [s for s in items if sys_name in s.get("name", "").lower()]
            return {"matched_systems": matched or items}

        elif name == "query_meeting_history":
            kw = arguments.get("keyword", "").strip().lower()
            items = db.query_mcp_collection("meeting_history")
            if not items:
                items = DEFAULT_MEETING_HISTORY
            matched = []
            for m in items:
                title_m = kw in m.get("title", "").lower()
                dec_m = any(kw in d.lower() for d in m.get("decisions", []))
                if title_m or dec_m:
                    matched.append(m)
            return {"meetings": matched}

        elif name == "update_jira_issue_status":
            key = arguments.get("issue_key", "").strip().upper()
            st = arguments.get("new_status", "").strip()
            comment = arguments.get("comment", "").strip()
            item = db.get_mcp_item("jira_issues", key)
            if not item:
                # search default
                for x in DEFAULT_JIRA_ISSUES:
                    if x["key"] == key:
                        item = dict(x)
                        break
            if not item:
                return {"error": f"Không tìm thấy ticket {key}"}
            item["status"] = st
            if comment:
                item.setdefault("comments", []).append(comment)
            db.put_mcp_item("jira_issues", key, item)
            return {"success": True, "key": key, "new_status": st, "message": f"Đã cập nhật trạng thái {key} sang '{st}'"}

        else:
            return {"error": f"Tool không tồn tại: {name}"}

    except Exception as e:
        log.error("meeting.mcp call_tool error (%s): %s", name, e)
        return {"error": str(e)}
