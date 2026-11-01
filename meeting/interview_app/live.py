"""Lớp "tai": STT realtime -> transcript có vai (interviewer / candidate).

Một engine: SonioxStream (stt-rt-v5) - diarization + vocab + endpoint detection,
token-level. Meta Muse đã bị bỏ khỏi app (11/09): đắt hơn ($0.18 vs $0.12/giờ),
chỉ có turn-level timing, cap 60 phút/session, pacing realtime bắt buộc nên phải
có task đệm im lặng, và WER tiếng Việt chưa bao giờ đo được vì không có key.
Client `web/muse.py` vẫn giữ cho /api/compare và bench.

Một InterviewSession = một buổi. Audio vào qua feed(); có thể nhiều luồng:
  - mic phòng:   1 luồng, engine tự tách người -> map speaker -> role
  - Recall.ai:   1 luồng / người tham dự, mỗi luồng 1 phiên STT riêng (không cần diarization)

Sự kiện đẩy ra qua asyncio.Queue để app fan-out sang browser.
"""
import asyncio
import audioop
import json
import logging
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import websockets

from meeting.interview_app import rolefix, voice


log = logging.getLogger("interview.live")

RATE = 16000
SEGMENT_GAP_S = 1.2      # cùng người, ngắt > ngần này thì tách segment mới (Soniox)
PRIME_TAIL_S = 1.0       # im lặng nối sau giọng mẫu: Soniox chốt câu mẫu, tách hẳn khỏi lời thật
PRIME_TIMEOUT_S = 8.0    # Soniox xử lý không kịp đoạn mẫu sau ngần này (cộng độ dài mẫu) thì thôi chờ
AUDIO_KEEP_S = 120       # giữ ngần này giây audio gần nhất để cắt lại từng câu khi so giọng

# MỘT luồng cho mọi việc so giọng: CPU của Render free rất nhỏ, chạy song song
# chỉ làm mọi câu cùng chậm; xếp hàng thì câu nào xong trước hiện trước.
VOICE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice")
END_WAIT_S = 120         # sau khi báo hết audio, chờ engine xử lý nốt phần còn dồn
# Soniox rớt kết nối giữa buổi (mạng chớp, Render nghẽn, bị đẩy audio quá nhanh) thì
# nối lại, chờ lần lượt ngần này giây giữa các lần thử. Không nối lại là luồng đó
# câm tới hết buổi - gặp thật 25/09: tiếng ứng viên trong Meet mất cả buổi.
RECONNECT_WAITS = (1, 2, 4, 8, 8, 8)
LOUD_RMS = 200           # khối 100ms to hơn ngần này (~ -44 dBFS) mới tính là có tiếng
SILENT_TAB_MIC_S = 20    # mic đã có ngần này giây tiếng mà tab Meet vẫn câm -> cảnh báo
SILENT_MIC_S = 30        # mic phòng thu ngần này giây mà toàn im lặng -> cảnh báo
VOICE_LATE_S = 600       # luồng mở khi model giọng chưa xong: chờ tối đa ngần này để bật so giọng
WAKE_LATE_S = 20.0       # câu gọi trợ lý chỉ được nhận ra muộn (khi vai được sửa) trong ngần này giây
# Kết quả so giọng đưa cho model gán vai (voice_hint): "safe" = chỉ "chủ phòng" / "người lạ", không tên người khác
# và không cụm đã bị nội dung bác; "full" = như trước 14/10 (cả tên người phỏng vấn khác); "off" = không đưa.
VOICE_HINT = "safe"

ENGINES = ("soniox",)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s.lower())
    return re.sub(r"[^\w\s]", "", s).strip()


class BaseStream:
    """Phần chung: gom segment, flush, theo dõi tiến độ. Engine con lo giao thức."""
    max_ahead_s = 10.0          # replay được đẩy trước tối đa bao nhiêu so với phần đã xử lý

    def __init__(self, name: str, session: "InterviewSession", diarize: bool):
        self.name = name                 # "mic" hoặc participant id
        self.session = session
        self.diarize = diarize
        self.ws = None
        self.task = None
        self.t0 = None                   # session clock lúc mở stream
        self.interim = ""
        self.cur = None                  # segment đang gom {t, dur, speaker, role, text}
        self.closed = False
        self.proc_s = 0.0                # giây audio engine đã xử lý
        self.sent_s = 0.0                # giây audio đã gửi
        self.last_audio = 0.0
        self.prime_ms = 0                # độ dài giọng mẫu ở đầu luồng, theo đồng hồ của luồng
        self.prime_votes: dict = {}      # nhãn Soniox -> số token của giọng mẫu mang nhãn đó
        self.prime_open = False          # còn đang gom phiếu cho giọng mẫu
        # Audio gần đây, chỉ giữ khi có so giọng: cắt lại đúng đoạn của từng câu.
        self.keep_audio = False
        self.audio = bytearray()
        self.audio_base = 0              # vị trí (byte, tính từ đầu luồng) của audio[0]
        self.fed = 0                     # tổng byte đã gửi - đồng hồ chính xác của luồng
        # Vai và tên mặc định của mọi câu trên luồng này (InterviewSession.stream_defaults).
        # Extension: tab -> ứng viên trừ khi so giọng nhận ra người phỏng vấn cùng;
        # mic -> chủ phòng, có tên nếu đã chọn giọng lưu.
        self.default_role = None
        self.who = None
        # So giọng của RIÊNG luồng này (voice.Speakers). Extension có hai luồng với hai
        # nhóm người khác nhau: mic = chủ phòng + người ngồi cùng phòng, tab = người
        # trong Meet - trộn chung một bộ thì cụm giọng của hai kênh thu lẫn vào nhau.
        self.voice = None
        # Nối lại Soniox giữa buổi. Mỗi kết nối Soniox đếm giờ token từ 0, nên câu
        # của kết nối sau mang thêm tok_off = số giây audio đã nhận trước lúc nó mở.
        # t0 thì KHÔNG đổi: clip() cần (t - t0) khớp đúng số byte đã nhận từ đầu.
        self.tok_off = 0.0
        self.down = False                # đang mất kết nối: audio vẫn đếm giờ, không gửi đi
        self.reconnecting = False
        self.ending = False              # end() đã gọi: Soniox đóng là bình thường, đừng nối lại
        self.loud_s = 0.0                # số giây audio có tiếng thật đã nhận - để cảnh báo luồng câm

    async def open(self):
        raise NotImplementedError

    async def feed(self, pcm: bytes):
        if not self.ws or self.closed:
            return
        self.last_audio = time.monotonic()
        sec = len(pcm) / (RATE * 2)
        self.sent_s += sec
        self.fed += len(pcm)
        if len(pcm) % 2 == 0 and audioop.rms(pcm, 2) >= LOUD_RMS:
            self.loud_s += sec
        if self.keep_audio:
            self.audio += pcm
            keep = AUDIO_KEEP_S * RATE * 2
            if len(self.audio) > keep + 10 * RATE * 2:     # cắt theo lô 10s, khỏi dời bộ nhớ mỗi 100ms
                drop = len(self.audio) - keep
                del self.audio[:drop]
                self.audio_base += drop
        if self.down:
            return                        # đang nối lại: bỏ khối này, dòng thời gian vẫn đúng
        try:
            await self.ws.send(pcm)
        except Exception as e:
            # Không được ném lên ws_ext/ws_audio: ném là cả kết nối của extension
            # hay trang web đứt theo, trong khi chỉ cần nối lại Soniox.
            self._lost(f"gửi audio lỗi ({type(e).__name__})")

    def _lost(self, reason: str):
        """Kết nối Soniox vừa mất ngoài ý muốn: đánh dấu và nối lại ở nền."""
        if self.closed or self.ending or self.reconnecting:
            return
        self.down = True
        self.reconnecting = True
        log.warning("session %s stream %s: %s - nối lại", self.session.id, self.name, reason)
        asyncio.get_event_loop().create_task(self._reconnect(reason))

    async def _reconnect(self, reason: str):
        self._flush()                    # câu đang nói dở: chốt lại, kết nối mới bắt đầu câu mới
        await self.session.emit({"type": "error", "text": f"Mất kết nối Soniox ở luồng {self.name} ({reason}) - đang nối lại…"})
        try:
            for i, wait in enumerate(RECONNECT_WAITS, 1):
                await asyncio.sleep(wait)
                if self.closed or self.ending:
                    return
                old = self.ws
                try:
                    await self.open()
                except Exception as e:
                    log.warning("session %s stream %s: nối lại lần %d hỏng (%s)", self.session.id, self.name, i, e)
                    continue
                # Không có await nào giữa hai dòng dưới: khối audio kế tiếp đi vào kết
                # nối mới đúng lúc token của nó bắt đầu đếm từ 0.
                self.tok_off = self.fed / (RATE * 2)
                self.prime_ms = 0
                self.down = False
                try:
                    await old.close()
                except Exception:
                    pass
                log.info("session %s stream %s: đã nối lại Soniox (lần %d)", self.session.id, self.name, i)
                await self.session.emit({"type": "stream", "stream": self.name, "state": "restored",
                                         "text": f"Đã nối lại luồng {self.name}"})
                return
            await self.session.emit({"type": "error", "text": f"Không nối lại được Soniox ở luồng {self.name} "
                                                              f"sau {len(RECONNECT_WAITS)} lần - bấm Dừng rồi bắt đầu lại"})
        finally:
            self.reconnecting = False

    def clip(self, t: float, dur: float) -> bytes:
        """Audio của đoạn [t, t+dur] theo giờ phiên - đúng đoạn Soniox báo cho một câu.

        Giờ của token là t0 + start_ms, và start_ms đếm theo chính audio đã gửi,
        nên (t - t0) giây kể từ byte đầu tiên là đúng chỗ, kể cả khi mic mở
        muộn hơn lúc mở luồng.
        """
        a = int((t - self.t0) * RATE) * 2 - self.audio_base
        b = a + int(dur * RATE) * 2
        if a < 0 or b > len(self.audio) or b <= a:
            return b""
        return bytes(self.audio[a:b])

    async def _signal_end(self):
        raise NotImplementedError

    async def prime(self, pcm: bytes):
        """Phát giọng mẫu người phỏng vấn vào ĐẦU luồng, trước audio thật.

        Soniox chỉ có diarization ẩn danh - không đăng ký được giọng ai. Nhưng
        trong MỘT phiên nó giữ nhãn nhất quán cho cùng một giọng, nên nếu giọng
        đầu tiên nó nghe là người phỏng vấn thì nhãn đó chính là người phỏng
        vấn cho cả buổi. Token của đoạn mẫu bị bỏ khỏi transcript (xem _recv).

        Phải ĐỢI Soniox xử lý xong đoạn mẫu rồi mới trả về: engine realtime
        chạy xấp xỉ thời gian thực, mở mic khi còn tồn 10 giây mẫu chưa xử lý
        là cả buổi trễ đúng 10 giây, vì nó không bao giờ đuổi kịp. ws_audio chỉ
        báo "ready" cho browser sau khi hàm này xong, nên mic chưa mở trước đó.
        """
        data = pcm + b"\x00" * (int(RATE * PRIME_TAIL_S) * 2)
        total = len(data) / (RATE * 2)
        self.prime_ms = int(total * 1000)
        self.prime_open = True
        step = RATE * 2 // 10                                    # 100ms
        for i in range(0, len(data), step):
            while self.sent_s - self.proc_s > self.max_ahead_s and not self.closed:
                await asyncio.sleep(0.05)
            await self.feed(data[i:i + step])
        deadline = time.monotonic() + total + PRIME_TIMEOUT_S
        while self.proc_s < self.sent_s - 0.2 and not self.closed and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        # Mốc 0 của luồng lùi lại đúng phần đã gửi, để audio thật tới ngay sau
        # đây mang đúng giờ của phiên chứ không bị cộng dồn độ dài đoạn mẫu.
        self.t0 = self.session.clock() - self.sent_s
        if self.prime_open:
            self._close_prime()

    def _vote(self, tok):
        if tok["text"].strip():
            spk = self._speaker(tok.get("speaker", ""))
            self.prime_votes[spk] = self.prime_votes.get(spk, 0) + 1

    def _close_prime(self):
        self.prime_open = False
        votes = self.prime_votes
        self.session.primed(max(votes, key=votes.get) if votes else None, dict(votes))

    async def end(self):
        self.ending = True
        if self.ws and not self.closed:
            try:
                await self._signal_end()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.task, END_WAIT_S)
            except Exception:
                pass
        await self.close()

    async def close(self):
        self.closed = True
        if self.prime_open:
            self._close_prime()
        self._flush()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    def _flush(self):
        if self.cur and re.search(r"\w", self.cur["text"]):
            self.cur["text"] = self.cur["text"].strip()
            self.session.commit(self.cur, self.name)
        self.cur = None

    def _speaker(self, raw) -> str:
        if not self.diarize:
            return self.name
        # Luồng diarize khác mic phòng (luồng tab của extension) mang tiền tố tên
        # luồng: nhãn "1" của tab và nhãn "1" của mic là hai người khác nhau.
        return str(raw) if self.name == "mic" else f"{self.name}:{raw}"

    async def _emit_interim(self, text: str):
        if text != self.interim:
            self.interim = text
            await self.session.emit({"type": "interim", "stream": self.name, "text": text})


# ----------------------------------------------------------------- Soniox ---
class SonioxStream(BaseStream):
    RT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
    MODEL = "stt-rt-v5"
    KEEPALIVE_S = 8
    max_ahead_s = 10.0

    async def open(self):
        cfg = {"api_key": os.environ["SONIOX_API_KEY"], "model": self.MODEL,
               "audio_format": "pcm_s16le", "sample_rate": RATE, "num_channels": 1,
               "enable_speaker_diarization": self.diarize,
               "enable_language_identification": True, "enable_endpoint_detection": True}
        lang = self.session.lang
        if lang and lang != "auto":
            cfg["language_hints"] = [lang.split("-")[0]]
        if self.session.vocab:
            cfg["context"] = {"terms": self.session.vocab[:100]}
        ws = await websockets.connect(self.RT_URL, open_timeout=20, max_size=None)
        await ws.send(json.dumps(cfg))
        self.ws = ws
        if self.t0 is None:              # nối lại thì giữ mốc cũ - xem tok_off
            self.t0 = self.session.clock()
        self.last_audio = time.monotonic()
        self.task = asyncio.create_task(self._recv(ws))
        asyncio.create_task(self._keepalive(ws))
        log.info("stream %s: Soniox mở (diarize=%s)", self.name, self.diarize)

    async def _keepalive(self, ws):
        while not self.closed and self.ws is ws:
            await asyncio.sleep(self.KEEPALIVE_S / 2)
            if time.monotonic() - self.last_audio > self.KEEPALIVE_S and not self.closed and self.ws is ws:
                try:
                    await ws.send(json.dumps({"type": "keepalive"}))
                except Exception:
                    return

    async def _signal_end(self):
        await self.ws.send("")           # chuỗi rỗng = hết audio

    async def _recv(self, ws=None):
        ws = ws or self.ws
        lost = None                      # lý do mất kết nối ngoài ý muốn, nếu có
        try:
            async for raw in ws:
                m = json.loads(raw)
                if m.get("error_code"):
                    await self.session.emit({"type": "error", "text": f"Soniox {m['error_code']}: {m.get('error_message','')}"})
                    # Lỗi cấu hình/khoá thì nối lại cũng hỏng y vậy; còn lại (quá hạn,
                    # quá tải, lỗi server) thì đáng thử lại.
                    if m["error_code"] not in (400, 401, 402, 403):
                        lost = f"Soniox báo lỗi {m['error_code']}"
                    break
                if m.get("total_audio_proc_ms") is not None:
                    self.proc_s = self.tok_off + m["total_audio_proc_ms"] / 1000
                interim = ""
                for tok in m.get("tokens", []):
                    if tok.get("text") == "<end>":
                        # <end> có thể không mang start_ms; đừng để nó rơi vào
                        # nhánh giọng mẫu rồi bị nuốt mất ở giữa buổi.
                        if tok.get("is_final") and not self.prime_open:
                            self._add_final(tok)
                        continue
                    if self.prime_ms and tok.get("start_ms", 0) < self.prime_ms:
                        # Token của giọng mẫu: KHÔNG BAO GIỜ vào transcript hay
                        # interim, kể cả khi Soniox chốt nó muộn sau khi đã mở mic.
                        if tok.get("is_final") and self.prime_open:
                            self._vote(tok)
                        continue
                    if tok.get("is_final"):
                        if self.prime_open:
                            self._close_prime()
                        self._add_final(tok)
                    else:
                        interim += tok["text"]
                await self._emit_interim(interim)
                if self.cur:
                    await self.session.emit({"type": "partial", "stream": self.name, "segment": self.cur})
                if m.get("finished"):
                    break
            else:
                lost = "Soniox đóng luồng"      # hết vòng mà không có "finished"
        except websockets.ConnectionClosed as e:
            lost = f"Soniox đóng kết nối ({e.code})"
        except Exception as e:
            log.exception("stream %s recv", self.name)
            lost = f"{type(e).__name__}: {e}"
        finally:
            # Kết nối cũ chết muộn sau khi đã có kết nối mới thì không đụng vào gì:
            # chốt câu hay nối lại lúc đó là cắt ngang kết nối đang chạy tốt.
            if self.ws is ws:
                self._flush()
                if lost and not self.closed and not self.ending:
                    self._lost(lost)

    def _add_final(self, tok):
        text = tok["text"]
        if text == "<end>":
            self._flush()
            return
        spk = self._speaker(tok.get("speaker", ""))
        start = self.t0 + self.tok_off + tok.get("start_ms", 0) / 1000
        end = self.t0 + self.tok_off + tok.get("end_ms", 0) / 1000
        role = self.session.role_of(spk)
        if role is None and self.default_role:
            self.session.roles[spk] = role = self.default_role
        c = self.cur
        if c and (c["speaker"] != spk or start - (c["t"] + c["dur"]) > SEGMENT_GAP_S):
            self._flush()
            c = None
        if c is None:
            self.cur = c = {"t": round(start, 2), "dur": 0, "speaker": spk, "role": role, "text": ""}
            if self.who:
                c["who"] = self.who
        c["text"] += text
        c["dur"] = round(max(c["dur"], end - c["t"]), 2)
        if role and not c.get("role"):
            c["role"] = role


STREAM_CLASS = {"soniox": SonioxStream}


# ---------------------------------------------------------------- Session ---
class InterviewSession:
    def __init__(self, sid: int, lang: str, vocab: list, wake_phrases: list, roles: dict | None = None,
                 engine: str = "soniox"):
        self.id = sid
        self.lang = lang
        self.vocab = vocab
        self.engine = engine if engine in STREAM_CLASS else "soniox"
        self.wake = [_norm(w) for w in wake_phrases if w.strip()]
        self.roles: dict = dict(roles or {})     # raw speaker label -> role
        self.segments: list = []
        self.streams: dict[str, BaseStream] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.started = time.monotonic()
        self.last_assist_t = -999.0
        # Người dùng đã sửa vai bằng tay -> app thôi tự đoán vai theo nội dung.
        # Sửa tay luôn thắng: đoán lại đè lên lựa chọn của người đang ngồi
        # phỏng vấn là cách nhanh nhất làm họ mất tin vào transcript.
        self.roles_locked = False
        # Giọng mẫu người phỏng vấn (PCM16 16kHz), dùng MỘT lần lúc mở luồng
        # mic rồi bỏ: không giữ giọng ai lâu hơn cần thiết - đó là dữ liệu sinh
        # trắc học.
        self.prime_pcm: bytes | None = None
        self.prime_label: str | None = None      # nhãn Soniox của giọng mẫu = người phỏng vấn
        # So giọng từng câu (voice.Speakers), bật khi có giọng mẫu và có model.
        # Khi bật, vai của TỪNG câu do giọng quyết; nhãn Soniox chỉ còn là phương
        # án lùi cho câu quá ngắn, theo các câu cùng nhãn gần nhất đã quyết.
        self.voice: "voice.Speakers | None" = None
        # Giọng đã lưu được chọn cho buổi này (app.create_session nạp vào):
        # [{"key", "name", "host", "emb"}]. Tất cả là người phỏng vấn; ai không
        # có trong danh sách là ứng viên.
        self.known: list = []
        # Giọng quen RIÊNG cho từng luồng, đè lên self.known (app.create_session đặt).
        # Extension: tab = người phỏng vấn cùng (không có chủ phòng - Meet không phát lại
        # giọng mình), mic = chủ phòng + người phỏng vấn cùng (có thể ngồi cạnh, nói chung mic).
        self.stream_known: dict = {}
        self.known_only: set = set()              # luồng không thể có ứng viên (mic của extension)
        self.stream_defaults: dict = {}           # tên luồng -> (vai mặc định, tên hiển thị)
        self.people: dict = {}                    # khoá người quen -> {"name", "host"}
        self._voice_segs: dict = {}               # key trong Speakers -> segment
        # Vai từng câu = rolefix.fuse(vai lúc chốt, so giọng, nội dung). Khoá là id(segment):
        # segment là dict sống suốt buổi trong self.segments, và không được thêm trường riêng
        # vào nó vì nó đi thẳng xuống DB và browser.
        self._base: dict = {}                     # vai lúc chốt theo nhãn, hoặc vai set_roles áp sau
        self._acoustic: dict = {}                 # (khoá người của voice.Speakers, cụm giọng chắc?)
        self._content: dict = {}                  # (vai, độ tin, số lần đã gán, số câu đi sau lúc gán)
        # Vai người dùng sửa tay cho TỪNG câu (set_seg_role): thắng mọi bằng chứng khác nhưng không
        # khoá cả buổi như sửa theo nhãn - giọng và nội dung vẫn tiếp tục gán các câu còn lại.
        self._manual: dict = {}
        # Cụm giọng của từng câu, (id(Speakers), mã cụm), và phiếu nội dung theo cụm [người phỏng vấn, ứng viên]
        # tính lại mỗi lượt _resolve_all - để nội dung của CẢ CỤM bác được vai mà giọng gán cho cụm đó.
        self._vclu: dict = {}
        self._votes: dict = {}
        self.content_lock = asyncio.Lock()
        self.role_stats = {"calls": 0, "errors": 0, "seconds": 0.0, "content_changed": 0,
                           "voice_changed": 0, "overrides": 0, "manual": 0}
        self.ended = False
        self.warned: set = set()                  # cảnh báo đã phát (mỗi loại một lần)
        self.on_wake = None                      # callback(trigger_text) do app gắn

    def clock(self) -> float:
        return time.monotonic() - self.started

    # --- vai ---------------------------------------------------------------
    def role_of(self, spk: str) -> str | None:
        if spk in self.roles:
            return self.roles[spk]
        if not self.roles:                       # heuristic: người nói đầu tiên là interviewer (CEO mở lời)
            self.roles[spk] = "interviewer"
            return "interviewer"
        if "candidate" not in self.roles.values():
            self.roles[spk] = "candidate"
            return "candidate"
        return None                              # người thứ 3: chưa rõ, UI gán tay

    def set_roles(self, roles: dict):
        self.roles = dict(roles)
        for s in self.segments:
            self._base[id(s)] = self.roles.get(s["speaker"])
            # Câu đã sửa tay từng câu giữ nguyên: lựa chọn cụ thể hơn thắng lựa chọn theo nhãn.
            s["role"] = self._manual.get(id(s), self._base[id(s)])
        for st in self.streams.values():
            if st.cur:
                st.cur["role"] = self.roles.get(st.cur["speaker"])

    def swap_roles(self):
        inv = {"interviewer": "candidate", "candidate": "interviewer"}
        self.set_roles({k: inv.get(v, v) for k, v in self.roles.items()})

    # --- streams -----------------------------------------------------------
    async def add_stream(self, name: str, diarize: bool) -> BaseStream:
        st = STREAM_CLASS[self.engine](name, self, diarize)
        st.default_role, st.who = self.stream_defaults.get(name, (None, None))
        self.streams[name] = st
        await st.open()
        if diarize and (self.prime_pcm or self.known_for(name)):
            if await self._start_voice(st):
                return st
            if not self.prime_pcm and voice.pending():
                # Model còn đang tải/nạp (Render vừa thức dậy): KHÔNG chờ - chờ là mất
                # tiếng. Thu ngay theo vai mặc định, bật so giọng khi model xong.
                asyncio.get_event_loop().create_task(self._voice_later(st))
                return st
            if self.prime_pcm:           # không có model: lùi về mồi giọng cho Soniox
                pcm, self.prime_pcm = self.prime_pcm, None
                await st.prime(pcm)
        return st

    def known_for(self, name: str) -> list:
        return self.stream_known.get(name, self.known)

    async def _start_voice(self, st) -> bool:
        """Bật so giọng từng câu. True = đã xử lý xong (dù dùng được hay không);
        False = không có model, để add_stream lùi về mồi giọng cho Soniox.

        Người quen = giọng đã lưu được chọn cho buổi + giọng mẫu thu tạm (nếu có).
        Giọng mẫu tạm là của chủ phòng: chủ phòng đã có giọng lưu thì gộp mẫu
        tạm vào đó - thu ngay tại chỗ nên nó khớp mic và phòng hôm nay hơn.
        Không nạp gì vào Soniox, nên không phải chờ nó xử lý trước khi mở mic.
        """
        if not voice.available():
            return False
        loop = asyncio.get_event_loop()
        people = {}
        for p in self.known_for(st.name):
            people[p["key"]] = p["emb"]
            self.people[p["key"]] = {"name": p["name"], "host": p["host"]}
        if self.prime_pcm:
            try:
                anchor = await loop.run_in_executor(VOICE_POOL, voice.embed, self.prime_pcm, voice.MIN_ANCHOR_S)
            except Exception as e:
                log.warning("session %s: không tính được giọng mẫu (%s) - lùi về mồi giọng", self.id, e)
                return False
            self.prime_pcm = None                 # dùng xong là bỏ, không giữ giọng ai
            if anchor is None:
                loop.create_task(self.emit({"type": "prime", "ok": False}))
            else:
                host = next((k for k, p in self.people.items() if p["host"]), None)
                if host:
                    people[host] = voice.merge(people[host], voice.Speakers.ANCHOR_W, anchor)
                else:
                    people["host"] = anchor
                    self.people["host"] = {"name": "", "host": True}
        if not people:
            return True
        # Chủ phòng nói trên mic của CHÍNH họ nên chắc chắn có mặt - nhận ở ngưỡng
        # thấp. Luồng tab của extension không có giọng chủ phòng (app.create_session
        # bỏ ra), nên ở đó ai cũng là "có thể vắng" và cần bằng chứng mạnh.
        host = next((k for k in people if self.people.get(k, {}).get("host")), None)
        st.voice = voice.Speakers(people, expected=host, known_only=st.name in self.known_only)
        self.voice = st.voice                     # "buổi có so giọng" - voice_hint đọc cờ này
        st.keep_audio = True
        names = [self.people[k]["name"] or "bạn" for k in people if k in self.people]
        log.info("session %s: so giọng từng câu trên luồng %s với %s", self.id, st.name, names)
        loop.create_task(self.emit({"type": "prime", "ok": True, "mode": "voice", "people": names,
                                    "stream": st.name}))
        return True

    async def _voice_later(self, st):
        """Bật so giọng cho luồng đã chạy, ngay khi model giọng nạp xong.

        Gặp thật 25/09: luồng tab của extension đứng chờ model trong khi luồng mic
        đã chạy, nên tiếng ứng viên trong lúc chờ mất sạch - và extension bản cũ
        (chưa biết chờ "ready") dồn audio lại rồi đổ một lèo, Soniox đóng luồng.
        Giờ luồng không bao giờ chờ model: câu trước lúc có model mang vai mặc định
        của luồng (tab = ứng viên), câu sau đó được so giọng như thường.
        """
        await self.emit({"type": "voice_wait",
                         "text": "Đang nạp model nhận diện giọng - tạm thời chưa gọi được tên người phỏng vấn cùng"})
        t0 = time.monotonic()
        while voice.pending() and not st.closed and time.monotonic() - t0 < VOICE_LATE_S:
            await asyncio.sleep(1.0)
        if st.closed or st.voice or self.roles_locked:
            return
        if not (voice.available() and await self._start_voice(st) and st.voice):
            log.warning("session %s: không bật được so giọng muộn (%s)", self.id, voice.status())
            await self.emit({"type": "prime", "ok": False})

    def primed(self, label: str | None, votes: dict):
        """Soniox đã gán nhãn cho giọng mẫu -> nhãn đó là người phỏng vấn."""
        loop = asyncio.get_event_loop()
        if not label:
            log.info("session %s: giọng mẫu không ra chữ nào, rơi về đoán theo nội dung", self.id)
            loop.create_task(self.emit({"type": "prime", "ok": False}))
            return
        self.prime_label = label
        # Giọng mẫu là bằng chứng mạnh nhất về người phỏng vấn: đè heuristic
        # "ai mở lời", và nhãn nào đang bị gán nhầm là người phỏng vấn thì gỡ ra.
        roles = {k: v for k, v in self.roles.items() if v != "interviewer" or k == label}
        roles[label] = "interviewer"
        self.set_roles(roles)
        log.info("session %s: giọng mẫu = nhãn %s (phiếu %s)", self.id, label, votes)
        loop.create_task(self.emit({"type": "roles", "roles": self.roles, "segments": self.segments,
                                    "auto": True, "prime": True,
                                    "note": f"Đã nhận ra giọng mẫu: Người {label} là bạn"}))

    async def feed(self, pcm: bytes, stream: str = "mic"):
        st = self.streams.get(stream)
        if st:
            await st.feed(pcm)
            self._check_silence()

    def _check_silence(self):
        """Cảnh báo MỘT lần khi một nguồn tiếng câm bất thường - thay vì để người
        phỏng vấn tới lúc chấm điểm mới biết transcript thiếu nửa cuộc nói chuyện."""
        tab, mic = self.streams.get("tab"), self.streams.get("mic")
        if tab and mic and "tab" not in self.warned and mic.loud_s >= SILENT_TAB_MIC_S and tab.loud_s < 0.5:
            self.warned.add("tab")
            log.warning("session %s: tab Meet câm (tab %.0fs audio, 0 tiếng; mic %.0fs tiếng)",
                        self.id, tab.sent_s, mic.loud_s)
            asyncio.get_event_loop().create_task(self.emit({"type": "error", "text":
                "Chưa nhận được tiếng nào từ tab Meet dù mic đã có tiếng - nếu ứng viên đã nói: kiểm tra "
                "tab Meet không bị tắt tiếng (chuột phải lên tab), rồi bấm Dừng và Bắt đầu lại ngay trên tab cuộc họp."}))
        if mic and not tab and mic.diarize and "mic" not in self.warned \
                and mic.sent_s >= SILENT_MIC_S and mic.loud_s < 0.5:
            self.warned.add("mic")
            log.warning("session %s: mic câm sau %.0fs audio", self.id, mic.sent_s)
            asyncio.get_event_loop().create_task(self.emit({"type": "error", "text":
                "Mic gần như không thu được tiếng - kiểm tra trình duyệt đang dùng đúng micro."}))

    async def end(self):
        self.ended = True
        await asyncio.gather(*(s.end() for s in list(self.streams.values())), return_exceptions=True)
        self.segments.sort(key=lambda s: s["t"])

    # --- transcript --------------------------------------------------------
    def snapshot(self) -> list:
        """Transcript tới đúng lúc này, GỒM CẢ phần chưa chốt.

        self.segments chỉ có segment đã chốt: Soniox gửi <end>, đổi người nói,
        hoặc ngắt quá SEGMENT_GAP_S. Câu trả lời ứng viên vừa nói xong thường
        còn nằm trong st.cur, vài từ cuối còn là interim. Gợi ý dựng trên
        self.segments vì thế cứ bám vào câu trả lời TRƯỚC ĐÓ - người dùng thấy
        bấm lần hai vẫn ra đúng gợi ý của lần đầu (gặp thật 24/09).

        Không đụng vào self.segments hay self.roles: đây là bản chụp để đọc,
        chốt sớm một segment sẽ cắt đôi câu đang nói dở.
        """
        out = list(self.segments)
        for st in self.streams.values():
            seg = dict(st.cur) if st.cur else None
            tail = st.interim
            if tail.strip():
                if seg is not None:
                    seg["text"] = seg["text"] + tail          # token Soniox tự mang dấu cách
                elif not st.diarize:
                    # Luồng không diarize (extension, Recall) biết chắc người nói
                    # là ai. Luồng mic phòng thì interim chưa có nhãn người -
                    # bỏ, còn hơn đoán rồi gán nhầm vai.
                    seg = {"t": round(self.clock(), 2), "dur": 0, "speaker": st.name,
                           "role": self.roles.get(st.name), "text": tail}
            if seg and seg["text"].strip():
                seg["text"] = seg["text"].strip()
                out.append(seg)
        out.sort(key=lambda s: s["t"])
        return out

    def commit(self, seg: dict, stream: str | None = None):
        self.segments.append(seg)
        self._base[id(seg)] = seg.get("role")
        # Kèm vai hiện tại: role_of() gán vai cho nhãn mới ngay lúc nhận token mà
        # không phát event "roles" nào, nên thanh "Người nói" cứ hiện "chưa rõ"
        # trong khi transcript bên dưới đã có vai (gặp thật 24/09).
        asyncio.get_event_loop().create_task(self.emit(
            {"type": "final", "segment": seg, "stream": stream, "roles": dict(self.roles)}))
        st = self.streams.get(stream) if stream else None
        if st and st.voice and st.keep_audio and not self.roles_locked:
            key = len(self.segments) - 1
            self._voice_segs[key] = seg
            asyncio.get_event_loop().create_task(self._voice_check(key, seg, st))
        if seg.get("role") == "interviewer" and self.on_wake:
            if self._wake_hit(seg["text"]) and self.clock() - self.last_assist_t > 10:
                self.last_assist_t = self.clock()
                asyncio.get_event_loop().create_task(self.on_wake(seg["text"]))

    # --- so giọng từng câu ---------------------------------------------------
    async def _voice_check(self, key: int, seg: dict, st):
        sp = st.voice
        label = str(seg.get("speaker"))
        pcm = st.clip(seg["t"] + 0.05, seg["dur"] - 0.1)         # bỏ mép: chỗ hay lẫn người trước

        def work():
            # Tính vector VÀ gom cụm lại cùng ở luồng VOICE_POOL: một luồng duy nhất
            # nên các câu vào Speakers đúng thứ tự chốt, và event loop không bị chặn.
            v = voice.embed(pcm) if pcm else None
            return v, sp.add(key, v, label, seg["t"], voice.voiced_s(pcm) if v is not None else 0.0)

        try:
            v, decided = await asyncio.get_event_loop().run_in_executor(VOICE_POOL, work)
        except Exception as e:
            log.warning("session %s: so giọng lỗi (%s)", self.id, e)
            return
        if self.roles_locked or st.voice is not sp:
            return
        # Log từng câu để kiểm lại bằng giọng người thật: giống_lưu thấp mà vẫn đúng
        # người là bình thường (giọng lưu thu ở chỗ khác), cụm mới là cái quyết.
        a, n, solid = sp.info.get(key, (None, 0, False))
        who = decided.get(key)
        log.info("session %s: câu %d nhãn %s %.1fs giống_lưu=%s cụm=%d câu%s -> %s%s",
                 self.id, key, label, seg.get("dur", 0), "-" if a is None else f"{a:.2f}", n,
                 "" if solid else " (chưa chắc)" if v is not None else " (quá ngắn)",
                 "ứng viên" if who == voice.CAND else "người phỏng vấn (chưa rõ ai)" if who == voice.ANY
                 else (self.people.get(who, {}).get("name") or who or "chưa chắc"),
                 f"; sửa lại {len(decided) - 1} câu trước" if len(decided) > 1 else "")
        self.apply_voice(sp)

    def apply_voice(self, sp):
        """Kết quả so giọng mới nhất -> bằng chứng giọng của từng câu, rồi tính lại vai các câu đổi.

        Đi hết sp.decided chứ không chỉ các câu vừa đổi người: một câu có thể giữ nguyên người
        nhưng chuyển từ mượn nhãn sang do cụm quyết (chắc hơn), và fuse() cần biết điều đó.
        """
        decided, strong, clu = sp.decided, sp.strong, sp.cluster
        for k, person in decided.items():
            seg = self._voice_segs.get(k)
            if seg is None:
                continue
            sk = id(seg)
            c = clu.get(k)
            self._vclu[sk] = None if c is None else (id(sp), c)
            if person is not None:
                ev = (person, bool(strong.get(k)))
            elif sk in self._acoustic:
                ev = (self._acoustic[sk][0], False)    # giọng thôi chắc: giữ người cũ, hạ xuống "yếu"
            else:
                continue
            self._acoustic[sk] = ev
        # Tính lại CẢ buổi chứ không chỉ câu vừa đổi: cụm gom lại thì phiếu nội dung của cụm đổi theo.
        self.role_stats["voice_changed"] += len(self._resolve_all())
        self._refresh_label_roles()

    def _cluster_votes(self) -> dict:
        """(id(Speakers), mã cụm) -> [số câu nội dung chắc là người phỏng vấn, số câu là ứng viên].
        Chỉ câu nội dung chắc từ rolefix.VETO_CONF (hay đã sửa tay) mới bỏ phiếu: tiếng đáp "ừ", "dạ" đoán theo
        vị trí hội thoại (độ tin 0.6-0.8) mà được bỏ phiếu thì lật nhầm cụm nhỏ toàn câu ngắn của chủ phòng."""
        votes: dict = {}
        for seg in self.segments:
            sk = id(seg)
            clu = self._vclu.get(sk)
            if clu is None:
                continue
            c = self._content.get(sk)
            role = self._manual.get(sk) or (c[0] if c and c[0] in rolefix.ROLES and c[1] >= rolefix.VETO_CONF else None)
            if role:
                votes.setdefault(clu, [0, 0])[0 if role == "interviewer" else 1] += 1
        return votes

    def annotate_evidence(self):
        """Cuối buổi: ghi vào từng câu bằng chứng đã quyết vai, để soi lại buổi THẬT khi người dùng báo sai
        vai (lỗi #23 không soi được vì bằng chứng chỉ nằm trong RAM). KHÔNG có vector giọng - chỉ quyết định
        và điểm: v = người giọng chọn (khoá giọng lưu, "?" ứng viên, "*" người phỏng vấn chưa rõ ai), vs = cụm
        chắc, cl = mã cụm, a = cụm giống giọng lưu nhất bao nhiêu, cv = phiếu nội dung của cụm [PV, UV],
        c / cc = vai và độ tin theo nội dung, m = người dùng đã sửa tay."""
        self._votes = self._cluster_votes()
        sims = {}
        for st in self.streams.values():
            if st.voice is not None:
                for k, info in st.voice.info.items():
                    seg = self._voice_segs.get(k)
                    if seg is not None and info and info[0] is not None:
                        sims[id(seg)] = round(float(info[0]), 2)
        for seg in self.segments:
            sk, ev = id(seg), {}
            if (a := self._acoustic.get(sk)) is not None:
                ev.update(v=a[0], vs=int(bool(a[1])))
            if (clu := self._vclu.get(sk)) is not None:
                ev["cl"] = clu[1]
                if clu in self._votes:
                    ev["cv"] = list(self._votes[clu])
            if sk in sims:
                ev["a"] = sims[sk]
            if (c := self._content.get(sk)) is not None:
                ev.update(c=c[0], cc=round(float(c[1]), 2))
            if sk in self._manual:
                ev["m"] = 1
            if ev:
                seg["ev"] = ev

    def _resolve_all(self) -> list:
        """Tính lại vai mọi câu -> [(câu, vai trước đó)] cho các câu đổi."""
        self._votes = self._cluster_votes()
        changed = []
        for seg in self.segments:
            before = seg.get("role")
            if self._resolve(seg):
                changed.append((seg, before))
        return changed

    def apply_content(self, decisions: dict) -> int:
        """{vị trí câu: (vai, độ tin, số câu đi sau lúc gán)} từ rolefix.fix_once -> số câu đổi vai."""
        for i, (role, conf, after) in decisions.items():
            if i >= len(self.segments):
                continue
            seg = self.segments[i]
            sk = id(seg)
            prev = self._content.get(sk)
            sure = role in rolefix.ROLES and conf >= rolefix.ACCEPT
            if prev and prev[0] in rolefix.ROLES and prev[1] >= rolefix.ACCEPT and not sure:
                role, conf = prev[0], prev[1]      # lượt sau không chắc: giữ quyết định chắc của lượt trước
            self._content[sk] = (role, conf, prev[2] + 1 if prev else 1, after)
        # Cả buổi: phiếu nội dung mới có thể bác vai giọng gán cho cả cụm, kể cả câu chưa có nội dung.
        changed = self._resolve_all()
        n = len(changed)
        for seg, before in changed:
            a = self._acoustic.get(id(seg))
            if a and a[1] and self._person(a[0])[0] == before:
                self.role_stats["overrides"] += 1
        self._refresh_label_roles()
        self.role_stats["content_changed"] += n
        return n

    def content_state(self) -> list:
        """(số lần đã gán theo nội dung, số câu đi sau lúc gán) của từng câu, theo thứ tự transcript.
        Câu sửa tay coi như đã gán xong: khỏi tốn lượt model."""
        return [(9, 99) if id(s) in self._manual else c[2:] if (c := self._content.get(id(s))) else (0, 0)
                for s in self.segments]

    def set_seg_role(self, i: int, role: str) -> bool:
        """Người dùng sửa vai MỘT câu. Không khoá buổi: so giọng và đọc nội dung vẫn gán các câu khác,
        và câu này đi kèm cho model đọc như một mốc chắc chắn (voice_hint)."""
        seg = self.segments[i]
        self._manual[id(seg)] = role
        seg["manual"] = True                      # xuống DB: biết câu nào người dùng đã sửa (đo độ đúng thật)
        self.role_stats["manual"] += 1
        if role == "interviewer":
            person, _ = self._acoustic.get(id(seg), (None, False))
            a_role, a_name = self._person(person)
            name, host = (a_name, self.people.get(person, {}).get("host", not self.people)) \
                if a_role == "interviewer" else self._sole_interviewer()
        else:
            name, host = None, False
        changed = self._apply_seg(seg, role, name, host)
        self._refresh_label_roles()
        return changed

    def _voice_eff(self, seg) -> tuple:
        """(khoá người, cụm chắc?) mà giọng gán cho câu, SAU khi đối chiếu cả cụm với nội dung: cụm bị nội
        dung bác thì giọng không còn nói câu này là ai (None)."""
        sk = id(seg)
        person, strong = self._acoustic.get(sk, (None, False))
        a_role = self._person(person)[0]
        clu = self._vclu.get(sk)
        v = self._votes.get(clu) if a_role and clu is not None else None
        if v and rolefix.veto(v[1] if a_role == "interviewer" else v[0], v[0] + v[1]):
            return None, False
        return person, strong

    def voice_hint(self, seg) -> str | None:
        """Mốc đi kèm câu cho model gán vai đọc: vai người dùng đã sửa tay, và kết quả so giọng ở dạng AN TOÀN.

        Chỉ nói "chủ phòng" hay "người lạ" (ứng viên) - người chắc chắn có mặt, và người không khớp ai. Không
        nói tên người phỏng vấn khác: họ có thể vắng, và giọng lưu thu cùng kênh với phòng phỏng vấn làm giọng
        gọi tên họ cho cả cụm câu của người khác (lỗi #23, 14/10) - model nghe theo thì sai theo, nội dung hết
        bác được giọng. Cụm đã bị nội dung bác (rolefix.veto) cũng không nói gì. Bỏ hẳn mốc giọng thì tiếng
        đáp "Vâng." của người phỏng vấn bị đoán theo vị trí thành ứng viên (bench/role_fix.py, 14/10).
        """
        m = self._manual.get(id(seg))
        if m:
            return "NGƯỜI DÙNG ĐÃ XÁC NHẬN: " + ("người phỏng vấn" if m == "interviewer" else "ứng viên")
        if not self.voice or VOICE_HINT == "off":
            return None
        person, strong = self._voice_eff(seg) if VOICE_HINT == "safe" else self._acoustic.get(id(seg), (None, False))
        sure = "" if strong else " (chưa chắc)"
        if person == voice.CAND:
            return "giọng: lạ, không khớp người phỏng vấn nào" + sure
        p = self.people.get(person, {}) if person is not None else {}
        if VOICE_HINT == "safe":
            return "giọng: chủ phòng" + sure if p.get("host") else None
        if person is None:
            return "giọng: chưa rõ"
        name = p.get("name") or ("chủ phòng" if p.get("host") else "người phỏng vấn")
        return "giọng: " + name + (" (chủ phòng)" if p.get("host") and p.get("name") else "") + sure

    def _refresh_label_roles(self):
        """Vai của nhãn Soniox cho câu MỚI (trước khi giọng, nội dung kịp quyết): theo 5 câu gần nhất
        của nhãn đó đã có bằng chứng - vẫn hơn vai mà nhãn được gán lúc đầu buổi."""
        votes: dict = {}
        for s in reversed(self.segments):
            sk = id(s)
            c = self._content.get(sk)
            if sk not in self._acoustic and sk not in self._manual \
                    and not (c and c[0] in rolefix.ROLES and c[1] >= rolefix.ACCEPT):
                continue
            vs = votes.setdefault(str(s.get("speaker")), [])
            if len(vs) < 5 and s.get("role"):
                vs.append(s["role"])
        for lab, vs in votes.items():
            if vs:
                top = max(set(vs), key=vs.count)
                if vs.count(top) * 2 > len(vs):
                    self.roles[lab] = top

    def _label_person(self, label: str) -> str | None:
        for st in self.streams.values():
            if st.voice and label in st.voice.labels:
                return st.voice.labels[label]
        return None

    def _person(self, key) -> tuple:
        """khoá người -> (vai, tên hiển thị). Người lạ là ứng viên, không có tên."""
        if key is None:
            return None, None
        if key == voice.CAND:
            return "candidate", None
        # voice.ANY: rõ là người phỏng vấn nhưng giọng lưu quá giống nhau - không gọi tên
        return "interviewer", self.people.get(key, {}).get("name") or None

    def _sole_interviewer(self) -> tuple:
        """(tên, chủ phòng?) cho câu mà NỘI DUNG nhận là người phỏng vấn còn giọng không nói là ai:
        chỉ gọi tên khi buổi có đúng một người quen."""
        if len(self.people) == 1:
            p = next(iter(self.people.values()))
            return p.get("name") or None, bool(p.get("host"))
        return None, not self.people

    def _resolve(self, seg) -> bool:
        """Vai cuối của một câu từ vai lúc chốt + giọng + nội dung (rolefix.fuse). True nếu đổi."""
        if self.roles_locked:
            return False
        sk = id(seg)
        if sk in self._manual:
            return False                          # người dùng đã sửa câu này: không bằng chứng nào đè lên
        person, strong = self._acoustic.get(sk, (None, False))
        a_role, a_name = self._person(person)
        clu = self._vclu.get(sk)
        v = self._votes.get(clu) if a_role and clu is not None else None
        if v and rolefix.veto(v[1] if a_role == "interviewer" else v[0], v[0] + v[1]):
            # Nội dung của CẢ cụm giọng nói ngược lại (cụm toàn câu trả lời mà giọng gọi là người phỏng vấn,
            # hay ngược lại): giọng chỉ đúng ở chỗ gom các câu này lại với nhau. Vai của cụm theo số đông nội
            # dung, thành bằng chứng yếu - câu có nội dung chắc vẫn theo nội dung của chính nó.
            a_role = "candidate" if a_role == "interviewer" else "interviewer"
            person, strong, a_name = None, False, None
        elif strong and a_role == "interviewer" and not self.people.get(person, {}).get("host") \
                and not (v and v[0] >= rolefix.CONFIRM_MIN and v[0] > v[1]):
            # Người phỏng vấn cùng (hay "người phỏng vấn chưa rõ ai") có thể VẮNG: giọng gọi tên họ chỉ là bằng
            # chứng mạnh khi nội dung của cả cụm cũng nói đó là người phỏng vấn. Chưa có thì là bằng chứng yếu
            # - nội dung từ 0.6 quyết. Buổi #74 (14/10): cả đoạn kể chuyện của người lạ mang tên "khánh" vì
            # giọng chắc thắng mọi nội dung dưới 0.9, mà nội dung chưa đủ 3 phiếu chắc để bác cả cụm.
            strong = False
        c = self._content.get(sk)
        role = rolefix.fuse(self._base.get(sk), (a_role, strong) if a_role else None, c[:2] if c else None)
        if role is None:
            return False
        if role == a_role and person is None:
            name, host = (None, False) if role == "candidate" else self._sole_interviewer()
        elif role == a_role:
            name, host = a_name, self.people.get(person, {}).get("host", not self.people)
        elif role == seg.get("role"):
            name, host = seg.get("who"), not self.people
        elif role == "interviewer":
            name, host = self._sole_interviewer()
        else:
            name, host = None, False
        return self._apply_seg(seg, role, name, host)

    def _apply_seg(self, seg: dict, role: str, name: str | None, host: bool) -> bool:
        if seg.get("role") == role and seg.get("who") == name:
            return False
        seg["role"] = role
        if name:
            seg["who"] = name
        else:
            seg.pop("who", None)
        i = next((j for j in range(len(self.segments) - 1, -1, -1) if self.segments[j] is seg), None)
        if i is None:
            return True
        loop = asyncio.get_event_loop()
        loop.create_task(self.emit({"type": "seg_role", "i": i, "t": seg["t"], "role": role, "who": name,
                                    "host": bool(host and self.people)}))
        # Câu gọi trợ lý bị gán nhầm vai lúc chốt thì đã bị bỏ qua - giờ nhận ra đó là CHỦ PHÒNG thì
        # bắt lại, nếu câu còn mới. Người phỏng vấn khác nói câu gọi thì không tính: "chỉ nhận khi
        # người nói là bạn".
        if role == "interviewer" and host and self.on_wake and self._wake_hit(seg["text"]) \
                and self.clock() - self.last_assist_t > 10 \
                and self.clock() - (seg["t"] + seg.get("dur", 0)) <= WAKE_LATE_S:
            self.last_assist_t = self.clock()
            loop.create_task(self.on_wake(seg["text"]))
        return True

    def _wake_hit(self, text: str) -> bool:
        n = _norm(text)
        return any(w in n for w in self.wake)

    # --- pubsub ------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        self.subscribers.discard(q)

    async def emit(self, ev: dict):
        for q in list(self.subscribers):
            q.put_nowait(ev)
