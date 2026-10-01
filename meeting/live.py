"""Meeting Audio Streaming, Soniox STT Realtime & Multi-Speaker Orchestration.

Xử lý luồng âm thanh PCM16 16kHz:
1. Kết nối Soniox Realtime API (stt-rt-v5) hỗ trợ Diarization và Custom Vocabulary.
2. Cắt lát audio từng câu để trích xuất CAM++ 192-dim vector (voice.embed).
3. Đưa vào MeetingSpeakers để định danh người quen / người lạ.
4. Phát hiện Wake-Word kích hoạt trợ lý AI (llm.think_and_act).
5. Kích hoạt Identity Inference Engine (identity.process_unknown_speakers) để đoán danh tính người lạ.
6. Phát tán sự kiện thời gian thực qua WebSockets cho Web UI.
"""
import asyncio
import audioop
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Set

import numpy as np
import websockets

from meeting import db, identity, llm, voice

log = logging.getLogger("meeting.live")

RATE = 16000
SEGMENT_GAP_S = 0.8       # Giảm ngắt từ 1.2s xuống 0.8s để chốt câu nhanh, mượt mà
AUDIO_KEEP_S = 120
LOUD_RMS = 200

LIVE_MEETINGS: Dict[int, "MeetingSession"] = {}


class MeetingStream:
    """Luồng nhận âm thanh PCM và gửi sang Soniox Realtime STT."""
    RT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
    MODEL = "stt-rt-v5"

    def __init__(self, name: str, session: "MeetingSession", diarize: bool = True):
        self.name = name
        self.session = session
        self.diarize = diarize
        self.ws = None
        self.task = None
        self.t0 = None
        self.cur = None
        self.closed = False
        self.down = False
        self.reconnecting = False
        self.tok_off = 0.0
        self.audio = bytearray()
        self.audio_base = 0
        self.fed = 0
        self.last_audio = 0.0

    async def open(self):
        api_key = os.getenv("SONIOX_API_KEY", "")
        if not api_key:
            log.warning("meeting.live: Chưa có SONIOX_API_KEY")
            return

        cfg = {
            "api_key": api_key,
            "model": self.MODEL,
            "audio_format": "pcm_s16le",
            "sample_rate": RATE,
            "num_channels": 1,
            "enable_speaker_diarization": self.diarize,
            "enable_language_identification": True,
            "enable_endpoint_detection": True,
            "language_hints": ["vi"]
        }
        if self.session.vocab:
            cfg["context"] = {"terms": self.session.vocab[:100]}

        self.ws = await websockets.connect(self.RT_URL, open_timeout=20, max_size=None)
        await self.ws.send(json.dumps(cfg))
        if self.t0 is None:
            self.t0 = self.session.clock()
        self.last_audio = time.monotonic()
        self.down = False
        self.task = asyncio.create_task(self._recv())
        asyncio.create_task(self._keepalive(self.ws))
        log.info("meeting.live: Mở luồng Soniox thành công cho session %d", self.session.id)

    async def _keepalive(self, ws):
        """Duy trì kết nối WebSocket với Soniox bằng keepalive ping định kỳ (chống timeout 8s)."""
        while not self.closed and self.ws is ws and not self.down:
            await asyncio.sleep(4.0)
            if time.monotonic() - self.last_audio > 4.0 and not self.closed and self.ws is ws:
                try:
                    await ws.send(json.dumps({"type": "keepalive"}))
                except Exception:
                    return

    def _lost(self, reason: str):
        """Khi kết nối Soniox bị gián đoạn: đánh dấu và tự động nối lại ngầm."""
        if self.closed or self.reconnecting:
            return
        self.down = True
        self.reconnecting = True
        log.warning("session %d stream %s: %s - đang tự động nối lại Soniox...", self.session.id, self.name, reason)
        asyncio.create_task(self._reconnect(reason))

    async def _reconnect(self, reason: str):
        await self._flush()
        for i, wait in enumerate([1, 2, 4, 8, 8], 1):
            await asyncio.sleep(wait)
            if self.closed:
                return
            try:
                old_ws = self.ws
                await self.open()
                self.tok_off = self.fed / (RATE * 2)
                self.down = False
                self.reconnecting = False
                try:
                    if old_ws:
                        await old_ws.close()
                except Exception:
                    pass
                log.info("session %d stream %s: Đã nối lại Soniox thành công sau lần %d", self.session.id, self.name, i)
                return
            except Exception as e:
                log.warning("session %d: Nối lại Soniox lần %d thất bại: %s", self.session.id, i, e)
        self.reconnecting = False

    async def feed(self, pcm: bytes):
        if not self.ws or self.closed or self.down:
            return
        self.last_audio = time.monotonic()
        self.fed += len(pcm)
        self.audio += pcm
        keep = AUDIO_KEEP_S * RATE * 2
        if len(self.audio) > keep + 10 * RATE * 2:
            drop = len(self.audio) - keep
            del self.audio[:drop]
            self.audio_base += drop

        try:
            await self.ws.send(pcm)
        except Exception as e:
            self._lost(f"gửi audio lỗi ({type(e).__name__})")

    def clip(self, t: float, dur: float) -> bytes:
        a = int((t - self.t0) * RATE) * 2 - self.audio_base
        b = a + int(dur * RATE) * 2
        if a < 0 or b > len(self.audio) or b <= a:
            return b""
        return bytes(self.audio[a:b])

    async def _recv(self):
        lost = None
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                if m.get("error_code"):
                    await self.session.emit({"type": "error", "text": f"Soniox {m['error_code']}: {m.get('error_message','')}"})
                    break

                interim = ""
                for tok in m.get("tokens", []):
                    if tok.get("text") == "<end>":
                        if tok.get("is_final"):
                            await self._add_final(tok)
                        continue
                    if tok.get("is_final"):
                        await self._add_final(tok)
                    else:
                        interim += tok.get("text", "")

                if interim:
                    await self.session.emit({"type": "interim", "stream": self.name, "text": interim})

                if m.get("finished"):
                    break
        except Exception as e:
            lost = f"Soniox recv {type(e).__name__}: {e}"
        finally:
            await self._flush()
            if lost and not self.closed:
                self._lost(lost)

    async def _add_final(self, tok: Dict[str, Any]):
        text = tok.get("text", "")
        if text == "<end>":
            await self._flush()
            return

        spk = str(tok.get("speaker", "0"))
        start = self.t0 + tok.get("start_ms", 0) / 1000
        end = self.t0 + tok.get("end_ms", 0) / 1000

        c = self.cur
        if c and (c["raw_speaker"] != spk or start - (c["t"] + c["dur"]) > SEGMENT_GAP_S):
            await self._flush()
            c = None

        if c is None:
            self.cur = c = {
                "t": round(start, 2),
                "dur": 0.0,
                "raw_speaker": spk,
                "text": ""
            }
        c["text"] += text
        c["dur"] = round(max(c["dur"], end - c["t"]), 2)

    async def _flush(self):
        c = self.cur
        self.cur = None
        if not c or not c["text"].strip():
            return

        clip_pcm = self.clip(c["t"], c["dur"])
        await self.session.on_segment_finalized(
            t_start=c["t"],
            t_end=c["t"] + c["dur"],
            raw_speaker=c["raw_speaker"],
            text=c["text"].strip(),
            clip_pcm=clip_pcm
        )

    async def close(self):
        self.closed = True
        await self._flush()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass


# ==============================================================================
# MEETING SESSION (QUẢN LÝ CUỘC HỌP TOÀN DIỆN)
# ==============================================================================
class MeetingSession:
    def __init__(self, meeting_id: int, title: str, vocab: Optional[List[str]] = None,
                 host_id: Optional[int] = None):
        self.id = meeting_id
        self.title = title
        self.vocab = vocab or []
        self.host_id = host_id
        self.started_at = time.monotonic()
        self.streams: Dict[str, MeetingStream] = {}
        self.subscribers: Set[asyncio.Queue] = set()
        self.segments: List[Dict[str, Any]] = []
        self.next_seg_idx = 1
        self._inference_lock = asyncio.Lock()

        # Nạp danh sách Anchor voices đã lưu trong DB
        anchors = {}
        first_vid = None
        for v in db.list_voices():
            full = db.get_voice(v["id"])
            if full and full.get("embedding"):
                try:
                    vec = np.asarray(full["embedding"], dtype=np.float32)
                    if first_vid is None:
                        first_vid = v["id"]
                    anchors[v["id"]] = {
                        "name": v["name"],
                        "vector": voice.unit(vec),
                        "role": v.get("role", ""),
                        "department": v.get("department", "")
                    }
                except Exception:
                    pass

        # Gán Host mặc định nếu chưa chọn (ví dụ Bùi Hồng Phúc)
        if host_id is None and first_vid is not None:
            host_id = first_vid
        self.host_id = host_id
        self.speakers = voice.MeetingSpeakers(anchors=anchors, expected_host_id=host_id)
        LIVE_MEETINGS[meeting_id] = self

    def clock(self) -> float:
        return time.monotonic() - self.started_at

    def set_host(self, host_id: int):
        self.host_id = host_id
        self.speakers.host_id = host_id
        if host_id in self.speakers.anchors:
            self.speakers.last_speaker_name = self.speakers.anchors[host_id]["name"]
            self.speakers.last_speaker_id = host_id
        log.info("meeting.live: Đã chuyển Host sang voice_id=%d (%s)",
                 host_id, self.speakers.last_speaker_name)

    async def add_stream(self, name: str, diarize: bool = True) -> MeetingStream:
        stream = MeetingStream(name, self, diarize=diarize)
        self.streams[name] = stream
        await stream.open()
        return stream

    async def feed(self, pcm: bytes, stream_name: str = "mic"):
        if stream_name in self.streams:
            await self.streams[stream_name].feed(pcm)

    async def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subscribers.discard(q)

    async def emit(self, event: Dict[str, Any]):
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    async def on_segment_finalized(self, t_start: float, t_end: float, raw_speaker: str,
                                   text: str, clip_pcm: bytes):
        """Xử lý khi một câu vừa được chốt - SONG SONG PHI CHẶN (ZERO-LAG)."""
        seg_idx = self.next_seg_idx
        self.next_seg_idx += 1

        # 1. Gán danh tính dự đoán tức thời (Host hoặc người nói gần nhất) để hiển thị không trễ
        provisional_name = self.speakers.last_speaker_name or (
            self.speakers.anchors[self.host_id]["name"] if (self.host_id and self.host_id in self.speakers.anchors) else "Người nói"
        )
        provisional_id = self.speakers.last_speaker_id or self.host_id

        seg_rec = {
            "id": seg_idx,
            "meeting_id": self.id,
            "t_start": t_start,
            "t_end": t_end,
            "speaker_id": provisional_id,
            "speaker_label": provisional_name,
            "text": text,
            "confidence": 0.85,
            "is_inferred": False
        }
        self.segments.append(seg_rec)

        # 2. PHÁT NGAY LẬP TỨC CHO GIAO DIỆN (Zero Latency < 30ms)
        await self.emit({
            "type": "segment",
            "segment": seg_rec
        })

        # 3. Chạy xử lý nhận diện CAM++ và lưu MongoDB song song trong thread pool
        asyncio.create_task(self._async_process_voice_and_save(
            seg_idx, t_start, t_end, raw_speaker, text, clip_pcm, seg_rec
        ))

        # 4. Kiểm tra Wake-Word
        wake_res = llm.detect_wake_word(text)
        if wake_res:
            wake_word, command = wake_res
            log.info("meeting.live: Bắt được wake-word '%s' với lệnh: '%s'", wake_word, command)
            asyncio.create_task(self._handle_ai_activation(command, text))

    async def _async_process_voice_and_save(self, seg_idx: int, t_start: float, t_end: float,
                                            raw_speaker: str, text: str, clip_pcm: bytes,
                                            seg_rec: Dict[str, Any]):
        """Xử lý tính vector giọng nói, gom cụm và lưu MongoDB ngầm song song."""
        vector = None
        voiced_time = 0.0
        if clip_pcm:
            voiced_time = voice.voiced_s(clip_pcm)
            if voiced_time >= voice.MIN_SEG_S:
                # Chạy CAM++ trên CPU worker thread để không chặn luồng chính
                vector = await asyncio.to_thread(voice.embed, clip_pcm)

        # Định danh lại qua MeetingSpeakers
        changes = self.speakers.add(
            key=seg_idx,
            v=vector,
            raw_label=raw_speaker,
            t=t_start,
            voiced=voiced_time,
            text=text
        )
        assigned_name, assigned_id = changes.get(seg_idx, (seg_rec["speaker_label"], seg_rec["speaker_id"]))

        # Nếu nhận dạng giọng nói trả về danh tính chính xác hơn, cập nhật ngay lên UI
        if assigned_name != seg_rec["speaker_label"] or assigned_id != seg_rec["speaker_id"]:
            seg_rec["speaker_label"] = assigned_name
            seg_rec["speaker_id"] = assigned_id
            await self.emit({
                "type": "segment_updated",
                "segment_id": seg_idx,
                "speaker_label": assigned_name,
                "speaker_id": assigned_id
            })

        # Cập nhật các câu cũ nếu cụm được gộp
        for old_k, (new_lbl, new_vid) in changes.items():
            if old_k != seg_idx:
                for s in self.segments:
                    if s["id"] == old_k and s["speaker_label"] != new_lbl:
                        s["speaker_label"] = new_lbl
                        s["speaker_id"] = new_vid
                        await self.emit({
                            "type": "segment_updated",
                            "segment_id": old_k,
                            "speaker_label": new_lbl,
                            "speaker_id": new_vid
                        })

        # Lưu vào MongoDB trong worker thread (không gây nghẽn kết nối mạng)
        await asyncio.to_thread(
            db.add_segment,
            self.id,
            t_start,
            t_end,
            assigned_name,
            text,
            assigned_id,
            0.95 if assigned_id else 0.5,
            False,
            vector.tolist() if vector is not None else None
        )

        # Kích hoạt suy luận danh tính nếu có nhãn chưa định danh
        lbl_lower = assigned_name.lower()
        if any(prefix in lbl_lower for prefix in ["người lạ", "người nói", "speaker", "unknown"]):
            asyncio.create_task(self._check_unknown_speakers())

    async def _handle_ai_activation(self, command: str, full_sentence: str):
        """Kích hoạt Thinking Engine khi gọi tên trợ lý."""
        prompt = command if len(command) > 5 else full_sentence

        await self.emit({
            "type": "ai_activated",
            "wake_word": "Jarvis",
            "prompt": prompt
        })

        async def _thinking_callback(thought: str):
            await self.emit({
                "type": "ai_thinking",
                "text": thought
            })

        async def _tool_callback(tool_info: Dict[str, Any]):
            await self.emit({
                "type": "ai_tool_call",
                "tool": tool_info.get("tool"),
                "args": tool_info.get("args")
            })

        res = await llm.think_and_act(
            meeting_id=self.id,
            prompt=prompt,
            segments=self.segments,
            on_thinking=_thinking_callback,
            on_tool=_tool_callback
        )

        await self.emit({
            "type": "ai_response",
            "response": res
        })

    async def _check_unknown_speakers(self):
        """Kiểm tra và suy luận người lạ trong buổi họp."""
        async with self._inference_lock:
            async def _on_renamed(payload: Dict[str, Any]):
                # Cập nhật segments trong bộ nhớ
                old_lbl = payload["old_label"]
                new_nm = payload["new_name"]
                for s in self.segments:
                    if s.get("speaker_label") == old_lbl:
                        s["speaker_label"] = new_nm
                await self.emit(payload)

            async def _on_suggest(payload: Dict[str, Any]):
                await self.emit(payload)

            await identity.process_unknown_speakers(
                meeting_id=self.id,
                meeting_speakers=self.speakers,
                segments=self.segments,
                on_renamed=_on_renamed,
                on_suggest=_on_suggest
            )

    async def close(self):
        for s in list(self.streams.values()):
            await s.close()
        self.streams.clear()
        LIVE_MEETINGS.pop(self.id, None)
        log.info("meeting.live: Đã đóng session %d", self.id)
