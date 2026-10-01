"""Kiểm tra end-to-end MCP server "urbox-meeting-data" qua giao thức MCP thật.

    python scripts/mcp_check.py            # stdio: spawn "python -m mcp_server" qua MCP_SERVER_CMD
    python scripts/mcp_check.py --http     # thêm: chạy server HTTP nền trên cổng 8765 rồi gọi qua MCP_SERVER_URL

Exit code 0 nếu mọi bước đều đạt.
"""
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MEETING_DB", "mock")

from meeting import mcp_client  # noqa: E402

EXPECTED_TOOLS = {"query_employee_directory", "query_jira_issues", "query_system_architecture",
                  "query_meeting_history", "update_jira_issue_status", "search_knowledge", "read_document"}
EXPECTED_TOP_DOC = "chinh-sach-doi-tra-voucher.md"


def _short(obj, n=160) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "..."


async def run_checks(label: str) -> bool:
    ok = True
    print(f"\n=== Kiểm tra qua {label} ===")
    if not await mcp_client.connected():
        print("  [LỖI] Không kết nối được MCP server")
        return False
    print("  [OK] Đã kết nối MCP server")

    tools = await mcp_client.list_tools()
    names = {t["name"] for t in tools}
    missing = EXPECTED_TOOLS - names
    print(f"  [{'OK' if not missing else 'LỖI'}] {len(tools)} tool: {', '.join(sorted(names))}")
    if missing:
        print(f"        Thiếu tool: {', '.join(sorted(missing))}")
        ok = False

    jira = await mcp_client.call_tool("query_jira_issues", {"status": "In Progress"})
    issues = jira.get("issues") or []
    print(f"  [{'OK' if issues else 'LỖI'}] query_jira_issues(status='In Progress'): {jira.get('count', 0)} ticket")
    for it in issues[:3]:
        print(f"        - {it.get('key')}: {it.get('title', '')[:70]} ({it.get('status')})")
    ok &= bool(issues)

    kb = await mcp_client.call_tool("search_knowledge", {"query": "đổi trả voucher", "top_k": 3})
    results = kb.get("results") or []
    top_ok = bool(results) and results[0].get("path") == EXPECTED_TOP_DOC
    print(f"  [{'OK' if top_ok else 'LỖI'}] search_knowledge('đổi trả voucher'): {kb.get('count', 0)} kết quả")
    for r in results:
        print(f"        - {r['score']:>7}  {r['path']}  |  {r['title']}")
    if results:
        print(f"        Trích đoạn: {results[0]['snippet'][:150]}...")
    ok &= top_ok

    if results:
        doc = await mcp_client.call_tool("read_document", {"path": results[0]["path"]})
        good = bool(doc.get("content"))
        print(f"  [{'OK' if good else 'LỖI'}] read_document('{results[0]['path']}'): {len(doc.get('content', ''))} ký tự")
        ok &= good
    bad = await mcp_client.call_tool("read_document", {"path": "../server.py"})
    print(f"  [{'OK' if bad.get('error') else 'LỖI'}] read_document('../server.py') bị chặn: {_short(bad)}")
    ok &= bool(bad.get("error"))

    await mcp_client.close()
    return ok


def _wait_port(host: str, port: int, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _port_busy(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def check_stdio() -> bool:
    os.environ.pop("MCP_SERVER_URL", None)
    os.environ["MCP_SERVER_CMD"] = f'"{sys.executable}" -m mcp_server'
    return asyncio.run(run_checks("stdio (MCP_SERVER_CMD)"))


def check_http(port: int) -> bool:
    host = "127.0.0.1"
    if _port_busy(host, port):
        print(f"\n[LỖI] Cổng {port} đang bị chiếm, dùng --port để chọn cổng khác")
        return False
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", MEETING_DB=os.getenv("MEETING_DB", "mock"))
    print(f"\nĐang khởi động MCP server HTTP trên {host}:{port} ...")
    proc = subprocess.Popen([sys.executable, "-m", "mcp_server", "--transport", "http", "--host", host,
                             "--port", str(port)], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        if not _wait_port(host, port, 30):
            err = proc.stderr.read().decode("utf-8", "replace")[-800:] if proc.poll() is not None else ""
            print(f"[LỖI] Server HTTP không lên sau 30 giây. {err}")
            return False
        os.environ.pop("MCP_SERVER_CMD", None)
        os.environ["MCP_SERVER_URL"] = f"http://{host}:{port}/mcp"
        return asyncio.run(run_checks(f"streamable-http ({os.environ['MCP_SERVER_URL']})"))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("Đã dừng MCP server HTTP")


def main() -> int:
    ap = argparse.ArgumentParser(description="Kiểm tra MCP server urbox-meeting-data")
    ap.add_argument("--http", action="store_true", help="kiểm tra thêm transport streamable-http")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    try:
        import mcp  # noqa: F401
    except ImportError:
        print('[LỖI] Chưa cài SDK MCP: pip install "mcp[cli]"')
        return 1

    ok = check_stdio()
    if args.http:
        ok = check_http(args.port) and ok
    print("\nKẾT QUẢ: " + ("TẤT CẢ ĐẠT" if ok else "CÓ BƯỚC KHÔNG ĐẠT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
