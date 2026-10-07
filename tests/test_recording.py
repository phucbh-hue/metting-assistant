"""Test lưu âm thanh cuộc họp: mặc định tắt, chỉ bật khi xác nhận mọi người đồng ý, tự xóa khi quá hạn."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from tests.test_meeting_session import SessionTestCase
from meeting import app as appmod
from meeting import db, live, recording


class RecordingTests(SessionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(recording, "ROOT", Path(self.tmp.name))
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        await super().asyncTearDown()

    async def feed(self, st, seconds):
        with mock.patch.object(st, "_lost"):
            await st.feed(b"\x01\x00" * int(16000 * seconds))

    async def test_records_only_after_consent_and_stops_when_turned_off(self):
        mid = db.create_meeting("Họp ghi âm")
        s = await live.get_session(mid)
        st = live.MeetingStream("mic", s)
        await self.feed(st, 1.0)
        self.assertEqual(recording.info(mid)["runs"], 0)              # mặc định không ghi
        pub = await s.set_recording(True)
        self.assertTrue(pub["enabled"])
        self.assertTrue(db.get_meeting(mid)["recording"]["consent_at"])
        await self.feed(st, 2.0)
        await st.pause()                                             # tắt mic: hết một lượt
        await self.feed(st, 1.5)                                     # bật mic lại: lượt mới
        st.stop_recording()
        runs = recording.runs(mid)
        self.assertEqual([r["file"] for r in runs], ["mic-001.pcm", "mic-002.pcm"])
        self.assertEqual([round(Path(r["path"]).stat().st_size / 32000, 1) for r in runs], [2.0, 1.5])
        self.assertLessEqual(runs[0]["start_t"], runs[1]["start_t"])
        await s.set_recording(False)
        await self.feed(st, 1.0)
        self.assertEqual(round(recording.info(mid)["seconds"], 1), 3.5)  # tắt rồi thì không ghi thêm
        wavs = recording.export_wav(mid, Path(self.tmp.name) / "wav")
        self.assertTrue(all(Path(w["wav"]).exists() for w in wavs))
        self.assertTrue(recording.delete(mid))
        self.assertEqual(recording.info(mid)["runs"], 0)

    def test_expired_recordings_are_purged(self):
        d = recording.meeting_dir(5)
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"runs": [{"file": "mic-001.pcm", "created_at": time.time() - 40 * 86400}]}),
                                         encoding="utf-8")
        fresh = recording.meeting_dir(6)
        fresh.mkdir(parents=True)
        (fresh / "manifest.json").write_text(json.dumps({"runs": [{"file": "mic-001.pcm", "created_at": time.time()}]}),
                                             encoding="utf-8")
        with mock.patch.object(recording, "RETENTION_DAYS", 30):
            self.assertEqual(recording.purge_expired(), 1)
        self.assertFalse(d.exists())
        self.assertTrue(fresh.exists())


class RecordingApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(recording, "ROOT", Path(self.tmp.name))
        self.patch.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        self.tmp.cleanup()

    def test_enable_needs_consent_and_delete_meeting_removes_audio(self):
        mid = db.create_meeting("Họp")
        r = self.client.post(f"/api/meetings/{mid}/recording", json={"enabled": True})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/meetings/{mid}/recording", json={"enabled": True, "consent": True}).json()
        self.assertTrue(r["enabled"])
        self.assertTrue(self.client.get(f"/api/meetings/{mid}").json()["recording"]["enabled"])
        recording.meeting_dir(mid).mkdir(parents=True, exist_ok=True)
        (recording.meeting_dir(mid) / "manifest.json").write_text('{"runs": []}', encoding="utf-8")
        self.client.delete(f"/api/meetings/{mid}")
        self.assertFalse(recording.meeting_dir(mid).exists())
