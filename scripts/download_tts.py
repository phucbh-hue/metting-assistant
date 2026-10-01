"""Tải giọng đọc tiếng Việt chạy trên máy (Piper, định dạng sherpa-onnx) vào models/tts/.

    python scripts/download_tts.py                 # giọng mặc định vais1000 (nữ, 22 kHz, ~67 MB)
    python scripts/download_tts.py --voice vits-piper-vi_VN-vivos-x_low

Giọng có sẵn: vits-piper-vi_VN-vais1000-medium, vits-piper-vi_VN-25hours_single-low, vits-piper-vi_VN-vivos-x_low.
Đổi giọng: đặt TTS_VOICE=<tên thư mục> trong .env. Dữ liệu huấn luyện vais1000: giấy phép CC BY 4.0.
"""
import argparse
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"


def main() -> int:
    ap = argparse.ArgumentParser(description="Tải giọng đọc tiếng Việt cho Meeting Copilot")
    ap.add_argument("--voice", default="vits-piper-vi_VN-vais1000-medium")
    ap.add_argument("--dest", default=str(ROOT / "models" / "tts"))
    args = ap.parse_args()
    dest = Path(args.dest)
    if (dest / args.voice).is_dir() and any((dest / args.voice).glob("*.onnx")):
        print(f"Đã có giọng {args.voice} ở {dest / args.voice}")
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    url = f"{BASE}{args.voice}.tar.bz2"
    print(f"Đang tải {url} ...")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "voice.tar.bz2"

        def progress(blocks, block_size, total):
            if total > 0:
                pct = min(100, blocks * block_size * 100 // total)
                print(f"\r  {pct}% ({total // 1_000_000} MB)", end="", flush=True)

        urllib.request.urlretrieve(url, archive, progress)
        print("\nĐang giải nén ...")
        with tarfile.open(archive, "r:bz2") as tf:
            root = dest.resolve()
            for m in tf.getmembers():   # chặn đường dẫn thoát khỏi thư mục đích
                if not (dest / m.name).resolve().is_relative_to(root):
                    raise RuntimeError(f"Tệp nén chứa đường dẫn không hợp lệ: {m.name}")
            tf.extractall(dest)
    print(f"Xong: {dest / args.voice}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
