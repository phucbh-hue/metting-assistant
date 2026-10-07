"""Bộ test offline: luôn dùng DB in-memory và tắt mọi API ngoài (Soniox, Claude, Gemini).

Chạy: python -m unittest discover -s tests -t . -v
"""
import os

os.environ["MEETING_DB"] = "mock"
# Model giọng cố định cho test (ngưỡng và vector tổng hợp đo theo CAM++); máy thật mặc định ERes2NetV2 nếu đã tải
os.environ["VOICE_MODEL"] = "campplus"
for _key in ("SONIOX_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
    os.environ[_key] = ""
os.environ.setdefault("IDENTITY_MIN_INTERVAL_S", "0")
# Tra cứu web mặc định không mở trình duyệt / không gọi mạng; test nào cần thì tự đặt WEB_SEARCH_PROVIDER và mock
os.environ["WEB_SEARCH_PROVIDER"] = "claude"
# Thư viện tài liệu: test không quét Desktop / Documents / Downloads của người dùng
os.environ["LIBRARY_DEFAULTS"] = "0"
os.environ.pop("LIBRARY_DIRS", None)
# Nguồn AI: test không gọi CLI của gói đăng ký
os.environ["LLM_PROVIDER"] = "claude"
os.environ["CLI_LLM_DISABLED"] = "1"           # chốt an toàn: lỡ chọn CLI thì báo lỗi thay vì chạy thật
