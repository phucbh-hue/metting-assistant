"""Gọi AI bằng gói đăng ký (Claude.ai, ChatGPT, Gemini) qua CLI chính chủ, thay cho API key.

- claude-cli: Claude Code (`claude -p`), đăng nhập bằng tài khoản Claude.ai (Pro, Max, Team, Enterprise).
- codex-cli: Codex CLI (`codex exec`), đăng nhập bằng tài khoản ChatGPT (Plus, Pro, Business...).
- gemini-cli: Gemini CLI (`gemini -p`), đăng nhập bằng tài khoản Google.

Mỗi lần gọi chạy một tiến trình con trong thư mục tạm trống: không công cụ, không MCP, không đọc cấu hình cá nhân,
prompt đi qua stdin, chỉ lấy câu trả lời văn bản. Các biến API key bị gỡ khỏi tiến trình con để chắc chắn dùng gói đăng
ký (không phát sinh tiền API). Gói đăng ký có hạn mức theo giờ / ngày của nhà cung cấp, hết hạn mức thì lời gọi báo lỗi.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("meeting.cli_llm")

TIMEOUT_S = float(os.getenv("CLI_LLM_TIMEOUT", "240"))
_SLOTS = threading.BoundedSemaphore(max(1, int(os.getenv("CLI_LLM_CONCURRENCY", "3"))))

PROVIDERS: Dict[str, Dict[str, str]] = {
    "claude-cli": {"label": "Claude.ai", "tool": "Claude Code", "bin": "claude",
                   "plan": "Claude Pro, Max, Team hoặc Enterprise",
                   "install": "npm install -g @anthropic-ai/claude-code",
                   "login": "Mở cửa sổ lệnh, gõ: claude  rồi gõ /login và chọn tài khoản Claude (gói đăng ký)"},
    "codex-cli": {"label": "ChatGPT", "tool": "Codex CLI", "bin": "codex",
                  "plan": "ChatGPT Plus, Pro, Business, Edu hoặc Enterprise",
                  "install": "npm install -g @openai/codex",
                  "login": "Mở cửa sổ lệnh, gõ: codex login  rồi chọn Sign in with ChatGPT"},
    "gemini-cli": {"label": "Gemini", "tool": "Gemini CLI", "bin": "gemini",
                   "plan": "tài khoản Google (miễn phí có hạn mức) hoặc gói Google AI Pro / Ultra",
                   "install": "npm install -g @google/gemini-cli",
                   "login": "Mở cửa sổ lệnh, gõ: gemini  rồi chọn Login with Google, đăng nhập xong gõ /quit"},
}

# Gỡ khỏi tiến trình con: có các biến này thì CLI dùng API key / cổng khác thay vì gói đăng ký.
_DROP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "GEMINI_API_KEY",
             "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_APPLICATION_CREDENTIALS")

CODEX_PREAMBLE = ("Bạn đang được dùng như một mô hình ngôn ngữ: chỉ trả lời bằng văn bản đúng theo hướng dẫn bên dưới. "
                  "Không chạy lệnh, không đọc hay sửa tệp, không hỏi lại.")


def model_of(provider: str) -> str:
    """Model dùng cho từng CLI (để trống = model mặc định của gói đăng ký)."""
    if provider == "claude-cli":       # "sonnet" = Sonnet mới nhất; Opus tốn hạn mức gói nhanh hơn nhiều
        return os.getenv("CLAUDE_CLI_MODEL") or "sonnet"
    if provider == "codex-cli":
        return os.getenv("CODEX_MODEL", "")
    if provider == "gemini-cli":
        return os.getenv("GEMINI_CLI_MODEL", "")
    return ""


# ------------------------------------------------------------ tìm CLI, trạng thái đăng nhập ---
_which_cache: Dict[str, Any] = {}


def resolve_cmd(name: str) -> Optional[List[str]]:
    """Lệnh để chạy một CLI. Trên Windows, shim .cmd của npm được đổi thành node + tệp JS: chạy thẳng không qua cmd.exe
    nên tham số có dấu tiếng Việt / ký tự đặc biệt không bị hỏng."""
    hit = _which_cache.get(name)
    if hit is not None and time.monotonic() - hit[1] < 30:
        return hit[0]
    path = shutil.which(name)
    cmd: Optional[List[str]] = None
    if path:
        cmd = [path]
        if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
            try:
                text = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            m = re.search(r'"%dp0%\\([^"]+?\.(?:js|mjs|cjs))"', text)
            if m:
                base = Path(path).parent
                js = base / m.group(1)
                node = str(base / "node.exe") if (base / "node.exe").exists() else shutil.which("node")
                if js.exists() and node:
                    cmd = [node, str(js)]
    _which_cache[name] = (cmd, time.monotonic())
    return cmd


def installed(provider: str) -> bool:
    spec = PROVIDERS.get(provider)
    return bool(spec and resolve_cmd(spec["bin"]))


def _read_json(p: Path) -> Dict[str, Any]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _login_state(provider: str) -> Dict[str, Any]:
    """Đã đăng nhập gói đăng ký chưa (chỉ xem tệp đăng nhập có hay không, không đọc hay trả về token)."""
    home = Path.home()
    if provider == "claude-cli":
        cdir = Path(os.getenv("CLAUDE_CONFIG_DIR") or home / ".claude")
        o = _read_json(cdir / ".credentials.json").get("claudeAiOauth") or {}
        if o.get("refreshToken") or o.get("accessToken"):
            return {"logged_in": True, "plan": str(o.get("subscriptionType") or "")}
        acct = _read_json(home / ".claude.json").get("oauthAccount")   # macOS: token nằm trong Keychain
        return {"logged_in": bool(acct), "plan": ""}
    if provider == "codex-cli":
        auth = _read_json(Path(os.getenv("CODEX_HOME") or home / ".codex") / "auth.json")
        if auth.get("tokens"):
            return {"logged_in": True, "plan": "ChatGPT"}
        if auth.get("OPENAI_API_KEY"):
            return {"logged_in": False, "plan": "API key (không phải gói ChatGPT)"}
        return {"logged_in": False, "plan": ""}
    if provider == "gemini-cli":
        g = home / ".gemini"
        return {"logged_in": (g / "oauth_creds.json").exists(), "plan": "Google" if (g / "oauth_creds.json").exists() else ""}
    return {"logged_in": False, "plan": ""}


def status() -> List[Dict[str, Any]]:
    out = []
    for pid, spec in PROVIDERS.items():
        st = _login_state(pid)
        out.append({"id": pid, "kind": "subscription", "label": spec["label"], "tool": spec["tool"],
                    "installed": installed(pid), "logged_in": st["logged_in"], "plan": st["plan"],
                    "plan_hint": spec["plan"], "install": spec["install"], "login": spec["login"],
                    "model": model_of(pid) or "mặc định của gói"})
    return out


# ------------------------------------------------------------ gọi ---
def _env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    env.update({"NO_COLOR": "1", "CI": "1"})
    env.update(extra or {})
    return env


def _json_tail(text: str) -> Optional[Dict[str, Any]]:
    """Đối tượng JSON cuối cùng trong đầu ra (CLI có thể in vài dòng thông báo trước JSON)."""
    s = (text or "").strip()
    if not s:
        return None
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except ValueError:
        pass
    for m in reversed(list(re.finditer(r"(?m)^\{", s))):
        try:
            d = json.loads(s[m.start():])
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return None


def _friendly(provider: str, msg: str) -> str:
    low = (msg or "").lower()
    spec = PROVIDERS[provider]
    if re.search(r"not logged in|please run /login|login required|unauthori[sz]ed|401|invalid api key|"
                 r"set an auth method|authentication|oauth", low):
        return f"{spec['tool']} chưa đăng nhập gói {spec['label']}. {spec['login']}."
    if re.search(r"rate.?limit|usage limit|quota|429|resource_exhausted|limit reached", low):
        return f"Gói {spec['label']} đã hết hạn mức tạm thời, thử lại sau hoặc chuyển nguồn AI khác."
    return f"{spec['tool']} lỗi: {msg.strip()[-300:] or 'không có thông báo'}"


def _run(cmd: List[str], stdin: str, cwd: str, env: Dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with _SLOTS:
        return subprocess.run(cmd, input=stdin.encode("utf-8"), capture_output=True, cwd=cwd, env=env,
                              timeout=timeout, creationflags=flags)


def run(provider: str, system: str, prompt: str, timeout: Optional[float] = None,
        model: Optional[str] = None) -> Dict[str, Any]:
    """Một lượt hỏi - đáp. Trả về {"text", "model", "input", "output", "cache_read", "cache_write", "estimated"}."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"Nguồn AI không hợp lệ: {provider}")
    if os.getenv("CLI_LLM_DISABLED") == "1":
        raise RuntimeError("Gọi AI qua CLI đang tắt (CLI_LLM_DISABLED=1)")
    base = resolve_cmd(spec["bin"])
    if not base:
        raise RuntimeError(f"Chưa cài {spec['tool']}. Cài bằng lệnh: {spec['install']}")
    timeout = timeout or TIMEOUT_S
    model = (model or "").strip() or model_of(provider)
    if not re.fullmatch(r"[A-Za-z0-9._:/@-]{0,80}", model):
        raise ValueError(f"Tên model không hợp lệ: {model}")
    with tempfile.TemporaryDirectory(prefix="mcopilot-llm-") as work:
        wd = Path(work)
        try:
            if provider == "claude-cli":
                return _claude(base, wd, system, prompt, model, timeout)
            if provider == "codex-cli":
                return _codex(base, wd, system, prompt, model, timeout)
            return _gemini(base, wd, system, prompt, model, timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{spec['tool']} không trả lời sau {int(timeout)} giây")


def _claude(base: List[str], wd: Path, system: str, prompt: str, model: str, timeout: float) -> Dict[str, Any]:
    sysfile = wd / "system.md"
    sysfile.write_text(system or "Bạn là trợ lý cuộc họp nội bộ.", encoding="utf-8")
    cmd = base + ["-p", "--output-format", "json", "--setting-sources", "", "--safe-mode", "--strict-mcp-config",
                  "--tools", "", "--no-session-persistence", "--system-prompt-file", str(sysfile)]
    if model:
        cmd += ["--model", model]
    effort = os.getenv("CLAUDE_CLI_EFFORT", "medium").strip()
    if effort:
        cmd += ["--effort", effort]
    r = _run(cmd, prompt, str(wd), _env(), timeout)
    out = r.stdout.decode("utf-8", "replace")
    data = _json_tail(out)
    if data is None:
        raise RuntimeError(_friendly("claude-cli", r.stderr.decode("utf-8", "replace") or out))
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise RuntimeError(_friendly("claude-cli", str(data.get("result") or data.get("subtype") or "")))
    u = data.get("usage") or {}
    used = list((data.get("modelUsage") or {}).keys())
    return {"text": str(data.get("result") or ""), "model": used[0] if used else model,
            "input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0), "estimated": not u}


def _codex(base: List[str], wd: Path, system: str, prompt: str, model: str, timeout: float) -> Dict[str, Any]:
    outfile = wd / "last-message.txt"
    cmd = base + ["exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                  "-s", "read-only", "--color", "never", "-C", str(wd), "-o", str(outfile), "--json"]
    if model:
        cmd += ["-m", model]
    effort = os.getenv("CODEX_EFFORT", "medium").strip()
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    cmd += ["-"]
    stdin = f"{CODEX_PREAMBLE}\n\n# Hướng dẫn\n{system}\n\n# Yêu cầu\n{prompt}"
    r = _run(cmd, stdin, str(wd), _env(), timeout)
    usage: Dict[str, Any] = {}
    text, err = "", ""
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type") if isinstance(ev, dict) else None
        if t == "turn.completed":
            usage = ev.get("usage") or {}
        elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            text = str(ev["item"].get("text") or text)
        elif t in ("error", "turn.failed"):
            e = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            err = str(e.get("message") or ev.get("message") or err)
    if outfile.exists():
        text = outfile.read_text(encoding="utf-8", errors="replace").strip() or text
    if not text:
        raise RuntimeError(_friendly("codex-cli", err or r.stderr.decode("utf-8", "replace")))
    inp = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    return {"text": text, "model": model or "codex", "input": max(0, inp - cached),
            "output": int(usage.get("output_tokens") or 0), "cache_read": cached, "cache_write": 0,
            "estimated": not usage}


def _gemini(base: List[str], wd: Path, system: str, prompt: str, model: str, timeout: float) -> Dict[str, Any]:
    sysfile = wd / "system.md"
    sysfile.write_text(system or "Bạn là trợ lý cuộc họp nội bộ.", encoding="utf-8")
    via_cmd = base[0].lower().endswith((".cmd", ".bat"))
    tail = "Follow the request above." if via_cmd else "Thực hiện yêu cầu ở trên."   # qua cmd.exe: chỉ dùng ASCII
    cmd = base + ["-p", tail, "-o", "json", "--approval-mode", "plan", "--skip-trust"]
    if model:
        cmd += ["-m", model]
    env = _env({"GEMINI_SYSTEM_MD": str(sysfile), "GEMINI_CLI_TRUST_WORKSPACE": "true"})
    r = _run(cmd, prompt, str(wd), env, timeout)
    data = _json_tail(r.stdout.decode("utf-8", "replace"))
    if data is None or data.get("error"):
        e = (data or {}).get("error") or {}
        raise RuntimeError(_friendly("gemini-cli", str(e.get("message") if isinstance(e, dict) else e)
                                     if e else r.stderr.decode("utf-8", "replace")))
    inp = out = cached = 0
    used = ""
    for name, m in ((data.get("stats") or {}).get("models") or {}).items():
        tk = (m or {}).get("tokens") or {}
        used = used or name
        inp += int(tk.get("prompt") or tk.get("input") or 0)
        out += int(tk.get("candidates") or 0) + int(tk.get("thoughts") or 0)
        cached += int(tk.get("cached") or 0)
    return {"text": str(data.get("response") or ""), "model": used or model or "gemini", "input": max(0, inp - cached),
            "output": out, "cache_read": cached, "cache_write": 0, "estimated": not (inp or out)}
