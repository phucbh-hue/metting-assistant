"""Đọc văn bản tiếng Việt thành giọng nói NGAY TRÊN MÁY (Piper qua sherpa-onnx): miễn phí, không cần API,
không gửi nội dung ra ngoài.

Model mặc định: vits-piper-vi_VN-vais1000-medium (22.050 Hz, ~63 MB) trong models/tts/
(tải bằng `python scripts/download_tts.py`). Tốc độ trên CPU: ~0,07 giây cho 1 giây âm thanh.
"""
import io
import logging
import os
import re
import threading
import time
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

log = logging.getLogger("meeting.tts")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VOICE = "vits-piper-vi_VN-vais1000-medium"
MODEL_DIR = Path(os.getenv("TTS_MODEL_DIR") or (ROOT / "models" / "tts" / os.getenv("TTS_VOICE", DEFAULT_VOICE)))
SPEED = float(os.getenv("TTS_SPEED", "1.05"))
MAX_CHARS = 600
CACHE_SIZE = 96

_tts = None
_load_error = ""
_lock = threading.Lock()
_cache: "OrderedDict[tuple, bytes]" = OrderedDict()


# ------------------------------------------------------------ đọc số tiếng Việt ---
_DIGITS = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]
_SCALES = ["", "nghìn", "triệu", "tỷ"]


def _read_3(n: int, full: bool) -> list:
    """Đọc nhóm 3 chữ số. full: nhóm không đứng đầu (đọc cả "không trăm", "linh")."""
    h, t, u = n // 100, (n // 10) % 10, n % 10
    out = []
    if h or full:
        out += [_DIGITS[h], "trăm"]
    if t == 0:
        if u and (h or full):
            out.append("linh")
        if u:
            out.append(_DIGITS[u])
    elif t == 1:
        out.append("mười")
        if u:
            out.append("lăm" if u == 5 else _DIGITS[u])
    else:
        out += [_DIGITS[t], "mươi"]
        if u:
            out.append({1: "mốt", 4: "tư", 5: "lăm"}.get(u, _DIGITS[u]))
    return out


def num_to_vi(n: int) -> str:
    """2026 -> 'hai nghìn không trăm hai mươi sáu', 1.200.000.000 -> 'một tỷ hai trăm triệu'."""
    if n == 0:
        return "không"
    if n < 0:
        return "âm " + num_to_vi(-n)
    if n >= 1000 ** 4:
        return " ".join(num_to_vi(int(d)) for d in str(n))
    groups = []
    while n:
        groups.append(n % 1000)
        n //= 1000
    words = []
    for i in range(len(groups) - 1, -1, -1):
        if groups[i]:
            words += _read_3(groups[i], full=i < len(groups) - 1)
            if _SCALES[i]:
                words.append(_SCALES[i])
    return " ".join(words)


def _decimal_vi(intpart: str, frac: str) -> str:
    return f"{num_to_vi(int(intpart))} phẩy {num_to_vi(int(frac)) if len(frac) <= 3 else ' '.join(_DIGITS[int(d)] for d in frac)}"


def _int_text(s: str) -> str:
    s = s.replace(".", "")
    return num_to_vi(int(s)) if len(s) <= 13 else " ".join(_DIGITS[int(d)] for d in s)


# Từ tiếng Anh hay gặp trong cuộc họp: đọc theo cách người Việt hay nói
_EN_WORDS = {
    "dashboard": "đát bọt", "slide": "xờ lai", "slides": "xờ lai", "ticket": "tích kê", "tickets": "tích kê",
    "review": "ri viu", "sprint": "xờ prin", "staging": "xtây ding", "report": "ri po", "meeting": "mít tinh",
    "deadline": "đét lai", "email": "i meo", "web": "quép", "website": "quép sai", "chart": "chát", "kpi": "ca pê i",
    "power bi": "pao ơ bi ai", "jira": "gi ra", "redis": "rê đít", "postgres": "pốt gờ rét", "api": "a pê i",
    "ok": "ô kê", "okay": "ô kê", "team": "tim", "online": "on lai", "offline": "ọp lai", "landing page": "len đinh pết",
    "mobile": "mô bai", "desktop": "đét tóp", "banner": "ben nơ", "backend": "bách en", "frontend": "phờ ron en",
    "devops": "đép óp", "data": "đây ta", "cache": "két", "database": "đây ta bây", "sale": "xêu", "podcast": "pót cát",
    "app": "áp", "bug": "bấc", "test": "tét",
}
_EN_RE = re.compile(r"(?<!\w)(" + "|".join(sorted((re.escape(k) for k in _EN_WORDS), key=len, reverse=True)) + r")(?!\w)",
                    re.I)
# Viết tắt chỉ khớp khi VIẾT HOA (tránh nhầm "ai" trong "ai phụ trách")
_ACRONYMS = {"AI": "ây ai", "IT": "ai ti", "UI": "diu ai", "UX": "diu ích", "KPI": "ca pê i", "API": "a pê i",
             "CEO": "xi i ô", "CTO": "xi ti ô", "QA": "kiu ây", "PR": "pi a", "HR": "ết a"}
_ACR_RE = re.compile(r"(?<!\w)(" + "|".join(_ACRONYMS) + r")(?!\w)")
_UNITS = {"ms": "mi li giây", "s": "giây", "km": "ki lô mét", "kg": "ki lô gam", "gb": "gi ga bai", "mb": "mê ga bai",
          "tb": "tê ra bai", "h": "giờ", "x": "lần"}


def normalize_vi(text: str) -> str:
    """Chuẩn hóa để đọc tự nhiên: số, tiền, ngày, giờ, %, đơn vị, mã ticket, ký hiệu markdown."""
    t = str(text or "")
    t = re.sub(r"[*_`#>|~]+", " ", t)
    t = t.replace(chr(0x2014), ", ").replace(chr(0x2013), " ").replace("&", " và ").replace("…", ". ")
    t = re.sub(r"\bTP\.?\s?HCM\b", "thành phố Hồ Chí Minh", t)
    t = re.sub(r"\bv\.v\.?", "vân vân", t)
    # mã ticket / mã sản phẩm: URBOX-102 -> URBOX 102
    t = re.sub(r"([A-Za-z])-(\d)", r"\1 \2", t)
    # ngày tháng năm, ngày tháng, giờ phút
    t = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
               lambda m: f"ngày {num_to_vi(int(m.group(1)))} tháng {num_to_vi(int(m.group(2)))} năm {num_to_vi(int(m.group(3)))}", t)
    t = re.sub(r"\b(\d{1,2})/(\d{1,2})\b(?!/)",
               lambda m: f"ngày {num_to_vi(int(m.group(1)))} tháng {num_to_vi(int(m.group(2)))}", t)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b",
               lambda m: f"{num_to_vi(int(m.group(1)))} giờ" + (f" {num_to_vi(int(m.group(2)))}" if int(m.group(2)) else ""), t)
    # tiền: 1.000.000đ, 500 triệu đồng, 2,5 tỷ VND
    t = re.sub(r"(\d[\d.,]*)\s*(₫|đ|vnđ|vnd)(?!\w)", r"\1 đồng", t, flags=re.I)
    # phần trăm
    t = re.sub(r"(\d+(?:,\d+)?)\s*%", r"\1 phần trăm", t)
    # đơn vị ngay sau số
    t = re.sub(r"(\d)\s*(ms|km|kg|gb|mb|tb|s|h|x)(?!\w)", lambda m: f"{m.group(1)} {_UNITS[m.group(2).lower()]}", t, flags=re.I)
    # số có dấu chấm ngăn cách hàng nghìn, số thập phân dấu phẩy, kiểu "10.10" (tên chương trình), số nguyên
    t = re.sub(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+(?![\d.,]\d)", lambda m: _int_text(m.group(0)), t)
    t = re.sub(r"(?<![\d.,])(\d+),(\d+)(?![\d.,]\d)", lambda m: _decimal_vi(m.group(1), m.group(2)), t)
    t = re.sub(r"(?<![\d.,])(\d{1,2})\.(\d{1,2})(?![\d.,]\d)",
               lambda m: f"{num_to_vi(int(m.group(1)))} {num_to_vi(int(m.group(2)))}", t)
    t = re.sub(r"\d+", lambda m: _int_text(m.group(0)), t)
    t = _ACR_RE.sub(lambda m: _ACRONYMS[m.group(1)], t)
    t = _EN_RE.sub(lambda m: _EN_WORDS[m.group(1).lower()], t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------------ model ---
def _find_model() -> Optional[Dict[str, str]]:
    if not MODEL_DIR.is_dir():
        return None
    onnx = sorted(p for p in MODEL_DIR.glob("*.onnx"))
    tokens = MODEL_DIR / "tokens.txt"
    data = MODEL_DIR / "espeak-ng-data"
    if not onnx or not tokens.exists():
        return None
    return {"model": str(onnx[0]), "tokens": str(tokens), "data_dir": str(data) if data.is_dir() else ""}


def model_present() -> bool:
    return _find_model() is not None


def _load():
    global _tts, _load_error
    if _tts is not None or _load_error:
        return _tts
    files = _find_model()
    if files is None:
        _load_error = f"Chưa có model giọng đọc ở {MODEL_DIR} (chạy: python scripts/download_tts.py)"
        return None
    try:
        import sherpa_onnx
        t0 = time.time()
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=files["model"], lexicon="", tokens=files["tokens"],
                                                           data_dir=files["data_dir"]),
                provider="cpu", num_threads=max(1, min(4, (os.cpu_count() or 2) // 2)), debug=False),
            max_num_sentences=2)
        _tts = sherpa_onnx.OfflineTts(cfg)
        log.info("meeting.tts: giọng đọc %s sẵn sàng sau %.2fs", Path(files["model"]).stem, time.time() - t0)
    except Exception as e:   # thiếu thư viện, model hỏng...
        _load_error = f"Không nạp được giọng đọc: {e}"
        log.warning("meeting.tts: %s", _load_error)
    return _tts


def preload() -> bool:
    with _lock:
        return _load() is not None


def status() -> Dict[str, Any]:
    ok = _tts is not None or (model_present() and not _load_error)
    return {"available": bool(ok), "engine": "piper-local", "voice": MODEL_DIR.name,
            "sample_rate": int(_tts.sample_rate) if _tts is not None else None, "reason": "" if ok else _load_error
            or f"Chưa có model giọng đọc ở {MODEL_DIR} (chạy: python scripts/download_tts.py)"}


def _wav(samples: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def synthesize(text: str, speed: Optional[float] = None) -> bytes:
    """Văn bản -> WAV PCM16 mono. Có bộ nhớ đệm cho các câu lặp lại ("Dạ, để em xem.")."""
    clean = normalize_vi(text)[:MAX_CHARS]
    if not clean:
        raise ValueError("Không có nội dung để đọc")
    sp = float(speed or SPEED)
    key = (clean, round(sp, 2))
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        tts = _load()
        if tts is None:
            raise RuntimeError(_load_error or "Giọng đọc chưa sẵn sàng")
        audio = tts.generate(clean, sid=0, speed=sp)
        data = _wav(np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate))
        _cache[key] = data
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return data
