"""Chạy thử Nemotron-3-Diarization trên âm thanh đã ghi của một cuộc họp và so với cách hệ thống đang phân vai.

Thử nghiệm (chưa nối vào phòng họp): Nemotron cho biết "ai nói lúc nào" (tối đa 8 người, đánh số theo thứ tự xuất hiện)
nhưng không cho vector giọng, nên nếu tốt sẽ thay NHÃN NGƯỜI NÓI CỦA SONIOX, còn ERes2NetV2 vẫn giữ để nhận lại người
quen. Model: bản ONNX cộng đồng (lượng tử hóa, ~120 MB) của nvidia/Nemotron-3-Diarization (giấy phép OpenMDW 1.1),
chạy trên CPU; đặc trưng mel theo mã tham chiếu của bản LiteRT.

Cài một lần (môi trường riêng, không đụng môi trường của server):
    python -m venv data\\nemotron-venv
    data\\nemotron-venv\\Scripts\\python -m pip install onnxruntime numpy msvc-runtime
    data\\nemotron-venv\\Scripts\\python scripts\\nemotron_diar.py --download
Chạy (cuộc họp đã bật "Ghi âm", server đang chạy ở cổng 8080):
    data\\nemotron-venv\\Scripts\\python scripts\\nemotron_diar.py 59
    data\\nemotron-venv\\Scripts\\python scripts\\nemotron_diar.py --wav file.wav     (chỉ in các đoạn)

Giới hạn: chạy nguyên khúc (offline). Mỗi lượt ghi dài hơn WINDOW_S được chia thành các khúc phân tích riêng (nhãn người
giữa các khúc không nối với nhau). Muốn chạy trực tiếp trong cuộc họp cần chuyển thêm bộ nhớ đệm người nói (AOSC).
"""
import argparse
import itertools
import json
import os
import sys
import time
import urllib.request
import wave
from pathlib import Path

if os.name == "nt":
    os.add_dll_directory(sys.prefix)        # msvc-runtime đặt msvcp140.dll ở đây (máy chưa cài Visual C++)
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MD = ROOT / "models" / "nemotron-diar"
FILES = {
    "model_quantized.onnx": "https://huggingface.co/developerjeremylive/Nemotron-3-Diarization-ONNX-etheroi/resolve/main/onnx/model_quantized.onnx",
    "model_quantized.onnx_data": "https://huggingface.co/developerjeremylive/Nemotron-3-Diarization-ONNX-etheroi/resolve/main/onnx/model_quantized.onnx_data",
    "frontend_mel128_257.bin": "https://huggingface.co/litert-community/Nemotron-3-Diarization-LiteRT/resolve/main/assets/frontend_mel128_257.bin",
    "hann400.bin": "https://huggingface.co/litert-community/Nemotron-3-Diarization-LiteRT/resolve/main/assets/hann400.bin",
}
WINDOW_S = 300.0       # khúc phân tích (encoder tối đa 5000 khung 80 ms = 400 s)
RATE = 16000


def download():
    MD.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        p = MD / name
        if p.exists() and p.stat().st_size > 0:
            continue
        print("tải", name)
        urllib.request.urlretrieve(url, p)


# ------------------------------------------------------------------ model ---
_mel_f = _win = _sess = None


def _frontend():
    global _mel_f, _win
    if _mel_f is None:
        _mel_f = np.fromfile(MD / "frontend_mel128_257.bin", dtype="<f4").reshape(128, 257)
        _win = np.zeros(512, np.float32)
        _win[56:456] = np.fromfile(MD / "hann400.bin", dtype="<f4")
    return _mel_f, _win


def mel(x: np.ndarray) -> np.ndarray:
    """pre-emphasis 0.97, khung tâm tại 160*i (cửa sổ 512, hann 400 ở giữa), FFT 512, |X|^2, mel [128 x 257], log(x + 2^-24)."""
    mf, win = _frontend()
    y = np.empty_like(x)
    y[0] = x[0]
    y[1:] = x[1:] - 0.97 * x[:-1]
    n = len(y) // 160 + 1
    pad = np.concatenate([np.zeros(256, np.float32), y, np.zeros(512, np.float32)])
    frames = pad[np.arange(512)[None, :] + 160 * np.arange(n)[:, None]] * win
    p = np.abs(np.fft.rfft(frames, n=512, axis=1)).astype(np.float32) ** 2
    return np.log(p @ mf.T + 2.0 ** -24).astype(np.float32)


def session():
    global _sess
    if _sess is None:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        _sess = ort.InferenceSession(str(MD / "model_quantized.onnx"), so, providers=["CPUExecutionProvider"])
    return _sess


def diarize(x: np.ndarray) -> np.ndarray:
    """Xác suất đang nói của 8 người, khung 10 ms (một lượt offline)."""
    f = mel(x)
    out = session().run(None, {"input_features": f[None], "cached_embeds": np.zeros((1, 0, 512), np.float32),
                               "attention_mask": np.ones((1, -(-f.shape[0] // 8)), np.int64)})
    return 1.0 / (1.0 + np.exp(-out[0][0]))


def segments(p: np.ndarray, thr: float = 0.5, min_s: float = 0.3):
    out = []
    for spk in range(p.shape[1]):
        on = np.concatenate([[False], p[:, spk] > thr, [False]])
        d = np.diff(on.astype(int))
        for a, b in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
            if (b - a) / 100 >= min_s:
                out.append((a / 100, b / 100, spk + 1))
    return sorted(out)


def load_wav(path) -> np.ndarray:
    with wave.open(str(path)) as w:
        if w.getframerate() != RATE or w.getnchannels() != 1:
            raise SystemExit("Cần WAV 16 kHz mono")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


# ------------------------------------------------------------- so sánh ---
def best_agreement(pairs):
    """% câu khớp sau khi ghép 1-1 tốt nhất giữa 2 cách gán nhãn."""
    if not pairs:
        return 0.0
    a_labels = sorted({a for a, _ in pairs}, key=str)
    b_labels = sorted({b for _, b in pairs}, key=str)
    cnt = {}
    for p in pairs:
        cnt[p] = cnt.get(p, 0) + 1
    if len(a_labels) > len(b_labels):
        a_labels, b_labels = b_labels, a_labels
        cnt = {(b, a): c for (a, b), c in cnt.items()}
    best = 0
    for perm in itertools.permutations(b_labels, len(a_labels)):
        best = max(best, sum(cnt.get((a, b), 0) for a, b in zip(a_labels, perm)))
    return best / len(pairs)


def run_meeting(mid: int, base: str):
    from meeting import recording            # đọc tệp ghi âm và manifest (không cần thư viện của server)
    data = json.load(urllib.request.urlopen(f"{base}/api/meetings/{mid}/export", timeout=120))
    segs = sorted(data["segments"], key=lambda s: s["t_start"])
    runs = recording.runs(mid)
    if not runs:
        have = sorted(int(d.name[1:]) for d in recording.ROOT.glob("m*") if d.name[1:].isdigit() and recording.runs(int(d.name[1:])))
        raise SystemExit(
            f"Cuộc họp {mid} chưa có âm thanh đã ghi. Hệ thống chỉ lưu âm thanh từ lúc bấm nút \"Ghi âm: tắt\" trên thanh "
            "tiêu đề phòng họp (cạnh \"Kết thúc cuộc họp\") và xác nhận mọi người đã đồng ý; phần nói trước đó, hoặc cuộc "
            "họp đã kết thúc, không ghi lại được.\n"
            + (f"Các cuộc họp đã có âm thanh: {', '.join(map(str, have))}" if have else "Hiện chưa có cuộc họp nào được ghi âm."))
    rows, total_audio, total_cpu = [], 0.0, 0.0
    for r in runs:
        x = np.frombuffer(Path(r["path"]).read_bytes(), dtype="<i2").astype(np.float32) / 32768.0
        total_audio += len(x) / RATE
        for w0 in np.arange(0, len(x) / RATE, WINDOW_S):
            chunk = x[int(w0 * RATE):int((w0 + WINDOW_S) * RATE)]
            if len(chunk) < RATE:
                continue
            t0 = time.time()
            p = diarize(chunk)
            total_cpu += time.time() - t0
            start = r["start_t"] + w0
            for s in segs:
                a, b = s["t_start"] - start, s["t_end"] - start
                if a < 0 or b > len(chunk) / RATE or b - a < 0.3:
                    continue
                m = p[int(a * 100):int(b * 100)].mean(axis=0)
                nem = f"{r['file']}@{int(w0)}:N{int(m.argmax()) + 1}" if m.max() >= 0.3 else None
                rows.append({"seq": s["seq"], "t": s["t_start"], "nemotron": nem, "soniox": f"e{s.get('epoch')}r{s.get('raw_speaker')}",
                             "system": s.get("speaker_label"), "text": s.get("text", "")})
    voiced = [r for r in rows if r["nemotron"]]
    print(f"Cuộc họp {mid}: {len(runs)} lượt ghi, {total_audio / 60:.1f} phút âm thanh, Nemotron xử lý {total_cpu:.1f}s CPU "
          f"(x{total_cpu / max(total_audio, 1):.3f} thời gian thực); {len(voiced)}/{len(rows)} câu có nhãn Nemotron.")
    print(f"  Khớp Nemotron với hệ thống hiện tại: {best_agreement([(r['nemotron'], r['system']) for r in voiced]):.0%}")
    print(f"  Khớp Nemotron với nhãn Soniox:        {best_agreement([(r['nemotron'], r['soniox']) for r in voiced]):.0%}")
    print(f"  Khớp hệ thống với nhãn Soniox:        {best_agreement([(r['system'], r['soniox']) for r in voiced]):.0%}")
    print("\n  seq    t(s)  Nemotron              Soniox  Hệ thống           Lời")
    for r in rows:
        print(f"  {r['seq']:>4} {r['t']:7.1f}  {str(r['nemotron'] or '-'):<20} {r['soniox']:<7} {str(r['system'])[:18]:<18} {r['text'][:60]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("meeting", nargs="?", type=int, help="mã cuộc họp đã ghi âm")
    ap.add_argument("--wav", help="chạy trên một tệp WAV 16 kHz mono và in các đoạn")
    ap.add_argument("--download", action="store_true", help="tải model (~120 MB) vào models/nemotron-diar")
    ap.add_argument("--base", default="http://127.0.0.1:8080", help="địa chỉ server (lấy câu và người nói)")
    a = ap.parse_args()
    if a.download:
        download()
    sys.path.insert(0, str(ROOT))
    if a.wav:
        x = load_wav(a.wav)
        t0 = time.time()
        p = diarize(x)
        print(f"{a.wav}: {len(x) / RATE:.1f}s, xử lý {time.time() - t0:.1f}s")
        for s, e, spk in segments(p):
            print(f"  người {spk}: {s:7.2f} - {e:7.2f}")
    elif a.meeting:
        run_meeting(a.meeting, a.base)
    elif not a.download:
        ap.print_help()


if __name__ == "__main__":
    main()
