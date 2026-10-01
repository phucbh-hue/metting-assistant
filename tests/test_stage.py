"""Test màn hình trình bày: bộ slide, lệnh giọng nói, sửa slide đang chiếu, nhận xét trong lúc xử lý."""
import asyncio
import json
import unittest
from unittest import mock

from tests.helpers import reset_db
from meeting import artifacts, db, live, llm

DECK = {"title": "Báo cáo Sprint 39", "slides": [
    {"title": "Báo cáo Sprint 39", "layout": "title", "bullets": ["Team Payment, 01/10/2026"], "notes": "Chào mọi người"},
    {"title": "Tiến độ Mega Sale", "layout": "bullets", "bullets": ["Landing page xong bản desktop", "Mobile còn banner"],
     "notes": "Nhấn mạnh hạn thứ sáu"},
    {"title": "Ngân sách quý 4", "layout": "metrics", "bullets": ["Ngân sách: 500.000.000đ", "Đã chi: 120.000.000đ"],
     "notes": "Đọc số đã chi"},
    {"title": "Rủi ro", "layout": "bullets", "bullets": ["Migrate Postgres chậm tiến độ"], "notes": ""},
]}


class DeckTests(unittest.TestCase):
    def test_normalize_messy_llm_output(self):
        raw = "Đây là bộ slide:\n```json\n" + json.dumps({"deck": {"title": "X", "slides": [
            {"title": "Mở đầu", "layout": "lạ", "bullets": "• ý một\n• ý hai"}, "Chỉ có tiêu đề", {"bullets": []}, 42]}},
            ensure_ascii=False) + "\n```"
        deck = artifacts.normalize_deck(artifacts._json_from_text(raw))
        self.assertEqual([s["title"] for s in deck["slides"]], ["Mở đầu", "Chỉ có tiêu đề"])
        self.assertEqual(deck["slides"][0]["layout"], "title")
        self.assertEqual(deck["slides"][0]["bullets"], ["ý một", "ý hai"])
        self.assertEqual(deck["slides"][1]["layout"], "bullets")
        self.assertIsNone(artifacts.normalize_deck({"slides": []}))
        self.assertIsNone(artifacts.normalize_deck("không phải json"))

    def test_normalize_insights(self):
        items = artifacts.normalize_insights({"insights": [{"kind": "risk", "text": "Ticket quá hạn"}, "Ý dạng chuỗi",
                                                           {"kind": "lạ", "text": "  "}, {"text": "Không rõ loại"}]})
        self.assertEqual([(i["kind"], i["text"]) for i in items],
                         [("risk", "Ticket quá hạn"), ("info", "Ý dạng chuỗi"), ("info", "Không rõ loại")])


class StageIntentTests(unittest.TestCase):
    def test_spoken_commands(self):
        cases = {
            "mở màn hình trình bày": {"action": "open"},
            "em hãy present về cái slide em đã nói đi": {"action": "present"},
            "tự chuyển slide và nói nội dung bên trong slide giúp anh": {"action": "present"},
            "dừng thuyết trình": {"action": "present_stop"},
            "bật chế độ trình chiếu": {"action": "open"},
            "thu nhỏ màn hình trình bày": {"action": "close"},
            "chuyển slide": {"action": "next"},
            "tiếp đi": {"action": "next"},
            "quay lại slide trước": {"action": "prev"},
            "mở slide số 3": {"action": "goto", "slide": 2},
            "mở slide về ngân sách": {"action": "topic", "query": "ngân sách"},
            "quay lại phần trình bày lúc nãy": {"action": "back"},
            "nhắc bài giúp anh": {"action": "prompt"},
            "nhận xét nhanh về cuộc họp": {"action": "analyze"},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(llm.stage_intent(text), expected)
        for text in ("tóm tắt các quyết định", "mở slide trình bày về ngân sách cho anh", "slide này đẹp quá"):
            with self.subTest(text=text):
                self.assertNotIn((llm.stage_intent(text) or {}).get("action"), ("open", "close"))
        self.assertTrue(llm.is_edit_command("sửa slide này thêm số liệu doanh thu"))
        self.assertFalse(llm.is_edit_command("slide này đẹp quá"))


class StageSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Họp báo cáo")
        self.s = await live.get_session(self.mid)
        self.q = await self.s.subscribe()
        self.aid = db.save_artifact(self.mid, "slides", "Slide: Báo cáo Sprint 39", json.dumps(DECK, ensure_ascii=False))

    async def asyncTearDown(self):
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

    async def test_navigation_history_and_back(self):
        st = await self.s.stage_action("show", artifact_id=self.aid)
        self.assertEqual((st["artifact_id"], st["slide"], st["can_back"]), (self.aid, 0, False))
        for expected in (1, 2, 3, 3):                      # không vượt quá slide cuối
            self.assertEqual((await self.s.stage_action("next"))["slide"], expected)
        self.assertEqual((await self.s.stage_action("prev"))["slide"], 2)
        self.assertEqual((await self.s.stage_action("goto", slide=0))["slide"], 0)
        self.assertEqual((await self.s.stage_action("topic", query="ngân sách"))["slide"], 2)
        minutes = db.save_artifact(self.mid, "minutes", "Biên bản", "# Biên bản")
        st = await self.s.stage_action("show", artifact_id=minutes)
        self.assertTrue(st["can_back"])
        st = await self.s.stage_action("back")
        self.assertEqual((st["artifact_id"], st["slide"]), (self.aid, 2))
        self.assertTrue(self.events("stage_state"))

    async def test_voice_commands_run_without_llm(self):
        await self.s.stage_action("show", artifact_id=self.aid)
        self.events()

        async def no_llm(*a, **k):
            raise AssertionError("Lệnh trình chiếu không được gọi LLM")

        with mock.patch.object(artifacts, "_call_llm", no_llm):
            await self.s._handle_ai_activation("chuyển slide", "Bông ơi, chuyển slide", "Bông")
            self.assertEqual(self.s.stage["slide"], 1)
            await self.s._handle_ai_activation("mở slide về ngân sách", "Bông ơi, mở slide về ngân sách", "Bông")
            self.assertEqual(self.s.stage["slide"], 2)
            await self.s._handle_ai_activation("quay lại slide trước", "Bông ơi, quay lại slide trước", "Bông")
            self.assertEqual(self.s.stage["slide"], 1)
            await self.s._handle_ai_activation("nhắc bài giúp anh", "Bông ơi, nhắc bài giúp anh", "Bông")
        say_events = [e for e in self.events() if e["type"] == "ai_say"]
        self.assertEqual([e["text"] for e in say_events],
                         ["Slide 2 trên 4: Tiến độ Mega Sale.", "Slide 3 trên 4: Ngân sách quý 4.",
                          "Slide 2 trên 4: Tiến độ Mega Sale."])
        self.assertTrue(all(e["quiet"] for e in say_events))   # chuyển slide chỉ hiện phụ đề, không đọc to

    async def test_prompt_event_for_teleprompter(self):
        await self.s.stage_action("show", artifact_id=self.aid)
        await self.s._handle_ai_activation("nhắc bài", "Bông ơi, nhắc bài", "Bông")
        self.assertEqual(len(self.events("stage_prompt")), 1)

    async def test_present_intent_starts_assistant_presentation(self):
        """"Em tự chuyển slide và nói nội dung bên trong slide" (#38): mở màn hình và bắt đầu thuyết trình."""
        self.events()
        await self.s._handle_ai_activation("thuyết trình giúp anh", "Bông ơi, thuyết trình giúp anh", "Bông")
        ev = self.events()
        self.assertEqual(self.s.stage["artifact_id"], self.aid)       # chưa có gì trên màn hình: lấy bộ slide mới nhất
        self.assertIn(("stage_command", "open"), [(e["type"], e.get("action")) for e in ev])
        start = [e for e in ev if e["type"] == "stage_present"]
        self.assertEqual((start[0]["action"], start[0]["slide"]), ("start", 0))
        await self.s._handle_ai_activation("dừng thuyết trình", "Bông ơi, dừng thuyết trình", "Bông")
        self.assertEqual([e["action"] for e in self.events("stage_present")], ["stop"])

    async def test_present_without_any_deck(self):
        db._get_db()["ai_artifacts"].delete_many({})
        await self.s._handle_ai_activation("present slide đi", "Bông ơi, present slide đi", "Bông")
        say = self.events("ai_say")
        self.assertIn("Chưa có bộ slide", say[0]["text"])

    async def test_no_content_on_stage(self):
        await self.s._handle_ai_activation("chuyển slide", "Bông ơi, chuyển slide", "Bông")
        say = self.events("ai_say")
        self.assertIn("chưa có nội dung", say[0]["text"])
        self.assertEqual(say[0]["mood"], "concerned")

    async def test_edit_current_slide_by_voice(self):
        await self.s.stage_action("show", artifact_id=self.aid, slide=2)
        new_deck = json.loads(json.dumps(DECK))
        new_deck["slides"][2]["bullets"].append("Còn lại: 380.000.000đ")

        async def fake(system, prompt, max_tokens=4000):
            self.assertIn("đang xem slide số 3", prompt)
            return json.dumps({"chat_message": "Mình đã thêm số tiền còn lại.", "focus_slide": 3, "deck": new_deck},
                              ensure_ascii=False)

        with mock.patch.object(artifacts, "_call_llm", fake):
            await self.s._handle_ai_activation("sửa slide này thêm dòng còn lại 380 triệu",
                                               "Bông ơi, sửa slide này thêm dòng còn lại 380 triệu", "Bông")
        st = self.s.stage
        self.assertNotEqual(st["artifact_id"], self.aid)
        self.assertEqual(st["slide"], 2)
        self.assertEqual(st["history"], [])                 # bản sửa của cùng bộ slide: không tính là "phần trước"
        art = db.get_artifact(st["artifact_id"])
        self.assertEqual((art["parent_id"], art["version"]), (self.aid, 2))
        self.assertIn("Còn lại: 380.000.000đ", json.loads(art["content"])["slides"][2]["bullets"])
        self.assertIn("Mình đã thêm số tiền còn lại.", [e["text"] for e in self.events("ai_say")])

    async def test_insights_are_emitted_before_slides_are_generated(self):
        order = []

        async def fake(system, prompt, max_tokens=4000):
            if "bộ điều phối tool" in system:
                return json.dumps({"needs_tool": False})
            if system.startswith(artifacts.SLIDES_SYSTEM):
                order.append("slides")
                return json.dumps(DECK, ensure_ascii=False)
            order.append("plan")
            return json.dumps({"insights": [{"kind": "risk", "text": "Ticket URBOX-102 đã quá hạn 30/09/2026"}],
                               "artifact_needed": "slides", "artifact_prompt": "Báo cáo sprint",
                               "chat_response": "Mình soạn slide báo cáo nhé."}, ensure_ascii=False)

        orig_emit = self.s.emit

        async def tracking_emit(ev):
            if ev["type"] == "ai_insights":
                order.append("insights")
            await orig_emit(ev)

        self.s.emit = tracking_emit
        with mock.patch.object(artifacts, "llm_available", lambda: True), \
                mock.patch.object(artifacts, "_call_llm", fake):
            await self.s._handle_ai_activation("làm slide báo cáo tiến độ sprint", "Bông ơi, làm slide báo cáo", "Bông")
        self.assertEqual(order, ["plan", "insights", "slides"])
        art = db.get_artifact(self.s.stage["artifact_id"])
        self.assertEqual(art["kind"], "slides")
        insights = self.events("ai_insights")[0]["items"]
        self.assertEqual(insights[0]["kind"], "risk")


if __name__ == "__main__":
    unittest.main()
