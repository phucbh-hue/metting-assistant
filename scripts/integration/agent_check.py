"""Kiểm thử trợ lý với LLM thật (CÓ TÍNH PHÍ): yêu cầu vẽ chart / báo cáo phải ra sản phẩm có nội dung.

    python scripts/integration/agent_check.py

DB vẫn in-memory (MEETING_DB=mock), dữ liệu Jira là dữ liệu giả lập. In ra các câu trợ lý nói trong lúc xử lý,
câu trả lời cuối và tóm tắt sản phẩm (dashboard: số KPI, loại biểu đồ; báo cáo: tiêu đề, độ dài).
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["MEETING_DB"] = "mock"

from meeting import artifacts, db, llm, mcp  # noqa: E402

SEGMENTS = [
    ("Bùi Hồng Phúc", "Chào mọi người, hôm nay mình review sprint 39 và kế hoạch Mega Sale."),
    ("Hương", "Landing page xong bản desktop, bản mobile còn phần banner, dự kiến thứ sáu xong."),
    ("Lê Văn Tuấn", "Migrate Postgres 18 em đang review connection pool, cuối tuần chạy thử staging."),
    ("Hương", "Em cần thêm số liệu đổi voucher theo ngày để làm báo cáo cho đối tác."),
]
REQUESTS = [
    "em hãy vẽ cho anh mấy cái chart, mấy cái báo cáo số liệu về tiến độ hiện tại mà anh em muốn điều chỉnh nha",
    "tổng hợp nhanh cuộc họp thành báo cáo: quyết định, việc cần làm, người phụ trách, hạn chót",
]


async def main():
    if not artifacts.llm_available():
        print("Chưa cấu hình ANTHROPIC_API_KEY hoặc GEMINI_API_KEY")
        return 1
    db.init()
    mcp.seed_mock_data()
    mid = db.create_meeting("Review Sprint 39")
    segs, t = [], 0.0
    for who, text in SEGMENTS:
        segs.append({"speaker_label": who, "text": text, "t_start": t, "t_end": t + 6})
        t += 7
    ok = True
    for req in ([REQUESTS[int(sys.argv[1])]] if len(sys.argv) > 1 else REQUESTS):
        print(f"\n=== Yêu cầu: {req}")
        said = []

        async def progress(text, kind="progress"):
            said.append(f"[{kind}] {text}")

        async def insights(items):
            said.extend(f"[nhận xét/{i['kind']}] {i['text']}" for i in items)

        t0 = time.time()
        res = await llm.think_and_act(mid, req, segs, on_progress=progress, on_insights=insights)
        print(f"  ({time.time() - t0:.1f}s, {len(res['tool_calls'])} lần tra cứu)")
        for line in said:
            print("  ", line)
        print("  Trả lời:", res["chat_response"])
        art = res.get("artifact")
        if not art:
            print("  !! Không có sản phẩm hiển thị")
            ok = False
            continue
        print(f"  Sản phẩm: {art['kind']} - {art['title']}")
        if art["kind"] == "dashboard":
            d = json.loads(art["content"])
            print(f"   KPI: {[k['label'] + '=' + k['value'] for k in d['kpis']]}")
            print(f"   Biểu đồ: {[c['type'] + ':' + c['title'] + (' (minh họa)' if c.get('sample') else '') for c in d['charts']]}")
        else:
            print(f"   {len(art['content'])} ký tự; dòng đầu: {art['content'].splitlines()[0][:90]}")
        if "xử lý xong" in res["chat_response"]:
            ok = False
    print("\nKẾT QUẢ:", "ĐẠT" if ok else "CHƯA ĐẠT")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
