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


# ------------------------------------------------------------ danh sách model của từng gói ---
CLAUDE_CLI_MODELS = [("sonnet", "Sonnet mới nhất", "khuyên dùng: nhanh, ít tốn hạn mức"),
                     ("opus", "Opus mới nhất", "mạnh hơn, tốn hạn mức nhanh hơn nhiều"),
                     ("haiku", "Haiku mới nhất", "nhanh nhất, cho việc đơn giản"),
                     ("claude-sonnet-5-5", "Claude Sonnet 5.5", ""), ("claude-opus-5-5", "Claude Opus 5.5", ""),
                     ("claude-opus-4-7", "Claude Opus 4.7", ""), ("claude-haiku-4-5", "Claude Haiku 4.5", "")]
# danh mục của Codex CLI 0.160.0 (02/10/2026), dùng khi chưa chạy được "codex debug models"
CODEX_BUILTIN = [("gpt-6.1-sol", "GPT-6.1-Sol", "Latest workhorse model for coding and everyday work."),
                 ("gpt-6-astra", "GPT-6-Astra", "Frontier intelligence for the most demanding work."),
                 ("gpt-6-sol", "GPT-6-Sol", "Previous generation workhorse model."),
                 ("gpt-6-luna", "GPT-6-Luna", "Fast and affordable model for easier tasks."),
                 ("gpt-5.6-sol", "GPT-5.6-Sol", "Older generation workhorse model.")]
GEMINI_CLI_ALIASES = [("auto", "Tự chọn", "Gemini CLI tự chọn model theo độ khó của yêu cầu"),
                      ("pro", "Pro mới nhất", "mạnh nhất"), ("flash", "Flash mới nhất", "nhanh"),
                      ("flash-lite", "Flash-Lite mới nhất", "nhanh nhất, nhẹ nhất")]
GEMINI_BUILTIN = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3-pro-preview", "gemini-2.5-pro"]
_GEMINI_CONST = re.compile(rb'\b((?:LATEST|BASE|DEFAULT|PREVIEW)_GEMINI(?:_[A-Z0-9]+)*_MODEL) = "(gemini-[0-9a-z.\-]+)"')
_list_cache: Dict[str, Any] = {}


def _entries(rows) -> List[Dict[str, Any]]:
    return [{"id": i, "label": l, "note": n} for i, l, n in rows]


def _gemini_label(mid: str) -> str:
    return " ".join(w if re.match(r"\d", w) else w.title() for w in mid.split("-"))


def _gemini_order(mid: str):
    v = re.search(r"gemini-(\d+(?:\.\d+)?)", mid)
    return (-(float(v.group(1)) if v else 0.0), "preview" in mid, mid)


def builtin_models(provider: str) -> List[Dict[str, Any]]:
    if provider == "claude-cli":
        return _entries(CLAUDE_CLI_MODELS)
    if provider == "codex-cli":
        return _entries(CODEX_BUILTIN)
    if provider == "gemini-cli":
        return _entries(GEMINI_CLI_ALIASES) + [{"id": m, "label": _gemini_label(m), "note": ""} for m in GEMINI_BUILTIN]
    return []


def _claude_models() -> List[Dict[str, Any]]:
    """Bí danh của Claude Code (luôn là bản mới nhất của gói) + model thêm mà tài khoản được dùng (Claude Code ghi trong
    ~/.claude.json sau khi đăng nhập, ví dụ Fable 5.1 với 1 triệu token ngữ cảnh)."""
    home = Path.home()
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json" if os.getenv("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    extra = []
    for o in _read_json(cfg).get("additionalModelOptionsCache") or []:
        v = str((o or {}).get("value") or "").strip() if isinstance(o, dict) else ""
        if v and re.fullmatch(r"[A-Za-z0-9._:/@\[\]-]{1,80}", v):
            note = str(o.get("description") or "").replace("\u2014", "-").replace("\u2013", "-")
            extra.append({"id": v, "label": str(o.get("label") or v), "note": note[:120]})
    base = _entries(CLAUDE_CLI_MODELS)
    return base[:3] + extra + [m for m in base[3:] if m["id"] not in {e["id"] for e in extra}]


def _codex_models() -> List[Dict[str, Any]]:
    """Danh mục model của Codex CLI ("codex debug models"); đã đăng nhập thì là danh mục của gói ChatGPT."""
    base = resolve_cmd("codex")
    if not base or os.getenv("CLI_LLM_DISABLED") == "1":
        return []
    with tempfile.TemporaryDirectory(prefix="mcopilot-llm-") as work:
        r = _run(base + ["debug", "models"], "", work, _env(), 60)
    data = _json_tail(r.stdout.decode("utf-8", "replace")) or {}
    rows = [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("slug") and m.get("visibility", "list") == "list"]
    rows.sort(key=lambda m: (m.get("priority") if isinstance(m.get("priority"), (int, float)) else 999, m["slug"]))
    return [{"id": str(m["slug"]), "label": str(m.get("display_name") or m["slug"]),
             "note": str(m.get("description") or "")[:120]} for m in rows]


def _gemini_models() -> List[Dict[str, Any]]:
    """Bí danh của Gemini CLI + các model khai báo trong gói Gemini CLI đã cài (đọc một lần, nhớ theo phiên bản)."""
    base = resolve_cmd("gemini")
    if not base:
        return []
    js = Path(base[-1]).resolve()
    bundle = js.parent
    if js.suffix not in (".js", ".mjs", ".cjs") or not bundle.is_dir():
        return []
    key = f"{bundle}|{js.stat().st_mtime}"
    if _list_cache.get("gemini_key") != key:
        found: Dict[str, str] = {}
        for f in sorted(bundle.glob("*.js")):
            try:
                data = f.read_bytes()
            except OSError:
                continue
            if b"_GEMINI_" not in data:
                continue
            for m in _GEMINI_CONST.finditer(data):
                const, mid = m.group(1).decode(), m.group(2).decode()
                if "EMBEDDING" not in const and not re.search(r"customtools|tts|image|embedding|live", mid):
                    found.setdefault(mid, const)
            if found:
                break                       # các hằng số nằm chung một tệp; không cần đọc hết 70 tệp
        _list_cache.update(gemini_key=key, gemini=sorted(found, key=_gemini_order))
    return _entries(GEMINI_CLI_ALIASES) + [{"id": m, "label": _gemini_label(m), "note": ""} for m in _list_cache["gemini"]]


def list_models(provider: str):
    """(danh sách model, nguồn "cli" | "builtin") để chọn trong Cài đặt."""
    if provider not in PROVIDERS:
        raise ValueError(f"Nguồn AI không hợp lệ: {provider}")
    if not installed(provider):
        return builtin_models(provider), "builtin"
    if provider == "claude-cli":
        models = _claude_models() if _login_state(provider)["logged_in"] else []
    elif provider == "codex-cli":
        models = _codex_models()
    else:
        models = _gemini_models()
    return (models, "cli") if models else (builtin_models(provider), "builtin")


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
    if not re.fullmatch(r"[A-Za-z0-9._:/@\[\]-]{0,80}", model):
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
