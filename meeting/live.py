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
import json
import logging
import os
import re
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

import numpy as np
import websockets

from meeting import artifacts, db, follow, identity, llm, voice

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
CMD_SETTLE_S = 1.2
AUTO_CONFIRM_S = 10.0           # Slide vừa tự chuyển theo lời nói: "chuyển slide" trong ngần này giây = xác nhận, không nhảy thêm
CMD_MAX_WAIT_S = 15.0
_SENTENCE_END = re.compile(r"[.?!…]\W*$")
# Câu dừng ở những từ này chắc chắn chưa nói xong ("em hãy", "tạo một cái", "chuyển sang")
_UNFINISHED_TAIL = {"hãy", "giúp", "cho", "một", "cái", "các", "những", "về", "là", "của", "và", "với", "để", "thì",
                    "mà", "tạo", "làm", "vẽ", "lập", "viết", "dựng", "xem", "sang", "tới", "đến", "qua", "thêm", "này"}

MAX_SEGMENT_S = 20.0            # Câu dài hơn: cắt tại dấu câu để transcript cập nhật đều
AUDIO_KEEP_S = 180              # Giữ tối đa ngần này giây audio để cắt clip / phát lại
REPLAY_MAX_S = 15.0             # Tối đa số giây audio phát lại sau khi nối lại Soniox
IDLE_CLOSE_S = float(os.getenv("SONIOX_IDLE_CLOSE_S", "30"))
MIN_ENROLL_S = voice.MIN_ANCHOR_S
AUTO_ENROLL_AI = os.getenv("AUTO_ENROLL_VOICES", "0").strip().lower() in ("1", "true", "yes", "on")

SESSIONS: Dict[int, "MeetingSession"] = {}
LIVE_MEETINGS = SESSIONS        # Tên cũ (tương thích)
_session_locks: Dict[int, asyncio.Lock] = {}


def _unfinished(command: str) -> bool:
    words = re.findall(r"\w+", (command or "").lower())
    return len(words) < 3 or words[-1] in _UNFINISHED_TAIL


# ==============================================================================
# SONIOX REALTIME STREAM
# ==============================================================================
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
        self._reconnects: List[float] = []
        self._hold_task: Optional[asyncio.Task] = None

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

    def _lost(self, reason: str):
        if self.closed or self.reconnecting or self._fatal:
            return
        now = time.monotonic()
        self._reconnects = [t for t in self._reconnects if now - t < 300] + [now]
        if len(self._reconnects) > 8:
            self._fatal = f"Soniox ngắt kết nối liên tục ({reason})"
            self.down = True
            self._set_state("error", self._fatal + ". Hãy tắt rồi bật lại mic.")
            return
        self.down = True
        self.reconnecting = True
        log.warning("session %d stream %s: %s - đang tự động nối lại Soniox...", self.session.id, self.name, reason)
        self._set_state("reconnecting", reason)
        asyncio.create_task(self._reconnect(int(self.final_pos * BPS)))

    async def _reconnect(self, replay_from: int):
        old, self.ws = self.ws, None
        if old is not None:
            self._closing.add(old)
            try:
                await old.close()
            except Exception:
                pass
        try:
            for i, wait in enumerate([0.5, 1, 2, 4, 8, 8, 15], 1):
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
            self._set_state("error", "Không nối lại được Soniox. Hãy tắt rồi bật lại mic.")
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
        keep = int(AUDIO_KEEP_S * BPS)
        if len(self.audio) > keep + 10 * BPS:
            drop = len(self.audio) - keep
            del self.audio[:drop]
            self.audio_base += drop
        if self.ws is None and not self.reconnecting and not self._open_lock.locked():
            self._lost("chưa có kết nối Soniox")
            return
        if self.down or self.ws is None:
            return
        try:
            await self.ws.send(pcm)
        except Exception as e:
            self._lost(f"gửi audio lỗi ({type(e).__name__})")

    async def pause(self):
        """Mic tắt: chốt chữ còn treo, giữ kết nối thêm IDLE_CLOSE_S giây rồi đóng để không tốn phí."""
        if self.closed or self.paused:
            return
        self.paused = True
        if self.is_open:
            try:
                await self.ws.send(json.dumps({"type": "finalize"}))
            except Exception:
                pass
        self._set_state("paused")

        async def _idle():
            try:
                await asyncio.sleep(IDLE_CLOSE_S)
                if self.paused and not self.closed:
                    log.info("meeting.live: mic nghỉ %.0fs -> đóng Soniox session %d", IDLE_CLOSE_S, self.session.id)
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
                    lost = f"Soniox {m.get('error_code')}: {m.get('error_message', '')}"
                    if int(m.get("error_code") or 0) in (400, 401, 402, 403):
                        self._fatal = lost   # lỗi cấu hình/tài khoản: nối lại cũng vô ích
                        self._set_state("error", lost)
                    await self.session.emit({"type": "error", "text": lost})
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
            self.session.note_interim()
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
        await self._close_ws()
        self.closed = True
        self._set_state("closed")


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
        self.audio_owner: Optional[object] = None
        self.disposed = False

        segments = segments or []
        self.speakers = voice.MeetingSpeakers(anchors=anchors or {}, expected_host_id=self.host_id)
        self.speakers.load_state(profiles or [], segments)
        self.segments: List[Dict[str, Any]] = []
        self._by_seq: Dict[Any, Dict[str, Any]] = {}
        backfill = []
        for s in segments:
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
        self._speech_at = 0.0                                  # lần cuối chữ tạm (interim) thay đổi
        self._lock = asyncio.Lock()
        # Màn hình trình bày: nội dung đang chiếu, slide hiện tại, lịch sử để "quay lại phần trước"
        self.stage: Dict[str, Any] = {"artifact_id": None, "slide": 0, "history": [], "shown_at_seq": 0,
                                      "follow": True, "changed_at": 0.0, "manual_at": 0.0, "auto_at": 0.0}
        self._follower = follow.SlideFollower()     # tự chuyển slide theo lời trình bày
        self._matchers: Dict[int, Any] = {}          # artifact_id -> (slides, SlideMatcher)
        self._follow_texts: Deque[str] = deque(maxlen=2)
        self._art_cache: Dict[int, Dict[str, Any]] = {}
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
                anchors[v["id"]] = {"name": v["name"], "vector": np.asarray(emb, dtype=np.float32),
                                    "role": v.get("role", ""), "department": v.get("department", "")}
        profiles = db.list_speakers(mid, with_vector=False)
        db.backfill_segment_seq(mid)
        segments = db.get_segments(mid, with_embedding=True)
        return cls(meeting, anchors=anchors, profiles=profiles, segments=segments)

    def _ensure_workers(self):
        if self._workers or self.disposed:
            return
        self._workers = [asyncio.create_task(self._proc_loop()), asyncio.create_task(self._db_loop())]
        if self._backfill:
            self._db(db.update_segments_speaker_by_seqs, self.id, self._backfill)
            self._backfill = []
        if not self._restored_profiles and self.speakers.profiles:
            self._db(db.upsert_speakers, self.id, self.speakers.export_profiles())
            self._restored_profiles = True

    def clock(self) -> float:
        return time.time() - self.started_at

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
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("meeting.live: hàng đợi sự kiện client đầy, bỏ qua sự kiện %s", event.get("type"))

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
        await self._queue.put({"seq": seq, "t_start": t_start, "t_end": t_end, "raw": raw_speaker,
                               "text": text, "clip": clip_pcm, "epoch": epoch, "stream": stream,
                               "vector": vector, "voiced": voiced})
        return seq

    async def _process(self, it: Dict[str, Any]):
        async with self._lock:
            await self._process_locked(it)

    async def _process_locked(self, it: Dict[str, Any]):
        seq, clip, vector, voiced = it["seq"], it["clip"] or b"", it["vector"], it["voiced"]
        if voiced is None:
            voiced = voice.voiced_s(clip) if clip else 0.0
        if vector is None and clip and voiced >= voice.MIN_SEG_S and voice.available():
            vector = await asyncio.to_thread(voice.embed, clip)
        changes = self.speakers.add(key=seq, v=vector, raw_label=it["raw"], t=it["t_start"], voiced=voiced,
                                    text=it["text"], epoch=it["epoch"], dur=it["t_end"] - it["t_start"],
                                    stream=it["stream"])
        prof = self.speakers.profile(self.speakers.sid_of(seq))
        seg = {
            "seq": seq, "meeting_id": self.id, "t_start": it["t_start"], "t_end": it["t_end"],
            "speaker_key": prof.sid, "speaker_label": prof.label, "speaker_id": prof.voice_id,
            "text": it["text"], "is_inferred": prof.origin == "ai", "raw_speaker": it["raw"],
            "confidence": round(float(prof.confidence or 0.0), 2),
        }
        self.segments.append(seg)
        self._by_seq[seq] = seg
        await self.emit({"type": "segment", "segment": seg})
        self._db(db.add_segment, self.id, it["t_start"], it["t_end"], prof.label, it["text"],
                 speaker_id=prof.voice_id, confidence=seg["confidence"], is_inferred=seg["is_inferred"],
                 raw_embedding=vector.tolist() if vector is not None else None, seq=seq,
                 speaker_key=prof.sid, raw_speaker=it["raw"], epoch=it["epoch"], stream=it["stream"],
                 voiced=round(float(voiced), 2))
        await self._apply_relabels([k for k in changes if k != seq])
        await self._sync_speakers()

        await self._check_wake(seg)
        await self._follow_slide(seg)
        self.identity.notify(seg)

    def note_interim(self):
        self._speech_at = time.monotonic()

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
            self._pending_wake = None
            if _SENTENCE_END.search(text):
                asyncio.create_task(self._handle_ai_activation(text, text, pw["name"]))
            else:                              # câu yêu cầu cũng có thể bị cắt giữa chừng: chờ nói nốt
                self._pending_cmd = {"name": pw["name"], "parts": [text], "first": text, "sid": seg["speaker_key"],
                                     "raw": seg.get("raw_speaker"), "start": now, "last": now, "token": object()}
                asyncio.create_task(self._settle_cmd(self._pending_cmd["token"]))

    def _release_cmd(self, pc: Dict[str, Any]):
        """Người gọi đã nói hết câu: ghép các đoạn và xử lý một lần."""
        if self._pending_cmd is not pc:
            return
        self._pending_cmd = None
        command = re.sub(r"\s+", " ", " ".join(pc["parts"])).strip()
        full = re.sub(r"\s+", " ", " ".join([pc["first"]] + pc["parts"][1:])).strip()
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
        await asyncio.sleep(WAKE_FOLLOWUP_S + 0.5)
        pw = self._pending_wake
        if pw is not None and pw["token"] is token:
            self._pending_wake = None
            await self.emit({"type": "ai_listening_end", "name": pw["name"], "reason": "timeout"})

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
                                      consent_by, True, len(vs), "merge")
        full = await asyncio.to_thread(db.get_voice, vid)
        p.voice_id = vid
        self.speakers.dirty.add(p.sid)
        anchor = {vid: {"name": p.name, "vector": np.asarray(full["embedding"], dtype=np.float32),
                        "role": p.role, "department": ""}} if full and full.get("embedding") else {}
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
        await self.drain()
        rows = await asyncio.to_thread(db.get_segments, self.id, True)
        async with self._lock:
            old = self.speakers
            fresh = voice.MeetingSpeakers(anchors={vid: dict(a) for vid, a in old.anchors.items()},
                                          expected_host_id=self.host_id)
            known = set()
            for s in rows:
                known.add(s["seq"])
                emb = s.get("raw_embedding")
                v = np.asarray(emb, dtype=np.float32) if emb and voice.is_valid_vector(emb) else None
                dur = max(0.0, float(s.get("t_end") or 0) - float(s.get("t_start") or 0))
                voiced = s.get("voiced")
                if voiced is None:
                    voiced = dur * 0.7 if v is not None else 0.0
                fresh.add(key=s["seq"], v=v, raw_label=s.get("raw_speaker"), t=float(s.get("t_start") or 0),
                          voiced=float(voiced), text=s.get("text", ""), epoch=int(s.get("epoch") or 0), dur=dur,
                          stream=s.get("stream") or "mic")
            for s in old.segs:          # câu vừa xử lý nhưng chưa kịp có trong kết quả đọc DB
                if s["key"] not in known:
                    rk = s.get("rk")
                    fresh.add(key=s["key"], v=s["v"], raw_label=s["raw"], t=s["t"], voiced=s["w"],
                              text=s["text"], epoch=rk[1] if rk else 0, dur=s["dur"], stream=rk[0] if rk else "mic")
            # Tên thuộc về người dùng hồ sơ cũ TRƯỚC TIÊN (người mới chen vào sau dưới cùng nhãn thì tách ra)
            for op in sorted((p for p in old.active_profiles() if p.name), key=lambda p: p.first_t):
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
    async def start_audio(self, owner: object, name: str = "mic", diarize: bool = True) -> MeetingStream:
        stream = self.streams.get(name)
        if stream is None or stream.closed:
            stream = MeetingStream(name, self, diarize=diarize)
            self.streams[name] = stream
        await stream.ensure_open()
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

    # ----------------------------------------------------------------- AI ---
    async def _handle_ai_activation(self, command: str, full_sentence: str, name: str = "", source: str = "voice"):
        """Kích hoạt trợ lý khi được gọi tên: lệnh trình chiếu xử lý ngay, còn lại qua Thinking Engine."""
        prompt = command if len(command) > 5 else full_sentence
        artifacts.set_meeting(self.id, "trợ lý")
        await self.emit({"type": "ai_activated", "wake_word": name or llm.assistant_config()["name"], "prompt": prompt,
                         "source": source})
        try:
            intent = llm.stage_intent(command)
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
                                          stage_art=self._stage_summary())
            await self.emit({"type": "ai_response", "response": res})
            if res.get("artifact"):
                await self.stage_action("show", artifact_id=res["artifact"]["id"])
        except Exception as e:
            log.warning("meeting.live: trợ lý AI lỗi: %s", e)
            await self.emit({"type": "ai_error", "text": f"Trợ lý AI lỗi: {e}"})

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
        deck = artifacts.normalize_deck(artifacts._json_from_text(art.get("content", "")))
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
        await self.emit({"type": "stage_state", "stage": self.stage_public(), "auto": auto})
        return self.stage_public()

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
        if action == "web_search":
            return await self._web_research(intent.get("query", ""))
        if action == "back" and intent.get("kind"):
            kind = intent["kind"]
            if await self._history_index(kind) is None:
                await self._say(f"Em chưa trình bày {llm.KIND_NAMES.get(kind, kind)} nào trước đó.", "concerned")
                return True
            await self.stage_action("back", kind=kind)
            art = await self._get_artifact(self.stage["artifact_id"])
            await self._say(f"Dạ, em quay lại {llm.KIND_NAMES.get(kind, kind)} \"{art['title'] if art else ''}\".", quiet=True)
            return True
        if action in ("present", "present_stop"):
            if action == "present_stop":
                await self.emit({"type": "stage_present", "action": "stop"})
                await self._say("Dạ, em dừng thuyết trình.", quiet=True)
                return True
            art = await self._get_artifact(self.stage["artifact_id"])
            if not self._slides_of(art):
                if art and art.get("kind") == "dashboard":          # thuyết trình dashboard: đọc KPI và điểm chính
                    await self.emit({"type": "stage_command", "action": "open"})
                    await self.emit({"type": "stage_present", "action": "start", "slide": 0})
                    return True
                decks = [a for a in await asyncio.to_thread(db.get_artifacts, self.id) if a.get("kind") == "slides"]
                if not decks:
                    await self._say("Chưa có bộ slide nào để em thuyết trình. Anh chị nhờ em soạn slide trước nhé.", "concerned")
                    return True
                await self.stage_action("show", artifact_id=decks[0]["id"])
                art = await self._get_artifact(self.stage["artifact_id"])
            await self.emit({"type": "stage_command", "action": "open"})
            await self._ensure_scripts(art)
            await self.emit({"type": "stage_present", "action": "start", "slide": self.stage["slide"]})
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

    async def _web_research(self, query: str) -> bool:
        """"Search giúp anh giá vàng hôm nay": tìm trên web, lưu thành báo cáo có nguồn, chiếu lên màn hình và đọc kết luận."""
        await self._progress(f"Dạ, em tra cứu trên mạng về {query} ngay ạ.", "ack")
        try:
            art = await artifacts.web_research(self.id, query, llm._context_lines(self.segments, n=30))
        except Exception as e:
            log.warning("meeting.live: tra cứu web lỗi: %s", e)
            await self._say(f"Em chưa tra cứu được: {e}", "concerned")
            return True
        self._art_cache[art["id"]] = art
        await self.emit({"type": "artifact_created", "artifact": {k: v for k, v in art.items() if k != "sources"}})
        await self.emit({"type": "stage_command", "action": "open"})
        await self.stage_action("show", artifact_id=art["id"])
        body = re.sub(r"^#.*$", "", art["content"], flags=re.M)
        body = body.split("## Chi tiết")[0]
        summary = re.sub(r"\s+", " ", re.sub(r"[*_`#\[\]()]", " ", body)).strip()
        sents = re.split(r"(?<=[.!?])\s+", summary)
        await self._say(" ".join(sents[:3])[:420] or "Em đã tra cứu xong, kết quả đang hiện trên màn hình.",
                        "happy")
        return True

    async def _open_deck_file(self, query: str) -> bool:
        """"Mở slide ở folder A": tìm trong thư viện slide trên máy, nhập vào cuộc họp và đưa lên màn hình."""
        from meeting import decks
        found = decks.find_files(query)
        if not found:
            fs = decks.folders()
            hint = f" Thư mục hiện có: {', '.join(fs[:5])}." if fs else f" Thư mục {decks.SLIDES_DIR} đang trống."
            await self._say(f"Em không thấy tệp slide nào khớp với yêu cầu.{hint}", "concerned")
            return True
        if len(found) > 1 and found[1]["score"] >= found[0]["score"]:
            names = "; ".join(f"{r['name']} trong {r['folder'] or 'thư mục gốc'}" for r in found[:3])
            await self._say(f"Em thấy nhiều tệp giống nhau: {names}. Anh chị nói rõ tên tệp giúp em.", "concerned")
            return True
        hit = found[0]
        try:
            art = await asyncio.to_thread(artifacts.import_deck, self.id, hit["path"])
        except Exception as e:
            await self._say(f"Em không đọc được tệp {hit['name']}: {e}", "concerned")
            return True
        self._art_cache[art["id"]] = art
        await self.emit({"type": "artifact_created", "artifact": art})
        await self.emit({"type": "stage_command", "action": "open"})
        await self.stage_action("show", artifact_id=art["id"])
        n = len(self._slides_of(art))
        await self._say(f"Dạ, em mở bộ slide \"{art['title'].replace('Slide: ', '')}\" từ thư mục {hit['folder'] or 'gốc'}, "
                        f"gồm {n} slide.")
        return True

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
            self.dispose_if_idle()
        return {"status": "ended", "minutes_status": minutes_status}

    async def _finalize_minutes(self):
        status = "failed"
        artifacts.set_meeting(self.id, "biên bản")
        try:
            try:
                await self.identity.run(force=True)
            except Exception as e:
                log.warning("meeting.live: đoán tên lần cuối lỗi: %s", e)
            try:   # nhận xét nhanh để trợ lý nói trong lúc chờ lập biên bản
                items = await artifacts.meeting_insights(self.segments, self.meeting)
                if items:
                    await self.emit({"type": "ai_insights", "items": items, "source": "meeting_end"})
            except Exception as e:
                log.warning("meeting.live: nhận xét cuối buổi lỗi: %s", e)
            art = await artifacts.generate_meeting_minutes(self.id, self.segments, self.title,
                                                           meeting=self.meeting, speakers=self.public_speakers())
            status = "done"
            await self.emit({"type": "artifact_created", "artifact": art})
            await self.stage_action("show", artifact_id=art["id"])
        except Exception as e:
            log.warning("meeting.live: lập biên bản lỗi: %s", e)
            await self.emit({"type": "error", "text": f"Không lập được biên bản tự động: {e}"})
        finally:
            self.meeting["minutes_status"] = status
            await asyncio.to_thread(db.update_meeting, self.id, {"minutes_status": status}, True)
            await self.emit({"type": "meeting_status", "status": "ended", "meeting": self.meeting,
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
