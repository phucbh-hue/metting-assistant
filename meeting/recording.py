"""Lưu âm thanh cuộc họp trên máy (tùy chọn, mặc định TẮT) để chạy lại / so sánh các bộ phân biệt người nói.

Giọng nói là dữ liệu sinh trắc học (Nghị định 13/2023/NĐ-CP):
- Chỉ ghi khi chủ phòng bật cho từng cuộc họp và xác nhận mọi người trong phòng đã đồng ý (lưu ai xác nhận, lúc nào).
- Chỉ lưu trên máy chạy ứng dụng (data/recordings/, không commit, không đẩy lên Atlas).
- Tự xóa sau RECORDING_RETENTION_DAYS ngày (mặc định 30); xóa cuộc họp thì xóa luôn; xóa riêng được bất cứ lúc nào.

Mỗi lần bật mic (một "lượt") là một tệp PCM 16-bit mono 16 kHz; manifest.json ghi tệp đó bắt đầu ở giây thứ mấy của
cuộc họp, để khớp với t_start của từng câu.
"""
import json
import logging
import os
import shutil
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("meeting.recording")

ROOT = Path(os.getenv("RECORDING_DIR") or Path(__file__).resolve().parent.parent / "data" / "recordings")
RETENTION_DAYS = float(os.getenv("RECORDING_RETENTION_DAYS", "30"))
RATE = 16000
_lock = threading.Lock()


def meeting_dir(mid: int) -> Path:
    return ROOT / f"m{int(mid)}"


def _manifest(mid: int) -> Dict[str, Any]:
    p = meeting_dir(mid) / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"meeting_id": int(mid), "runs": []}


def _save_manifest(mid: int, man: Dict[str, Any]):
    d = meeting_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")


class Run:
    """Một lượt ghi (từ lúc bật mic tới lúc tắt): ghi nối PCM vào một tệp."""

    def __init__(self, mid: int, stream: str, start_t: float):
        with _lock:
            man = _manifest(mid)
            self.file = f"{stream}-{len(man['runs']) + 1:03d}.pcm"
            man["runs"].append({"file": self.file, "stream": stream, "start_t": round(float(start_t), 3),
                                "created_at": time.time()})
            _save_manifest(mid, man)
        self.path = meeting_dir(mid) / self.file
        self._fh = open(self.path, "ab")

    def write(self, pcm: bytes):
        if self._fh is not None and pcm:
            self._fh.write(pcm)

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def info(mid: int) -> Dict[str, Any]:
    man = _manifest(mid)
    d = meeting_dir(mid)
    seconds = sum((d / r["file"]).stat().st_size / (2 * RATE) for r in man["runs"] if (d / r["file"]).exists())
    return {"runs": len(man["runs"]), "seconds": round(seconds, 1), "path": str(d) if d.exists() else "",
            "retention_days": RETENTION_DAYS}


def runs(mid: int) -> List[Dict[str, Any]]:
    """[{file, stream, start_t, path}] của các lượt đã ghi (để công cụ đọc lại âm thanh)."""
    d = meeting_dir(mid)
    return [{**r, "path": str(d / r["file"])} for r in _manifest(mid)["runs"] if (d / r["file"]).exists()]


def export_wav(mid: int, out: Path) -> List[Dict[str, Any]]:
    """Mỗi lượt ghi ra một tệp WAV (dùng cho công cụ đo / nghe lại). Trả về danh sách lượt kèm đường dẫn WAV."""
    out.mkdir(parents=True, exist_ok=True)
    res = []
    for r in runs(mid):
        wav = out / (Path(r["file"]).stem + ".wav")
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(Path(r["path"]).read_bytes())
        res.append({**r, "wav": str(wav)})
    return res


def delete(mid: int) -> bool:
    d = meeting_dir(mid)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    log.info("meeting.recording: đã xóa bản ghi âm cuộc họp %s", mid)
    return True


def purge_expired(now: Optional[float] = None) -> int:
    """Xóa bản ghi âm cũ hơn RECORDING_RETENTION_DAYS (theo lượt ghi mới nhất)."""
    if not ROOT.exists() or RETENTION_DAYS <= 0:
        return 0
    now = now or time.time()
    n = 0
    for d in ROOT.glob("m*"):
        if not d.is_dir():
            continue
        try:
            man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            last = max((r.get("created_at") or 0 for r in man.get("runs") or []), default=0)
        except (OSError, ValueError):
            last = d.stat().st_mtime
        if last and now - last > RETENTION_DAYS * 86400:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    if n:
        log.info("meeting.recording: đã xóa %d bản ghi âm quá %.0f ngày", n, RETENTION_DAYS)
    return n
