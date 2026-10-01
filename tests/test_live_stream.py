"""Test MeetingStream với Soniox giả lập: tách câu, timestamp sau khi nối lại, phát lại audio, finalize."""
import asyncio
import json
import os
import unittest
from unittest import mock

import numpy as np

from tests.helpers import reset_db
from meeting import db, live

BPS = live.BPS


class FakeSoniox:
    """WebSocket giả: ghi lại mọi thứ client gửi, server đẩy message qua push()."""
    instances = []

    def __init__(self):
        self.sent = []
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False
        FakeSoniox.instances.append(self)

    async def send(self, data):
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(data)
        if data == "":
            await self._inbox.put({"tokens": [], "finished": True})

    def push(self, msg):
        self._inbox.put_nowait(msg)

    def drop(self):
        """Giả lập rớt mạng."""
        self._inbox.put_nowait(ConnectionResetError("network down"))

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self._inbox.get()
        if m is None:
            raise StopAsyncIteration
        if isinstance(m, Exception):
            raise m
        return json.dumps(m)

    async def close(self):
        self.closed = True
        self._inbox.put_nowait(None)

    @property
    def audio_bytes(self) -> int:
        return sum(len(x) for x in self.sent if isinstance(x, (bytes, bytearray)))

    @property
    def control(self):
        return [json.loads(x) for x in self.sent if isinstance(x, str) and x]


async def fake_connect(*args, **kwargs):
    return FakeSoniox()


def tok(text, start_ms, end_ms, speaker="1", final=True):
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms, "speaker": speaker, "is_final": final}


def tone(seconds: float) -> bytes:
    t = np.arange(int(seconds * 16000)) / 16000
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16).tobytes()


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        FakeSoniox.instances = []
        self.patches = [mock.patch.object(live.websockets, "connect", fake_connect),
                        mock.patch.dict(os.environ, {"SONIOX_API_KEY": "test-key"})]
        for p in self.patches:
            p.start()
        self.mid = db.create_meeting("Họp stream")
        self.s = await live.get_session(self.mid)
        self.owner = object()
        self.stream = await self.s.start_audio(self.owner)

    async def asyncTearDown(self):
        await self.s.close()
        for p in self.patches:
            p.stop()
        live.SESSIONS.clear()

    async def wait_for(self, cond, timeout=5.0):
        for _ in range(int(timeout / 0.02)):
            if cond():
                return True
            await asyncio.sleep(0.02)
        return cond()

    async def test_config_tokens_to_segments(self):
        ws = FakeSoniox.instances[0]
        cfg = json.loads(ws.sent[0])
        self.assertTrue(cfg["enable_speaker_diarization"])
        self.assertEqual(cfg["sample_rate"], 16000)
        await self.stream.feed(tone(4.0))
        ws.push({"tokens": [tok("Xin", 0, 400), tok(" chào", 400, 900), tok(" mọi người.", 900, 1600),
                            tok(" Dạ", 2600, 2900, speaker="2"), tok(" vâng.", 2900, 3300, speaker="2"),
                            tok("<end>", 3300, 3300)], "final_audio_proc_ms": 3300})
        self.assertTrue(await self.wait_for(lambda: len(self.s.segments) == 2))
        a, b = self.s.segments
        self.assertEqual(a["text"], "Xin chào mọi người.")
        self.assertEqual((a["raw_speaker"], b["raw_speaker"]), ("1", "2"))
        self.assertEqual((a["speaker_label"], b["speaker_label"]), ("Người nói 1", "Người nói 2"))
        self.assertAlmostEqual(b["t_start"] - a["t_start"], 2.6, places=1)

    async def test_reconnect_replays_audio_and_keeps_timeline(self):
        """Lỗi cũ: sau khi nối lại, timestamp Soniox bắt đầu từ 0 nhưng code không cộng offset -> cắt sai audio."""
        ws1 = FakeSoniox.instances[0]
        await self.stream.feed(tone(3.0))
        ws1.push({"tokens": [tok("Câu một.", 0, 1500), tok("<end>", 1500, 1500)], "final_audio_proc_ms": 2000})
        self.assertTrue(await self.wait_for(lambda: len(self.s.segments) == 1))
        ws1.drop()                                   # rớt mạng sau khi Soniox đã chốt 2.0s đầu
        await self.stream.feed(tone(1.0))            # audio đến trong lúc mất kết nối -> giữ lại
        self.assertTrue(await self.wait_for(lambda: len(FakeSoniox.instances) == 2 and not self.stream.down))
        ws2 = FakeSoniox.instances[1]
        self.assertEqual(self.stream.epoch, 1)
        self.assertEqual(ws2.audio_bytes, 2 * BPS)   # phát lại 2.0s chưa chốt (từ 2.0s tới 4.0s)
        self.assertAlmostEqual(self.stream.conn_off, 2.0, places=2)
        ws2.push({"tokens": [tok("Câu hai.", 500, 1500, speaker="1"), tok("<end>", 1500, 1500)],
                  "final_audio_proc_ms": 1500})
        self.assertTrue(await self.wait_for(lambda: len(self.s.segments) == 2))
        first, second = self.s.segments
        self.assertAlmostEqual(second["t_start"] - first["t_start"], 2.5, places=1)
        self.assertEqual(second["speaker_label"], "Người nói 1")

    async def test_pause_sends_finalize_and_idle_close(self):
        ws = FakeSoniox.instances[0]
        await self.stream.feed(tone(1.0))
        with mock.patch.object(live, "IDLE_CLOSE_S", 0.1):
            await self.s.stop_audio(self.owner)
            self.assertIn({"type": "finalize"}, ws.control)
            self.assertTrue(await self.wait_for(lambda: self.stream.ws is None))
        self.assertIn("", ws.sent)                    # tín hiệu hết audio khi đóng
        # Bật mic lại -> mở kết nối mới (epoch mới)
        owner2 = object()
        await self.s.start_audio(owner2)
        self.assertEqual(len(FakeSoniox.instances), 2)
        self.assertEqual(self.stream.epoch, 1)

    async def test_fatal_error_stops_reconnect_loop(self):
        ws = FakeSoniox.instances[0]
        ws.push({"error_code": 401, "error_message": "Invalid API key"})
        self.assertTrue(await self.wait_for(lambda: self.stream.state == "error"))
        await asyncio.sleep(0.8)
        self.assertEqual(len(FakeSoniox.instances), 1)


if __name__ == "__main__":
    unittest.main()
