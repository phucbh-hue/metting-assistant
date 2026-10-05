"""Test giọng đọc tiếng Việt chạy trên máy: chuẩn hóa văn bản, tổng hợp WAV, API."""
import asyncio
import base64
import io
import json
import unittest
import wave
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import tts


class NumberTests(unittest.TestCase):
    def test_num_to_vi(self):
        cases = {0: "không", 5: "năm", 10: "mười", 14: "mười bốn", 15: "mười lăm", 21: "hai mươi mốt",
                 24: "hai mươi tư", 25: "hai mươi lăm", 105: "một trăm linh năm", 110: "một trăm mười",
                 2026: "hai nghìn không trăm hai mươi sáu", 1005000: "một triệu không trăm linh năm nghìn",
                 1200000000: "một tỷ hai trăm triệu", 500000000: "năm trăm triệu"}
        for n, text in cases.items():
            with self.subTest(n=n):
                self.assertEqual(tts.num_to_vi(n), text)

    def test_normalize_meeting_sentences(self):
        n = tts.normalize_vi
        self.assertEqual(n("Ngân sách 500.000.000đ, đã chi 24%."), "Ngân sách năm trăm triệu đồng, đã chi hai mươi tư phần trăm.")
        self.assertIn("ngày ba mươi tháng chín năm hai nghìn không trăm hai mươi sáu", n("Hạn 30/09/2026"))
        self.assertIn("mười bốn giờ ba mươi", n("họp lúc 14:30"))
        self.assertIn("URBOX một trăm linh hai", n("URBOX-102 quá hạn"))
        self.assertIn("tám mươi mi li giây", n("độ trễ 80 ms"))
        self.assertIn("ba phẩy năm", n("tăng 3,5 lần"))
        self.assertIn("mười mười", n("Mega Sale 10.10"))
        self.assertTrue(n("Ai phụ trách việc này?").startswith("Ai phụ trách"))     # "ai" tiếng Việt giữ nguyên
        self.assertIn("ây ai", n("team AI"))
        self.assertNotIn("*", n("**Tiến độ** tốt"))
        self.assertNotIn(chr(0x2014), n("A " + chr(0x2014) + " B"))


@unittest.skipUnless(tts.model_present(), "chưa tải model giọng đọc (python scripts/download_tts.py)")
class SynthesisTests(unittest.TestCase):
    def test_wav_and_cache(self):
        a = tts.synthesize("Dạ, em dựng dashboard ngay.")
        with wave.open(io.BytesIO(a)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth()), (1, 2))
            self.assertGreater(w.getnframes() / w.getframerate(), 0.8)
        self.assertIs(tts.synthesize("Dạ, em dựng dashboard ngay."), a)     # câu lặp lại lấy từ bộ nhớ đệm
        with self.assertRaises(ValueError):
            tts.synthesize("  **  ")


class TtsApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_status_and_speak(self):
        with mock.patch.dict("os.environ", {"SONIOX_API_KEY": ""}):
            st = self.client.get("/api/tts/status").json()
        self.assertEqual(st["engine"], "piper-local")
        self.assertEqual(self.client.post("/api/tts", json={"text": ""}).status_code, 422)
        r = self.client.post("/api/tts", json={"text": "Em tìm thấy 6 ticket."})
        if st["available"]:
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "audio/wav"))
        else:
            self.assertEqual(r.status_code, 503)


class FakeSoniox:
    """Máy chủ WebSocket giả lập giao thức Soniox TTS: mỗi câu trả 3 mảnh PCM (nội dung = văn bản) rồi terminated.

    close_after: đóng kết nối sau khi đọc xong ngần ấy câu (giả lập Soniox đóng kết nối rảnh)."""

    def __init__(self, close_after: int = 0):
        self.close_after = close_after
        self.connections = 0
        self.configs = []

    async def handler(self, ws):
        self.connections += 1
        done = 0
        streams = {}
        async for raw in ws:
            m = json.loads(raw)
            sid = m.get("stream_id")
            if "api_key" in m:
                streams[sid] = m
                self.configs.append(m)
                continue
            if m.get("text_end"):
                await asyncio.sleep(0.01 * len(streams))
                data = m["text"].encode("utf-8")
                for part in (data[:2], data[2:], b"!"):
                    await ws.send(json.dumps({"audio": base64.b64encode(part).decode(), "audio_end": False,
                                              "stream_id": sid}))
                    await asyncio.sleep(0)
                await ws.send(json.dumps({"audio_end": True, "stream_id": sid}))
                await ws.send(json.dumps({"terminated": True, "stream_id": sid}))
                done += 1
                if self.close_after and done >= self.close_after:
                    await ws.close()
                    return


class SonioxStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        tts._pcm_cache.clear()
        self.env = mock.patch.dict("os.environ", {"SONIOX_API_KEY": "test-key", "TTS_ENGINE": "auto"})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        tts._pcm_cache.clear()

    async def serve(self, fake):
        from websockets.asyncio.server import serve
        server = await serve(fake.handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self._close, server)
        url = mock.patch.object(tts, "SONIOX_URL", f"ws://127.0.0.1:{port}")
        url.start()
        self.addCleanup(url.stop)
        tts._soniox = tts.SonioxTTS()

    @staticmethod
    async def _close(server):
        server.close()
        await server.wait_closed()

    async def read(self, text):
        rate, engine, chunks = await tts.open_stream(text)
        return rate, engine, b"".join([c async for c in chunks])

    async def test_streams_share_one_connection_and_cache_repeats(self):
        fake = FakeSoniox()
        await self.serve(fake)
        res = await asyncio.gather(self.read("Dạ, em xem ngay."), self.read("Deadline là thứ Sáu."))
        self.assertEqual(res[0], (24000, "soniox", "Dạ, em xem ngay.".encode() + b"!"))
        self.assertEqual(res[1][2], "Deadline là thứ Sáu.".encode() + b"!")
        self.assertEqual(fake.connections, 1)                      # 2 câu cùng lúc trên một kết nối
        self.assertEqual({c["voice"] for c in fake.configs}, {"Linh"})
        self.assertEqual({c["language"] for c in fake.configs}, {"vi"})
        await self.read("Dạ, em xem ngay.")                         # câu lặp lại: lấy bộ nhớ đệm, không gọi lại
        self.assertEqual(len(fake.configs), 2)

    async def test_reconnects_when_soniox_closed_the_connection(self):
        fake = FakeSoniox(close_after=1)
        await self.serve(fake)
        self.assertEqual((await self.read("Câu một."))[2], "Câu một.".encode() + b"!")
        await asyncio.sleep(0.05)
        self.assertEqual((await self.read("Câu hai."))[2], "Câu hai.".encode() + b"!")
        self.assertEqual(fake.connections, 2)

    async def test_falls_back_to_local_voice_when_soniox_fails(self):
        async def broken(text, voice):
            raise RuntimeError("Soniox TTS lỗi 401: invalid api key")
            yield b""                                                # để là async generator
        with mock.patch.object(tts._soniox, "stream", broken), \
                mock.patch.object(tts, "_piper_pcm", mock.AsyncMock(return_value=(22050, b"\x01\x00" * 4))):
            self.assertEqual(await self.read("Em tìm thấy 6 ticket."), (22050, "piper-local", b"\x01\x00" * 4))

    async def test_piper_chosen_in_settings_never_calls_soniox(self):
        tts.save_settings("piper", "Mai")
        self.assertEqual(tts.engine(), "piper")
        with mock.patch.object(tts._soniox, "stream", side_effect=AssertionError("không được gọi Soniox")), \
                mock.patch.object(tts, "_piper_pcm", mock.AsyncMock(return_value=(22050, b"\x02\x00"))):
            self.assertEqual((await self.read("Xin chào"))[1], "piper-local")


class TtsSettingsTests(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_clean_text_keeps_english_numbers_and_codes(self):
        t = tts.clean_text("**Dashboard** Q3: 1.800.000đ, deadline 31/12/2026 " + chr(0x2014) + " ticket URBOX-102")
        self.assertEqual(t, "Dashboard Q3: 1.800.000đ, deadline 31/12/2026, ticket URBOX-102")

    def test_engine_follows_key_and_settings(self):
        with mock.patch.dict("os.environ", {"SONIOX_API_KEY": "", "TTS_ENGINE": "auto"}):
            self.assertEqual(tts.engine(), "piper")                 # không có khóa Soniox: giọng trên máy
        with mock.patch.dict("os.environ", {"SONIOX_API_KEY": "k", "TTS_ENGINE": "auto"}):
            self.assertEqual(tts.engine(), "soniox")
            tts.save_settings("piper", "Linh")
            self.assertEqual(tts.engine(), "piper")
        with self.assertRaises(ValueError):
            tts.save_settings("elevenlabs", "Linh")
        with self.assertRaises(ValueError):
            tts.save_settings("soniox", "Linh; rm -rf /")

    def test_settings_api_and_stream_endpoint(self):
        with TestClient(appmod.app) as client, \
                mock.patch.dict("os.environ", {"SONIOX_API_KEY": "", "TTS_ENGINE": "auto"}), \
                mock.patch.object(tts, "_piper_pcm", mock.AsyncMock(return_value=(22050, b"\x03\x00" * 8))):
            r = client.put("/api/settings/tts", json={"engine": "soniox", "voice": "Huong"}).json()
            self.assertEqual((r["engine"], r["voice"], r["active"]), ("soniox", "Huong", "piper"))   # chưa có khóa
            self.assertEqual([v["id"] for v in r["voices"]], ["Linh", "Mai", "Huong"])
            self.assertEqual(client.put("/api/settings/tts", json={"engine": "x", "voice": "Linh"}).status_code, 400)
            res = client.post("/api/tts/stream", json={"text": "Em tìm thấy 6 ticket."})
            self.assertEqual(res.status_code, 200)
            self.assertEqual((res.headers["x-sample-rate"], res.headers["x-tts-engine"]), ("22050", "piper-local"))
            self.assertEqual(res.content, b"\x03\x00" * 8)
            self.assertEqual(client.post("/api/tts/stream", json={"text": ""}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
