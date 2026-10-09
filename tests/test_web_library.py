"""Bản web (AUTH_REQUIRED=1): mỗi người chỉ thấy cuộc họp của mình; tài liệu lấy từ thư mục trên máy người dùng."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.helpers import reset_db
from tests.test_meeting_session import SessionTestCase
from meeting import app as appmod
from meeting import auth, db, decks, live

CLIENT = "123-test.apps.googleusercontent.com"
WEB = dict(ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS={"admin@urbox.vn"}, CLIENT_ID=CLIENT,
           TTL_S=3600)
MD = "# Kế hoạch quý 4\n\n## Mục tiêu\n- Tăng đối tác\n\n## Lộ trình\n- Tháng 10: thử nghiệm\n".encode()


def hdr(email):
    return {"Authorization": f"Bearer {auth.issue({'email': email, 'name': email})['token']}"}


class MeetingOwnershipTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.p = mock.patch.multiple(auth, **WEB)
        self.p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.p.stop()

    def test_each_person_only_sees_their_own_meetings(self):
        a, b, admin = hdr("an@urbox.vn"), hdr("binh@urbox.vn"), hdr("admin@urbox.vn")
        mid = self.client.post("/api/meetings", json={"title": "Họp của An"}, headers=a).json()["meeting_id"]
        legacy = db.create_meeting("Họp cũ trước khi có đăng nhập")
        art = db.save_artifact(meeting_id=mid, kind="report", title="Báo cáo", content="x", prompt_trigger="t")
        titles = lambda h: [m["title"] for m in self.client.get("/api/meetings", headers=h).json()["meetings"]]
        self.assertEqual(titles(a), ["Họp của An"])
        self.assertEqual(titles(b), [])
        self.assertEqual(titles(admin), ["Họp cũ trước khi có đăng nhập"])      # chủ cũ: quản trị viên
        self.assertEqual(self.client.get(f"/api/meetings/{mid}", headers=a).status_code, 200)
        for path in (f"/api/meetings/{mid}", f"/api/meetings/{mid}/export", f"/api/artifacts/{art}"):
            self.assertEqual(self.client.get(path, headers=b).status_code, 404, path)
            self.assertEqual(self.client.get(path, headers=admin).status_code, 404, path)   # quản trị viên cũng không
        self.assertEqual(self.client.delete(f"/api/meetings/{mid}", headers=b).status_code, 404)
        self.assertEqual(self.client.get(f"/api/meetings/{legacy}", headers=a).status_code, 404)
        self.assertEqual(self.client.get(f"/api/meetings/{legacy}", headers=admin).status_code, 200)
        tok_b = hdr("binh@urbox.vn")["Authorization"][7:]
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"/ws/meeting/{mid}/events?token={tok_b}") as ws:
                ws.receive_text()
        self.assertEqual(db.get_meeting(mid)["owner"], "an@urbox.vn")
        self.assertEqual(self.client.get("/api/stats", headers=b).json()["meetings_total"], 0)   # không đếm của người khác
        self.assertEqual(self.client.get("/api/stats", headers=a).json()["meetings_total"], 1)

    def test_usage_hides_other_peoples_meeting_titles(self):
        a, b = hdr("an@urbox.vn"), hdr("binh@urbox.vn")
        mid = self.client.post("/api/meetings", json={"title": "Họp bí mật"}, headers=a).json()["meeting_id"]
        db.record_llm_usage(mid, "claude", "m", "biên bản", 10, 5, 1.0)
        self.assertIn("Họp bí mật", str(self.client.get("/api/usage", headers=a).json()))
        usage = self.client.get("/api/usage", headers=b).json()
        self.assertNotIn("Họp bí mật", str(usage))
        self.assertIn("(cuộc họp của người khác)", str(usage))

    def test_server_library_is_off_on_the_web(self):
        a = hdr("an@urbox.vn")
        mid = self.client.post("/api/meetings", json={"title": "Họp"}, headers=a).json()["meeting_id"]
        self.assertTrue(self.client.get("/api/library", headers=a).json()["client"])
        r = self.client.post(f"/api/meetings/{mid}/open-file", json={"path": "C:/x.pdf"}, headers=a)
        self.assertEqual(r.status_code, 400)


class DocumentUploadApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.multiple(auth, **WEB), mock.patch.object(decks, "UPLOAD_DIR", Path(self.tmp.name))]
        for p in self.patches:
            p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()
        self.h = hdr("an@urbox.vn")
        self.mid = self.client.post("/api/meetings", json={"title": "Họp"}, headers=self.h).json()["meeting_id"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def leftovers(self):
        return [p for p in Path(self.tmp.name).rglob("*") if p.is_file()]

    def test_open_uploaded_file_then_original_is_deleted(self):
        r = self.client.post(f"/api/meetings/{self.mid}/doc-upload", headers=self.h, data={"purpose": "open", "folder": "Q4"},
                             files={"file": ("Kế hoạch quý 4.md", MD, "text/markdown")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["artifact_id"])
        said = " ".join(e.get("text", "") for e in r.json()["events"] if e.get("type") == "ai_say")
        self.assertIn("Kế hoạch quý 4.md", said)
        self.assertIn("trong thư mục Q4", said)
        self.assertEqual(self.leftovers(), [])             # server không giữ tệp gốc của người dùng

    def test_script_file_is_read_as_text(self):
        seen = {}

        async def fake_answer(self_, ans):
            seen.update(ans)
            return {"ok": True}
        with mock.patch.object(live.MeetingSession, "answer_present", fake_answer):
            r = self.client.post(f"/api/meetings/{self.mid}/doc-upload", headers=self.h, data={"purpose": "script"},
                                 files={"file": ("kich-ban.txt", "Slide 1: Chào anh chị\nSlide 2: Mục tiêu".encode(), "text/plain")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((seen["source"], seen["name"]), ("text", "kich-ban.txt"))
        self.assertIn("Slide 2: Mục tiêu", seen["text"])
        self.assertEqual(self.leftovers(), [])

    def test_bad_type_and_too_large_are_refused(self):
        r = self.client.post(f"/api/meetings/{self.mid}/doc-upload", headers=self.h, data={"purpose": "open"},
                             files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
        self.assertEqual(r.status_code, 400)
        with mock.patch.object(decks, "UPLOAD_MAX_MB", 0.001):
            r = self.client.post(f"/api/meetings/{self.mid}/doc-upload", headers=self.h, data={"purpose": "open"},
                                 files={"file": ("lon.md", b"#" * 5000, "text/markdown")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.leftovers(), [])
        other = hdr("binh@urbox.vn")         # người khác không gửi tệp vào cuộc họp của An được
        r = self.client.post(f"/api/meetings/{self.mid}/doc-upload", headers=other, data={"purpose": "open"},
                             files={"file": ("a.md", MD, "text/markdown")})
        self.assertEqual(r.status_code, 404)

    def test_client_library_keeps_only_supported_files(self):
        files = [{"fid": "d1/a.pdf", "rel": "a.pdf", "root": "Tài liệu", "name": "a", "ext": ".pdf"},
                 {"fid": "d1/b.exe", "rel": "b.exe", "root": "Tài liệu", "name": "b", "ext": ".exe"},
                 {"fid": "", "rel": "c.md", "name": "c", "ext": ".md"}]
        r = self.client.post(f"/api/meetings/{self.mid}/client-library", headers=self.h, json={"files": files})
        self.assertEqual(r.json()["files"], 1)


class VoiceOpenFromUserComputerTests(SessionTestCase):
    """"Jarvis, mở file kế hoạch quý 4": tìm theo tên trong thư mục người dùng đã chọn, xin trình duyệt gửi đúng tệp đó."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.multiple(auth, **WEB), mock.patch.object(decks, "UPLOAD_DIR", Path(self.tmp.name))]
        for p in self.patches:
            p.start()
        self.mid = db.create_meeting("Họp", owner="an@urbox.vn")
        self.s = await live.get_session(self.mid)
        self.s.set_client_library([
            {"fid": "d1/Q4/ke-hoach.md", "rel": "Q4/Kế hoạch quý 4.md", "root": "Tài liệu", "name": "Kế hoạch quý 4", "ext": ".md"},
            {"fid": "d1/bao-cao.pdf", "rel": "Báo cáo tài chính.pdf", "root": "Tài liệu", "name": "Báo cáo tài chính", "ext": ".pdf"}])

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        await super().asyncTearDown()

    async def browser(self, q, send=True):
        """Giả trình duyệt: chờ need_file rồi gửi tệp (hoặc báo không gửi được)."""
        while True:
            ev = await asyncio.wait_for(q.get(), 5)
            if ev.get("type") == "need_file":
                break
        self.assertEqual((ev["fid"], ev["purpose"]), ("d1/Q4/ke-hoach.md", "open"))
        if send:
            path = decks.save_upload(self.mid, ev["name"], [MD])
            self.assertTrue(self.s.resolve_client_file(ev["request_id"], path=str(path)))
        else:
            self.s.resolve_client_file(ev["request_id"], error="chưa cho phép đọc thư mục")

    async def test_voice_open_asks_browser_for_the_matching_file(self):
        q = await self.s.subscribe()
        await asyncio.gather(self.s._open_deck_file("mở file kế hoạch quý 4"), self.browser(q))
        arts = db.get_artifacts(self.mid)
        self.assertEqual(len(arts), 1)
        self.assertIn("Kế hoạch quý 4", arts[0]["title"])
        self.assertEqual([p for p in Path(self.tmp.name).rglob("*") if p.is_file()], [])

    async def test_browser_cannot_send_file(self):
        q = await self.s.subscribe()
        t0 = time.monotonic()
        await asyncio.gather(self.s._open_deck_file("mở file kế hoạch quý 4"), self.browser(q, send=False))
        self.assertLess(time.monotonic() - t0, 3)          # không chờ hết hạn khi trình duyệt đã báo lỗi
        said = [e["text"] for e in self.events(q) if e.get("type") == "ai_say"]
        self.assertTrue(any("chưa lấy được tệp" in t for t in said), said)
        self.assertEqual(db.get_artifacts(self.mid), [])

    async def test_no_folder_chosen_yet(self):
        self.s.client_files = []
        q = await self.s.subscribe()
        await self.s._open_deck_file("mở file kế hoạch quý 4")
        said = [e["text"] for e in self.events(q) if e.get("type") == "ai_say"]
        self.assertTrue(any("chưa chọn thư mục" in t for t in said), said)


if __name__ == "__main__":
    unittest.main()
