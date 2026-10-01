"""Test cổng gọi LLM (đếm lần gọi, token vào/ra theo cuộc họp), tra cứu web và xuất dữ liệu."""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import artifacts, db, live, llm


class FakeMessages:
    def __init__(self, text, inp=120, out=40, extra_blocks=None, fail=False):
        self.text, self.inp, self.out, self.extra, self.fail = text, inp, out, extra_blocks or [], fail
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("API lỗi giả lập")
        blocks = [SimpleNamespace(type="text", text=self.text, citations=None)] + self.extra
        return SimpleNamespace(content=blocks, usage=SimpleNamespace(input_tokens=self.inp, output_tokens=self.out,
                                                                    cache_read_input_tokens=0))


def fake_client(messages):
    return SimpleNamespace(messages=messages)


class UsageGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Họp A")

    async def test_each_call_is_logged_per_meeting_with_tokens(self):
        fm = FakeMessages("xin chào", inp=100, out=30)
        with mock.patch.object(artifacts, "_anthropic", lambda: fake_client(fm)), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x", "GEMINI_API_KEY": ""}), \
                mock.patch.object(artifacts, "PROVIDER", "claude"), mock.patch.object(artifacts, "CLAUDE_MODEL", "claude-opus-4-7"):
            artifacts.set_meeting(self.mid, "slide")
            self.assertEqual(await artifacts._call_llm("sys", "hi"), "xin chào")
            artifacts.set_meeting(None, "khác")
            await artifacts._call_llm("sys", "hi 2")
        rows = list(db._get_db()["llm_usage"].find({}, {"_id": 0}))
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["meeting_id"], rows[0]["purpose"], rows[0]["input_tokens"], rows[0]["output_tokens"]),
                         (self.mid, "slide", 100, 30))
        self.assertAlmostEqual(rows[0]["cost_usd"], (100 * 5 + 30 * 25) / 1e6, places=8)
        s = db.usage_summary()
        self.assertEqual((s["total"]["requests"], s["total"]["input_tokens"], s["total"]["output_tokens"]), (2, 200, 60))
        self.assertEqual(s["by_meeting"][0]["title"] if s["by_meeting"][0]["meeting_id"] == self.mid else None, "Họp A")
        self.assertEqual({p["purpose"] for p in s["by_purpose"]}, {"slide", "khác"})

    async def test_failed_call_is_logged_as_error(self):
        fm = FakeMessages("", fail=True)
        with mock.patch.object(artifacts, "_anthropic", lambda: fake_client(fm)), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x", "GEMINI_API_KEY": ""}), \
                mock.patch.object(artifacts, "PROVIDER", "claude"):
            artifacts.set_meeting(self.mid, "báo cáo")
            with self.assertRaises(RuntimeError):
                await artifacts._call_llm("sys", "hi")
        s = db.usage_summary()
        self.assertEqual((s["total"]["requests"], s["total"]["errors"], s["estimated_rows"]), (1, 1, 1))

    async def test_web_research_saves_report_with_sources(self):
        cite = SimpleNamespace(url="https://sjc.com.vn/gia-vang", title="Giá vàng SJC")
        text_block = SimpleNamespace(type="text", text="# Tra cứu: giá vàng\n\nGiá vàng SJC sáng 01/10/2026 là 95.000.000đ/lượng.\n\n## Chi tiết\n- Tăng 500.000đ so với hôm qua",
                                     citations=[cite])
        fm = FakeMessages("", inp=900, out=200)

        async def create(**kw):
            fm.calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="server_tool_use", name="web_search"), text_block],
                                   usage=SimpleNamespace(input_tokens=900, output_tokens=200, cache_read_input_tokens=0))
        fm.create = create
        with mock.patch.object(artifacts, "_anthropic", lambda: fake_client(fm)), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x", "WEB_SEARCH_PROVIDER": "claude"}):
            art = await artifacts.web_research(self.mid, "giá vàng hôm nay", "A: nói chuyện")
        self.assertEqual(fm.calls[0]["tools"][0]["type"], "web_search_20260209")
        self.assertEqual(art["kind"], "report")
        self.assertIn("## Nguồn", art["content"])
        self.assertIn("sjc.com.vn", art["content"])
        self.assertIn("1 lượt tìm", art["content"])
        self.assertEqual(db.usage_summary()["by_purpose"][0]["purpose"], "tra cứu web")

    async def test_voice_search_intent_shows_report_and_speaks_conclusion(self):
        s = await live.get_session(self.mid)
        q = await s.subscribe()
        fake_art = {"id": 77, "kind": "report", "title": "Tra cứu web: giá vàng", "content": "# Tra cứu: giá vàng\n\nGiá vàng SJC là 95.000.000đ một lượng, tăng nhẹ. Nguồn SJC.\n\n## Chi tiết\n- x",
                    "sources": [], "version": 1}
        db._get_db()["ai_artifacts"].insert_one(dict(fake_art, meeting_id=self.mid, created_at=1.0))

        async def fake_research(mid, query, ctx="", on_progress=None):
            self.assertEqual(query, "giá vàng hôm nay bao nhiêu")
            return dict(fake_art)
        with mock.patch.object(artifacts, "web_research", fake_research):
            await s._handle_ai_activation("em search giúp anh coi giá vàng hôm nay bao nhiêu", "Bông ơi, em search giúp anh coi giá vàng hôm nay bao nhiêu", "Bông")
        ev = []
        while not q.empty():
            ev.append(q.get_nowait())
        types = [e["type"] for e in ev]
        self.assertIn("artifact_created", types)
        self.assertEqual(s.stage["artifact_id"], 77)
        says = [e["text"] for e in ev if e["type"] == "ai_say"]
        self.assertTrue(any("95.000.000đ" in t for t in says), says)
        self.assertTrue(any(e["type"] == "ai_progress" and "tra cứu trên mạng" in e["text"] for e in ev))
        s.dispose(); live.SESSIONS.clear()


class UsageApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        live.SESSIONS.clear()

    def test_usage_and_export_endpoints(self):
        mid = self.client.post("/api/meetings", json={"title": "Họp X"}).json()["meeting_id"]
        db.record_llm_usage(mid, "claude", "claude-opus-4-7", "slide", 1000, 200, 3.2, cost_usd=0.01)
        u = self.client.get("/api/usage").json()
        self.assertEqual(u["total"]["requests"], 1)
        self.assertEqual(u["by_meeting"][0]["title"], "Họp X")
        r = self.client.get(f"/api/meetings/{mid}/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        data = r.json()
        self.assertEqual((data["meeting"]["id"], len(data["llm_usage"]), data["format"]), (mid, 1, "meeting-copilot-export-1"))
        self.assertEqual(self.client.get("/api/meetings/999/export").status_code, 404)


if __name__ == "__main__":
    unittest.main()
