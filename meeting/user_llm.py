"""Gói AI (Claude.ai / ChatGPT) riêng của từng người trên bản web.

Điều khoản của Anthropic và OpenAI không cho nhiều người dùng chung một tài khoản gói đăng ký, nên trên server:
- Mỗi người tự đăng nhập gói CỦA MÌNH qua CLI chính chủ (Claude Code, Codex). Đăng nhập nằm trong thư mục riêng
  `data/cli-users/<mã băm email>/` trên ổ lưu lâu dài, không ai khác dùng được.
- AI trong các cuộc họp người đó tạo dùng gói của chính họ; ai chưa kết nối (hoặc cuộc họp của người khác) dùng API key
  của công ty.
"""
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from meeting import cli_llm, user_prefs

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(os.getenv("CLI_USERS_DIR") or (ROOT / "data" / "cli-users"))
WEB_PROVIDERS = ("claude-cli", "codex-cli")


def home_for(email: str) -> Path:
    """Thư mục đăng nhập riêng của một người (tạo nếu chưa có, chỉ chủ sở hữu tiến trình đọc được)."""
    key = hashlib.sha256((email or "").strip().lower().encode()).hexdigest()[:16]
    d = BASE / key
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def chosen(email: str) -> Optional[str]:
    p = user_prefs.get(email).get("llm_provider")
    return p if p in WEB_PROVIDERS else None


def choose(email: str, provider: Optional[str]) -> None:
    if provider is not None and provider not in WEB_PROVIDERS:
        raise ValueError("Chỉ chọn được gói Claude.ai (claude-cli) hoặc ChatGPT (codex-cli)")
    user_prefs.update(email, llm_provider=provider)


def active(email: Optional[str]) -> Optional[Tuple[str, Path]]:
    """(gói, thư mục đăng nhập) nếu người này đã chọn gói, CLI đã cài và đã đăng nhập; không thì None (dùng API công ty)."""
    if not email:
        return None
    p = chosen(email)
    if not p or not cli_llm.installed(p):
        return None
    home = home_for(email)
    return (p, home) if cli_llm._login_state(p, home)["logged_in"] else None


def status(email: str) -> Dict[str, Any]:
    home = home_for(email)
    rows = cli_llm.status(home, WEB_PROVIDERS)
    for r in rows:
        r["login_job"] = cli_llm.login_status(r["id"], home)
    return {"providers": rows, "selected": chosen(email)}
