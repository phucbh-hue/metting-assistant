"""Bộ test offline: luôn dùng DB in-memory và tắt mọi API ngoài (Soniox, Claude, Gemini).

Chạy: python -m unittest discover -s tests -t . -v
"""
import os

os.environ["MEETING_DB"] = "mock"
for _key in ("SONIOX_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
    os.environ[_key] = ""
os.environ.setdefault("IDENTITY_MIN_INTERVAL_S", "0")
