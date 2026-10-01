"""Test REST API & WebSocket (FastAPI TestClient, DB in-memory, Soniox/LLM giả lập)."""
import io
import json
import os
import time
import unittest
import wave
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from tests.helpers import VoiceBank, reset_db
from tests.test_live_stream import fake_connect
from meeting import app as appmod
from meeting import artifacts, db, live, voice


class ApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.client = TestClient(appmod.app)
        self.client.__enter__()
        self.bank = VoiceBank(seed=60)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        live.SESSIONS.clear()

    def create(self, **kw) -> int:
        r = self.client.post("/api/meetings", json={"title": "Sprint 39", **kw})
        self.assertEqual(r.status_code, 200)
        return r.json()["meeting_id"]

    def add_segments(self, mid, script):
        async def _go():
            s = await live.get_session(mid)
            t = 0.0
            for row in script:
                k, raw, voiced = row[:3]
                text = row[3] if len(row) > 3 else "nội dung"
                await s.on_segment_finalized(t, t + voiced * 1.3, raw, text, voiced=voiced,
                                             vector=self.bank.vec(k, voiced) if voiced >= 1 else None)
                t += voiced * 1.3 + 0.5
            await s.drain()
        self.client.portal.call(_go)

    # ----------------------------------------------------------- meetings ---
    def test_create_meeting_details_and_list(self):
        mid = self.create(agenda=["Chốt release", "  "], expected_attendees=["Hương"])
        d = self.client.get(f"/api/meetings/{mid}").json()
        self.assertEqual(d["meeting"]["status"], "live")
        self.assertEqual(d["meeting"]["agenda"], ["Chốt release"])
        self.assertEqual(d["speakers"], [])
        self.add_segments(mid, [(0, "1", 3.0), (1, "2", 3.0)])
        d = self.client.get(f"/api/meetings/{mid}").json()
        self.assertEqual([p["label"] for p in d["speakers"]], ["Người nói 1", "Người nói 2"])
        self.assertNotIn("raw_embedding", d["segments"][0])
        self.assertNotIn("centroid", d["speakers"][0])
        row = self.client.get("/api/meetings").json()["meetings"][0]
        self.assertEqual((row["segment_count"], row["speaker_count"]), (2, 2))
        self.assertEqual(self.client.get("/api/stats").json()["meetings_live"], 1)
        self.assertEqual(self.client.get("/api/meetings/999").status_code, 404)

    def test_update_meeting_only_editable_fields(self):
        mid = self.create()
        self.client.put(f"/api/meetings/{mid}", json={"title": "Tên mới", "status": "ended"})
        m = db.get_meeting(mid)
        self.assertEqual((m["title"], m["status"]), ("Tên mới", "live"))

    # ----------------------------------------------------------- speakers ---
    def test_rename_speaker_api_and_validation(self):
        mid = self.create()
        self.add_segments(mid, [(0, "1", 3.0), (1, "2", 3.0)])
        r = self.client.post(f"/api/meetings/{mid}/speakers/2/rename", json={"name": "Hương", "role": "Khách mời"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["new_name"], "Hương")
        self.assertEqual([s["speaker_label"] for s in db.get_segments(mid)], ["Người nói 1", "Hương"])
        self.assertEqual(self.client.post(f"/api/meetings/{mid}/speakers/2/rename", json={"name": " "}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/meetings/{mid}/speakers/99/rename", json={"name": "X"}).status_code, 404)
        # API cũ theo nhãn vẫn dùng được
        r = self.client.post(f"/api/meetings/{mid}/confirm-identity",
                             json={"unknown_label": "Người nói 1", "confirmed_name": "Nam", "save_voice": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.get_segments(mid)[0]["speaker_label"], "Nam")

    def test_reassign_and_merge_api(self):
        mid = self.create()
        self.add_segments(mid, [(0, "1", 3.0), (1, "2", 3.0), (1, "2", 1.2)])
        r = self.client.post(f"/api/meetings/{mid}/segments/3/speaker", json={"target_sid": None})
        self.assertEqual(r.json()["speaker"]["label"], "Người nói 3")
        r = self.client.post(f"/api/meetings/{mid}/speakers/3/merge", json={"target_sid": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([s["speaker_key"] for s in db.get_segments(mid)], [1, 2, 2])
        labels = [p["label"] for p in self.client.get(f"/api/meetings/{mid}/speakers").json()["speakers"]]
        self.assertEqual(labels, ["Người nói 1", "Người nói 2"])

    def test_ended_meeting_can_still_be_corrected(self):
        """Sửa tên người nói sau khi họp xong (session được nạp lại từ DB rồi giải phóng)."""
        mid = self.create()
        self.add_segments(mid, [(0, "1", 3.0), (1, "2", 3.0)])
        self.client.post(f"/api/meetings/{mid}/end")
        live.SESSIONS.clear()
        r = self.client.post(f"/api/meetings/{mid}/speakers/1/rename", json={"name": "Bùi Hồng Phúc"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(mid, live.SESSIONS)
        d = self.client.get(f"/api/meetings/{mid}").json()
        self.assertEqual([p["label"] for p in d["speakers"]], ["Bùi Hồng Phúc", "Người nói 2"])

    def test_assistant_name_settings(self):
        self.assertEqual(self.client.get("/api/settings/assistant").json()["name"], "Jarvis")
        r = self.client.put("/api/settings/assistant", json={"name": " Bông ", "aliases": ["Bong", "bông", "  "]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["name"], r.json()["aliases"]), ("Bông", ["Bong"]))
        self.assertEqual(self.client.get("/api/settings/assistant").json()["name"], "Bông")
        self.assertEqual(self.client.put("/api/settings/assistant", json={"name": "1"}).status_code, 400)
        self.assertEqual(self.client.put("/api/settings/assistant", json={"name": "Bông", "aliases": ["x" * 40]}).status_code, 400)

    def test_reanalyze_endpoint(self):
        mid = self.create()
        self.add_segments(mid, [(0, "1", 3.0), (1, "2", 3.0)])
        r = self.client.post(f"/api/meetings/{mid}/reanalyze")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([p["label"] for p in r.json()["speakers"]], ["Người nói 1", "Người nói 2"])

    def test_infer_speakers_requires_llm(self):
        mid = self.create()
        self.assertEqual(self.client.post(f"/api/meetings/{mid}/infer-speakers").status_code, 503)

    # ---------------------------------------------------------- websockets ---
    def test_audio_ws_state_guard(self):
        with self.client.websocket_connect("/ws/meeting/99999/audio") as ws:
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")
            self.assertIn("không tồn tại", msg["text"])
        mid = self.create()
        with self.client.websocket_connect(f"/ws/meeting/{mid}/audio") as ws:
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")
            self.assertIn("SONIOX_API_KEY", msg["text"])
        with mock.patch.object(live.websockets, "connect", fake_connect), \
                mock.patch.dict(os.environ, {"SONIOX_API_KEY": "test-key"}):
            with self.client.websocket_connect(f"/ws/meeting/{mid}/audio") as ws:
                msg = ws.receive_json()
                self.assertEqual(msg["type"], "ready")
                ws.send_bytes(b"\x00" * 3200)
            self.client.post(f"/api/meetings/{mid}/end")
        with self.client.websocket_connect(f"/ws/meeting/{mid}/audio") as ws:
            msg = ws.receive_json()
            self.assertEqual(msg["type"], "error")
            self.assertIn("kết thúc", msg["text"])

    def test_events_ws_init_and_ping(self):
        mid = self.create()
        self.add_segments(mid, [(0, "1", 3.0), (1, "2", 3.0)])
        with self.client.websocket_connect(f"/ws/meeting/{mid}/events") as ws:
            init = ws.receive_json()
            self.assertEqual(init["type"], "init")
            self.assertEqual(len(init["speakers"]), 2)
            self.assertEqual(len(init["segments"]), 2)
            ws.send_text(json.dumps({"type": "ping"}))
            self.assertEqual(ws.receive_json()["type"], "pong")

    def test_archive_returns_immediately_then_minutes_done(self):
        mid = self.create()
        self.add_segments(mid, [(0, "1", 3.0, "Chốt release thứ sáu")])

        async def _llm(system, prompt, max_tokens=4000):
            return "# BIÊN BẢN CUỘC HỌP: Sprint 39"

        with mock.patch.object(artifacts, "llm_available", lambda: True), \
                mock.patch.object(artifacts, "_call_llm", _llm):
            r = self.client.post(f"/api/meetings/{mid}/archive").json()
            self.assertEqual((r["status"], r["minutes_status"]), ("ended", "pending"))
            for _ in range(100):
                if db.get_meeting(mid).get("minutes_status") == "done":
                    break
                time.sleep(0.03)
        self.assertEqual(db.get_meeting(mid)["minutes_status"], "done")
        self.assertEqual([a["kind"] for a in self.client.get(f"/api/meetings/{mid}").json()["artifacts"]], ["minutes"])

    # -------------------------------------------------------------- voices ---
    def test_voice_enroll_validation(self):
        silence = {"audio": ("v.pcm", b"\x00" * 32000 * 4, "application/octet-stream")}
        r = self.client.post("/api/voices/enroll-audio", data={"name": "Ai đó"}, files=silence)
        self.assertEqual(r.status_code, 400)
        self.assertIn("quá ngắn", r.json()["detail"])
        r = self.client.post("/api/voices/enroll-audio", data={"name": "  "}, files=silence)
        self.assertEqual(r.status_code, 400)

    @unittest.skipUnless(voice.available(), "Cần models/speaker.onnx")
    def test_voice_enroll_accepts_wav_44k_stereo(self):
        t = np.arange(int(5 * 44100)) / 44100
        sig = (np.sin(2 * np.pi * 180 * t) * 6000 + np.sin(2 * np.pi * 410 * t) * 3000).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(np.stack([sig, sig], axis=1).tobytes())
        r = self.client.post("/api/voices/enroll-audio", data={"name": "Mẫu thử", "role": "QA"},
                             files={"audio": ("v.wav", buf.getvalue(), "audio/wav")})
        self.assertEqual(r.status_code, 200, r.text)
        voices = self.client.get("/api/voices").json()["voices"]
        self.assertEqual([v["name"] for v in voices], ["Mẫu thử"])
        self.assertNotIn("embedding", voices[0])
        vid = voices[0]["id"]
        self.assertEqual(self.client.patch(f"/api/voices/{vid}", json={"role": "QA Lead"}).json()["voice"]["role"], "QA Lead")
        self.assertTrue(self.client.delete(f"/api/voices/{vid}").json()["success"])


if __name__ == "__main__":
    unittest.main()
