"""Chạy server ở chế độ DEMO và phát lại một cuộc họp mẫu có 3 người nói.

    python scripts/demo_replay.py --port 8090

- DB in-memory (không đụng MongoDB thật), không cần mic, không gọi Soniox.
- Vector giọng là dữ liệu tổng hợp (không phải giọng người thật).
- Mặc định tắt LLM để không tốn phí; thêm --with-llm để AI tự đoán tên người nói từ hội thoại.
Mở http://127.0.0.1:8090 rồi vào cuộc họp "Demo: Review Sprint 39" để xem transcript chạy trực tiếp.
"""
import argparse
import asyncio
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
from meeting import db, live  # noqa: E402

D = 192
SCRIPT = [
    (0, "1", "Chào mọi người, mình bắt đầu họp review sprint 39 nhé."),
    (1, "2", "Dạ em chào anh Phúc, em là Hương bên marketing, hôm nay em báo cáo chiến dịch Mega Sale mười tháng mười."),
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
