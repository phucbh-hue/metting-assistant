"""Test thư viện slide trên máy, lời thuyết trình chi tiết, quay lại nội dung đã trình bày theo loại."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import reset_db
from tests.test_stage import DECK
from meeting import artifacts, db, decks, live, llm

MD = """# Kế hoạch Q4
## Mục tiêu
- Doanh số 2.500.000.000đ
- 120 thương hiệu
Nhấn mạnh con số doanh số.
## Rủi ro
- Banner mobile trễ
"""


class DeckFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "mega sale").mkdir()
        (base / "mega sale" / "ke-hoach-q4.md").write_text(MD, encoding="utf-8")
        (base / "demo").mkdir()
        (base / "demo" / "khac.json").write_text(json.dumps(DECK, ensure_ascii=False), encoding="utf-8")
        (base / "README.md").write_text("# bỏ qua", encoding="utf-8")
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def test_markdown_import(self):
        deck = artifacts.normalize_deck(decks.read_deck(str(self.base / "mega sale" / "ke-hoach-q4.md")))
        self.assertEqual(deck["title"], "Kế hoạch Q4")
        self.assertEqual([s["title"] for s in deck["slides"]], ["Mục tiêu", "Rủi ro"])
        self.assertEqual(deck["slides"][0]["bullets"], ["Doanh số 2.500.000.000đ", "120 thương hiệu"])
        self.assertEqual(deck["slides"][0]["notes"], "Nhấn mạnh con số doanh số.")

    def test_find_by_folder_and_file_name(self):
        found = decks.find_files("em mở slide ở folder mega sale giúp anh", self.base)
        self.assertEqual(found[0]["folder"], "mega sale")
        self.assertEqual(decks.find_files("mở file slide kế hoạch q4", self.base)[0]["name"], "ke-hoach-q4")
        self.assertEqual(decks.find_files("mở slide ở folder không có", self.base), [])
        self.assertEqual(decks.folders(self.base), ["demo", "mega sale"])

    def test_intents(self):
        self.assertEqual(llm.stage_intent("em mở slide ở folder mega sale giúp anh")["action"], "open_file")
        self.assertEqual(llm.stage_intent("quay lại slide cũ khi nãy em đã present"), {"action": "back", "kind": "slides"})
        self.assertEqual(llm.stage_intent("quay lại cái sơ đồ lúc nãy"), {"action": "back", "kind": "diagram"})
        self.assertEqual(llm.stage_intent("quay lại slide trước")["action"], "prev")      # vẫn là slide trước trong bộ


class ScriptTests(unittest.TestCase):
    def test_deck_keeps_script_and_detects_missing(self):
        deck = artifacts.normalize_deck({"title": "X", "slides": [{"title": "A", "bullets": ["b"], "script": "lời " * 30}]})
        self.assertTrue(artifacts.deck_has_scripts(deck))
        self.assertFalse(artifacts.deck_has_scripts(artifacts.normalize_deck(DECK)))


class StageDeckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Họp")
        self.s = await live.get_session(self.mid)
        self.q = await self.s.subscribe()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "mega sale").mkdir()
        (base / "mega sale" / "ke-hoach-q4.md").write_text(MD, encoding="utf-8")
        self.p = mock.patch.object(decks, "SLIDES_DIR", base)
        self.p.start()

    async def asyncTearDown(self):
        self.p.stop()
        self.tmp.cleanup()
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()

    def events(self, kind=None):
        out = []
        while not self.q.empty():
            e = self.q.get_nowait()
            if kind is None or e["type"] == kind:
                out.append(e)
        return out

    async def test_open_file_imports_and_shows(self):
        await self.s._handle_ai_activation("mở slide ở folder mega sale giúp anh", "Bông ơi, mở slide ở folder mega sale giúp anh", "Bông")
        art = db.get_artifact(self.s.stage["artifact_id"])
        self.assertEqual((art["kind"], art["title"]), ("slides", "Slide: Kế hoạch Q4"))
        says = [e["text"] for e in self.events("ai_say")]
        self.assertTrue(any("mở bộ slide" in t and "mega sale" in t and "2 slide" in t for t in says), says)

    async def test_open_file_not_found_lists_folders(self):
        await self.s._handle_ai_activation("mở slide ở folder tài chính", "Bông ơi, mở slide ở folder tài chính", "Bông")
        say = self.events("ai_say")[0]
        self.assertIn("không thấy", say["text"])
        self.assertIn("mega sale", say["text"])

    async def test_back_to_previous_kind(self):
        aid = db.save_artifact(self.mid, "slides", "Slide: A", json.dumps(DECK, ensure_ascii=False))
        did = db.save_artifact(self.mid, "dashboard", "Dashboard: B", json.dumps({"title": "B", "kpis": [{"label": "x", "value": "1"}], "charts": []}))
        rid = db.save_artifact(self.mid, "report", "Báo cáo: C", "# C")
        await self.s.stage_action("show", artifact_id=aid, slide=2)
        await self.s.stage_action("show", artifact_id=did)
        await self.s.stage_action("show", artifact_id=rid)
        self.events()
        await self.s._handle_ai_activation("quay lại slide cũ khi nãy em đã present", "Bông ơi, quay lại slide cũ khi nãy em đã present", "Bông")
        self.assertEqual((self.s.stage["artifact_id"], self.s.stage["slide"]), (aid, 2))
        self.assertIn("quay lại bộ slide", self.events("ai_say")[0]["text"])
        await self.s._handle_ai_activation("quay lại dashboard lúc nãy", "Bông ơi, quay lại dashboard lúc nãy", "Bông")
        self.assertEqual(self.s.stage["artifact_id"], did)
        self.events()
        await self.s._handle_ai_activation("quay lại sơ đồ lúc nãy", "Bông ơi, quay lại sơ đồ lúc nãy", "Bông")
        self.assertIn("chưa trình bày sơ đồ", self.events("ai_say")[0]["text"])

    async def test_present_writes_detailed_scripts_first(self):
        aid = db.save_artifact(self.mid, "slides", "Slide: A", json.dumps(DECK, ensure_ascii=False))
        await self.s.stage_action("show", artifact_id=aid)
        self.events()

        async def fake(system, prompt, max_tokens=4000):
            self.assertIn("LỜI THUYẾT TRÌNH", system)
            return json.dumps({"scripts": [("lời chi tiết " * 15).strip()] * len(DECK["slides"])}, ensure_ascii=False)

        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            await self.s._handle_ai_activation("thuyết trình giúp anh", "Bông ơi, thuyết trình giúp anh", "Bông")
        ev = self.events()
        types = [e["type"] for e in ev]
        self.assertIn("ai_progress", types)
        self.assertIn("artifact_updated", types)
        new = db.get_artifact(self.s.stage["artifact_id"])
        self.assertEqual(new["parent_id"], aid)
        self.assertTrue(artifacts.deck_has_scripts(json.loads(new["content"])))
        self.assertEqual([e["action"] for e in ev if e["type"] == "stage_present"], ["start"])


if __name__ == "__main__":
    unittest.main()
