"""Kiểm thử tích hợp pipeline THẬT: file WAV -> Soniox Realtime -> CAM++ -> phân vai người nói.

    python scripts/integration/soniox_wav_replay.py youtube_nam_huong.wav --expect-speakers 2

- Cần SONIOX_API_KEY trong .env (tính phí theo thời lượng audio gửi lên).
- Luôn dùng DB in-memory: không ghi gì vào MongoDB thật.
- LLM tắt mặc định; thêm --with-llm để chạy thử AI đoán tên sau khi phát xong.
- File WAV: PCM 16-bit mono 16kHz.
"""
import argparse
import asyncio
import os
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

parser = argparse.ArgumentParser()
parser.add_argument("wav", help="Đường dẫn file WAV 16kHz mono")
parser.add_argument("--speed", type=float, default=1.0, help="Tốc độ gửi audio (1.0 = thời gian thực)")
parser.add_argument("--expect-speakers", type=int, default=None, help="Số người nói kỳ vọng (sai thì exit 1)")
parser.add_argument("--with-llm", action="store_true", help="Chạy AI đoán tên sau khi phát xong")
args = parser.parse_args()

os.environ["MEETING_DB"] = "mock"
if not args.with_llm:
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["GEMINI_API_KEY"] = ""

from meeting import db, live, voice  # noqa: E402


async def main() -> int:
    if not os.getenv("SONIOX_API_KEY"):
        print("Thiếu SONIOX_API_KEY trong .env")
        return 2
    with wave.open(args.wav, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            print("Cần WAV PCM 16-bit mono 16kHz")
            return 2
        pcm = w.readframes(w.getnframes())
    db.init()
    voice.warmup()
    mid = db.create_meeting(f"Replay {Path(args.wav).name}")
    s = await live.get_session(mid)
    owner = object()
    stream = await s.start_audio(owner)
    chunk = 3200  # 100ms
    t0 = time.time()
    for off in range(0, len(pcm), chunk):
        await stream.feed(pcm[off:off + chunk])
        await asyncio.sleep(0.1 / args.speed)
    await s.stop_audio(owner)          # finalize như khi người dùng tắt mic
    await asyncio.sleep(3.0)
    await stream.close()               # gửi hết audio, chờ Soniox chốt chữ cuối
    await s.drain()
    print(f"\nĐã phát {len(pcm) / 32000:.1f}s audio trong {time.time() - t0:.1f}s\n")
    for seg in s.segments:
        print(f"  [{seg['t_start']:6.1f}s] soniox={seg['raw_speaker']!s:>4}  {seg['speaker_label']:<14} | {seg['text'][:80]}")
    speakers = s.public_speakers()
    print("\nNgười nói:", ", ".join(f"{p['label']} ({p['n_segments']} câu, {p['speech_s']:.0f}s)" for p in speakers))
    if args.with_llm:
        results = await s.identity.run(force=True)
        await s.drain()
        for r in results:
            print(f"  AI: {r['old_label']} -> {r['new_name']} ({r['confidence']:.0%}, {r['action']}): {r['reasoning'][:100]}")
    if args.expect_speakers is not None and len(speakers) != args.expect_speakers:
        print(f"\nKHÔNG ĐẠT: kỳ vọng {args.expect_speakers} người nói, nhận được {len(speakers)}")
        return 1
    print("\nĐẠT")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
