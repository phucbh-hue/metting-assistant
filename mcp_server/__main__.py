"""python -m mcp_server [--transport stdio|http] [--host 127.0.0.1] [--port 8765] [--path /mcp]"""
import argparse
import os
import sys
from pathlib import Path

# Cho phép chạy trực tiếp bằng đường dẫn file (python .../mcp_server/__main__.py) từ thư mục bất kỳ
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main():
    ap = argparse.ArgumentParser(prog="python -m mcp_server",
                                 description="MCP server urbox-meeting-data (dữ liệu nội bộ + kho tri thức)")
    ap.add_argument("--transport", choices=["stdio", "http", "streamable-http"], default="stdio",
                    help="stdio (mặc định) hoặc http (streamable-http)")
    ap.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8765")))
    ap.add_argument("--path", default="/mcp", help="đường dẫn endpoint HTTP (mặc định /mcp)")
    args = ap.parse_args()

    from mcp_server.server import run
    run(args.transport, args.host, args.port, args.path)


if __name__ == "__main__":
    main()
