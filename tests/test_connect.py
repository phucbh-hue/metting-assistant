"""Test nhập khóa trên giao diện (.env), nút Kết nối gói đăng ký (đăng nhập CLI chạy nền) và bộ lệnh pnpm mst-urbox."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import artifacts, cli_llm, envfile, live

ROOT = Path(__file__).resolve().parent.parent


class FakeProc:
    """Tiến trình CLI giả: in sẵn vài dòng, tùy chọn ghi tệp đăng nhập rồi thoát."""

    def __init__(self, lines, rc=0, finish_after=None, on_finish=None):
        self.stdout = io.BytesIO("".join(f"{ln}\n" for ln in lines).encode("utf-8"))
        self.stdin = io.BytesIO()
        self._rc, self.final_rc, self.killed = None, rc, False
        if finish_after is not None:
            def done():
                time.sleep(finish_after)
                if on_finish:
                    on_finish()
                if self._rc is None:
                    self._rc = self.final_rc
            threading.Thread(target=done, daemon=True).start()

    def poll(self):
        return self._rc

    def kill(self):
        self.killed, self._rc = True, -9

    def wait(self, timeout=None):
        return self._rc


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.calls = []
        self.patches = [mock.patch.dict(os.environ, {"CLI_LLM_DISABLED": "0", "CODEX_HOME": str(self.home / "codex"),
                                                     "CLAUDE_CONFIG_DIR": str(self.home / "claude")}),
                        mock.patch.object(cli_llm, "WATCH_EVERY_S", 0.05),
                        mock.patch.object(cli_llm, "resolve_cmd", lambda name: [f"/bin/{name}"])]
        for p in self.patches:
            p.start()
        cli_llm._jobs.clear()

    def tearDown(self):
        for job in cli_llm._jobs.values():
            if job["state"] == "running":
                job["state"] = "cancelled"
        cli_llm._jobs.clear()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def popen(self, proc):
        def fake(cmd, **kw):
            self.calls.append((cmd, kw))
            return proc
        return mock.patch.object(cli_llm.subprocess, "Popen", fake)

    def wait_state(self, provider, want=("done", "error", "cancelled"), timeout=4.0):
        for _ in range(int(timeout / 0.05)):
            st = cli_llm.login_status(provider)
            if st["state"] in want:
                return st
            time.sleep(0.05)
        self.fail(f"đăng nhập {provider} vẫn {cli_llm.login_status(provider)['state']}")

    def test_codex_login_shows_url_and_finishes_when_credentials_written(self):
        def write_auth():
            (self.home / "codex").mkdir(parents=True, exist_ok=True)
            (self.home / "codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "t"}}), encoding="utf-8")
        proc = FakeProc(["Starting local login server on http://localhost:1455.",
                         "If your browser did not open, navigate to this URL to authenticate:", "",
                         "https://auth.openai.com/oauth/authorize?client_id=abc&state=xyz"],
                        rc=0, finish_after=0.3, on_finish=write_auth)
        with self.popen(proc):
            st = cli_llm.start_login("codex-cli")
            self.assertEqual(st["state"], "running")
            st = self.wait_state("codex-cli")
        self.assertEqual(self.calls[0][0], ["/bin/codex", "login"])
        self.assertNotIn("OPENAI_API_KEY", self.calls[0][1]["env"])
        self.assertEqual((st["state"], st["logged_in"], st["plan"]), ("done", True, "ChatGPT"))
        self.assertEqual(st["url"], "https://auth.openai.com/oauth/authorize?client_id=abc&state=xyz")

    def test_device_code_on_its_own_line(self):
        proc = FakeProc(["1. Open this link in your browser and sign in to your account", "https://auth.openai.com/codex/device",
                         "2. Enter this one-time code (expires in 15 minutes)", "ABCD-EF12G"])
        with self.popen(proc):
            cli_llm.start_login("codex-cli")
            time.sleep(0.3)
            st = cli_llm.login_status("codex-cli")
        self.assertEqual((st["url"], st["code"], st["can_paste"]), ("https://auth.openai.com/codex/device", "ABCD-EF12G", True))
        cli_llm.cancel_login("codex-cli")

    def test_gemini_login_consents_and_uses_google_login(self):
        cred = self.home / "gemini" / "oauth_creds.json"
        state = {"in": False}

        def login_ok():
            cred.parent.mkdir(parents=True, exist_ok=True)
            cred.write_text("{}", encoding="utf-8")
            state["in"] = True
        proc = FakeProc(["Opening authentication page in your browser. Do you want to continue? [Y/n]:",
                         "Attempting to open authentication page in your browser.", "Otherwise navigate to:",
                         "https://accounts.google.com/o/oauth2/v2/auth?client_id=1&scope=x"], rc=0, finish_after=0.3, on_finish=login_ok)
        with self.popen(proc), mock.patch.object(cli_llm, "cred_file", lambda p, h=None: cred), \
                mock.patch.object(cli_llm, "_login_state", lambda p, h=None: {"logged_in": state["in"], "plan": "Google" if state["in"] else ""}):
            cli_llm.start_login("gemini-cli")
            st = self.wait_state("gemini-cli")
        cmd, kw = self.calls[0]
        self.assertEqual(cmd[:2], ["/bin/gemini", "-p"])
        self.assertEqual(kw["env"]["GOOGLE_GENAI_USE_GCA"], "true")
        self.assertEqual(proc.stdin.getvalue(), b"y\n")
        self.assertEqual((st["state"], st["url"].startswith("https://accounts.google.com/o/oauth2")), ("done", True))

    @unittest.skipUnless(os.name == "nt", "Windows: Claude Code đăng nhập trong cửa sổ terminal riêng")
    def test_claude_login_opens_its_own_console_window(self):
        proc = FakeProc([])
        with self.popen(proc):
            st = cli_llm.start_login("claude-cli")
        cmd, kw = self.calls[0]
        self.assertEqual(cmd, ["/bin/claude", "auth", "login", "--claudeai"])
        self.assertEqual(kw["creationflags"], subprocess.CREATE_NEW_CONSOLE)
        self.assertNotIn("stdout", kw)
        self.assertEqual((st["window"], st["can_paste"]), (True, False))
        cli_llm.cancel_login("claude-cli")

    def test_paste_code_and_cancel(self):
        proc = FakeProc(["Paste code here if prompted >"])
        with self.popen(proc):
            cli_llm.start_login("codex-cli")
            cli_llm.submit_login_code("codex-cli", "abc123#state")
            self.assertEqual(proc.stdin.getvalue(), b"abc123#state\n")
            with self.assertRaises(ValueError):
                cli_llm.submit_login_code("codex-cli", "a\nb")
            st = cli_llm.cancel_login("codex-cli")
        self.assertEqual((st["state"], proc.killed), ("cancelled", True))
        with self.assertRaises(RuntimeError):
            cli_llm.submit_login_code("codex-cli", "x")

    def test_failed_login_reports_error(self):
        proc = FakeProc(["Error: login cancelled by user"], rc=1, finish_after=0.1)
        with self.popen(proc):
            cli_llm.start_login("codex-cli")
            st = self.wait_state("codex-cli")
        self.assertEqual(st["state"], "error")
        self.assertIn("login cancelled", st["error"])

    def test_not_installed_or_disabled(self):
        with mock.patch.object(cli_llm, "resolve_cmd", lambda name: None), self.assertRaises(RuntimeError) as cm:
            cli_llm.start_login("codex-cli")
        self.assertIn("pnpm mst-urbox install", str(cm.exception))
        with mock.patch.dict(os.environ, {"CLI_LLM_DISABLED": "1"}), self.assertRaises(RuntimeError):
            cli_llm.start_login("codex-cli")


class LocalCliTests(unittest.TestCase):
    def test_project_node_modules_are_found(self):
        with tempfile.TemporaryDirectory() as d:
            nm = Path(d) / "node_modules"
            codex = nm / "@openai" / "codex"
            (codex / "bin").mkdir(parents=True)
            (codex / "bin" / "codex.js").write_text("//", encoding="utf-8")
            (codex / "package.json").write_text(json.dumps({"bin": {"codex": "bin/codex.js"}}), encoding="utf-8")
            plat = "win32" if os.name == "nt" else ("darwin" if os.uname().sysname == "Darwin" else "linux")
            arch = "arm64" if cli_llm.platform.machine().lower() in ("arm64", "aarch64") else "x64"
            native = nm / "@anthropic-ai" / f"claude-code-{plat}-{arch}" / ("claude.exe" if os.name == "nt" else "claude")
            native.parent.mkdir(parents=True)
            native.write_bytes(b"")
            (nm / "@anthropic-ai" / "claude-code").mkdir()
            with mock.patch.object(cli_llm, "NODE_MODULES", nm), \
                    mock.patch.object(cli_llm.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None):
                cli_llm._which_cache.clear()
                self.assertEqual(cli_llm.resolve_cmd("codex"), ["/usr/bin/node", str((codex / "bin" / "codex.js").resolve())])
                self.assertEqual(cli_llm.resolve_cmd("claude"), [str(native.resolve())])
                self.assertIsNone(cli_llm.resolve_cmd("gemini"))
            cli_llm._which_cache.clear()


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"
        self.path.write_text("# cấu hình\nLLM_PROVIDER=claude   # chú thích\nSONIOX_API_KEY=old\n", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"SONIOX_API_KEY": "old"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_set_replace_and_clear_keep_other_lines(self):
        envfile.set_value("SONIOX_API_KEY", "abcdefghijkl1234", self.path)
        envfile.set_value("MONGODB_URL", "mongodb+srv://u:p@cluster0.ab.mongodb.net/?retryWrites=true", self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# cấu hình\nLLM_PROVIDER=claude   # chú thích\nSONIOX_API_KEY=abcdefghijkl1234\n", text)
        self.assertIn("MONGODB_URL=mongodb+srv://u:p@cluster0.ab.mongodb.net/?retryWrites=true\n", text)
        self.assertEqual(os.environ["SONIOX_API_KEY"], "abcdefghijkl1234")
        st = envfile.status()
        self.assertEqual((st["SONIOX_API_KEY"]["set"], st["SONIOX_API_KEY"]["masked"]), (True, "...1234"))
        self.assertEqual(st["MONGODB_URL"]["masked"], "cluster0.ab.mongodb.net")
        envfile.set_value("SONIOX_API_KEY", "", self.path)
        self.assertIn("SONIOX_API_KEY=\n", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("SONIOX_API_KEY", os.environ)
        os.environ.pop("MONGODB_URL", None)

    def test_validation(self):
        for name, value in [("OPENAI_API_KEY", "x"), ("ANTHROPIC_API_KEY", "abc"), ("MONGODB_URL", "http://x"),
                            ("SONIOX_API_KEY", "a b"), ("SONIOX_API_KEY", "a\nb")]:
            with self.assertRaises(ValueError, msg=name):
                envfile.set_value(name, value, self.path)

    def test_placeholder_counts_as_missing(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "your-gemini-key"}):
            self.assertFalse(envfile.status()["GEMINI_API_KEY"]["set"])


class SecretsApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"
        self.patches = [mock.patch.object(envfile, "ENV_PATH", self.path), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""})]
        for p in self.patches:
            p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        live.SESSIONS.clear()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_save_key_from_ui(self):
        artifacts._anthropic_client = object()
        r = self.client.put("/api/settings/secrets", json={"name": "ANTHROPIC_API_KEY", "value": "sk-ant-test-0000000000wxyz"})
        self.assertEqual((r.status_code, r.json()["set"], r.json()["masked"]), (200, True, "...wxyz"))
        self.assertIsNone(artifacts._anthropic_client)                      # client tạo lại với key mới
        self.assertIn("ANTHROPIC_API_KEY=sk-ant-test-0000000000wxyz", self.path.read_text(encoding="utf-8"))
        info = self.client.get("/api/llm/providers").json()
        self.assertEqual(next(p for p in info["providers"] if p["id"] == "claude")["key_masked"], "...wxyz")
        self.assertNotIn("sk-ant-test", json.dumps(self.client.get("/api/settings/secrets").json()))   # không trả lại khóa
        self.assertEqual(self.client.put("/api/settings/secrets", json={"name": "PATH", "value": "x"}).status_code, 400)

    def test_connect_endpoints(self):
        with mock.patch.object(cli_llm, "start_login", lambda p, h=None: {"provider": p, "state": "running", "url": "https://x"}), \
                mock.patch.object(cli_llm, "cancel_login", lambda p, h=None: {"provider": p, "state": "cancelled"}):
            self.assertEqual(self.client.post("/api/llm/connect/claude-cli").json()["state"], "running")
            self.assertEqual(self.client.delete("/api/llm/connect/claude-cli").json()["state"], "cancelled")
        self.assertEqual(self.client.post("/api/llm/connect/abc").status_code, 400)
        self.assertEqual(self.client.get("/api/llm/connect/codex-cli").json()["provider"], "codex-cli")

    def test_only_local_machine_can_change_keys(self):
        remote = TestClient(appmod.app, client=("10.1.2.3", 50000))
        self.assertEqual(remote.put("/api/settings/secrets", json={"name": "SONIOX_API_KEY", "value": "x"}).status_code, 403)
        self.assertEqual(remote.post("/api/llm/connect/codex-cli").status_code, 403)


@unittest.skipUnless(shutil.which("node"), "cần Node.js")
class MstUrboxScriptTests(unittest.TestCase):
    def node(self, *args):
        return subprocess.run(["node", *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_help_and_unknown_command(self):
        r = self.node("scripts/mst-urbox.mjs", "help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for word in ("pnpm mst-urbox install", "pnpm mst-urbox build", "pnpm mst-urbox web"):
            self.assertIn(word, r.stdout)
        self.assertNotEqual(self.node("scripts/mst-urbox.mjs", "abc").returncode, 0)

    def test_env_template_blanks_secrets(self):
        code = ("import('./scripts/mst-urbox.mjs').then(m => process.stdout.write(m.envTemplate("
                "'ANTHROPIC_API_KEY=sk-ant-api03-xxxxx\\nLLM_PROVIDER=claude  # c\\nMONGODB_URL=\"mongodb+srv://u:p@h\"\\n# ghi chú')))")
        r = self.node("--input-type=module", "-e", code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, 'ANTHROPIC_API_KEY=\nLLM_PROVIDER=claude  # c\nMONGODB_URL=\n# ghi chú')


if __name__ == "__main__":
    unittest.main()
