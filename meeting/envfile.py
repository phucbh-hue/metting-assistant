"""Khóa dịch vụ trong tệp .env của máy đang chạy: nhập ngay trên giao diện (Cài đặt) thay vì sửa tệp bằng tay.

Chỉ các khóa trong EDITABLE được đọc / ghi. Giá trị không bao giờ trả về giao diện, chỉ trả về "đã có" và 4 ký tự cuối.
Tệp .env nằm trên máy (đã có trong .gitignore), không lưu vào MongoDB để người khác có quyền DB không đọc được khóa.
"""
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.getenv("MEETING_ENV_FILE") or (ROOT / ".env"))

EDITABLE: Dict[str, Dict[str, str]] = {
    "ANTHROPIC_API_KEY": {"label": "Claude API key (Anthropic)", "hint": "sk-ant-...", "prefix": "sk-ant-"},
    "GEMINI_API_KEY": {"label": "Gemini API key (Google)", "hint": "AIza...", "prefix": ""},
    "SONIOX_API_KEY": {"label": "Soniox API key (nhận dạng giọng nói)", "hint": "khóa trong trang Soniox Console", "prefix": ""},
    "MONGODB_URL": {"label": "MongoDB Atlas (lưu trữ, để trống thì lưu trên máy)", "hint": "mongodb+srv://...", "prefix": ""},
}
RESTART_KEYS = {"MONGODB_URL"}                  # kết nối DB mở lúc khởi động: đổi xong cần chạy lại


def _masked(name: str, value: str) -> str:
    if not value:
        return ""
    if name == "MONGODB_URL":
        host = value.split("@", 1)[-1].split("/", 1)[0].split("?", 1)[0]
        return host[:60]
    return f"...{value[-4:]}" if len(value) > 8 else "đã có"


def status() -> Dict[str, Dict[str, Any]]:
    out = {}
    for name, meta in EDITABLE.items():
        val = os.getenv(name, "")
        placeholder = bool(re.search(r"x{4,}|your-|<user>|<password>", val))
        out[name] = {"label": meta["label"], "hint": meta["hint"], "set": bool(val) and not placeholder,
                     "masked": "" if placeholder else _masked(name, val), "restart": name in RESTART_KEYS}
    return out


def validate(name: str, value: str) -> str:
    if name not in EDITABLE:
        raise ValueError(f"Không được sửa khóa {name}")
    value = (value or "").strip().strip('"').strip("'")
    if len(value) > 600 or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("Giá trị không hợp lệ")
    if not value:
        return ""
    if name == "MONGODB_URL" and not re.match(r"mongodb(\+srv)?://", value):
        raise ValueError("Địa chỉ MongoDB phải bắt đầu bằng mongodb:// hoặc mongodb+srv://")
    if name == "ANTHROPIC_API_KEY" and not value.startswith("sk-ant-"):
        raise ValueError("API key của Anthropic bắt đầu bằng sk-ant-")
    if name != "MONGODB_URL" and re.search(r"\s", value):
        raise ValueError("API key không có khoảng trắng")
    return value


def _quote(value: str) -> str:
    if value and re.search(r"[\s#\"'\\]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def set_value(name: str, value: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Ghi (hoặc xóa khi rỗng) một khóa vào .env, giữ nguyên các dòng và chú thích khác, áp dụng ngay cho server."""
    value = validate(name, value)
    path = Path(path or ENV_PATH)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    new_line = f"{name}={_quote(value)}"
    done = False
    for i, ln in enumerate(lines):
        if pat.match(ln):
            lines[i] = new_line
            done = True
            break
    if not done:
        lines.append(new_line)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env-", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
    return status()[name]
