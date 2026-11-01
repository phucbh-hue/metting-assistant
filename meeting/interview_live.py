"""Thu âm buổi phỏng vấn qua extension riêng -> Soniox realtime -> lời thoại vào interview_sessions.

Khác pipeline cuộc họp (live.py): KHÔNG cần tách người nói (diarization tắt), vì vai do NGUỒN quyết định -
mic của người phỏng vấn = interviewer, tiếng tab (ứng viên qua Meet/Zoom/Teams) = candidate. Mỗi kết nối ws từ
extension là một luồng (stream=mic|tab); mỗi luồng mở một kết nối Soniox riêng, chốt câu theo endpoint rồi ghi
một segment {t, role, text} vào buổi. Trình duyệt xem buổi tự poll để hiện lời thoại (không cần ws ngược lại).
"""
import asyncio
import json
import logging
import os
import time
from typing import Optional

import websockets

from meeting import interview

log = logging.getLogger("meeting.interview_live")

RT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
MODEL = os.getenv("SONIOX_MODEL", "stt-rt-v5")
RATE = 16000
ROLE_OF = {"mic": "interviewer", "tab": "candidate"}


class InterviewStream:
    """Một luồng âm thanh (mic hoặc tab) của một buổi phỏng vấn -> Soniox -> segment."""

    def __init__(self, sid: int, role: str, started_at: float, terms: Optional[list] = None):
        self.sid = int(sid)
        self.role = role
        self.started_at = started_at or time.time()
        self.terms = (terms or [])[:100]
        self.ws = None
        self.buf = ""
        self.closed = False
        self.last_audio = time.monotonic()

    async def open(self):
        api_key = os.getenv("SONIOX_API_KEY", "")
        if not api_key:
            raise RuntimeError("Chưa cấu hình SONIOX_API_KEY")
        cfg = {"api_key": api_key, "model": MODEL, "audio_format": "pcm_s16le", "sample_rate": RATE,
               "num_channels": 1, "enable_speaker_diarization": False, "enable_language_identification": True,
               "enable_endpoint_detection": True,
               "language_hints": [x.strip() for x in os.getenv("SONIOX_LANGUAGE_HINTS", "vi,en").split(",") if x.strip()]}
        if self.terms:
            cfg["context"] = {"terms": self.terms}
        self.ws = await websockets.connect(RT_URL, open_timeout=20, max_size=None, ping_interval=None, close_timeout=5)
        await self.ws.send(json.dumps(cfg))
        asyncio.create_task(self._recv(self.ws))
        asyncio.create_task(self._keepalive(self.ws))

    async def _keepalive(self, ws):
        while not self.closed and self.ws is ws:
            await asyncio.sleep(3.0)
            if self.ws is not ws or self.closed:
                return
            if time.monotonic() - self.last_audio > 3.0:
                try:
                    await ws.send(json.dumps({"type": "keepalive"}))
                except Exception:
                    return

    async def feed(self, pcm: bytes):
        if self.closed or not pcm or self.ws is None:
            return
        self.last_audio = time.monotonic()
        try:
            await self.ws.send(pcm)
        except Exception as e:
            log.info("interview_live: gửi audio lỗi (%s)", type(e).__name__)

    async def _recv(self, ws):
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                m = json.loads(raw)
                if m.get("error_code"):
                    log.warning("interview_live buổi %s %s: Soniox %s %s", self.sid, self.role,
                                m.get("error_code"), m.get("error_message", ""))
                    break
                for tok in m.get("tokens", []):
                    txt = tok.get("text", "")
                    if txt in ("<end>", "<fin>"):
                        await self._flush()
                    elif tok.get("is_final"):
                        self.buf += txt
                if m.get("finished"):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.info("interview_live buổi %s %s: recv dừng (%s)", self.sid, self.role, type(e).__name__)
        finally:
            await self._flush()

    async def _flush(self):
        text = " ".join(self.buf.split()).strip()
        self.buf = ""
        if not text:
            return
        seg = {"t": round(time.time() - self.started_at, 1), "role": self.role, "text": text, "at": time.time()}
        try:
            await asyncio.to_thread(interview.append_segment, self.sid, seg)
        except Exception as e:
            log.warning("interview_live: ghi segment lỗi: %s", e)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        ws = self.ws
        if ws is not None:
            try:
                await ws.send(json.dumps({"type": "finalize"}))
                await asyncio.sleep(0.6)
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass
        await self._flush()


async def serve_ws(ws, sid: int, stream: str):
    """Vòng đời một kết nối ws từ extension: nhận PCM16 16kHz, đẩy sang Soniox, ghi lời thoại vào buổi.

    Người gọi (app.py) đã xác thực token và chấp nhận ws. stream ∈ {mic, tab}.
    """
    role = ROLE_OF.get(stream, "candidate")
    sess = await asyncio.to_thread(interview.get_session, sid)
    if not sess:
        await ws.close(code=4404)
        return
    pos = await asyncio.to_thread(interview.get_position, sess.get("position_id"))
    st = InterviewStream(sid, role, sess.get("started_at") or time.time(), (pos or {}).get("vocab"))
    try:
        await st.open()
    except Exception as e:
        log.warning("interview_live: mở Soniox lỗi: %s", e)
        try:
            await ws.send_json({"type": "error", "text": str(e)[:200]})
        except Exception:
            pass
        await ws.close(code=1011)
        return
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            b = msg.get("bytes")
            if b:
                await st.feed(b)
            elif msg.get("text"):                    # lệnh điều khiển (vd {"type":"bye"})
                try:
                    if json.loads(msg["text"]).get("type") == "bye":
                        break
                except Exception:
                    pass
    except Exception as e:
        log.info("interview_live: ws dừng (%s)", type(e).__name__)
    finally:
        await st.close()
