"""Lưu biên bản + ghi âm lên Google Drive của chủ nhóm: thư mục, Google Docs, MP3, chia sẻ, lưu lại, tự xóa ghi âm."""
import asyncio
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest import mock

import httpx
import numpy as np
from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import auth, db, drive_sync, gdrive, google_oauth, groups, live, recap_export, recording

WEB = dict(ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS=set(),
           CLIENT_ID="123-test.apps.googleusercontent.com", TTL_S=3600)
ENV = {"GOOGLE_OAUTH_CLIENT_ID": "123-test.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "s3cret",
       "AUTH_SECRET": "a-stable-secret-for-tests-0123456789"}
MINUTES = "# BIÊN BẢN CUỘC HỌP: Review\n\n## 1. Tóm tắt\nChốt phát hành **15/12**.\n\n| STT | Việc |\n|---|---|\n| 1 | Đối soát |\n"


class FakeDrive:
    """Google Drive giả: thư mục, tệp, nội dung, quyền chia sẻ (đủ cho những gì gdrive.Drive gọi)."""

    def __init__(self):
        self.files, self.sessions, self.n, self.fail = {}, {}, 0, 0
        self.calls = []

    def _new(self, meta, content=b"", mime=None):
        self.n += 1
        fid = f"f{self.n}"
        self.files[fid] = {"id": fid, "name": meta["name"], "mimeType": meta.get("mimeType") or mime or "",
                           "parents": meta.get("parents") or ["root"], "trashed": False,
                           "webViewLink": f"https://drive.google.com/{fid}", "content": content, "perms": {}}
        return self.files[fid]

    @staticmethod
    def _public(f):
        return {k: f[k] for k in ("id", "name", "mimeType", "parents", "trashed", "webViewLink")}

    def rmtree(self, fid):
        """Như Drive thật: xóa thư mục thì mọi thứ bên trong cũng mất."""
        for f in self.children(fid):
            self.rmtree(f["id"])
        self.files.pop(fid, None)

    def children(self, parent, name=None):
        return [f for f in self.files.values() if parent in f["parents"] and (name is None or f["name"] == name)]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        if self.fail:
            self.fail -= 1
            return httpx.Response(500, json={"error": {"message": "backend error"}})
        p, q = req.url.path, parse_qs(req.url.query.decode() if isinstance(req.url.query, bytes) else req.url.query)
        m = re.fullmatch(r"/drive/v3/files/([^/]+)/permissions(?:/([^/]+))?", p)
        if m:
            f = self.files.get(m.group(1))
            if f is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            if req.method == "POST":
                body = json.loads(req.content)
                pid = f"p{len(f['perms']) + 1}-{body['emailAddress']}"
                f["perms"][pid] = (body["emailAddress"], body["role"])
                return httpx.Response(200, json={"id": pid})
            f["perms"].pop(m.group(2), None)
            return httpx.Response(204)
        m = re.fullmatch(r"/(upload/)?drive/v3/files(?:/([^/]+))?", p)
        upload, fid = bool(m and m.group(1)), m.group(2) if m else None
        if upload and "upload_id" in q:                          # gửi nội dung của phiên resumable
            s = self.sessions.pop(q["upload_id"][0])
            if s["fid"]:
                self.files[s["fid"]]["content"] = req.content
                return httpx.Response(200, json=self._public(self.files[s["fid"]]))
            return httpx.Response(200, json=self._public(self._new(s["meta"], req.content, s["mime"])))
        if upload and q.get("uploadType") == ["resumable"]:
            sid = f"s{len(self.sessions) + self.n + 1}"
            self.sessions[sid] = {"meta": json.loads(req.content), "fid": fid, "mime": req.headers["x-upload-content-type"]}
            return httpx.Response(200, headers={"location": f"https://www.googleapis.com/upload/drive/v3/files?upload_id={sid}"})
        if upload:                                               # multipart: JSON + HTML
            boundary = req.headers["content-type"].split("boundary=")[1]
            parts = req.content.decode().split(f"--{boundary}")
            meta = json.loads(parts[1].split("\r\n\r\n", 1)[1].strip())
            html = parts[2].split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0].encode()
            if fid:
                self.files[fid].update(content=html, name=meta.get("name", self.files[fid]["name"]))
                return httpx.Response(200, json=self._public(self.files[fid]))
            return httpx.Response(200, json=self._public(self._new(meta, html)))
        if fid and req.method == "GET":
            f = self.files.get(fid)
            return httpx.Response(200, json=self._public(f)) if f else httpx.Response(404, json={"error": {"message": "x"}})
        if fid and req.method == "DELETE":
            return httpx.Response(204) if self.files.pop(fid, None) else httpx.Response(404, json={"error": {"message": "x"}})
        if fid and req.method == "PATCH":
            f = self.files[fid]
            body = json.loads(req.content or b"{}")
            if "name" in body:
                f["name"] = body["name"]
            if "addParents" in q:
                f["parents"] = [x for x in f["parents"] if x not in q.get("removeParents", [""])[0].split(",")] + q["addParents"]
            return httpx.Response(200, json=self._public(f))
        if req.method == "POST":
            return httpx.Response(200, json=self._public(self._new(json.loads(req.content))))
        query = q["q"][0]                                         # files.list theo tên trong thư mục cha
        name = re.search(r"name='((?:[^'\\]|\\.)*)'", query).group(1).replace("\\'", "'").replace("\\\\", "\\")
        parent = re.search(r"'([^']+)' in parents", query).group(1)
        return httpx.Response(200, json={"files": [self._public(f) for f in self.children(parent, name) if not f["trashed"]]})


class DriveSyncTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.fake = FakeDrive()
        self.tmp = tempfile.TemporaryDirectory()
        self.disconnected = set()

        async def token(email):
            if email in self.disconnected:
                raise google_oauth.NeedReconnect(f"{email} chưa kết nối Google Drive")
            return "tok"
        self.patches = [mock.patch.multiple(auth, **WEB), mock.patch.dict(os.environ, ENV),
                        mock.patch.object(google_oauth, "_transport", httpx.MockTransport(self.fake)),
                        mock.patch.object(google_oauth, "access_token", token),
                        mock.patch.object(drive_sync, "RETRY_DELAYS", (0.0, 0.0, 0.0)),
                        mock.patch.object(recording, "ROOT", Path(self.tmp.name))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def meeting(self, owner="an@urbox.vn", title="Review Sprint 40", gid=None, minutes=MINUTES, audio_s=1):
        mid = db.create_meeting(title, owner=owner)
        db.update_meeting(mid, {"started_at": 1791529200.0}, True)
        if gid:
            groups.set_meeting_group(mid, gid)
        if minutes:
            db.save_artifact(meeting_id=mid, kind="minutes", title="Biên bản", content=minutes, prompt_trigger="t")
        if audio_s:
            r = recording.Run(mid, "mic", 0.0)
            r.write((np.sin(np.arange(16000 * audio_s) / 16000 * 2 * np.pi * 300) * 5000).astype("<i2").tobytes())
            r.close()
        return mid

    def path_of(self, fid):
        parts, f = [], self.fake.files[fid]
        while True:
            parts.insert(0, f["name"])
            parent = f["parents"][0]
            if parent == "root":
                return "/".join(parts)
            f = self.fake.files[parent]

    def test_private_meeting_gets_folder_doc_and_audio(self):
        mid = self.meeting()
        d = asyncio.run(drive_sync.save_meeting(mid))
        self.assertEqual(d["status"], "done")
        folder = "Meeting Copilot/Cuộc họp riêng/09-10-2026 14h00 - Review Sprint 40"
        self.assertEqual(self.path_of(d["folder_id"]), folder)
        doc, audio = self.fake.files[d["doc_id"]], self.fake.files[d["audio_id"]]
        self.assertEqual(self.path_of(doc["id"]), f"{folder}/Biên bản - Review Sprint 40")
        self.assertEqual(doc["mimeType"], gdrive.DOC)
        self.assertIn(b"<table", doc["content"])
        self.assertIn("<b>15/12</b>".encode(), doc["content"])
        self.assertEqual((audio["name"], audio["mimeType"]), ("Ghi âm.mp3", "audio/mpeg"))
        self.assertGreater(len(audio["content"]), 500)
        self.assertEqual(db.get_meeting(mid)["drive"]["doc_url"], doc["webViewLink"])

    def test_saving_again_overwrites_instead_of_duplicating(self):
        mid = self.meeting()
        first = asyncio.run(drive_sync.save_meeting(mid))
        n_files = len(self.fake.files)
        db.save_artifact(meeting_id=mid, kind="minutes", title="Biên bản", content="# Biên bản mới\nĐã sửa.", prompt_trigger="t")
        puts = sum(1 for m_, _ in self.fake.calls if m_ == "PUT")
        again = asyncio.run(drive_sync.save_meeting(mid))
        self.assertEqual((again["doc_id"], again["folder_id"], again["audio_id"]),
                         (first["doc_id"], first["folder_id"], first["audio_id"]))
        self.assertEqual(len(self.fake.files), n_files)
        self.assertIn("Biên bản mới".encode(), self.fake.files[again["doc_id"]]["content"])
        self.assertEqual(sum(1 for m_, _ in self.fake.calls if m_ == "PUT"), puts)       # ghi âm không đổi: không tải lại

    def test_group_meeting_goes_to_owner_drive_and_folder_is_shared_with_members(self):
        g = groups.create_group("Sprint Payment", "an@urbox.vn")
        groups.update_group(g["id"], members=["binh@urbox.vn"])
        mid = self.meeting(owner="binh@urbox.vn", gid=g["id"])                # thành viên tạo cuộc họp
        d = asyncio.run(drive_sync.save_meeting(mid))
        self.assertEqual(d["keeper"], "an@urbox.vn")                         # vào Drive của chủ nhóm
        gfolder = groups.get_group(g["id"])["drive"]["folder_id"]
        self.assertEqual(self.path_of(gfolder), "Meeting Copilot/Sprint Payment")
        self.assertEqual(self.path_of(d["folder_id"]), "Meeting Copilot/Sprint Payment/09-10-2026 14h00 - Review Sprint 40")
        self.assertEqual([v for v in self.fake.files[gfolder]["perms"].values()], [("binh@urbox.vn", "writer")])
        groups.update_group(g["id"], members=["chi@urbox.vn"])
        asyncio.run(drive_sync.sync_group_sharing(g["id"]))
        self.assertEqual([v[0] for v in self.fake.files[gfolder]["perms"].values()], ["chi@urbox.vn"])

    def test_folder_deleted_on_drive_is_recreated_and_rename_follows(self):
        g = groups.create_group("BD", "an@urbox.vn")
        mid = self.meeting(gid=g["id"])
        d = asyncio.run(drive_sync.save_meeting(mid))
        old = groups.get_group(g["id"])["drive"]["folder_id"]
        self.fake.rmtree(old)                                                # chủ nhóm xóa thư mục trên Drive
        groups.update_group(g["id"], name="BD Miền Nam")
        d2 = asyncio.run(drive_sync.save_meeting(mid))
        self.assertNotEqual(d2["folder_id"], d["folder_id"])
        self.assertEqual(self.path_of(d2["doc_id"]), "Meeting Copilot/BD Miền Nam/09-10-2026 14h00 - Review Sprint 40/Biên bản - Review Sprint 40")

    def test_moving_meeting_into_a_group_moves_its_folder(self):
        mid = self.meeting()
        d = asyncio.run(drive_sync.save_meeting(mid))
        g = groups.create_group("Sprint Payment", "an@urbox.vn")
        groups.set_meeting_group(mid, g["id"])
        d2 = asyncio.run(drive_sync.save_meeting(mid))
        self.assertEqual(d2["folder_id"], d["folder_id"])
        self.assertEqual(self.path_of(d2["folder_id"]), "Meeting Copilot/Sprint Payment/09-10-2026 14h00 - Review Sprint 40")

    def test_keeper_not_connected_and_transient_errors(self):
        mid = self.meeting()
        self.disconnected.add("an@urbox.vn")
        asyncio.run(drive_sync._run(mid))
        self.assertEqual(db.get_meeting(mid)["drive"]["status"], "need_connect")
        self.disconnected.clear()
        self.fake.fail = 2                                                   # 2 lần Drive lỗi 500 rồi mới chạy
        asyncio.run(drive_sync._run(mid))
        self.assertEqual(db.get_meeting(mid)["drive"]["status"], "done")

    def test_old_recordings_are_deleted_but_minutes_kept(self):
        g = groups.create_group("BD", "an@urbox.vn")
        groups.update_group(g["id"], recording_drive_days=7)
        mid = self.meeting(gid=g["id"])
        d = asyncio.run(drive_sync.save_meeting(mid))
        self.assertEqual(asyncio.run(drive_sync.purge_recordings()), 0)      # còn trong hạn
        self.assertEqual(asyncio.run(drive_sync.purge_recordings(now=time.time() + 8 * 86400)), 1)
        self.assertNotIn(d["audio_id"], self.fake.files)
        self.assertIn(d["doc_id"], self.fake.files)
        d2 = asyncio.run(drive_sync.save_meeting(mid))
        self.assertIsNone(d2.get("audio_id"))                                # không tải lại ghi âm đã xóa theo hạn

    def test_minutes_done_schedules_drive_save(self):
        mid = self.meeting(minutes=None, audio_s=0)

        async def fake_minutes(meeting_id, *a, **k):
            aid = db.save_artifact(meeting_id=meeting_id, kind="minutes", title="Biên bản", content=MINUTES, prompt_trigger="t")
            return db.get_artifact(aid)

        async def go():
            s = await live.get_session(mid)
            with mock.patch.object(live.artifacts, "generate_meeting_minutes", fake_minutes), \
                    mock.patch.object(live.artifacts, "meeting_insights", mock.AsyncMock(return_value=[])), \
                    mock.patch.object(drive_sync, "schedule") as sch:
                await s._finalize_minutes()
            s.dispose()
            return sch
        self.assertEqual(asyncio.run(go()).call_args[0][0], mid)

    def test_api_save_and_deleting_recording_removes_it_from_drive(self):
        mid = self.meeting()
        d = asyncio.run(drive_sync.save_meeting(mid))
        h = {"Authorization": f"Bearer {auth.issue({'email': 'an@urbox.vn', 'name': 'An'})['token']}"}
        with TestClient(appmod.app) as c:
            self.assertEqual(c.post(f"/api/meetings/{mid}/drive-save", headers=h).status_code, 200)
            self.assertEqual(c.delete(f"/api/meetings/{mid}/recording", headers=h).status_code, 200)
        self.assertNotIn(d["audio_id"], self.fake.files)
        self.assertTrue(db.get_meeting(mid)["drive"].get("audio_deleted_at"))


if __name__ == "__main__":
    unittest.main()
