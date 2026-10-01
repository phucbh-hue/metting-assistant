"""Test giọng đọc tiếng Việt chạy trên máy: chuẩn hóa văn bản, tổng hợp WAV, API."""
import io
import unittest
import wave

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
        st = self.client.get("/api/tts/status").json()
        self.assertEqual(st["engine"], "piper-local")
        self.assertEqual(self.client.post("/api/tts", json={"text": ""}).status_code, 422)
        r = self.client.post("/api/tts", json={"text": "Em tìm thấy 6 ticket."})
        if st["available"]:
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "audio/wav"))
        else:
            self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
