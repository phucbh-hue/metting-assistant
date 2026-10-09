"""Kết nối Google Drive của từng người: OAuth authorization code, refresh token mã hóa, tự làm mới, thu hồi."""
import asyncio
import base64
import json
import os
import time
import unittest
from urllib.parse import parse_qs, urlparse
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import auth, db, google_oauth

WEB = dict(ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS=set(),
           CLIENT_ID="123-test.apps.googleusercontent.com", TTL_S=3600)
ENV = {"GOOGLE_OAUTH_CLIENT_ID": "123-test.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "s3cret",
       "AUTH_SECRET": "a-stable-secret-for-tests-0123456789", "PUBLIC_BASE_URL": ""}
DRIVE = "https://www.googleapis.com/auth/drive.file"


def id_token(email):
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'RS256'})}.{seg({'email': email, 'email_verified': True})}.sig"


class FakeGoogle:
    """Giả máy chủ OAuth của Google."""

    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        self.calls.append((str(request.url), form))
        if request.url.path == "/revoke":
            return httpx.Response(200)
        grant = form.get("grant_type", [""])[0]
        if grant == "authorization_code":
            code = form["code"][0]
            if form.get("client_secret") != ["s3cret"]:
                return httpx.Response(401, json={"error": "invalid_client"})
            if code == "bad":
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"})
            email = "someone@gmail.com" if code == "other" else "an@urbox.vn"
            scope = "openid email" if code == "nodrive" else f"openid {DRIVE} https://www.googleapis.com/auth/userinfo.email"
            return httpx.Response(200, json={"access_token": "at1", "expires_in": 3599, "refresh_token": "rt-secret-123",
                                             "scope": scope, "token_type": "Bearer", "id_token": id_token(email)})
        if grant == "refresh_token":
            if form["refresh_token"][0] == "rt-secret-123":
                return httpx.Response(200, json={"access_token": "at2", "expires_in": 3599, "scope": DRIVE})
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(404)


class GoogleOAuthTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.google = FakeGoogle()
        self.patches = [mock.patch.multiple(auth, **WEB), mock.patch.dict(os.environ, ENV),
                        mock.patch.object(google_oauth, "_transport", httpx.MockTransport(self.google))]
        for p in self.patches:
            p.start()
        google_oauth._ACCESS.clear()
        self.client = TestClient(appmod.app)
        self.client.__enter__()
        self.h = {"Authorization": f"Bearer {auth.issue({'email': 'an@urbox.vn', 'name': 'An'})['token']}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for p in self.patches:
            p.stop()

    def connect_url(self, return_to="http://testserver/"):
        r = self.client.post("/api/google/connect", json={"return_to": return_to}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["url"]

    def callback(self, url, code="good", state=None):
        q = parse_qs(urlparse(url).query)
        r = self.client.get("/api/google/callback", params={"code": code, "state": state or q["state"][0]},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return r.headers["location"]

    def test_consent_url_asks_for_drive_file_and_calendar_offline(self):
        q = parse_qs(urlparse(self.connect_url()).query)
        self.assertEqual(q["client_id"], [ENV["GOOGLE_OAUTH_CLIENT_ID"]])
        self.assertEqual(set(q["scope"][0].split()), {"openid", "email", DRIVE,
                                                      "https://www.googleapis.com/auth/calendar.events.readonly"})
        self.assertEqual((q["access_type"], q["prompt"], q["login_hint"]), (["offline"], ["consent"], ["an@urbox.vn"]))
        self.assertEqual(q["redirect_uri"], ["http://testserver/api/google/callback"])
        r = self.client.post("/api/google/connect", json={"return_to": "https://evil.example.com/"}, headers=self.h)
        self.assertEqual(r.status_code, 400)                 # không chuyển hướng tới trang lạ

    def test_connect_stores_encrypted_refresh_token(self):
        loc = self.callback(self.connect_url())
        self.assertTrue(loc.startswith("http://testserver/#/?drive=ok"), loc)
        st = self.client.get("/api/google/status", headers=self.h).json()
        self.assertTrue(st["connected"])
        self.assertEqual(st["google_email"], "an@urbox.vn")
        doc = db._get_db()["google_tokens"].find_one({"email": "an@urbox.vn"})
        self.assertNotIn("rt-secret-123", json.dumps(doc, default=str))      # chỉ lưu bản đã mã hóa
        self.assertEqual(asyncio.run(google_oauth.access_token("an@urbox.vn")), "at1")

    def test_tampered_state_wrong_account_or_no_drive_permission_is_refused(self):
        url = self.connect_url()
        state = parse_qs(urlparse(url).query)["state"][0]
        self.assertIn("drive=err", self.callback(url, state=state[:-2] + ("aa" if not state.endswith("aa") else "bb")))
        self.assertIn("drive=err", self.callback(self.connect_url(), code="other"))      # tài khoản Google khác
        self.assertIn("drive=err", self.callback(self.connect_url(), code="nodrive"))    # bỏ tick quyền Drive
        self.assertIn("drive=err", self.callback(self.connect_url(), code="bad"))
        self.assertFalse(self.client.get("/api/google/status", headers=self.h).json()["connected"])
        r = self.client.get("/api/google/callback", params={"error": "access_denied", "state": state}, follow_redirects=False)
        self.assertIn("drive=denied", r.headers["location"])

    def test_access_token_refreshes_and_revoked_grant_means_reconnect(self):
        self.callback(self.connect_url())
        google_oauth._ACCESS["an@urbox.vn"] = ("at1", time.time() - 5)                  # hết hạn
        self.assertEqual(asyncio.run(google_oauth.access_token("an@urbox.vn")), "at2")
        google_oauth._ACCESS.clear()
        db._get_db()["google_tokens"].update_one({"email": "an@urbox.vn"},
                                                 {"$set": {"refresh_enc": google_oauth._encrypt("rt-revoked", "an@urbox.vn")}})
        with self.assertRaises(google_oauth.NeedReconnect):
            asyncio.run(google_oauth.access_token("an@urbox.vn"))
        self.assertFalse(google_oauth.status("an@urbox.vn")["connected"])
        with self.assertRaises(google_oauth.NeedReconnect):
            asyncio.run(google_oauth.access_token("chua-ket-noi@urbox.vn"))

    def test_disconnect_revokes_at_google(self):
        self.callback(self.connect_url())
        st = self.client.post("/api/google/disconnect", headers=self.h).json()
        self.assertFalse(st["connected"])
        self.assertTrue(any("/revoke" in url for url, _ in self.google.calls))

    def test_local_mode_has_no_google_connect(self):
        with mock.patch.object(auth, "ENABLED", False):
            self.assertEqual(self.client.post("/api/google/connect", json={}).status_code, 400)
            self.assertFalse(self.client.get("/api/google/status").json()["available"])


if __name__ == "__main__":
    unittest.main()
