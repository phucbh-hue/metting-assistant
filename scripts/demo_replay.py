"""Chạy server ở chế độ DEMO và phát lại một cuộc họp mẫu có 3 người nói.

    python scripts/demo_replay.py --port 8090

- DB in-memory (không đụng MongoDB thật), không cần mic, không gọi Soniox.
- Vector giọng là dữ liệu tổng hợp (không phải giọng người thật).
- Mặc định tắt LLM để không tốn phí; thêm --with-llm để AI tự đoán tên người nói từ hội thoại.
- Có sẵn một bộ slide mẫu, một dashboard mẫu (dữ liệu Jira giả lập) và một sơ đồ tư duy mẫu: gõ "chuyển slide",
  "quay lại", "nhắc bài", "thuyết trình sơ đồ" vào ô lệnh (các lệnh này chạy không cần LLM), bấm vào một ý của sơ đồ
  để nghe ghi chú của ý đó, hoặc để kịch bản tự gọi "Jarvis ơi, chuyển slide".
Mở http://127.0.0.1:8090 rồi vào cuộc họp "Demo: Review Sprint 39" để xem transcript chạy trực tiếp.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

parser = argparse.ArgumentParser(description="Meeting Copilot demo replay")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8090)
parser.add_argument("--delay", type=float, default=4.0, help="Số giây chờ trước khi bắt đầu phát")
parser.add_argument("--gap", type=float, default=1.2, help="Khoảng nghỉ giữa các câu (giây)")
parser.add_argument("--with-llm", action="store_true", help="Giữ API key LLM trong .env để AI đoán tên")
parser.add_argument("--loop", action="store_true", help="Phát lại liên tục")
args = parser.parse_args()

os.environ["MEETING_DB"] = "mock"
os.environ["SONIOX_API_KEY"] = ""
if not args.with_llm:
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["GEMINI_API_KEY"] = ""

import numpy as np  # noqa: E402
import uvicorn  # noqa: E402

from meeting import app as appmod  # noqa: E402
from meeting import artifacts, db, live, llm, mcp  # noqa: E402

D = 192
SCRIPT = [
    (0, "1", "Chào mọi người, mình bắt đầu họp review sprint 39 nhé."),
    (1, "2", "Dạ em chào anh Phúc, em là Hương bên marketing, hôm nay em báo cáo chiến dịch Mega Sale mười tháng mười."),
    (0, "1", "Jarvis ơi, chuyển slide."),
    (0, "1", "Ok Hương, em nói tiến độ landing page trước đi."),
    (1, "2", "Landing page đã xong bản desktop, bản mobile còn phần banner, dự kiến thứ sáu là xong."),
    (1, "2", "Dạ."),
    (2, "3", "Bên backend em cache xong danh mục voucher bằng Redis rồi, độ trễ giảm còn khoảng tám mươi mili giây."),
    (0, "1", "Tốt. Tuấn ơi, phần migrate Postgres mười tám bên DevOps thế nào rồi?"),
    (2, "3", "Dạ em đang review cấu hình connection pool, cuối tuần chạy thử trên staging được anh."),
    (1, "2", "Em cần thêm số liệu đổi voucher theo ngày để làm báo cáo cho đối tác."),
    (0, "1", "Jarvis ơi, liệt kê giúp các việc cần làm và người phụ trách."),
    (2, "3", "Ừ."),
    (0, "1", "Vậy chốt: Hương xong mobile trước thứ sáu, Tuấn chạy staging cuối tuần, mình review lại vào thứ hai."),
]


def demo_deck():
    """Bộ slide mẫu (dữ liệu giả lập) khớp với kịch bản họp."""
    return {"title": "Review Sprint 39", "slides": [
        {"title": "Review Sprint 39 - Team Payment", "layout": "title",
         "bullets": [f"Họp ngày {time.strftime('%d/%m/%Y')}"], "notes": "Mở đầu, nêu hai mục chính của buổi họp"},
        {"title": "Tiến độ Mega Sale 10.10", "layout": "bullets",
         "bullets": ["Landing page xong bản desktop", "Bản mobile còn phần banner, dự kiến xong thứ sáu",
                     "Cần số liệu đổi voucher theo ngày cho báo cáo đối tác"],
         "notes": "Hương báo cáo, nhấn mạnh hạn thứ sáu"},
        {"title": "Hiệu năng và hạ tầng", "layout": "metrics",
         "bullets": ["Độ trễ danh mục voucher: 80 ms", "Bộ nhớ đệm: Redis", "Migrate Postgres 18: Staging cuối tuần"],
         "notes": "Tuấn cập nhật phần backend và DevOps"},
        {"title": "Việc cần làm", "layout": "two_column",
         "bullets": ["Hương: xong bản mobile trước thứ sáu", "Tuấn: chạy thử staging cuối tuần",
                     "Marketing: gửi số liệu đổi voucher", "Cả nhóm: review lại vào thứ hai"], "notes": ""},
    ]}


def demo_mindmap():
    """Sơ đồ tư duy mẫu (kiểu NotebookLM) khớp với kịch bản họp; bấm vào ý nào cũng có ghi chú để đọc khi không có AI."""
    return artifacts.normalize_mindmap({"type": "mindmap", "title": "Review Sprint 39", "root": {
        "label": "Review Sprint 39", "detail": "Hai mục chính: tiến độ Mega Sale 10.10 và hạ tầng; chốt lịch review thứ hai.",
        "children": [
            {"label": "Mega Sale 10.10", "tone": "doing", "detail": "Hương báo cáo: landing page xong bản desktop, mobile còn banner.",
             "children": [{"label": "Desktop đã xong", "tone": "done"},
                          {"label": "Banner mobile trước thứ sáu", "tone": "risk",
                           "detail": "Bản mobile còn phần banner, Hương hẹn xong thứ sáu."},
                          {"label": "Cần số liệu đổi voucher theo ngày", "tone": "todo",
                           "detail": "Marketing cần để làm báo cáo cho đối tác."}]},
            {"label": "Hiệu năng backend", "tone": "done", "detail": "Tuấn đã cache danh mục voucher bằng Redis.",
             "children": [{"label": "Redis cache danh mục voucher"}, {"label": "Độ trễ còn 80 ms", "tone": "done"}]},
            {"label": "DevOps: Postgres 18", "tone": "doing", "detail": "Đang review connection pool, cuối tuần chạy thử staging.",
             "children": [{"label": "Review connection pool"}, {"label": "Staging cuối tuần", "tone": "todo"}]},
            {"label": "Việc cần làm", "tone": "todo", "detail": "Ba việc đã có người nhận, review lại vào thứ hai.",
             "children": [{"label": "Hương: mobile trước thứ sáu"}, {"label": "Tuấn: staging cuối tuần"},
                          {"label": "Cả nhóm: review thứ hai"}]}]}})


def demo_dashboard():
    """Dashboard mẫu tính trực tiếp từ dữ liệu Jira giả lập (không gọi LLM)."""
    issues = mcp.call_tool("query_jira_issues", {}).get("issues") or []
    ov = llm.jira_overview(issues)
    by_status = sorted(ov["by_status"].items(), key=lambda kv: -kv[1])
    by_person = sorted(ov["by_assignee"].items(), key=lambda kv: -kv[1])
    points = ov["story_points_by_assignee"]
    return artifacts.normalize_dashboard({
        "title": "Tiến độ Sprint 39",
        "subtitle": "Ticket Jira của hệ thống demo (dữ liệu giả lập)",
        "kpis": [{"label": "Tổng ticket", "value": str(ov["total"]), "unit": "ticket"},
                 {"label": "Đang làm", "value": str(ov["by_status"].get("In Progress", 0)), "unit": "ticket"},
                 {"label": "Quá hạn", "value": str(len(ov["overdue"])), "unit": "ticket",
                  "delta": ", ".join(o["key"] for o in ov["overdue"]) or "Không có", "trend": "up" if ov["overdue"] else "flat"},
                 {"label": "Ưu tiên cao chưa xong", "value": str(len(ov["high_priority_open"])), "unit": "ticket"}],
        "charts": [
            {"type": "hbar", "title": "Ticket theo trạng thái", "unit": "ticket",
             "labels": [llm.status_vi(k) for k, _ in by_status], "series": [{"name": "Ticket", "data": [v for _, v in by_status]}]},
            {"type": "bar", "title": "Ticket theo người phụ trách", "unit": "ticket",
             "labels": [k for k, _ in by_person], "series": [{"name": "Ticket", "data": [v for _, v in by_person]}]},
            {"type": "donut", "title": "Story point theo người", "unit": "điểm",
             "labels": list(points), "series": [{"name": "Story point", "data": list(points.values())}]},
            {"type": "line", "title": "Ticket hoàn thành theo ngày", "unit": "ticket", "sample": True,
             "note": "Số liệu minh họa để xem thử dạng biểu đồ đường.",
             "labels": ["24/09", "25/09", "26/09", "29/09", "30/09", "01/10"],
             "series": [{"name": "Kế hoạch", "data": [1, 2, 3, 4, 5, 6]}, {"name": "Thực tế", "data": [1, 1, 2, 2, 3, 4]}]},
            {"type": "table", "title": "Ticket cần chú ý", "columns": ["Ticket", "Người phụ trách", "Trạng thái", "Hạn"],
             "rows": [[i.get("key"), i.get("assignee"), llm.status_vi(i.get("status")), llm._fmt_day(i.get("due_date"))]
                      for i in issues if str(i.get("status", "")).lower() != "done"][:8]},
        ],
        "highlights": [f"{o['key']} của {o['assignee']} đã quá hạn từ {o['due']}." for o in ov["overdue"][:2]]
        or ["Không có ticket quá hạn."],
        "source": "Nguồn: Jira giả lập (Mock MCP)",
    })


def unit(x):
    return (x / np.linalg.norm(x)).astype(np.float32)


class Voices:
    def __init__(self, seed=7, n=3):
        self.rng = np.random.default_rng(seed)
        self.channel = unit(self.rng.standard_normal(D))
        self.base = [unit(self.rng.standard_normal(D)) for _ in range(n)]

    def vec(self, k, voiced):
        sigma = 1.3 if voiced < 2 else 0.85 if voiced < 4 else 0.6
        return unit(0.7 * self.channel + self.base[k] + sigma * unit(self.rng.standard_normal(D)))

    def enrollment(self, k):
        return unit(0.7 * self.channel + self.base[k] + 0.3 * unit(self.rng.standard_normal(D)))


async def seed_history(bank: Voices):
    """Một cuộc họp đã kết thúc để danh sách có dữ liệu."""
    mid = db.create_meeting("Daily standup team Payment", meeting_type="Daily Standup")
    s = await live.get_session(mid)
    t = 3.0
    for k, raw, text in [(0, "1", "Hôm qua mình xong phần đối soát giao dịch."),
                         (2, "2", "Em đang sửa lỗi webhook thanh toán, chiều nay xong."),
                         (0, "1", "Ok, có gì vướng báo mình.")]:
        voiced = len(text.split()) * 0.32
        await s.on_segment_finalized(t, t + voiced * 1.25, raw, text, vector=bank.vec(k, voiced), voiced=voiced)
        t += voiced * 1.25 + 0.8
    await s.drain()
    await s.finish(generate_minutes=False)
    day = time.time() - 86400
    db.update_meeting(mid, {"started_at": day, "ended_at": day + 900}, internal=True)


async def replay():
    bank = Voices()
    db.save_voice("Bùi Hồng Phúc", bank.enrollment(0).tolist(), role="CTO", consent_by="self_enrolled_mic",
                  mode="replace")
    await seed_history(bank)
    while True:
        mid = db.create_meeting("Demo: Review Sprint 39", meeting_type="Sprint Planning",
                                expected_attendees=["Hương", "Lê Văn Tuấn"],
                                agenda=["Tiến độ Mega Sale 10.10", "Migrate Postgres 18"])
        s = await live.get_session(mid)
        db.save_artifact(mid, "dashboard", "Dashboard: Tiến độ Sprint 39", json.dumps(demo_dashboard(), ensure_ascii=False))
        db.save_artifact(mid, "diagram", "Sơ đồ: Review Sprint 39", json.dumps(demo_mindmap(), ensure_ascii=False))
        deck_id = db.save_artifact(mid, "slides", "Slide: Review Sprint 39", json.dumps(demo_deck(), ensure_ascii=False))
        await s.stage_action("show", artifact_id=deck_id)
        print(f"\n>>> Demo sẵn sàng: http://{args.host}:{args.port}/#/m/{mid}\n", flush=True)
        await asyncio.sleep(args.delay)
        t = 2.0
        for k, raw, text in SCRIPT:
            words = text.split()
            for i in range(2, len(words) + 1, 2):
                prof = s.speakers.peek(raw, 0, "mic")
                await s.emit({"type": "interim", "stream": "mic", "text": " ".join(words[:i]),
                              "speaker_key": prof.sid if prof else None, "speaker_label": prof.label if prof else None})
                await asyncio.sleep(0.16)
            voiced = max(0.3, len(words) * 0.32)
            await s.emit({"type": "interim", "stream": "mic", "text": "", "speaker_key": None, "speaker_label": None})
            await s.on_segment_finalized(t_start=round(t, 2), t_end=round(t + voiced * 1.25, 2), raw_speaker=raw,
                                         text=text, vector=bank.vec(k, voiced) if voiced >= 1.0 else None,
                                         voiced=voiced, epoch=0)
            t += voiced * 1.25 + 0.6
            await asyncio.sleep(args.gap)
        print(">>> Đã phát xong cuộc họp mẫu.", flush=True)
        if not args.loop:
            return
        await asyncio.sleep(10)


async def main():
    server = uvicorn.Server(uvicorn.Config(appmod.app, host=args.host, port=args.port, log_level="warning"))

    async def runner():
        while not server.started:
            await asyncio.sleep(0.1)
        await replay()

    await asyncio.gather(server.serve(), runner())


if __name__ == "__main__":
    asyncio.run(main())
