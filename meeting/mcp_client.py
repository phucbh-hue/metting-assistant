"""Client MCP thật cho Meeting Assistant - nói chuyện với MCP server qua giao thức MCP.

Cấu hình bằng biến môi trường (ưu tiên MCP_SERVER_URL nếu đặt cả hai):
- MCP_SERVER_URL: endpoint streamable-http, ví dụ http://127.0.0.1:8765/mcp
- MCP_SERVER_CMD: lệnh khởi chạy server stdio, ví dụ "python -m mcp_server"
  (token đầu "python"/"python3" được thay bằng sys.executable để dùng đúng venv; thư mục làm việc là gốc dự án)
- MCP_TIMEOUT_S: timeout mỗi lần gọi (mặc định 30 giây)
Không đặt biến nào: connected() trả False, call_tool() raise RuntimeError.

Phiên kết nối: giữ MỘT phiên (ClientSession) cho mỗi event loop của tiến trình. Phiên được mở và đóng
trong một asyncio task nền riêng (SDK dùng anyio task group nên phải vào/ra context trong cùng một task),
các lời gọi gửi vào task đó qua hàng đợi. Với stdio, server con chỉ spawn một lần thay vì mỗi lần gọi.
Phiên lỗi/đứt sẽ tự mở lại ở lần gọi kế tiếp. Event loop khác (ví dụ asyncio.run mới) có phiên riêng.

Không import `mcp` ở mức module để ứng dụng vẫn chạy khi chưa cài SDK.
"""
import asyncio
import json
import logging
import os
import shlex
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("meeting.mcp_client")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONNECT_TIMEOUT_S = 30.0


def _timeout() -> float:
    try:
        return float(os.getenv("MCP_TIMEOUT_S", "30"))
    except ValueError:
        return 30.0


def config() -> Dict[str, str]:
    """Cấu hình hiện tại: {'mode': 'http'|'stdio'|'', 'target': ...}."""
    url = os.getenv("MCP_SERVER_URL", "").strip()
    if url:
        return {"mode": "http", "target": url}
    cmd = os.getenv("MCP_SERVER_CMD", "").strip()
    if cmd:
        return {"mode": "stdio", "target": cmd}
    return {"mode": "", "target": ""}


def is_configured() -> bool:
    return bool(config()["mode"])


def _split_cmd(cmd: str) -> List[str]:
    parts = shlex.split(cmd, posix=(os.name != "nt"))
    parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    if parts and parts[0].lower() in ("python", "python3", "python.exe", "py"):
        parts[0] = sys.executable
    return parts


def _errlog():
    try:
        sys.stderr.fileno()
        return sys.stderr
    except Exception:
        return open(os.devnull, "w")


def _transport(cfg: Dict[str, str]):
    if cfg["mode"] == "http":
        try:
            from mcp.client.streamable_http import streamable_http_client as _http  # mcp >= 2
        except ImportError:  # mcp 1.x
            from mcp.client.streamable_http import streamablehttp_client as _http  # type: ignore
        return _http(cfg["target"])
    from mcp.client.stdio import StdioServerParameters, stdio_client
    parts = _split_cmd(cfg["target"])
    if not parts:
        raise RuntimeError("MCP_SERVER_CMD rỗng")
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "MEETING_DB": os.getenv("MCP_SERVER_DB", os.getenv("MEETING_DB", "mock"))}
    for k in ("MCP_KB_DIR", "MCP_LOG_LEVEL", "SYSTEMROOT", "TEMP", "TMP"):
        if os.getenv(k):
            env[k] = os.environ[k]
    params = StdioServerParameters(command=parts[0], args=parts[1:], env=env, cwd=str(PROJECT_ROOT))
    return stdio_client(params, errlog=_errlog())


class _Connection:
    """Một phiên MCP sống trong một asyncio task nền; nhận việc qua hàng đợi."""

    def __init__(self, cfg: Dict[str, str]):
        self.cfg = cfg
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready: asyncio.Future = self.loop.create_future()
        self.dead = False
        self.task = self.loop.create_task(self._run(), name="mcp-client-session")

    async def _run(self):
        try:
            from mcp import ClientSession
            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(_transport(self.cfg))
                read, write = streams[0], streams[1]
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                if not self.ready.done():
                    self.ready.set_result(True)
                while True:
                    job = await self.queue.get()
                    if job is None:
                        break
                    fn, fut = job
                    if fut.done():
                        continue
                    try:
                        res = await fn(session)
                        if not fut.done():
                            fut.set_result(res)
                    except asyncio.CancelledError:
                        if not fut.done():
                            fut.set_exception(RuntimeError("Phiên MCP bị hủy"))
                        raise
                    except Exception as e:  # lỗi của riêng lời gọi này, phiên vẫn dùng tiếp
                        if not fut.done():
                            fut.set_exception(e)
        except BaseException as e:  # noqa: BLE001 - gồm cả ExceptionGroup của anyio
            err = e if isinstance(e, Exception) else RuntimeError("Phiên MCP đã đóng")
            if not self.ready.done():
                self.ready.set_exception(RuntimeError(f"Không kết nối được MCP server ({self.cfg['mode']}): {err}"))
            if not isinstance(e, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                log.warning("meeting.mcp_client: phiên MCP kết thúc do lỗi: %s", err)
        finally:
            self.dead = True
            while not self.queue.empty():
                job = self.queue.get_nowait()
                if job and not job[1].done():
                    job[1].set_exception(RuntimeError("Phiên MCP đã đóng"))

    async def submit(self, fn: Callable[[Any], Awaitable[Any]], timeout: float) -> Any:
        await asyncio.wait_for(asyncio.shield(self.ready), CONNECT_TIMEOUT_S)
        if self.dead:
            raise RuntimeError("Phiên MCP đã đóng")
        fut = self.loop.create_future()
        await self.queue.put((fn, fut))
        return await asyncio.wait_for(fut, timeout)

    async def close(self):
        if not self.dead:
            await self.queue.put(None)
            try:
                await asyncio.wait_for(self.task, 10)
            except Exception:
                self.task.cancel()


_conns: Dict[int, _Connection] = {}


def _get_conn() -> _Connection:
    cfg = config()
    if not cfg["mode"]:
        raise RuntimeError("Chưa cấu hình MCP server: đặt MCP_SERVER_URL (http) hoặc MCP_SERVER_CMD (stdio)")
    loop = asyncio.get_running_loop()
    key = id(loop)
    conn = _conns.get(key)
    if conn is None or conn.dead or conn.cfg != cfg or conn.loop is not loop:
        for k in [k for k, c in _conns.items() if c.loop.is_closed()]:
            _conns.pop(k, None)
        conn = _Connection(cfg)
        _conns[key] = conn
    return conn


async def _with_session(fn: Callable[[Any], Awaitable[Any]]) -> Any:
    return await _get_conn().submit(fn, _timeout())


async def close():
    """Đóng phiên MCP của event loop hiện tại (dừng server stdio con nếu có)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    conn = _conns.pop(id(loop), None)
    if conn:
        await conn.close()


# ------------------------------------------------------------- PUBLIC API ---
async def connected() -> bool:
    """True nếu đã cấu hình và mở được phiên tới MCP server."""
    if not is_configured():
        return False
    try:
        await _with_session(lambda s: s.send_ping())
        return True
    except Exception as e:
        log.warning("meeting.mcp_client: không kết nối được MCP server: %s", e)
        return False


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


async def list_tools() -> List[Dict[str, Any]]:
    """Danh sách tool của server: [{name, description, input_schema}]."""
    res = await _with_session(lambda s: s.list_tools())
    return [{"name": t.name, "description": t.description or "",
             "input_schema": _attr(t, "input_schema", "inputSchema", default={}) or {}}
            for t in res.tools]


async def list_resources() -> List[Dict[str, Any]]:
    res = await _with_session(lambda s: s.list_resources())
    return [{"uri": str(r.uri), "name": r.name, "description": r.description or ""} for r in res.resources]


def parse_result(result: Any) -> Dict[str, Any]:
    """Chuyển CallToolResult thành dict: ưu tiên structured content, rồi JSON trong text content."""
    is_error = bool(_attr(result, "is_error", "isError", default=False))
    texts = [c.text for c in (_attr(result, "content", default=[]) or []) if getattr(c, "type", "") == "text"]
    structured = _attr(result, "structured_content", "structuredContent", default=None)
    if isinstance(structured, dict) and not is_error:
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    joined = "\n".join(texts).strip()
    if joined:
        try:
            data = json.loads(joined)
            out = data if isinstance(data, dict) else {"result": data}
        except ValueError:
            out = {"text": joined}
    else:
        out = {}
    if is_error:
        out.setdefault("error", joined or "Tool MCP trả về lỗi")
    return out


async def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Gọi tool trên MCP server, trả về dict kết quả. Raise RuntimeError nếu chưa cấu hình."""
    res = await _with_session(lambda s: s.call_tool(name, dict(arguments or {})))
    return parse_result(res)


async def read_resource(uri: str) -> str:
    res = await _with_session(lambda s: s.read_resource(uri))
    return "\n".join(getattr(c, "text", "") for c in res.contents)
