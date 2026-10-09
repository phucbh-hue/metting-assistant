"""Gói AI (Claude.ai / ChatGPT) riêng của từng người trên bản web: thư mục đăng nhập riêng, AI của cuộc họp dùng gói
của người tạo, chưa kết nối thì dùng API công ty, không ai dùng chung gói của người khác."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import artifacts, auth, cli_llm, db, user_llm

WEB = dict(ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS={"admin@urbox.vn"},
           CLIENT_ID="123-test.apps.googleusercontent.com", TTL_S=3600)


def login_claude(home: Path, plan="max"):
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"refreshToken": "r", "subscriptionType": plan}}),
                                         encoding="utf-8")


class CliHomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a, self.b = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_home_has_its_own_login(self):
        env = cli_llm._env(home=self.a)
        self.assertEqual((env["HOME"], env["CLAUDE_CONFIG_DIR"], env["CODEX_HOME"]),
                         (str(self.a), str(self.a / ".claude"), str(self.a / ".codex")))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        login_claude(self.a)
        self.assertEqual(cli_llm._login_state("claude-cli", self.a), {"logged_in": True, "plan": "max"})
        self.assertFalse(cli_llm._login_state("claude-cli", self.b)["logged_in"])      # người khác chưa đăng nhập
        (self.b / ".codex").mkdir(parents=True)
        (self.b / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"a": 1}}), encoding="utf-8")
        self.assertTrue(cli_llm._login_state("codex-cli", self.b)["logged_in"])
        self.assertFalse(cli_llm._login_state("codex-cli", self.a)["logged_in"])

    def test_server_login_uses_device_code_for_codex(self):
        with mock.patch.object(cli_llm, "resolve_cmd", lambda name: [f"/bin/{name}"]):
            cmd, env, _ = cli_llm.login_command("codex-cli", self.a)
            self.assertEqual(cmd, ["/bin/codex", "login", "--device-auth"])
            self.assertEqual(env["CODEX_HOME"], str(self.a / ".codex"))
            self.assertEqual(cli_llm.login_command("codex-cli")[0], ["/bin/codex", "login"])     # trên máy: như cũ
            self.assertEqual(cli_llm.login_command("claude-cli", self.a)[0], ["/bin/claude", "auth", "login", "--claudeai"])

    def test_logout_removes_only_that_persons_login(self):
        login_claude(self.a)
        login_claude(self.b)
        cli_llm.logout("claude-cli", self.a)
        self.assertFalse(cli_llm._login_state("claude-cli", self.a)["logged_in"])
        self.assertTrue(cli_llm._login_state("claude-cli", self.b)["logged_in"])


class MeetingUsesOwnersSubscriptionTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.multiple(auth, **WEB), mock.patch.object(user_llm, "BASE", Path(self.tmp.name)),
                        mock.patch.object(cli_llm, "installed", lambda p: True)]
        for p in self.patches:
            p.start()
        self.calls = []

        def fake_run(provider, system, prompt, timeout=None, model=None, home=None):
            self.calls.append((provider, home))
            if getattr(self, "run_fails", False):
                raise RuntimeError("Gói Claude.ai đã hết hạn mức tạm thời")
            return {"text": "qua gói riêng", "model": "sonnet", "input": 1, "output": 1, "cache_read": 0,
                    "cache_write": 0, "estimated": False}
        self.run_patch = mock.patch.object(cli_llm, "run", fake_run)
        self.run_patch.start()

        async def fake_api(system, prompt, max_tokens=4000, model=None):
            self.calls.append(("api", None))
            return "qua API công ty"
        self.api_patch = mock.patch.object(artifacts, "_claude_text", fake_api)
        self.api_patch.start()
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})
        self.env.start()

    def tearDown(self):
        for p in (self.env, self.api_patch, self.run_patch, *self.patches):
            p.stop()
        self.tmp.cleanup()

    def ask(self, mid):
        async def go():
            artifacts.set_meeting(mid, "trợ lý")
            return await artifacts._call_llm("hệ thống", "câu hỏi")
        return asyncio.run(go())

    def test_owner_with_subscription_uses_it_others_use_company_api(self):
        user_llm.choose("an@urbox.vn", "claude-cli")
        login_claude(user_llm.home_for("an@urbox.vn"))
        mine = db.create_meeting("Họp của An", owner="an@urbox.vn")
        other = db.create_meeting("Họp của Bình", owner="binh@urbox.vn")
        self.assertEqual(self.ask(mine), "qua gói riêng")
        self.assertEqual(self.calls[-1], ("claude-cli", user_llm.home_for("an@urbox.vn")))
        self.assertEqual(self.ask(other), "qua API công ty")                # Bình chưa kết nối: không dùng gói của An
        user_llm.choose("an@urbox.vn", None)
        self.assertEqual(self.ask(mine), "qua API công ty")                 # An chọn lại API công ty

    def test_chosen_but_not_logged_in_falls_back_to_company_api(self):
        user_llm.choose("an@urbox.vn", "codex-cli")
        mid = db.create_meeting("Họp", owner="an@urbox.vn")
        self.assertEqual(self.ask(mid), "qua API công ty")

    def test_subscription_error_uses_api_only_when_fallback_is_on(self):
        user_llm.choose("an@urbox.vn", "claude-cli")
        login_claude(user_llm.home_for("an@urbox.vn"))
        mid = db.create_meeting("Họp", owner="an@urbox.vn")
        self.run_fails = True
        with mock.patch.object(artifacts, "api_fallback", lambda: False):
            with self.assertRaises(RuntimeError):
                self.ask(mid)
        with mock.patch.object(artifacts, "api_fallback", lambda: True):
            self.assertEqual(self.ask(mid), "qua API công ty")

    def test_shared_server_provider_cannot_be_a_subscription(self):
        with self.assertRaises(ValueError):
            artifacts.set_provider("claude-cli")
        db.set_setting("llm_provider", "codex-cli")        # cài đặt cũ trên Atlas từ máy cá nhân
        artifacts._provider_cache.clear()
        self.assertIn(artifacts.provider(), artifacts.API_PROVIDERS)


class MyLlmApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.multiple(auth, **WEB), mock.patch.object(user_llm, "BASE", Path(self.tmp.name)),
                        mock.patch.object(cli_llm, "installed", lambda p: True)]
        for p in self.patches:
            p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()
        self.h = {"Authorization": f"Bearer {auth.issue({'email': 'an@urbox.vn', 'name': 'An'})['token']}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_choose_and_logout_my_subscription(self):
        st = self.client.get("/api/me/llm", headers=self.h).json()
        self.assertEqual([p["id"] for p in st["providers"]], ["claude-cli", "codex-cli"])
        self.assertIsNone(st["selected"])
        self.assertEqual(self.client.put("/api/me/llm", json={"provider": "gemini-cli"}, headers=self.h).status_code, 400)
        st = self.client.put("/api/me/llm", json={"provider": "claude-cli"}, headers=self.h).json()
        self.assertEqual(st["selected"], "claude-cli")
        login_claude(user_llm.home_for("an@urbox.vn"))
        st = self.client.get("/api/me/llm", headers=self.h).json()
        self.assertTrue(next(p for p in st["providers"] if p["id"] == "claude-cli")["logged_in"])
        st = self.client.post("/api/me/llm/claude-cli/logout", headers=self.h).json()
        self.assertFalse(next(p for p in st["providers"] if p["id"] == "claude-cli")["logged_in"])
        self.assertIsNone(st["selected"])


if __name__ == "__main__":
    unittest.main()
