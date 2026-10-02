"""Test chọn model cho từng nguồn AI: danh sách từ API / CLI, bảng giá, lưu và dùng đúng model đã chọn."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import artifacts, cli_llm, db, live


class PriceTests(unittest.TestCase):
    def test_price_lookup_handles_date_and_context_suffix(self):
        self.assertEqual(artifacts._price("claude-sonnet-5-5"), (2.0, 10.0, 0.1))
        self.assertEqual(artifacts._price("claude-haiku-4-5-20251001"), (1.0, 5.0, 0.1))
        self.assertEqual(artifacts._price("claude-fable-5-1[1m]"), (10.0, 50.0, 0.025))
        self.assertEqual(artifacts._price("gpt-6.1-sol"), (0.0, 0.0, 0.1))
        self.assertAlmostEqual(artifacts._cost_usd("claude-opus-4-5-20251101", 1_000_000, 0), 5.0)
        self.assertEqual(artifacts.pretty_model("claude-opus-4-5-20251101"), "Claude Opus 4.5")
        self.assertEqual(artifacts.pretty_model("gemini-3.8-flash"), "Gemini 3.8 Flash")

    def test_sonnet_is_default_and_recommended(self):
        e = artifacts.claude_entry("claude-sonnet-5-5")
        self.assertTrue(e["recommended"])
        self.assertIn("2 / 10 USD", e["note"])
        self.assertFalse(artifacts.claude_entry("claude-opus-4-7")["recommended"])


class ModelSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        artifacts._MODELS_CACHE.clear()

    async def test_selected_api_model_is_used_and_priced(self):
        artifacts.set_provider("claude", models={"claude": "claude-haiku-4-5", "claude-cli": "opus"})
        self.assertEqual((artifacts.claude_model(), artifacts.provider_model("claude-cli")), ("claude-haiku-4-5", "opus"))
        calls = []

        async def create(**kw):
            calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                   usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=0, cache_read_input_tokens=0))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        mid = db.create_meeting("Họp")
        with mock.patch.object(artifacts, "_anthropic", lambda: client), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            artifacts.set_meeting(mid, "slide")
            await artifacts._call_llm("s", "p")
        self.assertEqual(calls[0]["model"], "claude-haiku-4-5")
        row = db._get_db()["llm_usage"].find_one({"meeting_id": mid})
        self.assertEqual((row["model"], row["cost_usd"]), ("claude-haiku-4-5", 1.0))
        artifacts.set_provider("claude", models={"claude": ""})                 # chuỗi rỗng = quay về mặc định .env
        self.assertEqual(artifacts.claude_model(), artifacts.CLAUDE_MODEL)

    async def test_invalid_model_names_refused(self):
        for bad in ("sonnet; rm -rf /", "a" * 81, "model với dấu"):
            with self.assertRaises(ValueError):
                artifacts.set_provider("claude", models={"claude": bad})
        with self.assertRaises(ValueError):
            artifacts.set_provider("claude", models={"abc": "x"})

    async def test_claude_api_list_from_provider(self):
        page = SimpleNamespace(data=[SimpleNamespace(id="claude-sonnet-5-5", display_name="Claude Sonnet 5.5"),
                                     SimpleNamespace(id="claude-haiku-4-5-20251001", display_name="Claude Haiku 4.5")])

        async def lst(limit=100):
            return page
        client = SimpleNamespace(models=SimpleNamespace(list=lst))
        with mock.patch.object(artifacts, "_anthropic", lambda: client), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            r = await artifacts.list_models("claude")
        self.assertEqual(r["source"], "api")
        self.assertEqual([m["id"] for m in r["models"]], ["claude-sonnet-5-5", "claude-haiku-4-5-20251001"])
        self.assertIn("1 / 5 USD", r["models"][1]["note"])

    async def test_claude_api_list_falls_back_without_key_or_on_error(self):
        r = await artifacts.list_models("claude")
        self.assertEqual((r["source"], r["models"][0]["id"]), ("builtin", "claude-sonnet-5-5"))

        async def boom(limit=100):
            raise RuntimeError("mạng lỗi")
        client = SimpleNamespace(models=SimpleNamespace(list=boom))
        with mock.patch.object(artifacts, "_anthropic", lambda: client), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            r = await artifacts.list_models("claude", refresh=True)
        self.assertEqual((r["source"], r["error"]), ("builtin", "mạng lỗi"))

    async def test_gemini_api_list_keeps_text_models(self):
        rows = [SimpleNamespace(name="models/gemini-3.6-flash", display_name="Gemini 3.6 Flash", supported_actions=["generateContent"]),
                SimpleNamespace(name="models/gemini-3.8-flash", display_name="Gemini 3.8 Flash", supported_actions=["generateContent"]),
                SimpleNamespace(name="models/gemini-3.8-flash-tts", display_name="TTS", supported_actions=["generateContent"]),
                SimpleNamespace(name="models/gemini-3-pro-image", display_name="Nano Banana Pro", supported_actions=["generateContent"]),
                SimpleNamespace(name="models/gemini-embedding-001", display_name="Embedding", supported_actions=["embedContent"]),
                SimpleNamespace(name="models/gemini-pro-latest", display_name="Gemini Pro Latest", supported_actions=["generateContent"])]
        client = SimpleNamespace(models=SimpleNamespace(list=lambda: rows))
        with mock.patch.object(artifacts, "_gemini", lambda: client), mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}):
            r = await artifacts.list_models("gemini")
        self.assertEqual([m["id"] for m in r["models"]], ["gemini-pro-latest", "gemini-3.8-flash", "gemini-3.6-flash"])


class CliModelListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        cli_llm._list_cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_catalog_visible_models_by_priority(self):
        catalog = {"models": [{"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list", "priority": 2, "description": "Frontier"},
                              {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "priority": 1, "description": "Workhorse"},
                              {"slug": "codex-auto-review", "display_name": "Review", "visibility": "hide", "priority": 43}]}

        def fake_run(cmd, stdin, cwd, env, timeout):
            self.assertEqual(cmd[-2:], ["debug", "models"])
            return subprocess.CompletedProcess(cmd, 0, json.dumps(catalog).encode(), b"")
        with mock.patch.dict(os.environ, {"CLI_LLM_DISABLED": "0"}), mock.patch.object(cli_llm, "installed", lambda p: True), \
                mock.patch.object(cli_llm, "resolve_cmd", lambda n: ["/bin/codex"]), mock.patch.object(cli_llm, "_run", fake_run):
            models, source = cli_llm.list_models("codex-cli")
        self.assertEqual((source, [m["id"] for m in models]), ("cli", ["gpt-6.1-sol", "gpt-6-astra"]))

    def test_claude_extra_options_from_account(self):
        (self.dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"refreshToken": "r", "subscriptionType": "team"}}), encoding="utf-8")
        (self.dir / ".claude.json").write_text(json.dumps({"additionalModelOptionsCache": [
            {"value": "claude-fable-5-1[1m]", "label": "Fable", "description": "Fable 5.1 \u2014 Most capable"}]}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.dir)}), mock.patch.object(cli_llm, "installed", lambda p: True):
            models, source = cli_llm.list_models("claude-cli")
        ids = [m["id"] for m in models]
        self.assertEqual((source, ids[:4]), ("cli", ["sonnet", "opus", "haiku", "claude-fable-5-1[1m]"]))
        self.assertEqual(models[3]["note"], "Fable 5.1 - Most capable")              # không để gạch dài trong nội dung

    def test_gemini_models_read_from_installed_bundle(self):
        bundle = self.dir / "bundle"
        bundle.mkdir()
        (bundle / "gemini.js").write_text("//", encoding="utf-8")
        (bundle / "chunk-a.js").write_text('var x = 1; var DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"; var LATEST_GEMINI_FLASH_MODEL = "gemini-3.8-flash";\n'
                                           'var PREVIEW_GEMINI_MODEL = "gemini-3.1-pro-preview-customtools"; var DEFAULT_GEMINI_EMBEDDING_MODEL = "gemini-embedding-001";\n'
                                           'var PREVIEW_GEMINI_FLASH_LITE_MODEL = "none";', encoding="utf-8")
        with mock.patch.object(cli_llm, "installed", lambda p: True), \
                mock.patch.object(cli_llm, "resolve_cmd", lambda n: ["node", str(bundle / "gemini.js")]):
            models, source = cli_llm.list_models("gemini-cli")
        self.assertEqual((source, [m["id"] for m in models]),
                         ("cli", ["auto", "pro", "flash", "flash-lite", "gemini-3.8-flash", "gemini-2.5-pro"]))

    def test_not_installed_uses_builtin_list(self):
        with mock.patch.object(cli_llm, "installed", lambda p: False):
            models, source = cli_llm.list_models("codex-cli")
        self.assertEqual(source, "builtin")
        self.assertTrue(models)

    def test_model_with_context_suffix_is_accepted_by_cli(self):
        seen = {}

        def fake_run(cmd, stdin, cwd, env, timeout):
            seen["cmd"] = cmd
            out = {"type": "result", "subtype": "success", "is_error": False, "result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out).encode(), b"")
        with mock.patch.dict(os.environ, {"CLI_LLM_DISABLED": "0"}), mock.patch.object(cli_llm, "resolve_cmd", lambda n: ["/bin/claude"]), \
                mock.patch.object(cli_llm, "_run", fake_run):
            cli_llm.run("claude-cli", "s", "p", model="claude-fable-5-1[1m]")
        self.assertEqual(seen["cmd"][seen["cmd"].index("--model") + 1], "claude-fable-5-1[1m]")


class ModelApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        artifacts._MODELS_CACHE.clear()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        live.SESSIONS.clear()

    def test_models_endpoint_and_saving_several_models(self):
        r = self.client.get("/api/llm/models?provider=claude").json()
        self.assertEqual((r["provider"], r["source"], r["default"]), ("claude", "builtin", artifacts.CLAUDE_MODEL))
        self.assertTrue(any(m["recommended"] for m in r["models"]))
        self.assertEqual(self.client.get("/api/llm/models?provider=abc").status_code, 400)
        info = self.client.put("/api/llm/provider", json={"provider": "claude", "models": {"claude": "claude-opus-5-5", "gemini": "gemini-3.8-flash", "claude-cli": ""}}).json()
        rows = {p["id"]: p for p in info["providers"]}
        self.assertEqual((rows["claude"]["model"], rows["gemini"]["model"], rows["claude-cli"]["model"]),
                         ("claude-opus-5-5", "gemini-3.8-flash", cli_llm.model_of("claude-cli")))
        self.assertEqual(self.client.get("/api/llm/models?provider=claude").json()["selected"], "claude-opus-5-5")
        self.assertEqual(self.client.put("/api/llm/provider", json={"provider": "claude", "models": {"claude": "x y"}}).status_code, 400)

    def test_test_endpoint_uses_given_model(self):
        seen = {}

        def fake(provider, system, prompt, timeout=None, model=None):
            seen["model"] = model
            return {"text": "Xin chào", "model": model or "sonnet", "input": 1, "output": 1, "cache_read": 0, "cache_write": 0, "estimated": False}
        with mock.patch.object(cli_llm, "run", fake):
            r = self.client.post("/api/llm/test", json={"provider": "claude-cli", "model": "haiku"}).json()
        self.assertEqual((seen["model"], r["model"]), ("haiku", "haiku"))
        self.assertEqual(self.client.post("/api/llm/test", json={"provider": "claude-cli", "model": "a b"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
