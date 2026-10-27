"""Meeting Audio Streaming, Soniox STT Realtime & Multi-Speaker Orchestration.

Xử lý luồng âm thanh PCM16 16kHz:
1. Kết nối Soniox Realtime API (stt-rt-v5) có Diarization và Custom Vocabulary.
   - Mỗi lần mở kết nối là một "epoch": nhãn người nói của Soniox chỉ có nghĩa trong epoch đó.
   - Mất kết nối -> tự nối lại và PHÁT LẠI phần audio chưa được chốt (không mất chữ).
   - Tắt mic -> gửi finalize để chốt chữ cuối, nghỉ quá SONIOX_IDLE_CLOSE_S giây thì đóng stream (Soniox tính phí theo
     thời lượng stream mở).
2. Cắt lát audio từng câu để trích xuất CAM++ 192-dim vector (voice.embed).
3. Đưa vào MeetingSpeakers (voice.py) để phân vai theo HỒ SƠ NGƯỜI NÓI ổn định, lưu vào meeting_speakers.
4. Phát hiện Wake-Word kích hoạt trợ lý AI (llm.think_and_act).
5. IdentityEngine (identity.py) đoán tên người nói chưa định danh qua ngữ cảnh hội thoại.
6. Phát tán sự kiện thời gian thực qua WebSockets cho Web UI.
"""
import asyncio
import difflib
import json
import logging
import os
import re
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

import numpy as np
import websockets

from meeting import artifacts, auth, db, follow, identity, llm, recording, tts, voice

log = logging.getLogger("meeting.live")

RATE = 16000
BPS = RATE * 2                  # bytes / giây (PCM16 mono)
SEGMENT_GAP_S = 0.8             # Ngắt câu khi im lặng quá ngần này giây
SHORT_SEG_S = 1.2               # Câu ngắn hơn ngần này (hoặc dưới 3 từ) chờ nối với phần nói tiếp theo
SHORT_JOIN_GAP_S = 2.5          # ... nếu cùng người nói tiếp trong vòng ngần này giây
SHORT_HOLD_S = 2.5              # Chờ tối đa (giây thực) trước khi chốt câu ngắn
WAKE_FOLLOWUP_S = 8.0           # Chỉ gọi tên trợ lý rồi ngừng: chờ câu yêu cầu trong ngần này giây
# Lời gọi chưa nói hết câu (Soniox cắt ở chỗ ngập ngừng: "Thanh ơi, em hãy" | "tổng kết cuộc họp..."):
# chờ người đó nói tiếp, ngừng nói (không còn chữ tạm) ngần này giây thì mới xử lý cả câu
ASK_S = 180.0                   # câu hỏi "tự trình bày hay theo kịch bản" còn hiệu lực trong 3 phút
ANSWER_WINDOW_S = 40.0          # sau khi hỏi: câu nói tiếp theo không cần gọi tên, nếu đúng là câu trả lời
CMD_SETTLE_S = 1.2
AUTO_CONFIRM_S = 10.0           # Slide vừa tự chuyển theo lời nói: "chuyển slide" trong ngần này giây = xác nhận, không nhảy thêm
CMD_MAX_WAIT_S = 15.0
_SENTENCE_END = re.compile(r"[.?!…]\W*$")
# Câu dừng ở những từ này chắc chắn chưa nói xong ("em hãy", "tạo một cái", "chuyển sang")
_UNFINISHED_TAIL = {"hãy", "giúp", "cho", "một", "cái", "các", "những", "về", "là", "của", "và", "với", "để", "thì",
                    "mà", "tạo", "làm", "vẽ", "lập", "viết", "dựng", "xem", "sang", "tới", "đến", "qua", "thêm", "này",
                    "thể", "muốn", "cần", "nên", "sẽ", "phải", "đang", "đi"}

MAX_SEGMENT_S = 20.0            # Câu dài hơn: cắt tại dấu câu để transcript cập nhật đều
AUDIO_KEEP_S = 180              # Giữ tối đa ngần này giây audio để cắt clip / phát lại
REPLAY_MAX_S = 15.0             # Tối đa số giây audio phát lại sau khi nối lại Soniox
IDLE_CLOSE_S = float(os.getenv("SONIOX_IDLE_CLOSE_S", "30"))
# Soniox giới hạn ~10 kết nối nhận dạng ĐỒNG THỜI cho cả tổ chức. MỖI người trong Google Meet là một kết nối riêng, nên
# một cuộc họp đông + có người bật mic dễ chạm trần -> luồng mở sau (thường là mic) không xin được chỗ, mất transcript.
# Luồng của người trong Meet nghỉ nói thì NHẢ chỗ nhanh hơn (đóng sớm) để dành chỗ cho người đang nói và cho mic.
MEET_IDLE_CLOSE_S = float(os.getenv("MEET_IDLE_CLOSE_S", "8"))
# Soniox giới hạn MỖI phiên realtime 300 phút và mỗi tổ chức ~10 kết nối đồng thời (phiên để không cũng tính). Bật mic
# liên tục quá 300 phút sẽ bị cắt (max_duration_reached); nhiều luồng (mic + từng người trong Meet + tab) chạy song song
# dễ chạm trần đồng thời (max_concurrent_connections_reached, mã 429). Ta tự đổi phiên trước giới hạn thời gian, và khi
# bị giới hạn thì nối lại chậm rãi chứ không coi là lỗi chí mạng.
MAX_SESSION_S = float(os.getenv("SONIOX_MAX_SESSION_S", str(290 * 60)))
# Cuộc họp đang được ghi (mic / extension thu tiếng tab / bot Meet) mà mở quá ngần này giây vẫn chưa ai nói câu nào thì
# tự kết thúc và lập recap: đỡ để phòng trống ngốn Soniox/Recall. Đặt 0 để tắt.
IDLE_END_S = float(os.getenv("MEETING_IDLE_END_S", str(30 * 60)))
IDLE_CHECK_S = float(os.getenv("MEETING_IDLE_CHECK_S", "60"))
# Chống TRÙNG LẶP khi một câu nói vào server bằng nhiều đường cùng lúc: tiếng người online lọt vào mic phòng; vừa bật bot
# Recall vừa bật extension thu tiếng tab; nhiều máy trong CÙNG phòng cùng vào Meet nên tiếng một người thành nhiều luồng.
# Thứ tự ưu tiên giữ lại: meet (biết tên) > tab > mic. Khi trùng (>= DEDUP_OVERLAP từ VÀ chồng thời gian) thì bỏ bản ưu
# tiên thấp hơn; hai luồng meet cùng hạng thì giữ bản tới trước. Luồng dễ dính tiếng vọng (mic, và tab khi có bot) nếu bản
# "xa" chưa tới thì được giữ ECHO_HOLD_S giây chờ so. Cơ chế rẻ hơn vẫn giữ: bỏ hẳn luồng Meet của chính người đang bật
# mic (meet_ignored), bỏ câu chép trùng của chính mình (mirror).
ECHO_HOLD_S = float(os.getenv("MEET_ECHO_HOLD_S", "2.0"))
DEDUP_OVERLAP = float(os.getenv("DEDUP_OVERLAP", "0.7"))       # tỉ lệ từ trùng để coi hai câu là một
DEDUP_WINDOW_S = float(os.getenv("DEDUP_WINDOW_S", "3.0"))     # lệch thời gian tối đa để so hai câu (giây)
# Hai bản thu CÙNG một tiếng nói trùng nhau về thời gian (chỉ lệch do độ trễ Soniox); còn hai người thay phiên nói câu
# giống nhau thì nối tiếp, ít chồng thời gian. Chỉ bỏ khi khoảng thời gian trùng >= ngần này so với câu ngắn hơn, nhờ vậy
# không bỏ nhầm câu THẬT của người nói sau ("ừ, chốt thứ sáu" ngay sau khi người khác vừa nói).
DEDUP_TIME_OVERLAP = float(os.getenv("DEDUP_TIME_OVERLAP", "0.4"))
# Mỗi luồng Meet là MỘT người (tắt tách người nói: Soniox đôi khi chia một giọng thành 2 nhãn). Máy phòng họp có nhiều
# người cùng vào Meet thì đặt MEET_DIARIZE=1 để tách người trong luồng đó như mic.
MEET_DIARIZE = os.getenv("MEET_DIARIZE", "0") == "1"
# Cuộc họp riêng (bot Meet dùng chung) lập biên bản sau khi bên nhận âm thanh chốt tên người nói, chờ tối đa ngần này giây
NAMES_WAIT_S = 120.0
MIN_ENROLL_S = voice.MIN_ANCHOR_S
AUTO_ENROLL_AI = os.getenv("AUTO_ENROLL_VOICES", "0").strip().lower() in ("1", "true", "yes", "on")

SESSIONS: Dict[int, "MeetingSession"] = {}
LIVE_MEETINGS = SESSIONS        # Tên cũ (tương thích)
_session_locks: Dict[int, asyncio.Lock] = {}


def _is_fragment(command: str) -> bool:
    """Chưa có yêu cầu thật: tối đa 4 từ và dừng ở từ chưa trọn ý ("em có thể", "em hãy giúp")."""
    words = re.findall(r"\w+", (command or "").lower())
    return not words or (len(words) <= 4 and words[-1] in _UNFINISHED_TAIL)


def _unfinished(command: str) -> bool:
    words = re.findall(r"\w+", (command or "").lower())
    return len(words) < 3 or words[-1] in _UNFINISHED_TAIL


# ==============================================================================
# SONIOX REALTIME STREAM
# ==============================================================================
def soniox_error_kind(code: int, msg: str) -> str:
    """Phân loại lỗi Soniox để biết cách xử lý.

    - "rollover": hết 300 phút/phiên (max_duration_reached) -> đổi phiên ngay, không báo lỗi.
    - "limit": chạm trần đồng thời / rate (mã 429, max_concurrent_connections_reached...) -> nối lại chậm rãi.
    - "fatal": sai cấu hình, khóa, hết tiền (400/401/402/403) -> nối lại cũng vô ích.
    - "transient": lỗi khác -> nối lại như thường."""
    m = (msg or "").lower()
    if "duration" in m:
        return "rollover"
    if code == 429 or any(k in m for k in ("concurrent", "limit", "quota", "rate", "too many")):
        return "limit"
    if code in (400, 401, 402, 403):
        return "fatal"
    return "transient"


class MeetingStream:
    """Luồng nhận âm thanh PCM và gửi sang Soniox Realtime STT."""
    RT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
    MODEL = os.getenv("SONIOX_MODEL", "stt-rt-v5")

    def __init__(self, name: str, session: "MeetingSession", diarize: bool = True):
        self.name = name
        self.session = session
        self.diarize = diarize
        self.ws = None
        self.epoch = -1
        self.conn_off = 0.0           # Vị trí audio (giây) nơi kết nối Soniox hiện tại bắt đầu
        self.final_pos = 0.0          # Vị trí audio (giây) Soniox đã chốt chữ xong
        self.audio = bytearray()
        self.audio_base = 0           # Số byte đã bị cắt khỏi đầu bộ đệm
        self.fed = 0                  # Tổng số byte audio đã nhận từ mic
        self.runs: List[Tuple[int, float]] = []   # (vị trí byte, giờ cuộc họp) mỗi lần mic bật lại
        self.cur: Optional[Dict[str, Any]] = None
        self.closed = False
        self.down = True
        self.reconnecting = False
        self.paused = True
        self.last_audio = 0.0
        self.state = "idle"
        self._idle_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._closing: Set[Any] = set()
        self._last_interim = None
        self._open_lock = asyncio.Lock()
        self._fatal = ""              # Lỗi không thể tự khắc phục (sai API key, hết hạn mức...)
        self._rec: Optional[recording.Run] = None   # lượt ghi âm đang mở (chỉ khi cuộc họp bật ghi âm)
        self._reconnects: List[float] = []
        self._hold_task: Optional[asyncio.Task] = None
        self._sess_started = 0.0      # monotonic lúc mở phiên Soniox hiện tại (tự đổi phiên trước giới hạn 300 phút)
        self._soft = ""               # lý do nối lại "nhẹ": "limit" (Soniox giới hạn) / "rollover" (đổi phiên định kỳ)

    # ------------------------------------------------------------ status ---
    def _set_state(self, state: str, message: str = ""):
        if self.state == state and not message:
            return
        self.state = state
        asyncio.ensure_future(self.session.emit({"type": "stream_status", "stream": self.name,
                                                 "state": state, "message": message}))

    @property
    def is_open(self) -> bool:
        return self.ws is not None and not self.down

    # -------------------------------------------------------- connection ---
    async def open(self, replay_from: Optional[int] = None):
        async with self._open_lock:
            await self._open_locked(replay_from)

    async def _open_locked(self, replay_from: Optional[int] = None):
        api_key = os.getenv("SONIOX_API_KEY", "")
        if not api_key:
            raise RuntimeError("Chưa cấu hình SONIOX_API_KEY trong .env")
        cfg: Dict[str, Any] = {
            "api_key": api_key,
            "model": self.MODEL,
            "audio_format": "pcm_s16le",
            "sample_rate": RATE,
            "num_channels": 1,
            "enable_speaker_diarization": self.diarize,
            "enable_language_identification": True,
            "enable_endpoint_detection": True,
            "language_hints": [x.strip() for x in os.getenv("SONIOX_LANGUAGE_HINTS", "vi,en").split(",") if x.strip()],
        }
        terms = self.session.context_terms()
        if terms:
            cfg["context"] = {"terms": terms[:100]}

        self._set_state("connecting")
        # Tắt ping của thư viện websockets: Soniox có cơ chế keepalive riêng, ping/pong dễ timeout khi server đang
        # xử lý hàng đợi audio -> gây ngắt kết nối giả.
        ws = await websockets.connect(self.RT_URL, open_timeout=20, max_size=None, ping_interval=None, close_timeout=5)
        await ws.send(json.dumps(cfg))
        self.ws = ws
        self.down = True
        self.epoch = self.session.next_epoch()
        start = self.fed if replay_from is None else max(int(replay_from), self.audio_base,
                                                        self.fed - int(REPLAY_MAX_S * BPS))
        start -= start % 2
        self.conn_off = start / BPS
        self.final_pos = self.conn_off
        self._recv_task = asyncio.create_task(self._recv(ws, self.epoch, self.conn_off))
        asyncio.create_task(self._keepalive(ws))
        # Phát lại phần audio chưa được chốt (mất kết nối) rồi mới nhận audio trực tiếp
        sent = start
        while sent < self.fed:
            end = min(self.fed, sent + BPS)
            await ws.send(bytes(self.audio[sent - self.audio_base:end - self.audio_base]))
            sent = end
        self.down = False
        self.last_audio = time.monotonic()
        self._sess_started = time.monotonic()
        self._set_state("live" if not self.paused else "paused")
        log.info("meeting.live: mở Soniox session %d stream %s epoch %d (phát lại %.1fs)",
                 self.session.id, self.name, self.epoch, (self.fed - start) / BPS)

    async def ensure_open(self):
        if self.closed:
            raise RuntimeError("Stream đã đóng")
        self._fatal = ""   # người dùng chủ động bật lại mic -> thử lại
        for _ in range(100):
            if not self.reconnecting:
                break
            await asyncio.sleep(0.1)
        async with self._open_lock:
            if self.ws is None or (self.down and not self.reconnecting):
                await self._open_locked(replay_from=int(self.final_pos * BPS) if self.fed else None)

    async def _keepalive(self, ws):
        """Duy trì kết nối với Soniox bằng keepalive khi không có audio (chống timeout)."""
        while not self.closed and self.ws is ws:
            await asyncio.sleep(3.0)
            if self.ws is not ws or self.closed:
                return
            if time.monotonic() - self.last_audio > 3.0:
                try:
                    await ws.send(json.dumps({"type": "keepalive"}))
                except Exception:
                    return

    # Lịch chờ giữa các lần nối lại (giây). limit: chờ lâu hơn và thử nhiều lần để trần đồng thời kịp hạ; rollover: nhanh.
    _BACKOFF = {"": [0.5, 1, 2, 4, 8, 8, 15], "rollover": [0.5, 1, 2, 4, 8],
                "limit": [8, 12, 15, 20, 20, 30, 30, 30, 30, 30]}

    def _lost(self, reason: str):
        if self.closed or self.reconnecting or self._fatal:
            return
        soft, self._soft = self._soft, ""      # "limit"/"rollover": nối lại nhẹ, không tính vào ngưỡng ngắt liên tục
        now = time.monotonic()
        if not soft:
            self._reconnects = [t for t in self._reconnects if now - t < 300] + [now]
            if len(self._reconnects) > 8:
                self._fatal = f"Soniox ngắt kết nối liên tục ({reason})"
                self.down = True
                self._set_state("error", self._fatal + ". Hãy tắt rồi bật lại mic.")
                return
        self.down = True
        self.reconnecting = True
        msg = ({"limit": "Soniox đang giới hạn số kết nối, em tự nối lại...",
                "rollover": "Đang đổi phiên Soniox (giới hạn 300 phút mỗi phiên)..."}.get(soft, reason))
        log.warning("session %d stream %s: %s - nối lại Soniox (%s)...", self.session.id, self.name, reason, soft or "thường")
        self._set_state("reconnecting", msg)
        asyncio.create_task(self._reconnect(int(self.final_pos * BPS), soft))

    async def _reconnect(self, replay_from: int, soft: str = ""):
        old, self.ws = self.ws, None
        if old is not None:
            self._closing.add(old)
            try:
                await old.close()               # đóng hẳn phiên cũ trước khi mở phiên mới (trần đồng thời tính cả phiên để không)
            except Exception:
                pass
        try:
            for i, wait in enumerate(self._BACKOFF.get(soft, self._BACKOFF[""]), 1):
                await asyncio.sleep(wait)
                if self.closed or self._fatal:
                    return
                try:
                    async with self._open_lock:
                        await self._open_locked(replay_from=replay_from)
                    log.info("session %d stream %s: đã nối lại Soniox sau lần %d", self.session.id, self.name, i)
                    return
                except Exception as e:
                    log.warning("session %d: nối lại Soniox lần %d thất bại: %s", self.session.id, i, e)
            self._set_state("error", ("Soniox vẫn giới hạn số kết nối. Hãy bớt cuộc họp đang ghi hoặc thử lại sau."
                                      if soft == "limit" else "Không nối lại được Soniox. Hãy tắt rồi bật lại mic."))
        finally:
            self.reconnecting = False

    async def _close_ws(self):
        """Đóng kết nối Soniox có chốt chữ: gửi tín hiệu hết audio và chờ phản hồi 'finished'."""
        ws, self.ws = self.ws, None
        self.down = True
        if ws is None:
            return
        self._closing.add(ws)
        try:
            await ws.send("")
        except Exception:
            pass
        task = self._recv_task
        if task is not None and not task.done():
            # Soniox xử lý theo thời gian thực: chờ thêm đúng bằng phần audio còn tồn chưa chốt (tối đa 25s)
            backlog = max(0.0, self.fed / BPS - self.final_pos)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=min(25.0, 4.0 + backlog))
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass
        await self._flush()

    # -------------------------------------------------------------- audio ---
    async def feed(self, pcm: bytes):
        if self.closed or not pcm:
            return
        if self.paused or not self.runs:
            self.runs.append((self.fed, self.session.clock()))
            self.paused = False
            if self._idle_task:
                self._idle_task.cancel()
                self._idle_task = None
            if self.is_open:
                self._set_state("live")
        self.last_audio = time.monotonic()
        self.audio += pcm
        self.fed += len(pcm)
        self._record(pcm)
        keep = int(AUDIO_KEEP_S * BPS)
        if len(self.audio) > keep + 10 * BPS:
            drop = len(self.audio) - keep
            del self.audio[:drop]
            self.audio_base += drop
        if (self.is_open and not self.reconnecting and self._sess_started
                and time.monotonic() - self._sess_started >= MAX_SESSION_S):
            self._soft = "rollover"             # tới gần 300 phút: tự đổi phiên trước khi Soniox cắt giữa chừng
            self._lost("phiên Soniox tới giới hạn thời gian")
            return
        if self.ws is None and not self.reconnecting and not self._open_lock.locked():
            self._lost("chưa có kết nối Soniox")
            return
        if self.down or self.ws is None:
            return
        try:
            await self.ws.send(pcm)
        except Exception as e:
            self._lost(f"gửi audio lỗi ({type(e).__name__})")

    def _record(self, pcm: bytes):
        """Ghi âm (chỉ khi cuộc họp bật ghi âm và đã xác nhận mọi người đồng ý): mỗi lượt bật mic một tệp."""
        if not self.session.recording_on():
            self.stop_recording()
            return
        if self._rec is None:
            try:
                self._rec = recording.Run(self.session.id, self.name, self.session.clock() - len(pcm) / BPS)
            except OSError as e:
                log.warning("meeting.live: không ghi âm được: %s", e)
                return
        self._rec.write(pcm)

    def stop_recording(self):
        if self._rec is not None:
            self._rec.close()
            self._rec = None

    async def pause(self):
        """Mic tắt: chốt chữ còn treo, giữ kết nối thêm IDLE_CLOSE_S giây rồi đóng để không tốn phí."""
        if self.closed or self.paused:
            return
        self.stop_recording()
        self.paused = True
        if self.is_open:
            try:
                await self.ws.send(json.dumps({"type": "finalize"}))
            except Exception:
                pass
        self._set_state("paused")

        # Người trong Meet nghỉ nói thì nhả chỗ Soniox nhanh hơn mic (trần ~10 kết nối đồng thời cho cả tổ chức).
        idle_s = MEET_IDLE_CLOSE_S if self.name.startswith("meet") else IDLE_CLOSE_S

        async def _idle():
            try:
                await asyncio.sleep(idle_s)
                if self.paused and not self.closed:
                    log.info("meeting.live: luồng %s nghỉ %.0fs -> đóng Soniox (nhả chỗ) session %d",
                             self.name, idle_s, self.session.id)
                    await self._close_ws()
                    self._set_state("idle")
            except asyncio.CancelledError:
                pass
        self._idle_task = asyncio.create_task(_idle())

    def clip(self, start_s: float, end_s: float) -> bytes:
        a = int(start_s * RATE) * 2 - self.audio_base
        b = int(end_s * RATE) * 2 - self.audio_base
        a = max(a, 0)
        b = min(b, len(self.audio))
        if b - a < int(0.3 * BPS):
            return b""
        return bytes(self.audio[a:b])

    def meeting_t(self, pos_s: float) -> float:
        pos_b = pos_s * BPS
        for rp, rt in reversed(self.runs):
            if rp <= pos_b + 2:
                return rt + (pos_b - rp) / BPS
        return self.runs[0][1] if self.runs else pos_s

    # ------------------------------------------------------------ receive ---
    async def _recv(self, ws, epoch: int, conn_off: float):
        lost = None
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                m = json.loads(raw)
                if m.get("error_code"):
                    code = int(m.get("error_code") or 0)
                    lost = f"Soniox {code}: {m.get('error_message', '')}"
                    kind = soniox_error_kind(code, m.get("error_message", ""))
                    if kind == "fatal":
                        self._fatal = lost   # lỗi cấu hình/tài khoản: nối lại cũng vô ích
                        self._set_state("error", lost)
                        await self.session.emit({"type": "error", "text": lost})
                    else:                    # giới hạn / đổi phiên / lỗi tạm: nối lại chứ không báo lỗi gắt cho người dùng
                        self._soft = kind if kind in ("limit", "rollover") else ""
                        log.warning("session %d stream %s: %s (%s)", self.session.id, self.name, lost, kind)
                    break
                interim, interim_raw = "", None
                for tok in m.get("tokens", []):
                    txt = tok.get("text", "")
                    if txt == "<end>" and self._is_short(self.cur):
                        self._hold_flush()      # câu quá ngắn: chờ xem người đó có nói tiếp không
                        continue
                    if txt in ("<end>", "<fin>"):
                        await self._flush()
                        continue
                    if tok.get("is_final"):
                        await self._add_final(tok, epoch, conn_off)
                    else:
                        interim += txt
                        if tok.get("speaker") not in (None, ""):
                            interim_raw = str(tok["speaker"])
                if m.get("final_audio_proc_ms") is not None and ws is self.ws:
                    self.final_pos = max(self.final_pos, conn_off + m["final_audio_proc_ms"] / 1000)
                await self._emit_interim(interim, interim_raw, epoch)
                if m.get("finished"):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            lost = f"Soniox recv {type(e).__name__}: {e}"
        finally:
            await self._flush()
            await self._emit_interim("", None, epoch)
            if ws is self.ws and ws not in self._closing and not self.closed:
                self._lost(lost or "Soniox đóng kết nối")

    async def _emit_interim(self, interim: str, raw: Optional[str], epoch: int):
        c = self.cur
        text = ((c["text"] if c else "") + interim).strip()
        raw = raw or (c["raw"] if c else None)
        key = (text, raw)
        if key == self._last_interim:
            return
        self._last_interim = key
        prof = self.session.speakers.peek(raw, epoch, self.name) if raw is not None else None
        if text:
            self.session.note_interim(self.name)
        await self.session.emit({
            "type": "interim", "stream": self.name, "text": text,
            "speaker_key": prof.sid if prof else None,
            "speaker_label": prof.label if prof else None,
        })

    async def _add_final(self, tok: Dict[str, Any], epoch: int, conn_off: float):
        text = tok.get("text", "")
        spk = tok.get("speaker")
        raw = str(spk) if spk not in (None, "") else None
        start = conn_off + tok.get("start_ms", 0) / 1000
        end = conn_off + tok.get("end_ms", 0) / 1000

        c = self.cur
        gap_limit = SHORT_JOIN_GAP_S if self._is_short(c) else SEGMENT_GAP_S
        if c and (c["epoch"] != epoch or c["raw"] != raw or start - c["end"] > gap_limit):
            await self._flush()
            c = None
        elif c is not None:
            self._cancel_hold()
        if c is None:
            self.cur = c = {"epoch": epoch, "raw": raw, "start": start, "end": end, "text": ""}
        c["text"] += text
        c["end"] = max(c["end"], end)
        if c["end"] - c["start"] >= MAX_SEGMENT_S and text.strip()[-1:] in (".", "?", "!", ",", ";"):
            await self._flush()

    @staticmethod
    def _is_short(c: Optional[Dict[str, Any]]) -> bool:
        return c is not None and ((c["end"] - c["start"]) < SHORT_SEG_S or len(re.findall(r"\w+", c["text"])) < 3)

    def _cancel_hold(self):
        t, self._hold_task = self._hold_task, None
        if t is not None and t is not asyncio.current_task() and not t.done():
            t.cancel()

    def _hold_flush(self):
        c = self.cur
        self._cancel_hold()
        if c is None:
            return

        async def _later():
            try:
                await asyncio.sleep(SHORT_HOLD_S)
                if self.cur is c:
                    await self._flush()
            except asyncio.CancelledError:
                pass
        self._hold_task = asyncio.create_task(_later())

    async def _flush(self):
        self._cancel_hold()
        c, self.cur = self.cur, None
        if not c or not re.search(r"\w", c["text"]):
            return   # bỏ câu rỗng hoặc chỉ có dấu câu
        t0 = self.meeting_t(c["start"])
        await self.session.on_segment_finalized(
            t_start=round(t0, 2),
            t_end=round(t0 + max(0.0, c["end"] - c["start"]), 2),
            raw_speaker=c["raw"],
            text=c["text"].strip(),
            clip_pcm=self.clip(c["start"], c["end"]),
            epoch=c["epoch"],
            stream=self.name,
        )

    async def close(self):
        if self.closed:
            return
        if self._idle_task:
            self._idle_task.cancel()
        self.stop_recording()
        await self._close_ws()
        self.closed = True
        self._set_state("closed")


class VoiceGate:
    """Cổng tiếng nói cho luồng của một người trong Google Meet: im lặng quá IDLE_S giây thì tạm dừng luồng (đóng Soniox
    sau IDLE_CLOSE_S, đỡ tốn phí cho người tắt mic / ngồi nghe); có tiếng lại thì mở kèm PREROLL_S giây âm thanh ngay
    trước đó để không mất chữ đầu câu."""
    PREROLL_S = 0.6
    IDLE_S = float(os.getenv("MEET_IDLE_S", "15"))
    RMS_ON = float(os.getenv("MEET_VOICE_RMS", "300"))       # khoảng -40 dBFS

    def __init__(self):
        self.active = False
        self.last_voice = 0.0
        self.pre: Deque[bytes] = deque()
        self.pre_bytes = 0

    @staticmethod
    def rms(pcm: bytes) -> float:
        n = len(pcm) - len(pcm) % 2
        if n <= 0:
            return 0.0
        a = np.frombuffer(pcm[:n], dtype="<i2").astype(np.float32)
        return float(np.sqrt(np.mean(a * a)))

    def push(self, pcm: bytes, now: float) -> Tuple[List[bytes], bool]:
        """(các đoạn âm thanh cần đưa vào luồng, có tạm dừng luồng không)."""
        voiced = bool(pcm) and self.rms(pcm) >= self.RMS_ON
        if voiced:
            self.last_voice = now
        if self.active:
            if not voiced and now - self.last_voice > self.IDLE_S:
                self.active = False
                self._keep(pcm)
                return [], True
            return ([pcm] if pcm else []), False
        if not voiced:
            self._keep(pcm)
            return [], False
        self.active = True
        out = [c for c in self.pre if c] + [pcm]
        self.pre.clear()
        self.pre_bytes = 0
        return out, False

    def _keep(self, pcm: bytes):
        if not pcm:
            return
        self.pre.append(pcm)
        self.pre_bytes += len(pcm)
        while len(self.pre) > 1 and self.pre_bytes - len(self.pre[0]) >= self.PREROLL_S * BPS:
            self.pre_bytes -= len(self.pre.popleft())


def norm_name(name: str) -> str:
    """So tên không phân biệt dấu, hoa thường, khoảng trắng ("Bùi Hồng Phúc" = "bui hong phuc")."""
    t = unicodedata.normalize("NFKD", (name or "").replace("đ", "d").replace("Đ", "D"))
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).casefold().split())


def same_person(person: Dict[str, Any], user: Optional[Dict[str, Any]]) -> bool:
    """Người trong Meet có phải người đang bật mic trong ứng dụng không (máy trong phòng thường vào Meet bằng chính tài
    khoản đó): trùng email, hoặc trùng tên."""
    if not user:
        return False
    if person.get("email") and user.get("email") and person["email"].lower() == str(user["email"]).lower():
        return True
    return bool(person.get("name")) and norm_name(person["name"]) == norm_name(user.get("name") or "")


# ==============================================================================
# MEETING SESSION (QUẢN LÝ CUỘC HỌP TOÀN DIỆN)
# ==============================================================================
def _public_segment(s: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("seq", "meeting_id", "t_start", "t_end", "speaker_key", "speaker_label", "speaker_id",
            "text", "is_inferred", "raw_speaker", "confidence")
    return {k: s.get(k) for k in keys}


class MeetingSession:
    def __init__(self, meeting: Dict[str, Any], anchors: Optional[Dict[int, Dict[str, Any]]] = None,
                 profiles: Optional[List[Dict[str, Any]]] = None,
                 segments: Optional[List[Dict[str, Any]]] = None):
        self.id = int(meeting["id"])
        self.meeting = meeting
        self.title = meeting.get("title", "")
        self.vocab = meeting.get("vocab") or []
        self.host_id = meeting.get("host_id")
        self.started_at = float(meeting.get("started_at") or time.time())
        self.streams: Dict[str, MeetingStream] = {}
        self.subscribers: Set[asyncio.Queue] = set()
        self.audio_owner: Optional[object] = None    # thiết bị đang bật mic (một nguồn mic mỗi cuộc họp)
        self.tab_owner: Optional[object] = None       # extension đang thu tiếng tab (luồng "tab", chạy song song với mic)
        self.disposed = False

        segments = segments or []
        self.speakers = voice.MeetingSpeakers(anchors=anchors or {}, expected_host_id=self.host_id, model=voice.MODEL_ID)
        # Vector tạo bằng model khác (cuộc họp ghi trước khi đổi model) không so được với vector mới: bỏ khi nạp lại
        self.speakers.load_state(profiles or [], [s if voice.model_of(s) == voice.MODEL_ID or not s.get("raw_embedding")
                                                  else {**s, "raw_embedding": None} for s in segments])
        self.segments: List[Dict[str, Any]] = []
        self._by_seq: Dict[Any, Dict[str, Any]] = {}
        # Nhiều người dùng Copilot cùng một cuộc họp Google Meet (bản 3.21): một bot, cuộc họp nhận âm thanh gửi bản sao
        # từng câu sang cuộc họp riêng của từng người (phòng độc lập: sản phẩm AI, biên bản, Drive của riêng người đó).
        self._targets: Optional[List[Dict[str, Any]]] = None     # bản riêng đang nhận lời thoại (None: chưa đọc)
        self._src_map: Dict[int, int] = {}                        # bản riêng: seq bên nhận âm thanh -> seq của mình
        self._fwd_names: Dict[int, Tuple[str, str, str, str]] = {}   # tên đã gửi sang bản riêng (chỉ gửi khi đổi)
        self._mirror_at = 0.0
        self.names_final = asyncio.Event()                        # đã chốt tên người nói lúc kết thúc (bản riêng chờ)
        backfill = []
        for s in segments:
            if s.get("src_seq") is not None and s.get("seq") is not None:
                self._src_map[int(s["src_seq"])] = int(s["seq"])
            seg = _public_segment(s)
            sid = self.speakers.sid_of(seg["seq"])
            prof = self.speakers.profile(sid)
            if prof is not None and (seg.get("speaker_key") != prof.sid or seg.get("speaker_label") != prof.label):
                seg.update({"speaker_key": prof.sid, "speaker_label": prof.label, "speaker_id": prof.voice_id})
                backfill.append({"seq": seg["seq"], "speaker_key": prof.sid,
                                 "speaker_label": prof.label, "speaker_id": prof.voice_id})
            self.segments.append(seg)
            self._by_seq[seg["seq"]] = seg
        self.next_seq = max([int(s["seq"]) for s in self.segments if s.get("seq") is not None] + [0]) + 1
        self._epoch = max([int(s.get("epoch") or 0) for s in segments] + [-1]) + 1
        self._pending_enroll: Dict[int, Dict[str, Any]] = {}
        self._pending_wake: Optional[Dict[str, Any]] = None
        self._pending_cmd: Optional[Dict[str, Any]] = None   # lời gọi trợ lý đang chờ nói hết câu
        self._ai_tasks: set = set()                          # yêu cầu đang xử lý (để "đừng soạn nữa" dừng được)
        self._ask: Optional[Dict[str, Any]] = None            # câu hỏi đang chờ người dùng trả lời (cách trình bày tài liệu)
        self._speech_at = 0.0                                  # lần cuối chữ tạm (interim) thay đổi
        self._lock = asyncio.Lock()
        # Màn hình trình bày: nội dung đang chiếu, slide hiện tại, lịch sử để "quay lại phần trước"
        self.stage: Dict[str, Any] = {"artifact_id": None, "slide": 0, "history": [], "shown_at_seq": 0,
                                      "follow": True, "changed_at": 0.0, "manual_at": 0.0, "auto_at": 0.0, "log": []}
        # Lịch sử trình chiếu lưu trong DB (meetings.stage_log): chạy lại server vẫn "quay lại cái slide lúc nãy" được
        log_ = [e for e in (meeting.get("stage_log") or []) if isinstance(e, dict) and isinstance(e.get("artifact_id"), int)]
        if log_:
            self.stage["log"] = [{"artifact_id": e["artifact_id"], "slide": int(e.get("slide") or 0),
                                  "at": float(e.get("at") or 0)} for e in log_[-50:]]
            self.stage["history"] = [{"artifact_id": e["artifact_id"], "slide": e["slide"]} for e in self.stage["log"][:-1]][-20:]
            self.stage["artifact_id"] = self.stage["log"][-1]["artifact_id"]
            self.stage["slide"] = self.stage["log"][-1]["slide"]
        self._follower = follow.SlideFollower()     # tự chuyển slide theo lời trình bày
        self._matchers: Dict[int, Any] = {}          # artifact_id -> (slides, SlideMatcher)
        self._follow_texts: Deque[str] = deque(maxlen=2)
        self._art_cache: Dict[int, Dict[str, Any]] = {}
        self._explain_cache: Dict[Tuple[int, str], str] = {}     # (sơ đồ, ý) -> lời giải thích: bấm lại không gọi AI
        self._present_scope: Optional[Dict[str, Any]] = None     # "thuyết trình slide này": nhớ qua câu hỏi cách trình bày
        # Bản web: tài liệu nằm trên máy người dùng. Trình duyệt gửi danh sách TÊN tệp trong thư mục đã chọn; nội dung
        # một tệp chỉ được gửi lên khi cần mở / đọc kịch bản (need_file -> trình duyệt gửi tệp -> resolve_client_file).
        self.client_files: List[Dict[str, Any]] = []
        self._file_waits: Dict[str, "asyncio.Future"] = {}
        # Cuộc họp có bot Google Meet (bản 3.18): mỗi người trong Meet một luồng "meet<id>". mic_user: người đang bật mic
        # trong ứng dụng (họp nửa online nửa trực tiếp: luồng Meet của chính máy đó bị bỏ qua để không chép trùng).
        self.meet_people: Dict[str, Dict[str, Any]] = {}
        self.meet_bot: Optional[Dict[str, Any]] = None
        self.mic_user: Optional[Dict[str, Any]] = None
        self._meet_modes: Dict[str, str] = dict(((meeting.get("meet") or {}).get("modes") or {}))
        self._gates: Dict[str, VoiceGate] = {}
        self._stream_raws: Dict[str, Dict[int, Set[str]]] = {}
        # Câu gần đây của các luồng "xa" (meet*/tab) để chống trùng: (t_start, t_end, các từ, tên luồng).
        self._remote_texts: Deque[Tuple[float, float, List[str], str]] = deque(maxlen=80)
        self._remote_voiced_at = 0.0          # lần cuối một luồng xa đang NÓI DỞ (interim): chỉ giữ mic để so trùng khi remote thực sự đang nói
        self.bd = None                                         # trợ lý BD (nhóm bật chế độ BD, bản 3.19)
        self.game = None                                       # trò chơi có thưởng đang chạy (meeting/game.py)
        self._held: Set[asyncio.Task] = set()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._db_queue: asyncio.Queue = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self.identity = identity.IdentityEngine(self)
        self._backfill = backfill
        self._restored_profiles = bool(profiles)
        SESSIONS[self.id] = self

    # ------------------------------------------------------------ loading ---
    @classmethod
    def from_db(cls, meeting: Dict[str, Any]) -> "MeetingSession":
        """Nạp toàn bộ trạng thái cuộc họp từ DB (chạy trong thread vì truy vấn mạng)."""
        mid = int(meeting["id"])
        anchors = {}
        for v in db.list_voices(with_embedding=True):
            emb = v.get("embedding")
            if emb and voice.is_valid_vector(emb):
                anchors[v["id"]] = {"name": v["name"], "vector": np.asarray(emb, dtype=np.float32), "model": voice.model_of(v),
                                    "role": v.get("role", ""), "department": v.get("department", "")}
        profiles = db.list_speakers(mid, with_vector=False)
        db.backfill_segment_seq(mid)
        segments = db.get_segments(mid, with_embedding=True)
        return cls(meeting, anchors=anchors, profiles=profiles, segments=segments)

    def _ensure_workers(self):
        if self._workers or self.disposed:
            return
        self._workers = [asyncio.create_task(self._proc_loop()), asyncio.create_task(self._db_loop()),
                         asyncio.create_task(self._idle_watch())]
        if self._backfill:
            self._db(db.update_segments_speaker_by_seqs, self.id, self._backfill)
            self._backfill = []
        if not self._restored_profiles and self.speakers.profiles:
            self._db(db.upsert_speakers, self.id, self.speakers.export_profiles())
            self._restored_profiles = True

    def clock(self) -> float:
        return time.time() - self.started_at

    def recording_on(self) -> bool:
        rec = self.meeting.get("recording") or {}
        return bool(rec.get("enabled") and rec.get("consent_at")) and self.is_live()

    def recording_public(self) -> Dict[str, Any]:
        rec = self.meeting.get("recording") or {}
        return {**recording.info(self.id), "enabled": self.recording_on(), "consent_by": rec.get("consent_by", ""),
                "consent_at": rec.get("consent_at")}

    async def set_recording(self, enabled: bool, consent_by: str = "meeting_host") -> Dict[str, Any]:
        """Bật / tắt ghi âm. Bật = chủ phòng xác nhận mọi người trong phòng đã đồng ý (lưu ai, lúc nào)."""
        rec = dict(self.meeting.get("recording") or {})
        if enabled:
            rec.update({"enabled": True, "consent_by": consent_by, "consent_at": time.time(),
                        "retention_days": recording.RETENTION_DAYS})
        else:
            rec["enabled"] = False
            for st in self.streams.values():
                st.stop_recording()
        self.meeting["recording"] = rec
        await asyncio.to_thread(db.update_meeting, self.id, {"recording": rec}, True)
        pub = self.recording_public()
        await self.emit({"type": "recording", "recording": pub})
        return pub

    def next_epoch(self) -> int:
        e = self._epoch
        self._epoch += 1
        return e

    def is_live(self) -> bool:
        return self.meeting.get("status") != "ended"

    def context_terms(self) -> List[str]:
        names = [p.name for p in self.speakers.active_profiles() if p.name]
        names += [a["name"] for a in self.speakers.anchors.values()]
        names += [n for n in (self.meeting.get("expected_attendees") or []) if isinstance(n, str)]
        seen, out = set(), []
        for t in llm.assistant_terms() + list(self.vocab) + names:   # tên trợ lý trước để chắc chắn được gửi
            k = str(t).strip()
            if k and k.casefold() not in seen:
                seen.add(k.casefold())
                out.append(k)
        return out

    # --------------------------------------------------------- pub / sub ---
    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(q)
        self._ensure_workers()
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subscribers.discard(q)

    async def emit(self, event: Dict[str, Any]):
        if event.get("type") in ("ai_listening", "ai_thinking"):
            tts.warm()                    # sắp có câu trả lời: mở sẵn kết nối giọng đọc
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("meeting.live: hàng đợi sự kiện client đầy, bỏ qua sự kiện %s", event.get("type"))
        if event.get("type") == "interim" and self._targets:
            await self._forward_interim(event)       # chữ đang nói cũng hiện ở bản riêng đang mở

    def public_speakers(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.speakers.visible_profiles():
            d = p.to_dict(with_vector=False)
            d["pending_enroll"] = p.sid in self._pending_enroll
            out.append(d)
        return out

    def snapshot(self) -> Dict[str, Any]:
        return {
            "type": "init",
            "meeting": self.meeting,
            "segments": self.segments,
            "speakers": self.public_speakers(),
            "suggestions": self.identity.pending_suggestions(),
            "streams": {n: s.state for n, s in self.streams.items()},
            "mic_active": self.audio_owner is not None,
            "stage": self.stage_public(),
            "recording": self.recording_public(),
            "meet": self.meet_public(),
            "game": self.game.public() if self.game is not None else None,
        }

    # ----------------------------------------------------------- workers ---
    def _db(self, fn: Callable, *args, **kwargs):
        """Ghi DB tuần tự theo thứ tự phát sinh (insert luôn trước update)."""
        self._ensure_workers()
        self._db_queue.put_nowait((fn, args, kwargs))

    async def _db_loop(self):
        while True:
            fn, args, kwargs = await self._db_queue.get()
            try:
                await asyncio.to_thread(fn, *args, **kwargs)
            except Exception as e:
                log.warning("meeting.live: ghi DB lỗi (%s): %s", getattr(fn, "__name__", fn), e)
            finally:
                self._db_queue.task_done()

    async def _proc_loop(self):
        while True:
            item = await self._queue.get()
            try:
                await self._process(item)
            except Exception:
                log.exception("meeting.live: lỗi xử lý câu %s", item.get("seq"))
            finally:
                self._queue.task_done()

    async def drain(self, timeout: float = 30.0):
        """Chờ xử lý xong mọi câu và ghi xong DB."""
        self._ensure_workers()
        try:
            if self._held:                       # câu mic đang giữ để chặn tiếng vọng
                await asyncio.wait_for(asyncio.gather(*list(self._held), return_exceptions=True), ECHO_HOLD_S + 5)
            await asyncio.wait_for(self._queue.join(), timeout)
            await asyncio.wait_for(self._db_queue.join(), timeout)
        except asyncio.TimeoutError:
            log.warning("meeting.live: drain timeout cho session %d", self.id)

    # --------------------------------------------------------- segments ---
    async def on_segment_finalized(self, t_start: float, t_end: float, raw_speaker: Optional[str], text: str,
                                   clip_pcm: bytes = b"", epoch: int = 0, stream: str = "mic",
                                   vector: Optional[np.ndarray] = None, voiced: Optional[float] = None) -> int:
        """Một câu vừa được Soniox chốt: xếp hàng xử lý tuần tự (giữ đúng thứ tự câu)."""
        seq = self.next_seq
        self.next_seq += 1
        self._ensure_workers()
        item = {"seq": seq, "t_start": t_start, "t_end": t_end, "raw": raw_speaker, "text": text, "clip": clip_pcm,
                "epoch": epoch, "stream": stream, "vector": vector, "voiced": voiced}
        st = str(stream)
        # Luồng "xa" (meet*/tab) là bản ghi sạch của tiếng người online. mic (và tab khi có cả bot) dễ dính tiếng vọng
        # của chúng, nên được chống trùng. Thứ tự ưu tiên giữ lại: meet (biết tên) > tab > mic.
        # Cách chống trùng: nếu bản của luồng xa ĐÃ có sẵn và trùng -> bỏ ngay (không giữ, không đảo thứ tự); nếu luồng xa
        # ĐANG nói dở (bản của nó có thể tới sau mic vài trăm ms) -> giữ mic lại chờ rồi mới so.
        if st.startswith("meet"):
            # Nhiều máy trong CÙNG phòng cùng vào Meet: tiếng một người lọt vào mic của mọi máy -> nhiều luồng meet* cùng
            # một câu. Giữ luồng tới trước, bỏ các luồng meet khác trùng (cùng ưu tiên "biết tên" nên giữ bản đầu tiên).
            if self._dup_meet(st, t_start, t_end, text):
                log.info("meeting.live: bỏ câu %s trùng luồng Meet khác (nhiều máy cùng phòng vào Meet): %s", st, text[:80])
                return seq
            self._remote_texts.append((t_start, t_end, self._words(text), st))
        elif st == "tab":
            meet_only = (lambda s: s.startswith("meet"))
            if self._has_live_meet():                              # vừa bot vừa extension: bỏ tab trùng bản của bot (biết tên)
                if self._is_echo(t_start, t_end, text, meet_only):
                    log.info("meeting.live: bỏ câu tab trùng bản của bot Meet (chống trùng lặp): %s", text[:80])
                    return seq
                if self._remote_voice_recent():
                    self._hold(item, meet_only)
                    return seq
            self._remote_texts.append((t_start, t_end, self._words(text), "tab"))   # để mic sau đó so được với tab
        elif st == "mic":                                          # mic dính tiếng người online (meet/tab lọt vào mic phòng)
            if self._is_echo(t_start, t_end, text, self._is_remote):
                log.info("meeting.live: bỏ câu mic trùng tiếng người online (chống trùng lặp): %s", text[:80])
                return seq
            if self._remote_voice_recent():
                self._hold(item, self._is_remote)
                return seq
        await self._queue.put(item)
        return seq

    @staticmethod
    def _words(text: str) -> List[str]:
        return re.sub(r"[^\w\s]", " ", (text or "").casefold()).split()

    @staticmethod
    def _is_remote(stream: str) -> bool:
        """Luồng tiếng người online (bản ghi sạch): bot Meet từng người hoặc tiếng tab của extension."""
        return stream.startswith("meet") or stream == "tab"

    def _has_live_meet(self) -> bool:
        return any(n.startswith("meet") for n in self.streams)

    def _remote_voice_recent(self) -> bool:
        """Có luồng xa (meet/tab) vừa có tiếng trong ~5 giây: chỉ khi đó mới giữ mic để so trùng (khỏi trễ khi họp thường)."""
        now = time.monotonic()
        return (now - self._mirror_at < 5.0 or now - self._remote_voiced_at < 5.0
                or any(g.active or now - g.last_voice < 5.0 for g in self._gates.values()))

    @staticmethod
    def _time_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
        """Khoảng thời gian hai câu chồng lên nhau, tính theo câu NGẮN hơn (1.0 = một câu nằm trọn trong câu kia)."""
        inter = max(0.0, min(a1, b1) - max(a0, b0))
        short = min(a1 - a0, b1 - b0)
        return inter / short if short > 1e-6 else 0.0

    def _match(self, mine: List[str], other: List[str]) -> bool:
        if not other:
            return False
        if len(mine) < 3:                            # câu rất ngắn: phải trùng y hệt mới bỏ (tránh bỏ nhầm "vâng", "đúng rồi")
            return mine == other
        sm = difflib.SequenceMatcher(None, mine, other, autojunk=False)
        return sum(b.size for b in sm.get_matching_blocks()) / len(mine) >= DEDUP_OVERLAP

    def _is_echo(self, t_start: float, t_end: float, text: str, want: Callable[[str], bool]) -> bool:
        """Câu này có trùng câu của một luồng xa (want lọc theo tên luồng) nói CÙNG LÚC không - kể cả câu đang nói dở.
        Câu đã chốt phải trùng cả chữ lẫn THỜI GIAN (hai bản thu một tiếng nói chồng thời gian; hai người thay phiên
        nói câu giống nhau thì nối tiếp, ít chồng -> không bị bỏ nhầm)."""
        mine = self._words(text)
        if not mine:
            return False
        for ts, te, w, s in self._remote_texts:
            if not want(s) or ts - DEDUP_WINDOW_S > t_end or te + DEDUP_WINDOW_S < t_start:
                continue
            if self._time_overlap(t_start, t_end, ts, te) >= DEDUP_TIME_OVERLAP and self._match(mine, w):
                return True
        for st in self.streams.values():             # luồng xa đang NÓI DỞ (interim): coi như đang trùng thời gian
            if want(st.name) and st._last_interim and st._last_interim[0]:
                if self._match(mine, self._words(st._last_interim[0])):
                    return True
        return False

    def _dup_meet(self, st: str, t_start: float, t_end: float, text: str) -> bool:
        """Câu từ một luồng Meet có trùng câu của luồng Meet KHÁC nói cùng lúc không (cùng một tiếng nói vào nhiều máy)."""
        return self._is_echo(t_start, t_end, text, lambda s: s.startswith("meet") and s != st)

    def _hold(self, item: Dict[str, Any], want: Callable[[str], bool]):
        task = asyncio.create_task(self._hold_segment(item, want))
        self._held.add(task)
        task.add_done_callback(self._held.discard)

    async def _hold_segment(self, item: Dict[str, Any], want: Callable[[str], bool]):
        await asyncio.sleep(ECHO_HOLD_S)
        if self._is_echo(item["t_start"], item["t_end"], item["text"], want):
            log.info("meeting.live: bỏ câu %s trùng tiếng người online (chống trùng lặp): %s",
                     item.get("stream"), item["text"][:80])
            return
        if item.get("stream") == "tab":              # tab đã qua kiểm trùng với bot: đưa vào bộ đệm để mic sau so được
            self._remote_texts.append((item["t_start"], item["t_end"], self._words(item["text"]), "tab"))
        await self._queue.put(item)

    async def _process(self, it: Dict[str, Any]):
        async with self._lock:
            await self._process_locked(it)

    async def _process_locked(self, it: Dict[str, Any]):
        kind = it.get("kind", "segment")
        if kind == "names":                          # bên nhận âm thanh đổi tên người nói
            await self._apply_mirror_names(it["profiles"])
            return
        if kind == "relabel":                        # bên nhận âm thanh đổi người nói của các câu đã chép
            await self._apply_mirror_relabel(it["items"])
            return
        mirror = bool(it.get("mirror"))
        seq, clip, vector, voiced = it["seq"], it.get("clip") or b"", it.get("vector"), it.get("voiced")
        if mirror:                                   # câu chép từ cuộc họp nhận âm thanh: người nói theo bên đó
            force, voiced = await self._mirror_sid(it), 0.0
        else:
            if voiced is None:
                voiced = voice.voiced_s(clip) if clip else 0.0
            if vector is None and clip and voiced >= voice.MIN_SEG_S and voice.available():
                vector = await asyncio.to_thread(voice.embed, clip)
            force = self._meet_profile(it) if str(it["stream"]).startswith("meet") else None
        changes = self.speakers.add(key=seq, v=vector, raw_label=it.get("raw"), t=it["t_start"], voiced=voiced,
                                    text=it["text"], epoch=it.get("epoch", 0), dur=it["t_end"] - it["t_start"],
                                    stream=it["stream"], force_sid=force)
        prof = self.speakers.profile(self.speakers.sid_of(seq))
        seg = {
            "seq": seq, "meeting_id": self.id, "t_start": it["t_start"], "t_end": it["t_end"],
            "speaker_key": prof.sid, "speaker_label": prof.label, "speaker_id": prof.voice_id,
            "text": it["text"], "is_inferred": prof.origin == "ai", "raw_speaker": it.get("raw"),
            "confidence": round(float(prof.confidence or 0.0), 2),
        }
        self.segments.append(seg)
        self._by_seq[seq] = seg
        if mirror:
            self._src_map[int(it["src_seq"])] = seq
        await self.emit({"type": "segment", "segment": seg})
        if self.game is not None and self.game.active and not it.get("backfill"):
            self.game.on_segment(seg)                # trò chơi: câu trả lời bằng giọng nói
        self._db(db.add_segment, self.id, it["t_start"], it["t_end"], prof.label, it["text"],
                 speaker_id=prof.voice_id, confidence=seg["confidence"], is_inferred=seg["is_inferred"],
                 raw_embedding=vector.tolist() if vector is not None else None, seq=seq,
                 speaker_key=prof.sid, raw_speaker=it.get("raw"), epoch=it.get("epoch", 0), stream=it["stream"],
                 emb_model=voice.MODEL_ID if vector is not None else None,
                 voiced=round(float(voiced), 2), src_seq=it.get("src_seq"))
        await self._apply_relabels([k for k in changes if k != seq])
        await self._sync_speakers()

        if mirror:
            if it.get("backfill"):                   # câu cũ chép bù cho người vào sau: không gọi trợ lý, không gợi ý
                return
            if it.get("wake"):                       # chính chủ cuộc họp riêng này gọi trợ lý trong Meet
                await self._check_wake(seg)
        else:
            stream = str(it["stream"])
            person = self.meet_people.get(stream) if stream.startswith("meet") else None
            targets = await self._copy_targets()
            route = self._wake_route(person, targets) if targets else None
            if targets:
                await self._forward_segment(seg, it, prof, route)
            if route is None:                        # người có cuộc họp riêng đang mở gọi trợ lý: phòng của người đó trả lời
                await self._check_wake(seg)
            self.identity.notify(seg)
        await self._follow_slide(seg)
        if self.meeting.get("group_id"):
            from meeting import bd
            bd.on_segment(self, seg)                 # nhóm BD: gợi ý cho đội BD trên bảng riêng

    def note_interim(self, stream: Optional[str] = None):
        self._speech_at = time.monotonic()
        if stream and self._is_remote(stream):        # luồng xa đang nói dở: đủ để giữ mic so trùng ngay từ câu đầu
            self._remote_voiced_at = time.monotonic()

    async def _check_wake(self, seg: Dict[str, Any]):
        """Phát hiện lời gọi trợ lý.

        - Câu gọi đã trọn ("Jarvis ơi, tóm tắt giúp anh.") -> xử lý ngay.
        - Câu gọi bị cắt giữa chừng ("Jarvis ơi, em hãy") -> chờ người đó nói nốt rồi xử lý cả câu.
        - Chỉ gọi tên rồi ngừng ("Jarvis ơi.") -> chờ câu yêu cầu tiếp theo."""
        text, now = seg["text"], time.monotonic()
        pc = self._pending_cmd
        if pc is not None:
            if seg["speaker_key"] == pc["sid"] or (pc["raw"] is not None and seg.get("raw_speaker") == pc["raw"]):
                pc["parts"].append(text)
                pc["last"] = now
                if _SENTENCE_END.search(text):
                    self._release_cmd(pc)
                return
            if not _unfinished(" ".join(pc["parts"])):
                self._release_cmd(pc)      # người khác nói tiếp: lời gọi trước coi như đã xong
        wake = llm.detect_wake_word(text)
        if wake:
            name, command = wake
            n_words = len(re.findall(r"\w+", command))
            if n_words >= 2 and _SENTENCE_END.search(text):
                self._pending_wake = None
                log.info("meeting.live: gọi trợ lý '%s' với yêu cầu '%s'", name, command)
                asyncio.create_task(self._handle_ai_activation(command, text, name))
            elif n_words >= 1:
                self._pending_wake = None
                self._pending_cmd = {"name": name, "parts": [command], "first": text, "sid": seg["speaker_key"],
                                     "raw": seg.get("raw_speaker"), "start": now, "last": now, "token": object()}
                await self.emit({"type": "ai_listening", "name": name, "sid": seg["speaker_key"]})
                asyncio.create_task(self._settle_cmd(self._pending_cmd["token"]))
            else:
                token = object()
                self._pending_wake = {"name": name, "until": now + WAKE_FOLLOWUP_S, "token": token}
                await self.emit({"type": "ai_listening", "name": name, "sid": seg["speaker_key"]})
                asyncio.create_task(self._expire_wake(token))
            return
        pw = self._pending_wake
        if pw is not None and now <= pw["until"] and re.search(r"\w", text):
            if pw.get("answer") and not llm.present_answer(text, asked=True):
                return                          # chờ câu trả lời; người khác nói chuyện khác thì không gọi AI
            self._pending_wake = None
            cmd = f"{pw['prefix']} {text}".strip() if pw.get("prefix") else text   # nối phần đã nói dở
            if _SENTENCE_END.search(text):
                asyncio.create_task(self._handle_ai_activation(cmd, cmd, pw["name"]))
            else:                              # câu yêu cầu cũng có thể bị cắt giữa chừng: chờ nói nốt
                self._pending_cmd = {"name": pw["name"], "parts": [cmd], "first": cmd, "sid": seg["speaker_key"],
                                     "raw": seg.get("raw_speaker"), "start": now, "last": now, "token": object()}
                asyncio.create_task(self._settle_cmd(self._pending_cmd["token"]))

    def _release_cmd(self, pc: Dict[str, Any]):
        """Người gọi đã nói hết câu: ghép các đoạn và xử lý một lần."""
        if self._pending_cmd is not pc:
            return
        self._pending_cmd = None
        command = re.sub(r"\s+", " ", " ".join(pc["parts"])).strip()
        full = re.sub(r"\s+", " ", " ".join([pc["first"]] + pc["parts"][1:])).strip()
        if _is_fragment(command):
            # "Jarvis ơi, em có thể..." rồi ngừng: chưa có yêu cầu, chờ câu tiếp theo thay vì gọi AI với nửa câu
            token = object()
            self._pending_wake = {"name": pc["name"], "until": time.monotonic() + WAKE_FOLLOWUP_S, "token": token,
                                  "prefix": command}
            asyncio.create_task(self.emit({"type": "ai_listening", "name": pc["name"], "sid": pc.get("sid")}))
            asyncio.create_task(self._expire_wake(token))
            return
        log.info("meeting.live: gọi trợ lý '%s' với yêu cầu '%s'", pc["name"], command)
        asyncio.create_task(self._handle_ai_activation(command, full, pc["name"]))

    async def _settle_cmd(self, token: object):
        while True:
            await asyncio.sleep(0.2)
            pc = self._pending_cmd
            if pc is None or pc["token"] is not token:
                return
            now = time.monotonic()
            idle = now - max(pc["last"], self._speech_at)
            if idle >= CMD_SETTLE_S or now - pc["start"] >= CMD_MAX_WAIT_S:
                self._release_cmd(pc)
                return

    async def _expire_wake(self, token: object):
        pw = self._pending_wake
        until = pw["until"] if pw is not None and pw["token"] is token else time.monotonic() + WAKE_FOLLOWUP_S
        await asyncio.sleep(max(0.5, until - time.monotonic() + 0.5))
        pw = self._pending_wake
        if pw is not None and pw["token"] is token:
            self._pending_wake = None
            await self.emit({"type": "ai_listening_end", "name": pw["name"], "reason": "answer" if pw.get("answer") else "timeout"})

    async def _apply_relabels(self, keys: List[Any], is_inferred: Optional[bool] = None) -> int:
        items = []
        for k in keys:
            seg = self._by_seq.get(k)
            sid = self.speakers.sid_of(k)
            prof = self.speakers.profile(sid)
            if seg is None or prof is None:
                continue
            inferred = prof.origin == "ai" if is_inferred is None else is_inferred
            if (seg["speaker_key"], seg["speaker_label"], seg["speaker_id"], seg.get("is_inferred")) == \
                    (prof.sid, prof.label, prof.voice_id, inferred):
                continue
            seg.update({"speaker_key": prof.sid, "speaker_label": prof.label, "speaker_id": prof.voice_id,
                        "is_inferred": inferred})
            items.append({"seq": k, "speaker_key": prof.sid, "speaker_label": prof.label,
                          "speaker_id": prof.voice_id, "is_inferred": inferred})
        if items:
            await self.emit({"type": "segments_relabeled", "items": items})
            self._db(db.update_segments_speaker_by_seqs, self.id, items)
            await self._forward_relabel(items)
        return len(items)

    async def _sync_speakers(self):
        dirty = self.speakers.pop_dirty()
        merges = self.speakers.pop_merges()
        for src, dst, auto in merges:
            self._pending_enroll.pop(src, None)
            await self.emit({"type": "speakers_merged", "source_sid": src, "target_sid": dst, "auto": auto})
        for src, new in self.speakers.pop_splits():
            await self.emit({"type": "speakers_split", "source_sid": src, "new_sid": new})
        if not dirty:
            return
        await self._forward_names(dirty)
        self._db(db.upsert_speakers, self.id, [p.to_dict(with_vector=True) for p in dirty])
        await self.emit({"type": "speakers", "speakers": self.public_speakers()})
        # Hồ sơ đã có tên, đang chờ đủ dữ liệu giọng để lưu mẫu
        for p in dirty:
            if p.sid in self._pending_enroll and p.active and p.weight >= MIN_ENROLL_S:
                opts = self._pending_enroll.pop(p.sid)
                asyncio.create_task(self.save_profile_voice(p.sid, **opts))

    # ---------------------------------------------------- speaker actions ---
    async def rename_speaker(self, sid: int, name: str, role: str = "", email: str = "",
                             save_voice: bool = False, origin: str = "manual", confidence: float = 1.0,
                             consent_by: str = "meeting_host_confirm") -> Dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("Tên không được để trống")
        before = self.speakers.profile(sid)
        if before is None:
            raise KeyError(f"Không tìm thấy người nói {sid}")
        old_label = before.label
        # Tên trùng một hồ sơ giọng đã lưu -> liên kết, trừ khi giọng khác hẳn (2 người trùng tên)
        known = await asyncio.to_thread(db.get_voice_by_name, name)
        voice_id = None
        if known and self._voice_compatible(before, known):
            voice_id = known["id"]
        p, keys = self.speakers.rename(sid, name, voice_id=voice_id, role=role or (known or {}).get("role", ""),
                                       origin=origin, confidence=confidence)
        n = await self._apply_relabels(keys)
        await self._sync_speakers()
        voice_info = None
        if save_voice:
            voice_info = await self.save_profile_voice(p.sid, email=email, consent_by=consent_by)
        await self.emit({"type": "speaker_renamed", "sid": p.sid, "old_label": old_label, "new_name": p.label,
                         "origin": origin, "confidence": confidence, "voice_id": p.voice_id})
        return {"success": True, "speaker": p.to_dict(with_vector=False), "updated_segments": n,
                "old_label": old_label, "new_name": p.label, "voice_id": p.voice_id, "voice": voice_info}

    async def save_profile_voice(self, sid: int, email: str = "", consent_by: str = "meeting_host_confirm") -> Dict[str, Any]:
        """Lưu mẫu giọng của hồ sơ vào Voice Registry để các buổi sau tự nhận ra."""
        p = self.speakers.profile(sid)
        if p is None or not p.name:
            return {"saved": False, "reason": "Người nói chưa có tên"}
        vs, ws = self.speakers.vectors_of(p.sid)
        have = float(sum(ws))
        if not vs or have < MIN_ENROLL_S:
            self._pending_enroll[p.sid] = {"email": email, "consent_by": consent_by}
            self.speakers.dirty.add(p.sid)
            await self._sync_speakers()
            return {"saved": False, "pending": True, "have_s": round(have, 1), "need_s": MIN_ENROLL_S}
        centroid = voice.merge_vectors(vs, ws)
        known = await asyncio.to_thread(db.get_voice_by_name, p.name)
        if known and not self._voice_compatible(p, known):
            return {"saved": False, "conflict": True,
                    "reason": f"Đã có hồ sơ giọng '{known['name']}' nhưng giọng nói khác hẳn - có thể là người trùng tên. "
                              "Hãy đặt tên phân biệt (ví dụ thêm họ hoặc phòng ban) rồi lưu lại."}
        vid = await asyncio.to_thread(db.save_voice, p.name, centroid.tolist(), p.role, "", email,
                                      consent_by, True, len(vs), "merge", self.speakers.model)
        full = await asyncio.to_thread(db.get_voice, vid)
        p.voice_id = vid
        self.speakers.dirty.add(p.sid)
        anchor = {vid: {"name": p.name, "vector": np.asarray(full["embedding"], dtype=np.float32),
                        "role": p.role, "department": "", "model": voice.model_of(full)}} if full and full.get("embedding") else {}
        for s in list(SESSIONS.values()):
            s.speakers.update_anchors(anchor, rebind=s is not self)
        await self._apply_relabels(self.speakers.keys_of(p.sid))
        await self._sync_speakers()
        log.info("meeting.live: đã lưu mẫu giọng '%s' (voice_id=%s, %.1fs)", p.name, vid, have)
        return {"saved": True, "voice_id": vid, "seconds": round(have, 1)}

    async def merge_speakers(self, src_sid: int, dst_sid: int) -> Dict[str, Any]:
        src, dst = self.speakers.profile(src_sid), self.speakers.profile(dst_sid)
        if src is None or dst is None:
            raise KeyError("Không tìm thấy người nói")
        if src.sid == dst.sid:
            return {"success": True, "moved": 0}
        keys = self.speakers.merge(src.sid, dst.sid)
        n = await self._apply_relabels(keys)
        await self._sync_speakers()
        return {"success": True, "moved": n, "target": dst.to_dict(with_vector=False)}

    async def reassign_segment(self, seq: int, target_sid: Optional[int]) -> Dict[str, Any]:
        if self._by_seq.get(seq) is None:
            raise KeyError(f"Không tìm thấy câu {seq}")
        dst, _ = self.speakers.reassign(seq, target_sid)
        if dst is None:
            raise KeyError("Không tìm thấy người nói đích")
        await self._apply_relabels([seq])
        await self._sync_speakers()
        return {"success": True, "seq": seq, "speaker": dst.to_dict(with_vector=False)}

    async def split_speaker_from(self, seq: int) -> Dict[str, Any]:
        """Từ câu `seq` trở đi, các câu của người nói này thuộc về một người nói mới."""
        if self._by_seq.get(seq) is None:
            raise KeyError(f"Không tìm thấy câu {seq}")
        async with self._lock:
            q, moved = self.speakers.split_from(seq)
            if q is None:
                raise ValueError("Đây là câu đầu tiên của người nói này nên không cần tách. Hãy đổi tên hoặc đổi người nói của câu.")
            await self._apply_relabels(moved)
            await self._sync_speakers()
        log.info("meeting.live: người dùng tách %d câu từ câu %d thành người nói %d", len(moved), seq, q.sid)
        return {"success": True, "speaker": q.to_dict(with_vector=False), "moved": len(moved)}

    async def reanalyze(self) -> Dict[str, Any]:
        """Chạy lại nhận diện người nói cho toàn bộ cuộc họp bằng thuật toán hiện tại.

        Dùng dữ liệu đã lưu (vector giọng, nhãn Soniox, epoch). Tên đã đặt được giữ cho hồ sơ mới chứa nhiều
        câu nhất của người đó."""
        if self._src_map:
            raise ValueError("Cuộc họp này chép lời từ bot Google Meet dùng chung (không có dữ liệu giọng): người nói "
                             "theo cuộc họp của người mời bot, sửa tên hoặc đổi người nói của từng câu nếu cần")
        await self.drain()
        rows = await asyncio.to_thread(db.get_segments, self.id, True)
        async with self._lock:
            old = self.speakers
            # Dùng vector của model chiếm đa số trong cuộc họp (cuộc họp cũ: CAM++), bỏ vector của model khác
            counts: Dict[str, int] = {}
            for s in rows:
                if s.get("raw_embedding"):
                    counts[voice.model_of(s)] = counts.get(voice.model_of(s), 0) + 1
            model = max(counts, key=counts.get) if counts else voice.MODEL_ID
            anchors = {}
            for v in await asyncio.to_thread(db.list_voices, True):
                if v.get("embedding") and voice.is_valid_vector(v["embedding"]):
                    anchors[v["id"]] = {"name": v["name"], "vector": np.asarray(v["embedding"], dtype=np.float32),
                                        "role": v.get("role", ""), "department": v.get("department", ""),
                                        "model": voice.model_of(v)}
            fresh = voice.MeetingSpeakers(anchors=anchors, expected_host_id=self.host_id, model=model)
            known = set()
            for s in rows:
                known.add(s["seq"])
                emb = s.get("raw_embedding") if voice.model_of(s) == model else None
                v = np.asarray(emb, dtype=np.float32) if emb and voice.is_valid_vector(emb) else None
                dur = max(0.0, float(s.get("t_end") or 0) - float(s.get("t_start") or 0))
                voiced = s.get("voiced")
                if voiced is None:
                    voiced = dur * 0.7 if v is not None else 0.0
                fresh.add(key=s["seq"], v=v, raw_label=s.get("raw_speaker"), t=float(s.get("t_start") or 0),
                          voiced=float(voiced), text=s.get("text", ""), epoch=int(s.get("epoch") or 0), dur=dur,
                          stream=s.get("stream") or "mic")
            for s in old.segs:          # câu vừa xử lý nhưng chưa kịp có trong kết quả đọc DB
                if s["key"] not in known and old.model == model:
                    rk = s.get("rk")
                    fresh.add(key=s["key"], v=s["v"], raw_label=s["raw"], t=s["t"], voiced=s["w"],
                              text=s["text"], epoch=rk[1] if rk else 0, dur=s["dur"], stream=rk[0] if rk else "mic")
            # Tên thuộc về người dùng hồ sơ cũ TRƯỚC TIÊN (người mới chen vào sau dưới cùng nhãn thì tách ra)
            # Tên đến từ mẫu giọng thì không mang theo: mẫu giọng tự xét lại trên hồ sơ mới (lỗi thật #57: tên của
            # anh Phúc rơi vào hồ sơ anh Duy vì anh Duy nói câu đầu tiên của hồ sơ cũ bị gộp chung)
            for op in sorted((p for p in old.active_profiles() if p.name and p.origin != "voiceprint"),
                             key=lambda p: p.first_t):
                order = [fresh.sid_of(k) for k in old.keys_of(op.sid)]
                for sid in dict.fromkeys(x for x in order if x is not None):
                    np_ = fresh.profile(sid)
                    if np_ is not None and not np_.name and fresh.find_by_name(op.name) is None:
                        np_.name, np_.voice_id, np_.role = op.name, op.voice_id or np_.voice_id, op.role
                        np_.origin, np_.locked, np_.confidence = op.origin, op.locked, op.confidence
                        break
            fresh.pop_dirty()
            fresh.pop_merges()
            fresh.pop_splits()
            self.speakers = fresh
            self._pending_enroll.clear()
            self.identity.reset()
            items = []
            for seg in self.segments:
                prof = fresh.profile(fresh.sid_of(seg["seq"]))
                if prof is None:
                    continue
                seg.update({"speaker_key": prof.sid, "speaker_label": prof.label, "speaker_id": prof.voice_id,
                            "is_inferred": prof.origin == "ai"})
                items.append({"seq": seg["seq"], "speaker_key": prof.sid, "speaker_label": prof.label,
                              "speaker_id": prof.voice_id, "is_inferred": prof.origin == "ai"})
            self._db(db.replace_speakers, self.id, fresh.export_profiles())
            self._db(db.update_segments_speaker_by_seqs, self.id, items)
            self._db(db.set_inferences_status, self.id, "pending", "stale")
        await self.drain()
        snap = self.snapshot()
        snap["artifacts"] = await asyncio.to_thread(db.get_artifacts, self.id)
        await self.emit(snap)
        log.info("meeting.live: phân tích lại cuộc họp %d -> %d người nói", self.id, len(self.public_speakers()))
        return {"success": True, "speakers": self.public_speakers(), "segments": len(items)}

    VOICE_CONFLICT_MAX = 0.30   # cosine dưới mức này với mẫu giọng trùng tên -> coi là người khác

    def _voice_compatible(self, p: voice.SpeakerProfile, known: Dict[str, Any]) -> bool:
        """Hồ sơ người nói có thể là cùng người với hồ sơ giọng `known` (trùng tên) hay không."""
        if p.voice_id is not None and p.voice_id == known.get("id"):
            return True
        emb = known.get("embedding")
        c = p.centroid()
        if c is None or not emb or not voice.is_valid_vector(emb) or p.weight < 2.0:
            return True   # chưa đủ dữ liệu để phản bác
        return voice.cosine_sim(c, voice.unit(np.asarray(emb, dtype=np.float32))) >= self.VOICE_CONFLICT_MAX

    def forget_voice(self, voice_id: int):
        self.speakers.forget_voice(voice_id)

    def set_host(self, host_id: int):
        self.host_id = host_id
        self.speakers.host_id = host_id
        self.meeting["host_id"] = host_id

    # ------------------------------------------------------------- audio ---
    async def start_audio(self, owner: object, name: str = "mic", diarize: bool = True,
                          claim_owner: bool = True) -> MeetingStream:
        """claim_owner=False: luồng phụ (extension thu tiếng tab) chạy song song mà không chiếm "nguồn mic" của cuộc họp."""
        stream = self.streams.get(name)
        if stream is None or stream.closed:
            stream = MeetingStream(name, self, diarize=diarize)
            self.streams[name] = stream
        await stream.ensure_open()
        if claim_owner:
            self.audio_owner = owner
        return stream

    async def stop_audio(self, owner: object, name: str = "mic"):
        if self.audio_owner is not owner:
            return
        self.audio_owner = None
        stream = self.streams.get(name)
        if stream is not None:
            await stream.pause()

    async def feed(self, pcm: bytes, stream_name: str = "mic"):
        if stream_name in self.streams:
            await self.streams[stream_name].feed(pcm)

    # ------------------------------------------------------- Google Meet ---
    def set_mic_user(self, user: Optional[Dict[str, Any]]):
        """Người đang bật mic trong ứng dụng (None: mic tắt). Luồng Meet trùng người đó bị bỏ qua khi mic đang bật."""
        self.mic_user = {"email": str(user.get("email") or "").lower(), "name": str(user.get("name") or "")} if user else None
        if self.meet_people:
            asyncio.ensure_future(self._emit_meet())

    def _meet_mode(self, stream: str) -> str:
        return self._meet_modes.get(stream, "auto")

    def meet_ignored(self, stream: str) -> bool:
        mode = self._meet_mode(stream)
        if mode in ("ignore", "listen"):
            return mode == "ignore"
        person = self.meet_people.get(stream) or {}
        return self.audio_owner is not None and same_person(person, self.mic_user)

    def meet_public(self) -> Dict[str, Any]:
        people = [{"stream": k, "name": v.get("name") or "", "email": v.get("email") or "", "is_host": v.get("is_host"),
                   "mode": self._meet_mode(k), "ignored": self.meet_ignored(k), "speaking": bool(v.get("speaking"))}
                  for k, v in self.meet_people.items()]
        out = {"bot": self.meet_bot, "people": people}
        if self.copy_of:
            out["copy"] = True          # cuộc họp riêng: lời thoại chép từ bot do người khác mời (bot dùng chung)
        return out

    async def _emit_meet(self):
        await self.emit({"type": "meet", "meet": self.meet_public()})

    async def set_meet_bot(self, bot: Optional[Dict[str, Any]]):
        self.meet_bot = bot
        await self._emit_meet()

    async def set_meet_mode(self, stream: str, mode: str) -> Dict[str, Any]:
        """auto: bỏ qua khi trùng người đang bật mic; ignore: luôn bỏ qua (máy trong phòng); listen: luôn chép."""
        if stream not in self.meet_people:
            raise KeyError(stream)
        if mode not in ("auto", "ignore", "listen"):
            raise ValueError("Chế độ không hợp lệ")
        if mode == "auto":
            self._meet_modes.pop(stream, None)
        else:
            self._meet_modes[stream] = mode
        await asyncio.to_thread(db.update_meeting, self.id, {"meet.modes": dict(self._meet_modes)}, True)
        if self.meet_ignored(stream):
            await self._pause_meet(stream)
        await self._emit_meet()
        return self.meet_public()

    async def _pause_meet(self, stream: str):
        g = self._gates.get(stream)
        if g is not None:
            g.active = False
        person = self.meet_people.get(stream)
        if person is not None:
            person["speaking"] = False
        st = self.streams.get(stream)
        if st is not None and not st.closed:
            await st.pause()

    async def feed_meet(self, participant: Dict[str, Any], pcm: bytes):
        """Âm thanh của một người trong Google Meet (từ bot Recall): mỗi người một luồng nhận dạng riêng."""
        if not self.is_live() or self.disposed:
            return
        stream = f"meet{participant.get('id')}"
        person = self.meet_people.get(stream)
        changed = False
        if person is None:
            person = {"name": participant.get("name") or "", "email": participant.get("email") or "",
                      "is_host": bool(participant.get("is_host")), "speaking": False, "failed_at": 0.0}
            self.meet_people[stream] = person
            changed = True
        elif participant.get("name") and (person["name"], person["email"]) != (participant["name"], participant.get("email") or ""):
            person.update(name=participant["name"], email=participant.get("email") or "")
            changed = True
        if self.meet_ignored(stream):
            if person.get("speaking"):
                await self._pause_meet(stream)
                changed = True
            if changed:
                await self._emit_meet()
            return
        gate = self._gates.setdefault(stream, VoiceGate())
        out, stop = gate.push(pcm, time.monotonic())
        if stop:
            await self._pause_meet(stream)
            await self._emit_meet()
            return
        if not out:
            if changed:
                await self._emit_meet()
            return
        st = self.streams.get(stream)
        if st is None or st.closed:
            st = MeetingStream(stream, self, diarize=MEET_DIARIZE)
            self.streams[stream] = st
        if not st.is_open and not st.reconnecting:
            if time.monotonic() - person.get("failed_at", 0.0) < 30:
                gate.active = False
                return
            try:
                await st.ensure_open()
            except Exception as e:
                person["failed_at"] = time.monotonic()
                gate.active = False
                log.warning("meeting.live: không mở được luồng %s của cuộc họp %d: %s", stream, self.id, e)
                await self.emit({"type": "error", "text": f"Không nhận dạng được giọng của {person['name'] or 'người trong Meet'}: {e}"})
                return
        if not person["speaking"]:
            person["speaking"] = True
            changed = True
        if changed:
            await self._emit_meet()
        for chunk in out:
            await st.feed(chunk)

    async def meet_disconnected(self):
        """Bot ngắt kết nối âm thanh: chốt chữ còn treo của mọi luồng Meet (Recall nối lại thì chạy tiếp)."""
        for stream in list(self.meet_people):
            await self._pause_meet(stream)
        if self.meet_people:
            await self._emit_meet()

    def _meet_profile(self, it: Dict[str, Any]) -> Optional[int]:
        """Hồ sơ người nói cho câu từ luồng Meet: luồng chỉ một giọng thì là người trong Meet đó (đặt tên theo Meet);
        luồng có nhiều giọng (máy trong phòng vào Meet) thì tách người như mic."""
        stream = str(it["stream"])
        per_epoch = self._stream_raws.setdefault(stream, {})
        if it["raw"] is not None:
            per_epoch.setdefault(int(it["epoch"]), set()).add(str(it["raw"]))
        if any(len(v) > 1 for v in per_epoch.values()):
            return None
        person = self.meet_people.get(stream) or {}
        return self.speakers.pin_profile(stream, person.get("name") or "", float(it["t_start"])).sid

    # ------------------------------------- bot Meet dùng chung: cuộc họp riêng của từng người ---
    # Nhiều người dùng Copilot trong cùng một cuộc họp Google Meet: chỉ một bot. Cuộc họp nhận âm thanh (của người hẹn bot)
    # gửi bản sao từng câu, tên người nói, việc đổi người nói sang cuộc họp riêng của từng người kia (meet_bots quyết định
    # ai có cuộc họp riêng). Cuộc họp riêng là một phòng độc lập: trợ lý, sản phẩm AI, biên bản, Drive của riêng người đó.
    @property
    def copy_of(self) -> Optional[int]:
        """Cuộc họp nhận âm thanh mà cuộc họp này chép lời theo (None: không phải cuộc họp riêng từ bot dùng chung)."""
        v = (self.meeting.get("meet") or {}).get("copy_of")
        return int(v) if v else None

    def set_copy_targets(self, targets: List[Dict[str, Any]]):
        """meet_bots báo các cuộc họp riêng đang nhận lời thoại từ cuộc họp này: [{meeting_id, email, name}]."""
        self._targets = [dict(t) for t in targets]

    async def _copy_targets(self) -> List[Dict[str, Any]]:
        if self._targets is None:
            rid = (self.meeting.get("meet") or {}).get("rid")
            if not rid or self.copy_of:
                self._targets = []
            else:
                from meeting import meet_bots
                self._targets = await asyncio.to_thread(meet_bots.targets_for, rid, self.id)
        return self._targets

    async def _target_sessions(self) -> List[Tuple[Dict[str, Any], "MeetingSession"]]:
        out = []
        for t in await self._copy_targets():
            c = await get_session(int(t["meeting_id"]))
            if c is not None and not c.disposed and c.id != self.id:
                out.append((t, c))
        return out

    @staticmethod
    def _mirror_info(p: voice.SpeakerProfile) -> Dict[str, Any]:
        """Người nói gửi sang cuộc họp riêng: khóa gắn hồ sơ (luồng Meet của người đó, hoặc số hồ sơ bên này) và tên."""
        return {"pin": p.pin or f"src:{p.sid}", "src_sid": p.sid, "name": p.name, "role": p.role, "origin": p.origin,
                "confidence": round(float(p.confidence or 0.0), 3)}

    def _wake_route(self, person: Optional[Dict[str, Any]], targets: List[Dict[str, Any]]) -> Optional[int]:
        """Người trong Meet gọi trợ lý mà người đó đang mở cuộc họp riêng của mình: phòng đó trả lời, không phải phòng này.
        Không mở (hoặc người khác gọi) thì phòng này trả lời như trước."""
        if not person:
            return None
        for t in targets:
            c = SESSIONS.get(int(t["meeting_id"]))
            if c is not None and c.subscribers and c.is_live() and same_person(person, t):
                return c.id
        return None

    async def _forward_segment(self, seg: Dict[str, Any], it: Dict[str, Any], prof: voice.SpeakerProfile,
                               route: Optional[int]):
        stream = str(it["stream"])
        person = self.meet_people.get(stream) if stream.startswith("meet") else None
        base = {"kind": "segment", "mirror": True, "src_seq": seg["seq"], "text": seg["text"], "stream": stream,
                "person": {"name": person.get("name") or "", "email": person.get("email") or ""} if person else None,
                **self._mirror_info(prof)}
        for _, c in await self._target_sessions():
            off = self.started_at - c.started_at                   # cùng một mốc giờ: lệch nhau khi gắn phòng có sẵn
            c.ingest_mirror(dict(base, t_start=seg["t_start"] + off, t_end=seg["t_end"] + off, wake=route == c.id))

    async def _forward_relabel(self, items: List[Dict[str, Any]]):
        pairs = await self._target_sessions()
        if not pairs:
            return
        out = []
        for d in items:
            p = self.speakers.profile(d.get("speaker_key"))
            if p is not None:
                out.append({"src_seq": d["seq"], **self._mirror_info(p)})
        if out:
            for _, c in pairs:
                c.ingest_mirror({"kind": "relabel", "items": out})

    async def _forward_names(self, dirty: List[voice.SpeakerProfile]):
        pairs = await self._target_sessions()
        if not pairs:
            return
        out = []
        for p in dirty:
            if not p.active:
                continue
            info = self._mirror_info(p)
            sig = (info["pin"], info["name"], info["role"], info["origin"])
            if self._fwd_names.get(p.sid) == sig:
                continue                                           # chỉ thêm câu, tên không đổi: không gửi
            self._fwd_names[p.sid] = sig
            out.append(info)
        if out:
            for _, c in pairs:
                c.ingest_mirror({"kind": "names", "profiles": out})

    async def _forward_interim(self, ev: Dict[str, Any]):
        stream = str(ev.get("stream") or "")
        person = self.meet_people.get(stream) if stream.startswith("meet") else None
        p = self.speakers.profile(ev.get("speaker_key")) if ev.get("speaker_key") is not None else None
        key = stream if stream.startswith("meet") else (self._mirror_info(p)["pin"] if p is not None else None)
        for t in list(self._targets or []):
            c = SESSIONS.get(int(t["meeting_id"]))
            if c is not None and c.subscribers and c.is_live() and c.id != self.id:
                await c.mirror_interim(ev, key, person)

    async def attach_copy(self, target: Dict[str, Any], backfill: bool = True, persist: Optional[Callable] = None):
        """Thêm một cuộc họp riêng (người vào sau): chép bù các câu đã có rồi mới gửi câu mới, đúng thứ tự.
        persist: ghi danh sách cuộc họp riêng vào bản ghi bot (chạy trong khóa để không lọt câu)."""
        async with self._lock:
            targets = list(await self._copy_targets())
            if persist is not None:
                await asyncio.to_thread(persist)
            c = await get_session(int(target["meeting_id"]))
            if c is not None and backfill:
                off = self.started_at - c.started_at
                for seg in list(self.segments):
                    p = self.speakers.profile(seg.get("speaker_key"))
                    if p is None:
                        continue
                    c.ingest_mirror({"kind": "segment", "mirror": True, "backfill": True, "src_seq": seg["seq"],
                                     "text": seg["text"], "stream": "mirror", "person": None, "wake": False,
                                     "t_start": seg["t_start"] + off, "t_end": seg["t_end"] + off, **self._mirror_info(p)})
            if all(int(t["meeting_id"]) != int(target["meeting_id"]) for t in targets):
                targets.append(dict(target))
            self._targets = targets

    # bên nhận (cuộc họp riêng)
    def ingest_mirror(self, item: Dict[str, Any]):
        """Câu / tên người nói / đổi người nói chép từ cuộc họp nhận âm thanh, xử lý tuần tự cùng hàng đợi câu."""
        if self.disposed:
            return
        if item.get("kind", "segment") == "segment":
            if not self.is_live():
                return
            person = item.get("person")
            if person and self.audio_owner is not None and same_person(person, self.mic_user):
                return                      # máy này vừa bật mic trong phòng vừa vào Meet: mic đã chép câu này
            if str(item.get("stream") or "").startswith("meet"):
                self._remote_texts.append((item["t_start"], item["t_end"], self._words(item["text"]), str(item.get("stream"))))
                self._mirror_at = time.monotonic()                # mic trong phòng (nếu bật) chặn tiếng vọng của câu này
            item = dict(item, seq=self.next_seq)
            self.next_seq += 1
        self._ensure_workers()
        self._queue.put_nowait(item)

    async def mirror_interim(self, ev: Dict[str, Any], key: Optional[str], person: Optional[Dict[str, Any]]):
        if not self.is_live() or (person and self.audio_owner is not None and same_person(person, self.mic_user)):
            return
        p = self.speakers.pinned(key) if key else None
        if ev.get("text"):
            self.note_interim(ev.get("stream"))
        await self.emit({"type": "interim", "stream": ev.get("stream"), "text": ev.get("text") or "",
                         "speaker_key": p.sid if p is not None else None,
                         "speaker_label": p.label if p is not None else ev.get("speaker_label")})

    async def _mirror_sid(self, it: Dict[str, Any]) -> int:
        p = self.speakers.mirror_profile(it["pin"], int(it["src_sid"]), float(it["t_start"]))
        q, keys = self.speakers.mirror_name(p.sid, it.get("name") or "", it.get("role") or "",
                                            it.get("origin") or "new", float(it.get("confidence") or 0.0))
        if keys:
            await self._apply_relabels(keys)
        return (q or p).sid

    async def _apply_mirror_names(self, profiles: List[Dict[str, Any]]):
        keys: List[Any] = []
        for d in profiles:
            p = self.speakers.pinned(d["pin"])
            if p is None:
                continue                    # người này chưa có câu nào ở đây: tên đi kèm câu đầu tiên
            _, ks = self.speakers.mirror_name(p.sid, d.get("name") or "", d.get("role") or "",
                                              d.get("origin") or "new", float(d.get("confidence") or 0.0))
            keys += ks
        if keys:
            await self._apply_relabels(sorted(set(keys)))
        await self._sync_speakers()

    async def _apply_mirror_relabel(self, items: List[Dict[str, Any]]):
        keys: List[Any] = []
        for d in items:
            seq = self._src_map.get(int(d["src_seq"]))
            seg = self._by_seq.get(seq) if seq is not None else None
            if seg is None:
                continue
            p = self.speakers.mirror_profile(d["pin"], int(d["src_sid"]), float(seg["t_start"]))
            q, ks = self.speakers.mirror_name(p.sid, d.get("name") or "", d.get("role") or "",
                                              d.get("origin") or "new", float(d.get("confidence") or 0.0))
            keys += ks
            target = q or p
            if self.speakers.sid_of(seq) != target.sid:
                self.speakers.reassign(seq, target.sid)
                keys.append(seq)
        if keys:
            await self._apply_relabels(sorted(set(keys)))
        await self._sync_speakers()

    async def _wait_source_names(self):
        """Cuộc họp riêng lập biên bản: chờ bên nhận âm thanh chốt tên người nói lúc kết thúc (tên đã áp vào đây)."""
        src = SESSIONS.get(self.copy_of) if self.copy_of else None
        if src is not None and not src.disposed and src.meeting.get("minutes_status") == "pending":
            try:
                await asyncio.wait_for(src.names_final.wait(), NAMES_WAIT_S)
            except asyncio.TimeoutError:
                log.info("meeting.live: chờ tên người nói từ cuộc họp %s quá lâu, lập biên bản luôn", self.copy_of)
        await self.drain()

    # ----------------------------------------------------------------- AI ---
    async def _handle_ai_activation(self, command: str, full_sentence: str, name: str = "", source: str = "voice"):
        """Kích hoạt trợ lý khi được gọi tên: lệnh trình chiếu xử lý ngay, còn lại qua Thinking Engine."""
        prompt = command if len(command) > 5 else full_sentence
        artifacts.set_meeting(self.id, "trợ lý")
        task = asyncio.current_task()
        if task is not None:
            self._ai_tasks.add(task)
            task.add_done_callback(self._ai_tasks.discard)
        await self.emit({"type": "ai_activated", "wake_word": name or llm.assistant_config()["name"], "prompt": prompt,
                         "source": source})
        try:
            if self.game is not None and self.game.active and await self._game_voice(command):
                return
            if self._ask and time.monotonic() < self._ask["until"]:
                ans = llm.present_answer(command, asked=True)
                if ans:
                    await self.answer_present(ans)
                    return
            intent = llm.stage_intent(command)
            if intent and intent.get("action") == "present":
                intent = dict(intent, text=command)
            if intent and await self._handle_stage_intent(intent):
                return
            if self.stage["artifact_id"] is not None and llm.is_edit_command(command):
                await self._progress("Dạ, em sửa ngay.", kind="ack")
                await self._edit_on_stage(command)
                return
        except Exception as e:
            log.warning("meeting.live: xử lý lệnh trình chiếu lỗi: %s", e)
            await self.emit({"type": "ai_error", "text": f"Em chưa làm được: {e}"})
            return

        async def _insights(items: List[Dict[str, str]]):
            await self.emit({"type": "ai_insights", "items": items, "source": "request"})

        async def _thinking(thought: str):
            await self.emit({"type": "ai_thinking", "text": thought})

        async def _tool(tool_info: Dict[str, Any]):
            await self.emit({"type": "ai_tool_call", "tool": tool_info.get("tool"), "args": tool_info.get("args")})

        await self._progress(llm.ack_phrase(command), kind="ack")
        try:
            res = await llm.think_and_act(meeting_id=self.id, prompt=prompt, segments=self.segments,
                                          on_thinking=_thinking, on_tool=_tool, on_insights=_insights,
                                          on_progress=self._progress, meeting=self.meeting,
                                          stage_art=self._stage_summary(), library=await self._library_summary())
            await self.emit({"type": "ai_response", "response": res})
            await self.apply_ai_result(res)
        except Exception as e:
            log.warning("meeting.live: trợ lý AI lỗi: %s", e)
            await self.emit({"type": "ai_error", "text": f"Trợ lý AI lỗi: {e}"})

    async def apply_ai_result(self, res: Dict[str, Any]):
        """Kết quả của trợ lý lên màn hình: mở lại nội dung đã có (show) hoặc sản phẩm vừa tạo."""
        show = res.get("show")
        if show and show.get("artifact_id") is not None:
            aid = int(show["artifact_id"])
            slide = show.get("slide")
            if slide is None and self._slides_of(await self._get_artifact(aid)):
                slide = self._last_slide(aid)
            await self.emit({"type": "stage_command", "action": "open"})
            await self.stage_action("show", artifact_id=aid, slide=slide or 0)
        elif res.get("artifact"):
            await self.stage_action("show", artifact_id=res["artifact"]["id"])
            if res["artifact"].get("kind") == "game":   # trò chơi: mở màn hình và dẫn luôn
                await self.emit({"type": "stage_command", "action": "open"})
                await self.start_game(res["artifact"])
                return
        if res.get("present"):                        # "vẽ sơ đồ rồi thuyết trình luôn"
            art = await self._get_artifact(self.stage["artifact_id"])
            if art and art.get("kind") in ("diagram", "dashboard"):
                await self.present_artifact(art)
            elif self._slides_of(art):
                await self.emit({"type": "stage_command", "action": "open"})
                await self._ensure_scripts(art)
                await self.emit({"type": "stage_present", "action": "start", "slide": self.stage["slide"]})

    # ------------------------------------------------- trò chơi có thưởng ---
    async def start_game(self, art: Dict[str, Any]) -> Dict[str, Any]:
        """Mở một lượt chơi từ sản phẩm loại game (lượt cũ đang chạy thì dừng). Trợ lý dẫn trên màn hình trình chiếu."""
        from meeting import game
        try:
            g = game.normalize(json.loads(art.get("content") or "{}"))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Trò chơi không đọc được: {e}")
        if self.game is not None and self.game.active:
            await self.game.stop("Bắt đầu lượt chơi mới")
        run = game.GameRun(self, int(art["id"]), g)
        self.game = run
        if self.stage["artifact_id"] != int(art["id"]):
            await self.stage_action("show", artifact_id=int(art["id"]))
        run.task = asyncio.create_task(run.run())
        return run.public()

    async def game_command(self, action: str) -> Dict[str, Any]:
        """start (bắt đầu từ màn chờ), next (bỏ qua thời gian chờ), scores (xem bảng điểm), stop."""
        g = self.game
        if g is None or not g.active:
            raise ValueError("Chưa có trò chơi nào đang chạy")
        if action == "start":
            g.start()
        elif action == "next":
            g.start() if g.phase == "lobby" else g.skip()
        elif action == "stop":
            await g.stop()
            await self._say("Dạ, em dừng trò chơi. Cảm ơn cả nhà đã tham gia!")
        elif action == "scores":
            board = g.leaderboard(3)
            await self._say("Bảng điểm: " + ", ".join(f"{r['name']} {r['points']} điểm" for r in board) + "."
                            if board else "Chưa có ai ghi điểm ạ.")
        else:
            raise ValueError("Lệnh không hợp lệ")
        return g.public()

    async def _game_voice(self, command: str) -> bool:
        """Gọi tên trợ lý trong lúc chơi: lệnh điều khiển trò chơi thì làm ngay; đang đếm ngược thì không gọi AI."""
        from meeting import game
        cmd = game.command_of(command)
        if cmd is not None:
            if cmd == "start" and self.game.phase != "lobby":
                cmd = "next"
            await self.game_command(cmd)
            return True
        return self.game.phase in ("reading", "answering", "closing")

    # ------------------------------------------------- màn hình trình bày ---
    STAGE_ACTIONS = ("show", "next", "prev", "goto", "topic", "back", "follow")
    # Khi trợ lý đang thuyết trình, khuôn mặt nói nên mic gửi khoảng lặng; lệnh người dùng đến giữa chừng vẫn được xử lý
    # ở frontend (nút Dừng, phím Esc) hoặc khi trợ lý ngắt nghỉ giữa hai slide.

    def stage_public(self) -> Dict[str, Any]:
        st = self.stage
        return {"artifact_id": st["artifact_id"], "slide": st["slide"], "can_back": bool(st["history"]),
                "shown_at_seq": st["shown_at_seq"], "follow": bool(st["follow"])}

    async def _get_artifact(self, aid: Optional[int]) -> Optional[Dict[str, Any]]:
        if aid is None:
            return None
        if aid not in self._art_cache:
            art = await asyncio.to_thread(db.get_artifact, int(aid))
            if art is not None:
                self._art_cache[aid] = art
        return self._art_cache.get(aid)

    @staticmethod
    def _slides_of(art: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not art or art.get("kind") != "slides":
            return []
        deck = artifacts.load_deck(art.get("content", ""))
        return deck["slides"] if deck else []

    @staticmethod
    def _find_slide(slides: List[Dict[str, Any]], query: str) -> Optional[int]:
        words = {w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 1}
        best, best_score = None, 0.0
        for i, sl in enumerate(slides):
            text = " ".join([sl.get("title", "")] * 2 + sl.get("bullets", [])).lower()
            have = set(re.findall(r"\w+", text))
            score = len(words & have) / max(1, len(words))
            if score > best_score:
                best, best_score = i, score
        return best if best_score >= 0.5 else None

    async def _history_index(self, kind: Optional[str]) -> Optional[int]:
        """Vị trí trong lịch sử của nội dung đã trình bày gần nhất thuộc loại kind (None = mục cuối)."""
        hist = self.stage["history"]
        if not hist:
            return None
        if not kind:
            return len(hist) - 1
        for i in range(len(hist) - 1, -1, -1):
            if hist[i]["artifact_id"] == self.stage["artifact_id"]:
                continue
            art = await self._get_artifact(hist[i]["artifact_id"])
            if art and art.get("kind") == kind:
                return i
        return None

    async def stage_action(self, action: str, artifact_id: Optional[int] = None, slide: Optional[int] = None,
                           query: Optional[str] = None, follow: Optional[bool] = None,
                           auto: bool = False, kind: Optional[str] = None) -> Dict[str, Any]:
        """Điều khiển màn hình trình bày: show (đưa nội dung lên), next/prev/goto/topic (chuyển slide), back,
        follow (bật/tắt tự chuyển slide theo lời trình bày). auto=True: do hệ thống tự chuyển theo lời nói."""
        st = self.stage
        before = (st["artifact_id"], st["slide"])
        if action == "follow":
            st["follow"] = bool(follow) if follow is not None else not st["follow"]
            self._follower.pending = None
        elif action == "show":
            art = await self._get_artifact(artifact_id)
            if art is None:
                raise KeyError(f"Không tìm thấy nội dung {artifact_id}")
            if st["artifact_id"] is not None and st["artifact_id"] != art["id"] \
                    and art.get("parent_id") != st["artifact_id"]:   # bản sửa của cùng nội dung: không lưu lịch sử
                st["history"] = (st["history"] + [{"artifact_id": st["artifact_id"], "slide": st["slide"]}])[-20:]
            n = max(1, len(self._slides_of(art)))
            st["artifact_id"], st["slide"] = art["id"], min(max(int(slide or 0), 0), n - 1)
        elif action == "back":
            idx = await self._history_index(kind)
            if idx is not None:
                prev = st["history"].pop(idx)
                if kind and st["artifact_id"] is not None:      # quay lại theo loại: giữ chỗ hiện tại để còn quay tiếp
                    st["history"] = (st["history"] + [{"artifact_id": st["artifact_id"], "slide": st["slide"]}])[-20:]
                st["artifact_id"], st["slide"] = prev["artifact_id"], prev["slide"]
        elif st["artifact_id"] is not None:
            slides = self._slides_of(await self._get_artifact(st["artifact_id"]))
            n = max(1, len(slides))
            if action == "next":
                st["slide"] = min(n - 1, st["slide"] + 1)
            elif action == "prev":
                st["slide"] = max(0, st["slide"] - 1)
            elif action == "goto":
                st["slide"] = min(n - 1, max(0, int(slide or 0)))
            elif action == "topic":
                idx = self._find_slide(slides, query or "")
                if idx is not None:
                    st["slide"] = idx
        if (st["artifact_id"], st["slide"]) != before:
            st["shown_at_seq"] = self.next_seq - 1
            st["changed_at"] = time.monotonic()
            st["auto_at" if auto else "manual_at"] = st["changed_at"]
            self._follow_texts.clear()
            self._log_stage()
        await self.emit({"type": "stage_state", "stage": self.stage_public(), "auto": auto})
        return self.stage_public()

    def _log_stage(self):
        """Ghi lại nội dung đang chiếu (mỗi nội dung một dòng, cập nhật slide đang xem) vào DB."""
        st = self.stage
        if st["artifact_id"] is None:
            return
        lg = st["log"]
        if lg and lg[-1]["artifact_id"] == st["artifact_id"]:
            lg[-1].update(slide=st["slide"], at=time.time())
        else:
            lg.append({"artifact_id": st["artifact_id"], "slide": st["slide"], "at": time.time()})
            del lg[:-50]
        self.meeting["stage_log"] = [dict(e) for e in lg]
        self._db(db.update_meeting, self.id, {"stage_log": self.meeting["stage_log"]}, internal=True)

    def _last_slide(self, aid: int) -> int:
        for e in reversed(self.stage["log"]):
            if e["artifact_id"] == aid:
                return int(e.get("slide") or 0)
        return 0

    async def _library_summary(self) -> Dict[str, Any]:
        """Nội dung đã tạo / đã chiếu trong cuộc họp (mới nhất trước) để trợ lý mở lại đúng thứ người dùng nhắc."""
        arts = await asyncio.to_thread(db.get_artifacts, self.id)
        arts = sorted(arts, key=lambda a: (a.get("created_at") or 0, a.get("id") or 0), reverse=True)[:15]
        shown = [e["artifact_id"] for e in self.stage["log"]]
        items = []
        for a in arts:
            it = {"id": a["id"], "kind": a.get("kind"), "title": a.get("title", ""), "version": int(a.get("version") or 1),
                  "on_stage": a["id"] == self.stage["artifact_id"], "shown": a["id"] in shown}
            if a.get("kind") == "slides":
                it["slides"] = [s.get("title", "") for s in self._slides_of(a)][:10]
                if it["on_stage"]:
                    it["slide"] = self.stage["slide"] + 1
            items.append(it)
        return {"items": items, "order": shown[-12:]}

    async def _recall(self, intent: Dict[str, Any], announce: bool = True) -> bool:
        """Mở lại nội dung đã có: theo chủ đề ("slide điểm cộng"), theo loại đã chiếu gần nhất ("sơ đồ lúc nãy"),
        cái đầu tiên, hoặc bất kỳ nội dung vừa chiếu trước đó. Không khớp chủ đề thì để trợ lý hiểu câu nói (False)."""
        kind, query = intent.get("kind"), (intent.get("query") or "").strip()
        arts = sorted(await asyncio.to_thread(db.get_artifacts, self.id),
                      key=lambda a: (a.get("created_at") or 0, a.get("id") or 0))          # cũ -> mới
        if kind:
            arts = [a for a in arts if a.get("kind") == kind]
        name = llm.KIND_NAMES.get(kind, kind) if kind else "nội dung"
        if not arts:
            if not kind:
                return False
            if announce:
                await self._say(f"Em chưa tạo {name} nào trong cuộc họp này.", "concerned")
            return True
        cur = self.stage["artifact_id"]
        cur_art = await self._get_artifact(cur) if cur is not None else None
        by_id = {a["id"]: a for a in arts}
        target, slide = None, None
        if query:
            best = None
            for rank, a in enumerate(arts):                         # cùng điểm: bản mới hơn thắng
                title = re.sub(r"^[^:]{1,24}:\s*", "", a.get("title") or "")
                sc, hit = llm.topic_score(query, title)
                cand = (sc, hit, None)
                for i, sl in enumerate(self._slides_of(a)):
                    s1, h1 = llm.topic_score(query, sl.get("title", ""))
                    s2, h2 = llm.topic_score(query, " ".join([sl.get("title", "")] + list(sl.get("bullets") or [])))
                    c2 = (max(s1, s2 * 0.8), max(h1, h2), i)
                    if c2[:2] > cand[:2]:
                        cand = c2
                key = (cand[0], cand[1], rank)
                if best is None or key > best[0]:
                    best = (key, a, cand[2])
            (sc, hit, _), a, idx = best
            if not (sc >= 0.5 or (hit >= 2 and sc >= 0.34)):
                return False
            target, slide = a, idx
        elif intent.get("first"):
            target = arts[0]
        else:
            for h in reversed(self.stage["history"]):              # nội dung đã chiếu gần nhất (khác cái đang chiếu)
                if h["artifact_id"] in by_id and h["artifact_id"] != cur:
                    target, slide = by_id[h["artifact_id"]], h.get("slide")
                    break
            if target is None:
                cur_title = (cur_art or {}).get("title")
                others = [a for a in arts if a["id"] != cur and a.get("title") != cur_title]
                if others:
                    target = others[-1]
                elif cur_art is not None and (not kind or cur_art.get("kind") == kind):
                    sl = self._slides_of(cur_art)
                    if intent.get("past") and sl and self.stage["slide"] > 0:   # chỉ có bộ này: lùi về slide trước
                        await self.emit({"type": "stage_command", "action": "open"})
                        await self.stage_action("prev")
                        i = self.stage["slide"]
                        if announce:
                            await self._say(f"Slide {i + 1} trên {len(sl)}: {sl[i]['title']}.", quiet=True)
                        return True
                    if announce:
                        await self._say(f"Đang chiếu {llm.KIND_NAMES.get(cur_art.get('kind'), 'nội dung')} "
                                        f"\"{cur_art.get('title', '')}\" rồi ạ, trước đó chưa có {name} nào khác.", quiet=True)
                    await self.emit({"type": "stage_command", "action": "open"})
                    return True
                else:
                    target = arts[-1]
        slides = self._slides_of(target)
        if slide is None:
            slide = self._last_slide(target["id"]) if slides else 0
        slide = min(max(int(slide or 0), 0), max(0, len(slides) - 1))
        await self.emit({"type": "stage_command", "action": "open"})
        if target["id"] == cur:
            await self.stage_action("goto", slide=slide)
        else:
            await self.stage_action("show", artifact_id=target["id"], slide=slide)
        if announce:
            where = f", slide {slide + 1}: {slides[slide]['title']}" if slides and (query or slide) else ""
            kname = llm.KIND_NAMES.get(target.get("kind"), "nội dung")
            await self._say(f"Dạ, em mở lại {kname} \"{target.get('title', '')}\"{where}.", quiet=True)
        return True

    def _deck_matcher(self) -> Optional[Any]:
        art = self._art_cache.get(self.stage["artifact_id"]) if self.stage["artifact_id"] is not None else None
        if not art or art.get("kind") != "slides":
            return None
        if art["id"] not in self._matchers:
            slides = self._slides_of(art)
            self._matchers[art["id"]] = (slides, follow.SlideMatcher(slides)) if len(slides) > 1 else None
        return self._matchers[art["id"]]

    async def _follow_slide(self, seg: Dict[str, Any]):
        """Đang chiếu bộ slide: lời trình bày chuyển sang ý của slide khác thì tự chuyển slide theo."""
        st = self.stage
        if not st["follow"] or not re.search(r"\w", seg.get("text") or ""):
            return
        if self._pending_cmd is not None or llm.detect_wake_word(seg["text"]):
            return                                   # câu gọi trợ lý, không phải lời trình bày
        deck = self._deck_matcher()
        if deck is None:
            return
        slides, matcher = deck
        self._follow_texts.append(seg["text"])
        # So bằng câu vừa nói (câu trước còn nói về slide cũ sẽ níu lại); câu quá ngắn thì ghép với câu trước
        spoken = seg["text"] if len(follow.tokens(seg["text"])) >= 6 else " ".join(self._follow_texts)
        idx = self._follower.decide(matcher.scores(spoken), st["slide"], time.monotonic(),
                                    st["changed_at"], st["manual_at"])
        if idx is None:
            return
        await self.stage_action("goto", slide=idx, auto=True)
        await self._say(f"Theo lời trình bày: slide {idx + 1} - {slides[idx]['title']}.", quiet=True)

    async def _progress(self, text: str, kind: str = "progress"):
        """Trợ lý báo tiến độ bằng lời trong lúc xử lý (không kết thúc lượt trả lời)."""
        await self.emit({"type": "ai_progress", "text": text, "kind": kind})

    def _stage_summary(self) -> Optional[Dict[str, Any]]:
        art = self._art_cache.get(self.stage["artifact_id"]) if self.stage["artifact_id"] is not None else None
        return {"kind": art.get("kind"), "title": art.get("title", "")} if art else None

    async def _say(self, text: str, mood: str = "happy", quiet: bool = False):
        """quiet: chỉ hiện phụ đề, không đọc to (xác nhận chuyển slide: màn hình đổi là đủ, không cắt lời người nói)."""
        await self.emit({"type": "ai_say", "text": text, "mood": mood, "quiet": quiet})

    async def _handle_stage_intent(self, intent: Dict[str, Any]) -> bool:
        """Thực hiện lệnh trình chiếu ngay (không gọi LLM). Trả về False nếu nên để LLM xử lý."""
        action = intent["action"]
        if action in ("open", "close"):
            await self.emit({"type": "stage_command", "action": action})
            await self._say("Dạ, em mở màn hình trình bày đây." if action == "open" else "Đã thu nhỏ màn hình trình bày.")
            return True
        if action == "prompt":
            await self.emit({"type": "stage_prompt"})
            return True
        if action == "open_file":
            return await self._open_deck_file(intent.get("query", ""))
        if action == "cancel":
            me = asyncio.current_task()
            running = [t for t in list(self._ai_tasks) if t is not me and not t.done()]
            for t in running:
                t.cancel()
            await self.emit({"type": "ai_cancelled", "count": len(running)})
            await self._say("Dạ, em dừng lại ạ." if running else "Dạ, em không làm nữa ạ.", quiet=True)
            return True
        if action in ("latest", "version"):
            return await self._show_saved(intent)
        if action == "topic":
            found = await self._find_topic(intent.get("query", ""))
            if found is None:
                if self.stage["artifact_id"] is None and not await self._decks():
                    return False                          # chưa có slide nào: để trợ lý hiểu câu này
                await self._say(f"Em chưa thấy slide nào nói về {intent.get('query', '')}.", "concerned")
                return True
            aid, idx = found
            same_deck = aid == self.stage["artifact_id"]
            await self.emit({"type": "stage_command", "action": "open"})
            if same_deck:
                await self.stage_action("goto", slide=idx)
            else:
                await self.stage_action("show", artifact_id=aid, slide=idx)
            art = await self._get_artifact(aid)
            sl = self._slides_of(art)
            where = "" if same_deck else f", trong bộ \"{(art or {}).get('title', '').replace('Slide: ', '')}\""
            await self._say(f"Slide {idx + 1} trên {len(sl)}: {sl[idx]['title']}{where}.", quiet=True)
            return True
        if action == "web_search":
            return await self._web_research(intent.get("query", ""))
        if action == "explain":                       # "giải thích nhánh nạp voucher" khi đang chiếu sơ đồ
            art = await self._get_artifact(self.stage["artifact_id"])
            if not art or art.get("kind") != "diagram":
                return False                          # không có sơ đồ trên màn hình: để trợ lý hiểu câu này
            found = self._find_node(artifacts.load_diagram(art.get("content", "")), intent.get("query", ""))
            if found is None or found[1] < (0.6 if intent.get("loose") else 0.34):
                return False
            await self.emit({"type": "stage_command", "action": "open"})
            await self.explain_node(art["id"], node=found[0]["key"], label=found[0]["label"])
            return True
        if action == "back" and (intent.get("kind") or intent.get("query") or intent.get("first") or intent.get("past")):
            return await self._recall(intent)
        if action == "back" and not self.stage["history"] and await self._recall({"action": "back", "past": True}):
            return True                               # chưa có lịch sử (cuộc họp cũ): mở nội dung gần nhất khác
        if action in ("present", "present_stop"):
            if action == "present_stop":
                await self.emit({"type": "stage_present", "action": "stop"})
                await self._say("Dạ, em dừng thuyết trình.", quiet=True)
                return True
            rc = intent.get("recall")
            if rc:                                      # "thuyết trình lại cái slide hồi nãy": mở lại bộ đó trước
                cur = await self._get_artifact(self.stage["artifact_id"])
                if not cur or cur.get("kind") != rc.get("kind"):   # đang chiếu đúng loại đó thì trình bày luôn cái đang chiếu
                    await self._recall(rc, announce=False)
            art = await self._get_artifact(self.stage["artifact_id"])
            want = intent.get("kind")
            if want in ("diagram", "dashboard") and (not art or art.get("kind") != want):
                same = [a for a in await asyncio.to_thread(db.get_artifacts, self.id) if a.get("kind") == want]
                if not same:          # "thuyết trình sơ đồ" mà chưa có sơ đồ nào
                    await self._say(f"Chưa có {llm.KIND_NAMES.get(want, want)} nào để em thuyết trình. Anh chị nhờ em "
                                    f"{'vẽ sơ đồ' if want == 'diagram' else 'dựng dashboard'} trước nhé.", "concerned")
                    return True
                art = max(same, key=lambda a: (a.get("created_at") or 0, a.get("id") or 0))
            if art and art.get("kind") in ("diagram", "dashboard"):   # sơ đồ, dashboard: đi qua từng phần, phần đó sáng lên
                await self.present_artifact(art)
                return True
            if not self._slides_of(art):
                decks = [a for a in await asyncio.to_thread(db.get_artifacts, self.id) if a.get("kind") == "slides"]
                if not decks:
                    await self._say("Chưa có bộ slide nào để em thuyết trình. Anh chị nhờ em soạn slide trước nhé.", "concerned")
                    return True
                await self.stage_action("show", artifact_id=decks[0]["id"])
                art = await self._get_artifact(self.stage["artifact_id"])
            await self.emit({"type": "stage_command", "action": "open"})
            if intent.get("slide") is not None:       # "thuyết trình slide 9", "từ slide 9": tới đúng slide đó trước
                n = len(self._slides_of(art))
                want_slide = max(0, min(int(intent["slide"]), n - 1))
                if self.stage["slide"] != want_slide:
                    await self.stage_action("goto", slide=want_slide)
            only = intent.get("scope") == "one"      # "thuyết trình slide này": chỉ trình bày một slide rồi dừng
            scope = {"slide": self.stage["slide"], "only": only} if only or intent.get("slide") is not None else None
            deck = artifacts.load_deck(art.get("content", "")) or {}
            ans = llm.present_answer(intent.get("text", ""), explicit=True)
            if ans and ans.get("mode") == "auto" and (not artifacts.deck_is_file(deck) or deck.get("script_mode") == "auto"):
                ans = None                            # "tự trình bày" bộ đã có lời / do AI soạn: trình bày như cũ, không soạn lại
            if ans:                                   # "trình bày theo kịch bản trong ghi chú": nói luôn cách trình bày
                self._ask = {"kind": "present_mode", "artifact_id": art["id"], "until": time.monotonic() + ASK_S}
                self._present_scope = scope
                await self.answer_present(ans)
                return True
            if artifacts.deck_is_file(deck) and not deck.get("script_mode"):
                self._present_scope = scope
                await self._ask_present_mode(art)     # tài liệu mở từ máy: hỏi tự trình bày hay theo kịch bản
                return True
            await self._ensure_scripts(art)
            await self.emit({"type": "stage_present", "action": "start", "slide": self.stage["slide"],
                             **({"only": True} if only else {})})
            return True
        if action in ("follow_on", "follow_off"):
            await self.stage_action("follow", follow=action == "follow_on")
            await self._say("Dạ, em sẽ tự chuyển slide theo nội dung anh chị đang trình bày." if action == "follow_on"
                            else "Dạ, em tắt tự chuyển slide, anh chị chuyển bằng lời hoặc phím mũi tên nhé.")
            return True
        if action == "analyze":
            await self.analyze_now()
            return True
        if action == "next" and time.monotonic() - self.stage["auto_at"] < AUTO_CONFIRM_S:
            # Slide vừa tự chuyển theo lời nói; người trình bày bảo "chuyển slide" là muốn đúng slide này
            self.stage["auto_at"] = 0.0
            self.stage["manual_at"] = time.monotonic()
            slides = self._slides_of(await self._get_artifact(self.stage["artifact_id"]))
            if slides:
                i = self.stage["slide"]
                await self._say(f"Đang ở slide {i + 1}: {slides[i]['title']} rồi ạ.", quiet=True)
                return True
        if self.stage["artifact_id"] is None:
            if action == "topic":
                return False
            await self._say("Hiện chưa có nội dung nào trên màn hình trình bày. Anh chị có thể nhờ em soạn slide hoặc dựng dashboard.", "concerned")
            return True
        if action == "back" and not self.stage["history"]:
            await self._say("Không còn phần trình bày nào trước đó.", "concerned")
            return True
        if action == "topic":
            slides = self._slides_of(await self._get_artifact(self.stage["artifact_id"]))
            idx = self._find_slide(slides, intent.get("query", ""))
            if idx is None:
                if not slides:
                    return False
                await self._say(f"Em chưa thấy slide nào nói về {intent.get('query', '')}.", "concerned")
                return True
            await self.stage_action("goto", slide=idx)
        else:
            await self.stage_action(action, slide=intent.get("slide"))
        art = await self._get_artifact(self.stage["artifact_id"])
        slides = self._slides_of(art)
        if slides:
            i = self.stage["slide"]
            await self._say(f"Slide {i + 1} trên {len(slides)}: {slides[i]['title']}.", quiet=True)
        else:
            await self._say(f"Đang trình bày {art['title'] if art else 'nội dung trước'}.", quiet=True)
        return True

    async def _ensure_scripts(self, art: Optional[Dict[str, Any]]):
        """Bộ slide chưa có lời thuyết trình chi tiết (nhập từ tệp, bản cũ): nhờ LLM viết rồi lưu thành bản mới."""
        deck = artifacts.normalize_deck(artifacts._json_from_text((art or {}).get("content", ""))) if art else None
        if not deck or artifacts.deck_has_scripts(deck) or not artifacts.llm_available():
            return
        artifacts.set_meeting(self.id, "lời thuyết trình")
        await self._progress("Em soạn lời thuyết trình chi tiết cho từng slide trước, khoảng nửa phút ạ.", "status")
        try:
            new_deck = await artifacts.generate_scripts(deck, llm._context_lines(self.segments))
        except Exception as e:
            log.warning("meeting.live: viết lời thuyết trình lỗi: %s", e)
            return
        aid = await asyncio.to_thread(db.save_artifact, self.id, "slides", art["title"],
                                      json.dumps(new_deck, ensure_ascii=False), "lời thuyết trình", art["id"])
        new_art = await asyncio.to_thread(db.get_artifact, aid)
        self._art_cache[aid] = new_art
        await self.emit({"type": "artifact_updated", "artifact": new_art})
        await self.stage_action("show", artifact_id=aid, slide=self.stage["slide"])

    # ------------------------------------------------------- thuyết trình sơ đồ, dashboard ---
    async def present_artifact(self, art: Dict[str, Any]):
        """Đưa sơ đồ / dashboard lên màn hình, soạn lời thuyết trình nếu chưa có, rồi trình bày từng phần."""
        await self.emit({"type": "stage_command", "action": "open"})
        if self.stage["artifact_id"] != art["id"]:
            await self.stage_action("show", artifact_id=art["id"])
        art = await self._ensure_walkthrough(art)
        await self.emit({"type": "stage_present", "action": "start", "slide": 0, "artifact_id": art["id"]})

    async def _ensure_walkthrough(self, art: Dict[str, Any]) -> Dict[str, Any]:
        """Sơ đồ / dashboard chưa có lời thuyết trình: nhờ AI soạn theo cuộc họp rồi lưu thành bản mới.
        Không có AI thì trang tự đọc theo cấu trúc (nhánh, biểu đồ, điểm chính)."""
        if artifacts.has_walkthrough(art) or not artifacts.llm_available():
            return art
        name = "sơ đồ" if art.get("kind") == "diagram" else "dashboard"
        await self._progress(f"Dạ, em soạn lời trình bày cho {name} này trước, chờ em chút ạ.", "status")
        artifacts.set_meeting(self.id, "lời thuyết trình")
        try:
            obj = await artifacts.generate_walkthrough(art, llm._context_lines(self.segments))
        except Exception as e:
            log.warning("meeting.live: soạn lời thuyết trình %s lỗi: %s", name, e)
            return art
        aid = await asyncio.to_thread(db.save_artifact, self.id, art["kind"], art["title"],
                                      artifacts.dump_content(art["kind"], obj), "lời thuyết trình", art["id"])
        new_art = await asyncio.to_thread(db.get_artifact, aid)
        self._art_cache[aid] = new_art
        await self.emit({"type": "artifact_updated", "artifact": new_art})
        await self.stage_action("show", artifact_id=aid)
        return new_art

    # ------------------------------------------------------- giải thích một ý trong sơ đồ ---
    @staticmethod
    def _find_node(dg: Dict[str, Any], query: str) -> Optional[Tuple[Dict[str, Any], float]]:
        """Ý của sơ đồ khớp nhất với câu nói ("nhánh nạp voucher") và mức khớp 0-1."""
        if dg.get("type") == "mindmap":
            cands = [(e["node"].get("id"), e["node"]["label"], e["node"].get("detail", ""))
                     for e in artifacts.mindmap_index(dg).values()]
        else:
            cands = [("", lab, "") for lab in artifacts.mermaid_labels(dg.get("code", ""))]
        best, best_key = None, (0.0, 0)
        for nid, label, detail in cands:
            s1, h1 = llm.topic_score(query, label)
            s2, h2 = llm.topic_score(query, f"{label} {detail}")
            key = (max(s1, s2 * 0.8), max(h1, h2))
            if key > best_key:
                best, best_key = (nid, label), key
        if best is None or best_key[1] == 0:
            return None
        picked = artifacts.diagram_node(dg, node=best[0] or "", label=best[1])
        return (picked, best_key[0]) if picked else None

    async def explain_node(self, aid: int, node: str = "", label: str = "", req: str = "") -> Dict[str, Any]:
        """Người dùng bấm vào một ý của sơ đồ (hoặc nói "giải thích nhánh X"): trợ lý giải thích bằng giọng nói,
        bám nội dung cuộc họp. Lời giải thích nhớ theo (sơ đồ, ý) để bấm lại không gọi AI."""
        art = await self._get_artifact(aid)
        if not art or art.get("kind") != "diagram":
            raise KeyError(f"Không tìm thấy sơ đồ {aid}")
        dg = artifacts.load_diagram(art.get("content", ""))
        picked = artifacts.diagram_node(dg, node=node, label=label)
        if picked is None:
            raise KeyError("Không tìm thấy ý này trong sơ đồ")
        base = {"artifact_id": aid, "node": picked["key"], "label": picked["label"], "path": picked["path"], "req": req}
        await self.emit({"type": "node_explaining", **base})
        ck = (aid, picked["key"])
        text, cached = self._explain_cache.get(ck), True
        if text is None:
            cached = False
            if artifacts.llm_available():
                artifacts.set_meeting(self.id, "giải thích sơ đồ")
                try:
                    text = await artifacts.explain_node(dg, picked, llm._context_lines(self.segments, max_chars=12000))
                except Exception as e:
                    log.warning("meeting.live: giải thích ý '%s' lỗi: %s", picked["label"], e)
            if not text:     # không có AI hoặc AI lỗi: đọc ghi chú có sẵn trong sơ đồ
                text = picked.get("detail") or (f"{picked['label']}. Ý này gồm: {', '.join(picked['children'])}."
                                                 if picked.get("children") else f"{picked['label']}.")
            else:
                self._explain_cache[ck] = text
        out = {**base, "text": text, "cached": cached}
        await self.emit({"type": "node_explained", **out})
        return out

    async def _web_research(self, query: str) -> bool:
        """"Search giúp anh giá vàng hôm nay": tìm trên web, lưu thành báo cáo có nguồn, chiếu lên màn hình và đọc kết luận."""
        await self._progress(f"Dạ, em tra cứu trên mạng về {query} ngay ạ.", "ack")
        try:
            art = await artifacts.web_research(self.id, query, llm._context_lines(self.segments, n=30),
                                               on_progress=self._progress)
        except Exception as e:
            log.warning("meeting.live: tra cứu web lỗi: %s", e)
            await self._say(f"Em chưa tra cứu được: {e}", "concerned")
            return True
        self._art_cache[art["id"]] = art
        await self.emit({"type": "artifact_created", "artifact": {k: v for k, v in art.items() if k != "sources"}})
        await self.emit({"type": "stage_command", "action": "open"})
        await self.stage_action("show", artifact_id=art["id"])
        body = re.sub(r"\[\d+(?:\s*[,-]\s*\d+)*\]", "", art["content"])          # bỏ số nguồn [1] khi đọc to
        body = re.sub(r"^#.*$", "", body, flags=re.M)
        body = body.split("## Chi tiết")[0]
        summary = re.sub(r"\s+", " ", re.sub(r"[*_`#\[\]()]", " ", body)).strip()
        sents = re.split(r"(?<=[.!?])\s+", summary)
        await self._say(" ".join(sents[:3])[:420] or "Em đã tra cứu xong, kết quả đang hiện trên màn hình.",
                        "happy")
        return True

    async def _saved(self, kind: str) -> List[Dict[str, Any]]:
        """Sản phẩm đã lưu của cuộc họp theo loại, mới nhất trước (cùng thời điểm thì mã lớn hơn là mới hơn)."""
        arts = [a for a in await asyncio.to_thread(db.get_artifacts, self.id) if a.get("kind") == kind]
        return sorted(arts, key=lambda a: (a.get("created_at") or 0, a.get("id") or 0), reverse=True)

    async def _decks(self) -> List[Dict[str, Any]]:
        return await self._saved("slides")

    async def _find_topic(self, query: str) -> Optional[Tuple[int, int]]:
        """Slide khớp chủ đề: bộ đang chiếu trước, sau đó các bộ khác (mới nhất trước). Trả về (artifact_id, slide)."""
        cur = await self._get_artifact(self.stage["artifact_id"]) if self.stage["artifact_id"] is not None else None
        order = ([cur] if cur and self._slides_of(cur) else []) + \
                [a for a in await self._decks() if not cur or a["id"] != cur["id"]]
        for art in order:
            idx = self._find_slide(self._slides_of(art), query)
            if idx is not None:
                return art["id"], idx
        return None

    async def _show_saved(self, intent: Dict[str, Any]) -> bool:
        """"Slide gần nhất em vừa tạo" / "slide v1": mở lại sản phẩm đã có, không tạo mới."""
        kind = intent.get("kind")
        cur = await self._get_artifact(self.stage["artifact_id"]) if self.stage["artifact_id"] is not None else None
        kind = kind or (cur or {}).get("kind") or "slides"
        name = llm.KIND_NAMES.get(kind, kind)
        arts = await self._saved(kind)
        if not arts:
            await self._say(f"Em chưa tạo {name} nào trong cuộc họp này.", "concerned")
            return True
        target = arts[0]
        if intent["action"] == "version":
            base = cur if cur and cur.get("kind") == kind else arts[0]
            same = [a for a in arts if a.get("title") == base.get("title")]
            target = next((a for a in same if int(a.get("version") or 1) == intent["n"]), None)
            if target is None:
                vs = ", ".join(f"v{v}" for v in sorted({int(a.get("version") or 1) for a in same}))
                await self._say(f"\"{base.get('title', '')}\" chỉ có bản {vs}.", "concerned")
                return True
        await self.emit({"type": "stage_command", "action": "open"})
        await self.stage_action("show", artifact_id=target["id"])
        ver = f" bản v{int(target.get('version') or 1)}" if intent["action"] == "version" else ""
        await self._say(f"Dạ, em mở lại {name} \"{target.get('title', '')}\"{ver}.", quiet=True)
        return True

    # ------------------------------------------------- tài liệu trên máy người dùng (bản web) ---
    CLIENT_FILE_WAIT_S = 90.0      # chờ trình duyệt gửi tệp lên (tệp lớn, mạng chậm)

    def set_client_library(self, files: List[Dict[str, Any]]) -> int:
        """Danh sách tệp trong các thư mục người dùng chọn trên máy (chỉ tên, không có nội dung)."""
        from meeting import decks
        out = []
        for f in files[:5000]:
            ext = str(f.get("ext") or "").lower()
            fid = str(f.get("fid") or "")[:600]
            if not fid or ext not in decks.EXTS:
                continue
            out.append({"fid": fid, "rel": str(f.get("rel") or "")[:600], "root": str(f.get("root") or "")[:120],
                        "name": str(f.get("name") or "")[:200], "ext": ext})
        self.client_files = out
        return len(out)

    def client_mode(self) -> bool:
        """Tài liệu lấy từ máy người dùng (bản web) thay vì thư mục trên máy chạy server."""
        return auth.ENABLED

    async def _client_file(self, entry: Dict[str, Any], purpose: str) -> Optional[str]:
        """Nhờ trình duyệt gửi lên một tệp trong thư mục đã chọn. Trả về đường dẫn tạm trên server, hoặc None nếu trình
        duyệt không gửi được (chưa cho phép đọc thư mục, tệp đã bị xóa, không ai đang mở phòng họp...)."""
        import secrets
        rid = secrets.token_hex(8)
        fut = asyncio.get_running_loop().create_future()
        self._file_waits[rid] = fut
        try:
            await self.emit({"type": "need_file", "request_id": rid, "fid": entry["fid"], "purpose": purpose,
                             "name": f"{entry.get('name', '')}{entry.get('ext', '')}"})
            return await asyncio.wait_for(fut, self.CLIENT_FILE_WAIT_S)
        except (asyncio.TimeoutError, RuntimeError) as e:
            log.info("meeting.live: không nhận được tệp %s từ trình duyệt: %s", entry.get("fid"), e)
            return None
        finally:
            self._file_waits.pop(rid, None)

    def resolve_client_file(self, request_id: str, path: Optional[str] = None, error: str = "") -> bool:
        """Trình duyệt đã gửi tệp (path) hoặc báo không gửi được (error). False nếu không còn ai chờ."""
        fut = self._file_waits.get(request_id)
        if fut is None or fut.done():
            return False
        if path:
            fut.set_result(path)
        else:
            fut.set_exception(RuntimeError(error or "trình duyệt không gửi được tệp"))
        return True

    async def _open_client_file(self, query: str) -> bool:
        from meeting import decks
        if not self.client_files:
            await self._say("Anh chị chưa chọn thư mục tài liệu trên máy. Bấm Mở tài liệu, hoặc vào Cài đặt, Thư mục tài liệu "
                            "để chọn thư mục, rồi gọi lại em nhé.", "concerned")
            return True
        found = decks.rank_entries(query, self.client_files)
        if not found:
            roots = ", ".join(sorted({f["root"] for f in self.client_files if f.get("root")})[:4])
            await self._say(f"Em không thấy tệp nào khớp trong {roots or 'thư mục đã chọn'}. Anh chị bấm Mở tài liệu để chọn "
                            "tệp giúp em.", "concerned")
            return True
        if len(found) > 1 and found[1]["score"] >= found[0]["score"]:
            names = "; ".join(f"{r['name']}{r['ext']} trong {r['folder'] or r['root']}" for r in found[:3])
            await self._say(f"Em thấy nhiều tệp giống nhau: {names}. Anh chị nói rõ tên tệp giúp em.", "concerned")
            return True
        entry = found[0]
        name = f"{entry['name']}{entry['ext']}"
        await self._progress(f"Dạ, em lấy tệp {name} từ máy anh chị.", "status")
        path = await self._client_file(entry, "open")
        if not path:
            await self._say(f"Em chưa lấy được tệp {name} từ máy anh chị. Có thể trình duyệt cần được cho phép đọc thư mục: "
                            "anh chị bấm Mở tài liệu rồi chọn lại tệp giúp em.", "concerned")
            return True
        try:
            return await self.open_path(path, query, display=(name, entry["folder"] or entry["root"]))
        finally:
            decks.discard_upload(path)

    async def _open_deck_file(self, query: str) -> bool:
        """"Mở file báo cáo Q3 trong Downloads": tìm trong các thư mục tài liệu, mở lên màn hình, hỏi cách trình bày."""
        from meeting import decks
        if self.client_mode():
            return await self._open_client_file(query)
        found = await asyncio.to_thread(decks.find_files, query)
        if not found:
            roots = ", ".join(r.name for r in decks.library_roots()) or str(decks.SLIDES_DIR)
            subs = decks.folders(decks.SLIDES_DIR)
            hint = f" Em đã tìm trong {roots}" + (f"; thư mục slide có: {', '.join(subs[:5])}" if subs else "") + "."
            await self._say(f"Em không thấy tệp nào khớp với yêu cầu.{hint} Anh chị có thể bấm Mở tài liệu để chọn tệp.",
                            "concerned")
            return True
        if len(found) > 1 and found[1]["score"] >= found[0]["score"]:
            names = "; ".join(f"{r['name']}{r['ext']} trong {r['folder'] or Path(r['root']).name}" for r in found[:3])
            await self._say(f"Em thấy nhiều tệp giống nhau: {names}. Anh chị nói rõ tên tệp giúp em.", "concerned")
            return True
        return await self.open_path(found[0]["path"], query)

    async def open_path(self, path: str, command: str = "", display: Optional[Tuple[str, str]] = None) -> bool:
        """Mở một tệp trên máy lên màn hình trình chiếu rồi hỏi cách trình bày (hoặc làm luôn nếu câu lệnh đã nói).
        display: (tên tệp, thư mục) để nói với người dùng khi tệp đang nằm ở thư mục tạm (gửi lên từ trình duyệt)."""
        p = Path(path)
        shown, folder = display or (p.name, p.parent.name or p.anchor)
        if p.suffix.lower() in (".pdf", ".pptx", ".ppt"):
            await self._progress(f"Dạ, em mở {shown}, đang dựng hình từng trang ạ.", "status")
        try:
            art = await asyncio.to_thread(artifacts.import_deck, self.id, str(p))
        except Exception as e:
            await self._say(f"Em không đọc được tệp {shown}: {e}", "concerned")
            return True
        self._art_cache[art["id"]] = art
        await self.emit({"type": "artifact_created", "artifact": art})
        await self.emit({"type": "stage_command", "action": "open"})
        await self.stage_action("show", artifact_id=art["id"])
        deck = artifacts.load_deck(art.get("content", "")) or {"slides": []}
        n = len(deck["slides"])
        unit = "trang" if p.suffix.lower() in (".pdf", ".docx") else "slide"
        where = f" trong thư mục {folder}" if folder else ""
        note = ""
        if (deck.get("source") or {}).get("exact") is False:     # PowerPoint chưa dựng được hình đúng thiết kế
            from meeting import decks
            note = (" Em chưa dựng được hình đúng thiết kế của tệp này nên đang hiện chữ và hình theo bố cục của ứng dụng; "
                    + ("anh chị thử mở lại tệp, hoặc lưu thêm bản PDF (File > Save As > PDF) rồi mở bản PDF."
                       if decks._soffice() else
                       "máy chạy Copilot cần cài LibreOffice (miễn phí) để hiện đúng thiết kế, hoặc anh chị lưu thêm bản "
                       "PDF (File > Save As > PDF) rồi mở bản PDF."))
        await self._say(f"Dạ, em mở \"{shown}\"{where}, gồm {n} {unit}.{note}", "concerned" if note else "happy",
                        quiet=True)
        ans = llm.present_answer(command, explicit=True) if command else None
        self._present_scope = None                    # tệp vừa mở: trình bày từ trang đầu
        if ans:
            self._ask = {"kind": "present_mode", "artifact_id": art["id"], "until": time.monotonic() + ASK_S}
            await self.answer_present(ans)
        else:
            await self._ask_present_mode(art)
        return True

    # ------------------------------------------------------------ cách trình bày tài liệu ---
    async def _ask_present_mode(self, art: Dict[str, Any]):
        deck = artifacts.load_deck(art.get("content", "")) or {"slides": []}
        has_notes = sum(1 for s in deck["slides"] if (s.get("notes") or "").strip())
        self._ask = {"kind": "present_mode", "artifact_id": art["id"], "until": time.monotonic() + ASK_S}
        q = ("Anh chị muốn em tự trình bày hay trình bày theo kịch bản ạ? Nếu theo kịch bản thì anh chị chỉ em kịch bản "
             "ở đâu để em trình bày.")
        if has_notes:
            q += f" Tệp này có sẵn ghi chú ở {has_notes} slide, em có thể đọc theo ghi chú đó."
        await self.emit({"type": "ai_ask", "kind": "present_mode", "artifact_id": art["id"], "text": q,
                         "title": art.get("title", ""), "has_notes": bool(has_notes)})
        await self._say(q)
        self._open_answer_window()

    def _open_answer_window(self, seconds: float = ANSWER_WINDOW_S):
        """Câu tiếp theo (không cần gọi tên) được hiểu là câu trả lời nếu đúng là câu trả lời."""
        token = object()
        self._pending_wake = {"name": llm.assistant_config()["name"], "until": time.monotonic() + seconds,
                              "token": token, "answer": True}
        asyncio.create_task(self._expire_wake(token))

    async def answer_present(self, ans: Dict[str, Any]) -> Dict[str, Any]:
        """Thực hiện câu trả lời: tự trình bày (AI viết lời) hoặc theo kịch bản (ghi chú, tệp khác, văn bản dán vào)."""
        from meeting import decks
        aid = (self._ask or {}).get("artifact_id") or ans.get("artifact_id") or self.stage["artifact_id"]
        art = await self._get_artifact(aid)
        deck = artifacts.load_deck((art or {}).get("content", ""))
        if not art or not deck:
            await self._say("Chưa có tài liệu nào trên màn hình để em trình bày.", "concerned")
            return {"ok": False}
        n = len(deck["slides"])
        if ans.get("mode") == "human":                # người trong phòng tự trình bày: em chỉ chuyển slide theo lời
            self._present_scope = None
            await self._ask_done()
            await self.stage_action("follow", follow=True)
            await self._say("Dạ, anh chị trình bày nhé. Em sẽ tự chuyển slide theo nội dung anh chị đang nói.", quiet=True)
            return {"ok": True, "mode": "human"}
        if ans.get("mode") == "auto":
            await self._ask_done()
            if not artifacts.llm_available():
                await self._say("Em chưa kết nối được AI để tự viết lời trình bày. Anh chị chỉ em kịch bản nhé.", "concerned")
                return {"ok": False}
            artifacts.set_meeting(self.id, "lời thuyết trình")
            await self._progress(f"Dạ, em tự trình bày. Em soạn lời cho {n} slide trước, chờ em chút ạ.", "ack")
            try:
                new_deck = await artifacts.generate_scripts(deck, llm._context_lines(self.segments))
            except Exception as e:
                await self._say(f"Em chưa soạn được lời trình bày: {e}", "concerned")
                return {"ok": False}
            return await self._present_with(art, new_deck, "Em bắt đầu trình bày ạ.")
        source = ans.get("source")
        scripts, label = None, ""
        if source == "notes":
            scripts = [s.get("notes", "") for s in deck["slides"]]
            label = "ghi chú trong tệp"
            if not any(x.strip() for x in scripts):
                await self._ask_where("Tệp này chưa có ghi chú cho slide nào. Anh chị chỉ em tệp kịch bản, hoặc dán kịch bản vào khung chat nhé.")
                return {"ok": False, "need": "where"}
        elif source in ("file", "text"):
            text = ans.get("text") or ""
            if source == "file":
                path, shown, temp = ans.get("path"), "", False
                if not path and self.client_mode():
                    found = decks.rank_entries(ans.get("query", ""), self.client_files)
                    if not found:
                        await self._ask_where("Em không thấy tệp kịch bản nào khớp trong thư mục đã chọn. Anh chị nói lại tên "
                                              "tệp, hoặc bấm Chọn tệp kịch bản giúp em.")
                        return {"ok": False, "need": "where"}
                    shown = f"{found[0]['name']}{found[0]['ext']}"
                    path = await self._client_file(found[0], "script")
                    if not path:
                        await self._ask_where(f"Em chưa lấy được tệp {shown} từ máy anh chị. Anh chị bấm Chọn tệp kịch bản "
                                              "giúp em.")
                        return {"ok": False, "need": "where"}
                    temp = True
                elif not path:
                    exclude = (deck.get("source") or {}).get("path")
                    found = await asyncio.to_thread(decks.find_files, ans.get("query", ""), None, exclude)
                    if not found:
                        await self._ask_where("Em không thấy tệp kịch bản nào khớp. Anh chị nói lại tên tệp, hoặc bấm Chọn tệp kịch bản giúp em.")
                        return {"ok": False, "need": "where"}
                    path = found[0]["path"]
                try:
                    text = await asyncio.to_thread(decks.read_text, path)
                except Exception as e:
                    await self._ask_where(f"Em không đọc được tệp kịch bản: {e}")
                    return {"ok": False, "need": "where"}
                finally:
                    if temp:
                        decks.discard_upload(path)
                label = f"tệp {shown or Path(path).name}"
            else:
                label = f"tệp {ans['name']}" if ans.get("name") else "kịch bản anh chị dán vào"
            if not text.strip():
                await self._ask_where("Kịch bản đang trống. Anh chị gửi lại giúp em nhé.")
                return {"ok": False, "need": "where"}
            scripts = decks.split_script(text, n)
            if scripts is None:
                if not artifacts.llm_available():
                    scripts = [text] + [""] * (n - 1)
                else:
                    await self._progress("Em đang ghép kịch bản với từng slide, giữ nguyên câu chữ ạ.", "status")
                    artifacts.set_meeting(self.id, "ghép kịch bản")
                    try:
                        scripts = await artifacts.align_script(text, deck)
                    except Exception as e:
                        await self._say(f"Em chưa ghép được kịch bản: {e}", "concerned")
                        return {"ok": False}
        else:
            await self._ask_where("Dạ, kịch bản nằm ở đâu ạ? Trong phần ghi chú của tệp, một tệp khác (anh chị nói tên tệp), "
                                  "hay anh chị dán vào khung chat?")
            return {"ok": False, "need": "where"}
        await self._ask_done()
        new_deck = artifacts.deck_with_scripts(deck, scripts, "script", label)
        filled = sum(1 for x in scripts if (x or "").strip())
        tail = "" if filled == n else f" Có {n - filled} slide chưa có lời trong kịch bản, em sẽ đọc nội dung trên slide."
        return await self._present_with(art, new_deck, f"Dạ, em trình bày theo {label}.{tail}")

    async def _ask_done(self):
        """Đã có câu trả lời: đóng câu hỏi (cả thẻ hỏi trên màn hình)."""
        if self._ask is not None:
            self._ask = None
            await self.emit({"type": "ai_ask_done"})

    async def _ask_where(self, text: str):
        if self._ask is None:
            self._ask = {"kind": "present_mode", "artifact_id": self.stage["artifact_id"], "until": time.monotonic() + ASK_S}
        self._ask["until"] = time.monotonic() + ASK_S
        await self.emit({"type": "ai_ask", "kind": "script_where", "artifact_id": self._ask["artifact_id"], "text": text})
        await self._say(text, "concerned")
        self._open_answer_window()

    async def _present_with(self, art: Dict[str, Any], new_deck: Dict[str, Any], message: str) -> Dict[str, Any]:
        aid = await asyncio.to_thread(db.save_artifact, self.id, "slides", art["title"],
                                      json.dumps(new_deck, ensure_ascii=False), "cách trình bày", art["id"])
        new_art = await asyncio.to_thread(db.get_artifact, aid)
        self._art_cache[aid] = new_art
        await self.emit({"type": "artifact_updated", "artifact": new_art})
        await self.emit({"type": "stage_command", "action": "open"})
        scope, self._present_scope = self._present_scope, None      # "thuyết trình slide này" trước khi được hỏi
        n = max(1, len(new_deck.get("slides") or []))
        start = min(max(int(scope["slide"]), 0), n - 1) if scope else 0
        await self.stage_action("show", artifact_id=aid, slide=start)
        await self._say(message, quiet=True)
        await self.emit({"type": "stage_present", "action": "start", "slide": start,
                         **({"only": True} if scope and scope.get("only") else {})})
        return {"ok": True, "artifact_id": aid}

    async def _edit_on_stage(self, command: str):
        """Sửa nội dung đang trình chiếu bằng lời nói ("sửa slide này thêm số liệu doanh thu")."""
        st = self.stage
        await self.emit({"type": "ai_thinking", "text": "Em đang sửa nội dung đang trình chiếu..."})
        try:
            res = await artifacts.co_design_refine(self.id, st["artifact_id"], command, slide_index=st["slide"])
        except Exception as e:
            await self.emit({"type": "ai_error", "text": f"Em chưa sửa được: {e}"})
            return
        self._art_cache[res["id"]] = res
        message = res.pop("chat_message", "") or "Em đã sửa xong."
        await self.emit({"type": "artifact_updated", "artifact": res})
        await self.stage_action("show", artifact_id=res["id"], slide=res.get("focus_slide", st["slide"]))
        await self._say(message)

    async def analyze_now(self, focus: str = "") -> List[Dict[str, str]]:
        """Trợ lý xem lại cuộc họp và nêu nhận xét (rủi ro, việc chưa có người nhận, điểm cần cải thiện)."""
        artifacts.set_meeting(self.id, "nhận xét")
        await self.emit({"type": "ai_thinking", "text": "Em đang xem lại toàn bộ cuộc họp..."})
        items = await artifacts.meeting_insights(self.segments, self.meeting, focus)
        await self.emit({"type": "ai_insights", "items": items, "source": "analysis"})
        return items

    # --------------------------------------------------------- lifecycle ---
    def _capturing(self) -> bool:
        """Có nguồn âm thanh đang chạy (mic / extension thu tiếng tab / bot Meet): chỉ tự kết thúc phòng đang thực sự ghi."""
        return (self.audio_owner is not None or bool(self.meet_bot)
                or any(not s.closed for s in self.streams.values()))

    async def _idle_watch(self):
        """Mở quá IDLE_END_S mà chưa ai nói câu nào -> tự kết thúc cuộc họp và lập recap. Chỉ với phòng đang được ghi
        (không phải cuộc họp riêng nhận lời thoại từ bot dùng chung: phòng gốc tự lo)."""
        if IDLE_END_S <= 0:
            return
        try:
            while not self.disposed:
                await asyncio.sleep(IDLE_CHECK_S)
                if (self.is_live() and self.copy_of is None and not self.segments and self._speech_at == 0.0
                        and self._capturing() and self.clock() >= IDLE_END_S):
                    await self._auto_finish()
                    return
        except asyncio.CancelledError:
            pass

    async def _auto_finish(self):
        mins = int(self.clock() // 60)
        log.info("meeting.live: cuộc họp %d không có lời nói %d phút -> tự kết thúc + recap", self.id, mins)
        await self._say(f"Đã khoảng {mins} phút không có ai nói, em xin kết thúc cuộc họp và lưu lại recap ạ.", quiet=True)
        try:
            art = await asyncio.to_thread(artifacts.empty_recap, self.id, self.title, self.meeting, float(mins))
            self._art_cache[art["id"]] = art
            await self.emit({"type": "artifact_created", "artifact": art})
        except Exception as e:
            log.warning("meeting.live: tạo recap tự động lỗi: %s", e)
        await self.finish(generate_minutes=False)

    async def finish(self, generate_minutes: bool = True) -> Dict[str, Any]:
        """Kết thúc cuộc họp: chốt chữ cuối, lưu trạng thái 'ended', lập biên bản ở chế độ nền."""
        for s in list(self.streams.values()):
            await s.close()
        self.audio_owner = None
        await self.drain()
        await asyncio.to_thread(db.end_meeting, self.id)
        fresh = await asyncio.to_thread(db.get_meeting, self.id)
        if fresh:
            self.meeting = fresh
        else:
            self.meeting["status"] = "ended"
        has_minutes = any(a.get("kind") == "minutes" for a in await asyncio.to_thread(db.get_artifacts, self.id))
        minutes_status = "pending" if (generate_minutes and self.segments and not has_minutes
                                       and artifacts.llm_available()) else None
        if minutes_status:
            self.meeting["minutes_status"] = minutes_status
            await asyncio.to_thread(db.update_meeting, self.id, {"minutes_status": "pending"}, True)
        await self.emit({"type": "meeting_status", "status": "ended", "meeting": self.meeting,
                         "minutes_status": minutes_status})
        if minutes_status:
            asyncio.create_task(self._finalize_minutes())
        else:
            self.names_final.set()
            self.dispose_if_idle()
        return {"status": "ended", "minutes_status": minutes_status}

    async def _finalize_minutes(self):
        status = "failed"
        artifacts.set_meeting(self.id, "biên bản")
        try:
            if self.copy_of is not None:
                await self._wait_source_names()   # tên người nói theo bên nhận âm thanh (đã chốt lúc kết thúc)
            if self.copy_of is None or len(self._src_map) < len(self.segments):
                try:
                    await self.identity.run(force=True)
                except Exception as e:
                    log.warning("meeting.live: đoán tên lần cuối lỗi: %s", e)
            self.names_final.set()
            if self.subscribers:
                try:   # nhận xét nhanh để trợ lý nói trong lúc chờ lập biên bản (không ai mở phòng thì thôi)
                    items = await artifacts.meeting_insights(self.segments, self.meeting)
                    if items:
                        await self.emit({"type": "ai_insights", "items": items, "source": "meeting_end"})
                except Exception as e:
                    log.warning("meeting.live: nhận xét cuối buổi lỗi: %s", e)
            # shared: cuộc họp riêng của nhiều người (bot Meet dùng chung) cùng lời thoại, cùng mẫu thì lập một lần
            art = await artifacts.generate_meeting_minutes(self.id, self.segments, self.title,
                                                           meeting=self.meeting, speakers=self.public_speakers(),
                                                           shared=True)
            status = "done"
            await self.emit({"type": "artifact_created", "artifact": art})
            await self.stage_action("show", artifact_id=art["id"])
            from meeting import drive_sync
            drive_sync.schedule(self.id)                 # bản web: lưu biên bản + ghi âm lên Drive của nhóm (chạy nền)
        except Exception as e:
            log.warning("meeting.live: lập biên bản lỗi: %s", e)
            await self.emit({"type": "error", "text": f"Không lập được biên bản tự động: {e}"})
        finally:
            self.names_final.set()
            self.meeting["minutes_status"] = status
            await asyncio.to_thread(db.update_meeting, self.id, {"minutes_status": status}, True)
            await self.emit({"type": "meeting_status", "status": "ended", "meeting": self.meeting,
                             "minutes_status": status})
            self.dispose_if_idle()

    async def regenerate_minutes(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """Lập lại biên bản bằng một mẫu khác (chạy nền như lúc kết thúc cuộc họp, ra phiên bản mới của biên bản)."""
        if self.meeting.get("minutes_status") == "pending":
            raise ValueError("Biên bản đang được lập, anh chị chờ xong rồi lập lại")
        if not self.segments:
            raise ValueError("Cuộc họp chưa có lời nói nào để lập biên bản")
        self.meeting["minutes_status"] = "pending"
        await asyncio.to_thread(db.update_meeting, self.id, {"minutes_status": "pending"}, True)
        await self.emit({"type": "meeting_status", "status": self.meeting.get("status"), "meeting": self.meeting,
                         "minutes_status": "pending"})
        asyncio.create_task(self._regenerate(template))
        return {"minutes_status": "pending", "template": template.get("name")}

    async def _regenerate(self, template: Dict[str, Any]):
        status = "failed"
        try:
            art = await artifacts.generate_meeting_minutes(self.id, self.segments, self.title, meeting=self.meeting,
                                                           speakers=self.public_speakers(), template=template)
            status = "done"
            await self.emit({"type": "artifact_created", "artifact": art})
            from meeting import drive_sync
            drive_sync.schedule(self.id)
        except Exception as e:
            log.warning("meeting.live: lập lại biên bản lỗi: %s", e)
            await self.emit({"type": "error", "text": f"Không lập lại được biên bản: {e}"})
        finally:
            self.meeting["minutes_status"] = status
            await asyncio.to_thread(db.update_meeting, self.id, {"minutes_status": status}, True)
            await self.emit({"type": "meeting_status", "status": self.meeting.get("status"), "meeting": self.meeting,
                             "minutes_status": status})
            self.dispose_if_idle()

    def dispose_if_idle(self):
        if self.is_live() or self.subscribers or self.audio_owner is not None:
            return
        if self.meeting.get("minutes_status") == "pending":
            return
        if not self._queue.empty() or not self._db_queue.empty():
            return
        self.dispose()

    def dispose(self):
        self.disposed = True
        if self.bd is not None:
            self.bd.close()
        if self.game is not None and self.game.task is not None and not self.game.task.done():
            self.game.task.cancel()
        for t in self._workers:
            t.cancel()
        self._workers = []
        if SESSIONS.get(self.id) is self:
            SESSIONS.pop(self.id, None)
        log.info("meeting.live: giải phóng session %d", self.id)

    async def close(self):
        """Dừng toàn bộ stream và giải phóng session (dùng khi xóa cuộc họp)."""
        for s in list(self.streams.values()):
            await s.close()
        self.streams.clear()
        self.dispose()


async def get_session(mid: int, create: bool = True) -> Optional[MeetingSession]:
    """Lấy session đang chạy hoặc nạp lại từ DB (an toàn khi nhiều kết nối mở cùng lúc)."""
    s = SESSIONS.get(mid)
    if s is not None and not s.disposed:
        return s
    if not create:
        return None
    lock = _session_locks.setdefault(mid, asyncio.Lock())
    async with lock:
        s = SESSIONS.get(mid)
        if s is not None and not s.disposed:
            return s
        meeting = await asyncio.to_thread(db.get_meeting, mid)
        if not meeting:
            return None
        s = await asyncio.to_thread(MeetingSession.from_db, meeting)
        SESSIONS[mid] = s
        return s
