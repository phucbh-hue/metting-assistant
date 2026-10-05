"""Giọng đọc của trợ lý. Hai nguồn:

1. Soniox TTS (tts-rt-v2, mặc định khi có SONIOX_API_KEY): giọng người Việt tự nhiên, đọc đúng từ tiếng Anh xen giữa
   câu ("dashboard", "pull request", "Google Cloud"), số tiền, ngày tháng, mã ticket mà không cần chuẩn hóa. Phát dần
   qua WebSocket: âm thanh đầu tiên sau ~0,6 giây khi kết nối đang mở. Khoảng 0,70 USD mỗi giờ giọng đọc.
   Đo 04/10/2026: cho Soniox STT nghe lại, câu Việt-Anh đọc bằng Soniox gần như đúng hết, Piper sai phần lớn từ
   tiếng Anh ("sprint review" -> "The Pain Review", "Google Cloud qua webhook" -> "VOOL Airline qua wire").
2. Piper qua sherpa-onnx, chạy NGAY TRÊN MÁY: miễn phí, không cần mạng, không gửi nội dung ra ngoài; tiếng Anh chỉ
   đọc được theo bảng phiên âm. Model vits-piper-vi_VN-vais1000-medium (22.050 Hz, ~63 MB) trong models/tts/
   (tải bằng `python scripts/download_tts.py`). Dùng khi chọn trong Cài đặt, khi không có khóa Soniox hoặc mất mạng.
"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import threading
import time
import wave
from collections import OrderedDict
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

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
    cloud = engine() == "soniox"
    return {"available": bool(ok or cloud), "engine": "soniox" if cloud else "piper-local",
            "voice": settings()["voice"] if cloud else MODEL_DIR.name, "piper": bool(ok), "soniox": soniox_ready(),
            "sample_rate": SONIOX_RATE if cloud else (int(_tts.sample_rate) if _tts is not None else None),
            "reason": "" if ok or cloud else _load_error
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


# ============================================================== Soniox TTS ---
SONIOX_URL = "wss://tts-rt.soniox.com/tts-websocket"
SONIOX_MODEL = os.getenv("SONIOX_TTS_MODEL", "tts-rt-v2")
SONIOX_RATE = 24000
SONIOX_MAX_CHARS = 1500
# Giọng người Việt của Soniox (mọi giọng đều nói được tiếng Việt, nhưng giọng người Việt tự nhiên nhất).
# Đo bằng Soniox STT nghe lại: Linh và Mai đọc đúng gần hết các từ tiếng Anh trong câu mẫu, Huong nhầm 1-2 từ.
SONIOX_VOICES = [
    {"id": "Linh", "gender": "female", "description": "Nữ, trẻ, nhẹ nhàng, thong thả"},
    {"id": "Mai", "gender": "female", "description": "Nữ miền Nam, nói chậm, êm"},
    {"id": "Huong", "gender": "female", "description": "Nữ miền Nam, tươi sáng, rõ ràng, dễ nghe lâu"},
]
DEFAULT_SONIOX_VOICE = "Linh"
SETTINGS_KEY = "tts"
ENGINES = ("auto", "soniox", "piper")
_pcm_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_settings_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def clean_text(text: str) -> str:
    """Chuẩn hóa nhẹ cho Soniox: bỏ ký hiệu markdown; số, ngày, tiền, mã ticket, tiếng Anh Soniox tự đọc đúng."""
    t = re.sub(r"[*_`#>|~]+", " ", str(text or ""))
    t = t.replace(chr(0x2014), ", ").replace("&", " và ").replace(chr(0x2026), ". ")
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()[:SONIOX_MAX_CHARS]


def settings() -> Dict[str, str]:
    """Nguồn giọng đọc đã chọn (Cài đặt > Giọng đọc), mặc định theo .env: TTS_ENGINE, TTS_SONIOX_VOICE.

    Nhớ 10 giây để mỗi câu đọc không phải hỏi DB (Atlas) ngay trong vòng lặp sự kiện."""
    now = time.monotonic()
    if _settings_cache["value"] is not None and now - _settings_cache["at"] < 10:
        return dict(_settings_cache["value"])
    cfg = {"engine": (os.getenv("TTS_ENGINE") or "auto").strip().lower(),
           "voice": (os.getenv("TTS_SONIOX_VOICE") or DEFAULT_SONIOX_VOICE).strip()}
    try:
        from meeting import db
        saved = json.loads(db.get_setting(SETTINGS_KEY, "") or "{}")
        cfg.update({k: str(v) for k, v in saved.items() if k in ("engine", "voice") and v})
    except Exception as e:
        log.debug("meeting.tts: đọc cài đặt giọng đọc lỗi: %s", e)
    if cfg["engine"] not in ENGINES:
        cfg["engine"] = "auto"
    _settings_cache.update({"at": now, "value": dict(cfg)})
    return cfg


def save_settings(engine_name: str, voice: str) -> Dict[str, str]:
    engine_name = (engine_name or "auto").strip().lower()
    if engine_name not in ENGINES:
        raise ValueError(f"Nguồn giọng đọc không hợp lệ: {engine_name}")
    voice = (voice or DEFAULT_SONIOX_VOICE).strip()
    if not re.fullmatch(r"[A-Za-z][\w-]{0,40}", voice):
        raise ValueError(f"Tên giọng không hợp lệ: {voice}")
    from meeting import db
    db.set_setting(SETTINGS_KEY, json.dumps({"engine": engine_name, "voice": voice}))
    _settings_cache["value"] = None
    return settings()


def soniox_ready() -> bool:
    return bool((os.getenv("SONIOX_API_KEY") or "").strip())


def engine() -> str:
    """Nguồn sẽ dùng: Soniox khi được chọn (hoặc tự động) và có khóa, ngược lại Piper trên máy."""
    return "soniox" if settings()["engine"] in ("auto", "soniox") and soniox_ready() else "piper"


class SonioxTTS:
    """Một kết nối WebSocket dùng chung cho mọi câu (tối đa 4 câu cùng lúc), tự đóng sau 20 giây không dùng.

    Mở kết nối mất ~0,85 giây; kết nối đang mở thì âm thanh đầu tiên về sau ~0,6 giây. Đo 05/10/2026: kết nối mới
    mà không đọc câu nào thì Soniox đóng sau ~10 giây; đã đọc ít nhất một câu thì rảnh 25 giây vẫn dùng tiếp được."""
    IDLE_CLOSE_S = 20.0
    FIRST_AUDIO_TIMEOUT_S = 8.0
    CHUNK_TIMEOUT_S = 12.0

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._n = 0

    def _bind(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:          # vòng lặp mới (khởi động lại server / test): bắt đầu lại từ đầu
            self._loop, self._ws, self._idle = loop, None, None
            self._queues: Dict[str, asyncio.Queue] = {}
            self._lock = asyncio.Lock()
            self._sem = asyncio.Semaphore(4)

    async def _connect(self):
        import websockets
        async with self._lock:
            if self._ws is None:
                self._ws = await websockets.connect(SONIOX_URL, open_timeout=10, max_size=None, ping_interval=None,
                                                    close_timeout=2)
                asyncio.create_task(self._read(self._ws))
            return self._ws

    async def _drop(self, ws):
        async with self._lock:
            if self._ws is ws:
                self._ws = None

    async def _read(self, ws):
        try:
            async for msg in ws:
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                q = self._queues.get(m.get("stream_id"))
                if q is not None:
                    q.put_nowait(m)
        except Exception as e:
            log.info("meeting.tts: kết nối Soniox TTS đóng: %s", e)
        finally:
            if self._ws is ws:
                self._ws = None
            for q in self._queues.values():
                q.put_nowait({"closed": True})

    def _arm_idle(self):
        if self._idle is not None:
            self._idle.cancel()
        self._idle = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self):
        try:
            await asyncio.sleep(self.IDLE_CLOSE_S)
        except asyncio.CancelledError:
            return
        if not self._queues and self._ws is not None:
            ws, self._ws = self._ws, None
            try:
                await ws.close()
            except Exception:
                pass

    async def warm(self):
        """Mở sẵn kết nối (trợ lý vừa được gọi). Soniox đóng nếu ~10 giây chưa có câu nào: câu sau tự mở lại."""
        self._bind()
        try:
            await self._connect()
        except Exception as e:
            log.debug("meeting.tts: mở sẵn kết nối Soniox lỗi: %s", e)

    async def stream(self, text: str, voice: str) -> AsyncIterator[bytes]:
        """PCM 16-bit mono 24 kHz, từng mảnh ngay khi Soniox sinh ra."""
        self._bind()
        async with self._sem:
            if self._idle is not None:
                self._idle.cancel()
                self._idle = None
            got = False
            for attempt in (1, 2):           # kết nối cũ đã bị Soniox đóng: mở lại một lần
                ws = await self._connect()
                self._n += 1
                sid = f"tts-{self._n}"
                q: asyncio.Queue = asyncio.Queue()
                self._queues[sid] = q
                try:
                    await ws.send(json.dumps({"api_key": os.getenv("SONIOX_API_KEY", "").strip(), "model": SONIOX_MODEL,
                                              "language": "vi", "voice": voice, "audio_format": "pcm_s16le",
                                              "sample_rate": SONIOX_RATE, "stream_id": sid}))
                    await ws.send(json.dumps({"text": text, "text_end": True, "stream_id": sid}))
                    while True:
                        m = await asyncio.wait_for(q.get(), self.CHUNK_TIMEOUT_S if got else self.FIRST_AUDIO_TIMEOUT_S)
                        if m.get("closed"):
                            raise ConnectionError("Soniox đóng kết nối")
                        if m.get("error_code"):
                            raise RuntimeError(f"Soniox TTS lỗi {m.get('error_code')}: {m.get('error_message')}")
                        if m.get("audio"):
                            got = True
                            yield base64.b64decode(m["audio"])
                        if m.get("terminated"):
                            return
                except Exception as e:
                    retry = (isinstance(e, (ConnectionError, OSError)) or type(e).__module__.startswith("websockets"))
                    if retry and not got and attempt == 1:
                        await self._drop(ws)
                        continue
                    raise
                finally:
                    self._queues.pop(sid, None)
                    if not self._queues:
                        self._arm_idle()


_soniox = SonioxTTS()


def warm():
    """Trợ lý vừa được gọi: mở sẵn kết nối Soniox để câu trả lời có tiếng sau ~0,6 giây thay vì ~1,5 giây."""
    if engine() != "soniox":
        return
    try:
        asyncio.get_running_loop().create_task(_soniox.warm())
    except RuntimeError:          # không có vòng lặp sự kiện (gọi từ luồng khác)
        pass


async def _piper_pcm(text: str, speed: Optional[float]) -> Tuple[int, bytes]:
    wav = await asyncio.to_thread(synthesize, text, speed)
    with wave.open(io.BytesIO(wav)) as w:
        return w.getframerate(), w.readframes(w.getnframes())


async def open_stream(text: str, speed: Optional[float] = None) -> Tuple[int, str, AsyncIterator[bytes]]:
    """(tần số mẫu, nguồn, các mảnh PCM 16-bit mono). Soniox lỗi trước khi có âm thanh thì đọc bằng Piper.

    Chờ mảnh âm thanh đầu tiên rồi mới trả về để biết chắc nguồn (và tần số mẫu) sẽ dùng."""
    if engine() == "soniox":
        clean = clean_text(text)
        if not clean:
            raise ValueError("Không có nội dung để đọc")
        voice = settings()["voice"]
        key = (clean, voice)
        if key in _pcm_cache:                          # câu lặp lại ("Dạ, để em xem."): không gọi lại
            _pcm_cache.move_to_end(key)
            data = _pcm_cache[key]

            async def cached():
                yield data
            return SONIOX_RATE, "soniox", cached()
        gen = _soniox.stream(clean, voice)
        try:
            first = await gen.__anext__()
        except Exception as e:              # StopAsyncIteration (không có âm thanh) hoặc lỗi mạng / khóa
            log.warning("meeting.tts: Soniox TTS không đọc được, chuyển sang Piper: %s", e or "không có âm thanh")
            await gen.aclose()
            first = None
        if first is not None:
            async def rest():
                buf = bytearray(first)
                yield first
                try:
                    async for chunk in gen:
                        buf += chunk
                        yield chunk
                except Exception as e:
                    log.warning("meeting.tts: Soniox TTS ngắt giữa câu: %s", e)
                    return
                finally:
                    await gen.aclose()
                if len(clean) <= 200:
                    _pcm_cache[key] = bytes(buf)
                    while len(_pcm_cache) > CACHE_SIZE:
                        _pcm_cache.popitem(last=False)
            return SONIOX_RATE, "soniox", rest()
    sr, pcm = await _piper_pcm(text, speed)

    async def one():
        yield pcm
    return sr, "piper-local", one()
