"""Test đăng nhập của bản web (AUTH_REQUIRED=1): Google chỉ @urbox.vn, token ký HMAC, quyền quản trị, WebSocket."""
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import auth, db

CLIENT = "123-test.apps.googleusercontent.com"


def google_info(email="an.nv@urbox.vn", **kw):
    info = {"aud": CLIENT, "iss": "https://accounts.google.com", "exp": str(int(time.time()) + 600),
            "email": email, "email_verified": "true", "name": "Nguyễn Văn An"}
    info.update(kw)
    return info


class AuthTokenTests(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.multiple(auth, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS={"admin@urbox.vn"},
                                     CLIENT_ID=CLIENT, TTL_S=3600)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_token_roundtrip_expiry_and_tamper(self):
        tok = auth.issue({"email": "an.nv@urbox.vn", "name": "An"})["token"]
        self.assertEqual(auth.check(tok)["email"], "an.nv@urbox.vn")
        self.assertFalse(auth.check(tok)["admin"])
        self.assertIsNone(auth.check(tok, now=time.time() + 7200))                 # hết hạn
        head, body, sig = tok.split(".")
        self.assertIsNone(auth.check(f"{head}.{body}x.{sig}"))                       # sửa nội dung
        with mock.patch.object(auth, "SECRET", b"z" * 32):
            self.assertIsNone(auth.check(tok))                                       # khóa ký khác
        self.assertIsNone(auth.check("Bearer abc"))

    def test_google_token_checks(self):
        with mock.patch.object(auth, "_tokeninfo", return_value=google_info()):
            self.assertEqual(auth.verify_google("x" * 20), {"email": "an.nv@urbox.vn", "name": "Nguyễn Văn An"})
        bad = [google_info(email="an@gmail.com"), google_info(aud="other-client"), google_info(email_verified="false"),
               google_info(exp=str(int(time.time()) - 5)), google_info(iss="evil.example.com"),
               google_info(email="an@urbox.vn.evil.com")]
        for info in bad:
            with self.subTest(info=info), mock.patch.object(auth, "_tokeninfo", return_value=info):
                with self.assertRaises(auth.AuthError):
                    auth.verify_google("x" * 20)

    def test_access_log_does_not_contain_token(self):
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                ("1.2.3.4", "GET", "/api/deck-assets/a/b.png?token=v1.abc.def&x=1", "1.1", 200), None)
        auth.RedactTokenFilter().filter(rec)
        self.assertNotIn("v1.abc.def", rec.getMessage())
        self.assertIn("token=***&x=1", rec.getMessage())


class WebModeApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.p = mock.patch.multiple(auth, ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"],
                                     ADMINS={"admin@urbox.vn"}, CLIENT_ID=CLIENT, TTL_S=3600)
        self.p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.p.stop()

    def login(self, email="an.nv@urbox.vn"):
        with mock.patch.object(auth, "_tokeninfo", return_value=google_info(email=email)):
            r = self.client.post("/api/auth/google", json={"credential": "x" * 40})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["token"]

    def test_public_and_protected_paths(self):
        for path in ("/healthz", "/api/auth/config", "/config.js", "/"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/api/auth/config").json()["google_client_id"], CLIENT)
        for path in ("/api/meetings", "/api/health", "/api/voices", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        # 401 vẫn có header CORS: trang trên Vercel đọc được để hiện màn hình đăng nhập
        r = self.client.get("/api/meetings", headers={"Origin": "https://meeting.vercel.app"})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.headers.get("access-control-allow-origin"))

    def test_login_then_use_api_with_header_or_query_token(self):
        tok = self.login()
        me = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
        self.assertEqual(me["user"]["email"], "an.nv@urbox.vn")
        self.assertEqual(self.client.get("/api/meetings", headers={"Authorization": f"Bearer {tok}"}).status_code, 200)
        self.assertEqual(self.client.get(f"/api/meetings?token={tok}").status_code, 200)       # ảnh, WebSocket
        with mock.patch.object(auth, "_tokeninfo", return_value=google_info(email="an@gmail.com")):
            r = self.client.post("/api/auth/google", json={"credential": "x" * 40})
        self.assertEqual(r.status_code, 403)
        self.assertIn("urbox.vn", r.json()["detail"])

    def test_only_admins_change_shared_settings(self):
        user, admin = self.login(), self.login("admin@urbox.vn")
        h = lambda t: {"Authorization": f"Bearer {t}"}
        self.assertEqual(self.client.get("/api/settings/assistant", headers=h(user)).status_code, 200)
        body = {"name": "Bông", "aliases": []}
        self.assertEqual(self.client.put("/api/settings/assistant", json=body, headers=h(user)).status_code, 403)
        self.assertEqual(self.client.put("/api/settings/assistant", json=body, headers=h(admin)).status_code, 200)
        self.assertEqual(self.client.post("/api/llm/connect/claude-cli", headers=h(user)).status_code, 403)
        # Đổi khóa dịch vụ: không còn dựa vào địa chỉ IP (sau proxy giả được), chỉ quản trị viên
        with mock.patch.object(appmod.envfile, "set_value", return_value={"set": True}) as sv:
            r = self.client.put("/api/settings/secrets", json={"name": "SONIOX_API_KEY", "value": "abc"}, headers=h(admin))
        self.assertEqual(r.status_code, 200, r.text)
        sv.assert_called_once()
        # Người dùng thường vẫn tạo và dùng cuộc họp bình thường
        self.assertEqual(self.client.post("/api/meetings", json={"title": "Họp"}, headers=h(user)).status_code, 200)

    def test_websocket_needs_token_and_recording_consent_names_the_user(self):
        tok = self.login()
        mid = self.client.post("/api/meetings", json={"title": "Họp web"},
                               headers={"Authorization": f"Bearer {tok}"}).json()["meeting_id"]
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"/ws/meeting/{mid}/events") as ws:
                ws.receive_text()
        with self.client.websocket_connect(f"/ws/meeting/{mid}/events?token={tok}") as ws:
            self.assertIn('"meeting"', ws.receive_text())
        from meeting import recording
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(recording, "ROOT", Path(tmp)):
            r = self.client.post(f"/api/meetings/{mid}/recording", json={"enabled": True, "consent": True},
                                 headers={"Authorization": f"Bearer {tok}"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(db.get_meeting(mid)["recording"]["consent_by"], "an.nv@urbox.vn")
            self.client.delete(f"/api/meetings/{mid}/recording", headers={"Authorization": f"Bearer {tok}"})


class LocalModeTests(unittest.TestCase):
    """Chạy trên máy (mặc định): không đăng nhập, như trước."""

    def test_no_login_needed(self):
        reset_db()
        with TestClient(appmod.app) as c:
            self.assertFalse(c.get("/api/auth/config").json()["required"])
            self.assertEqual(c.get("/api/meetings").status_code, 200)
            self.assertEqual(c.post("/api/auth/google", json={"credential": "x" * 40}).status_code, 400)


if __name__ == "__main__":
    unittest.main()


class EnvValueTests(unittest.TestCase):
    """Lỗi thật 08/10/2026: chép MONGODB_URL từ .env (có dấu nháy) sang Render, server không kết nối được Atlas."""

    def test_values_pasted_from_env_file_are_cleaned(self):
        from meeting import envfile
        url = "mongodb+srv://u:p@x.mongodb.net/?retryWrites=true"
        for raw in (f'"{url}"', f"'{url}'", f"  {url}  ", f'MONGODB_URL="{url}"', f"MONGODB_URL={url}", url):
            self.assertEqual(envfile.clean_value("MONGODB_URL", raw), url, raw)
        self.assertEqual(envfile.clean_value("SONIOX_API_KEY", '"abc"'), "abc")
        self.assertEqual(envfile.clean_value("X", '"a"b"'), 'a"b')       # chỉ bỏ cặp nháy bao ngoài
        with mock.patch.dict("os.environ", {"SONIOX_API_KEY": '"abc"', "ANTHROPIC_API_KEY": "sk-ant-x"}):
            self.assertEqual(envfile.clean_environ(("SONIOX_API_KEY", "ANTHROPIC_API_KEY")), ["SONIOX_API_KEY"])
            import os
            self.assertEqual(os.environ["SONIOX_API_KEY"], "abc")

    def test_wrong_scheme_message_points_at_the_pasted_value(self):
        from meeting import db
        msg = db._explain_mongo_error(ValueError("Invalid URI scheme: URI must begin with 'mongodb://' or 'mongodb+srv://'"))
        self.assertIn("dấu nháy", msg)
