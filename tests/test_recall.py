"""Test "quay lại / xem lại nội dung đã trình bày": câu nói thật (#41, #42), lịch sử lưu DB, tìm theo chủ đề, trợ lý mở lại."""
import json
import unittest
from unittest import mock

from tests.helpers import reset_db
from meeting import artifacts, db, live, llm

DECK_A = {"title": "Sự học khai phóng P1", "slides": [
    {"title": "Định nghĩa khai phóng", "bullets": ["Khai minh", "Giải phóng"]},
    {"title": "Hai cách hiểu về khai phóng", "bullets": ["Học để biết", "Học để làm người"]},
    {"title": "Khai mở tâm trí", "bullets": ["Nhìn vào chính mình"]}]}
DECK_B = {"title": "Review phim Trại buôn người", "slides": [
    {"title": "Tổng quan", "bullets": ["Đạo diễn", "Bối cảnh"]},
    {"title": "Điểm cộng", "bullets": ["Đầu tư sản xuất", "Diễn xuất"]},
    {"title": "Điểm trừ", "bullets": ["Kịch bản dài"]}]}
DASH = {"title": "Doanh thu", "kpis": [{"label": "Doanh thu", "value": "1.000.000đ"}], "charts": []}


class RecallIntentTests(unittest.TestCase):
    def test_real_phrases(self):
        cases = {
            "em hãy quay lại cái bộ slide mà em vừa trình bày trước đó cho anh xem nha": {"action": "back", "kind": "slides", "past": True},
            "em hãy quay lại cái slide trước đó": {"action": "back", "kind": "slides", "past": True},
            "em quay lại cái phần slide điểm cộng ấy": {"action": "back", "kind": "slides", "query": "điểm cộng"},
            "em quay lại cái slide hai cách hiểu về khai phóng đi": {"action": "back", "kind": "slides", "query": "hai cách hiểu khai phóng"},
            "cho anh xem lại cái dashboard vừa rồi": {"action": "back", "kind": "dashboard", "past": True},
            "quay về cái sơ đồ kiến trúc": {"action": "back", "kind": "diagram", "query": "kiến trúc"},
            "quay lại nội dung em vừa trình bày": {"action": "back", "past": True},
            "em quay lại cái spotlight": {"action": "back", "query": "spotlight"},
            "quay lại slide trước": {"action": "prev"},
            "quay lại slide số 7": {"action": "goto", "slide": 6},
            "quay lại slide số bảy": {"action": "goto", "slide": 6},
            "chuyển sang slide hai": {"action": "goto", "slide": 1},
            "chuyển sang slide hai cách hiểu": {"action": "topic", "query": "hai cách hiểu"},
            "em mở lại cái bộ slide mới nhất đi": {"action": "latest", "kind": "slides"},
            "quay lại slide v1": {"action": "version", "n": 1, "kind": "slides"},
            "em xem lại nha": None,
        }
        for text, want in cases.items():
            self.assertEqual(llm.stage_intent(text), want, text)
        self.assertEqual(llm.stage_intent("chiếu lại cái slide đầu tiên em làm")["first"], True)
        self.assertEqual(llm.stage_intent("thuyết trình lại cái slide hồi nãy")["recall"]["kind"], "slides")
        self.assertNotIn("recall", llm.stage_intent("em hãy present về cái slide em đã nói đi"))
        self.assertEqual(llm.stage_intent("mở lại file kế hoạch q4 trong downloads")["action"], "open_file")

    def test_topic_score_ignores_accents_and_fillers(self):
        self.assertEqual(llm.topic_score("diem cong", "Điểm cộng của phim"), (1.0, 2))
        self.assertEqual(llm.topic_score("cái đó em ơi", "Điểm cộng"), (0.0, 0))


class RecallSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        llm.set_assistant_config("Bông", [])
        self.mid = db.create_meeting("Họp")
        self.a = db.save_artifact(self.mid, "slides", "Slide: Sự học khai phóng P1", json.dumps(DECK_A, ensure_ascii=False))
        self.b = db.save_artifact(self.mid, "slides", "Slide: Review phim Trại buôn người", json.dumps(DECK_B, ensure_ascii=False))
        self.d = db.save_artifact(self.mid, "dashboard", "Dashboard: Doanh thu", json.dumps(DASH, ensure_ascii=False))
        self.s = await live.get_session(self.mid)
        self.q = await self.s.subscribe()

    async def asyncTearDown(self):
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()

    def says(self):
        out = []
        while not self.q.empty():
            e = self.q.get_nowait()
            if e["type"] == "ai_say":
                out.append(e["text"])
        return out

    async def ask(self, text):
        await self.s._handle_ai_activation(text, f"Bông ơi, {text}", "Bông")

    async def test_history_survives_restart(self):
        await self.s.stage_action("show", artifact_id=self.a, slide=2)
        await self.s.stage_action("show", artifact_id=self.d)
        await self.s.drain()
        await self.s._db_queue.join()
        self.assertEqual([e["artifact_id"] for e in db.get_meeting(self.mid)["stage_log"]], [self.a, self.d])
        self.s.dispose()
        live.SESSIONS.clear()
        self.s = await live.get_session(self.mid)                 # như chạy lại run.cmd
        self.q = await self.s.subscribe()
        self.assertEqual((self.s.stage["artifact_id"], self.s.stage["history"][-1]["artifact_id"]), (self.d, self.a))
        await self.ask("em hãy quay lại cái bộ slide mà em vừa trình bày trước đó cho anh xem nha")
        self.assertEqual((self.s.stage["artifact_id"], self.s.stage["slide"]), (self.a, 2))
        self.assertIn("mở lại bộ slide", self.says()[-1])

    async def test_topic_across_decks_and_versions(self):
        await self.s.stage_action("show", artifact_id=self.d)
        await self.ask("em quay lại cái phần slide điểm cộng ấy")
        self.assertEqual((self.s.stage["artifact_id"], self.s.stage["slide"]), (self.b, 1))
        await self.ask("em quay lại cái slide sự học, à, khai phóng, chia sẻ của thầy Trung P1 đi")
        self.assertEqual(self.s.stage["artifact_id"], self.a)
        await self.ask("em quay lại cái slide hai cách hiểu về khai phóng đi")
        self.assertEqual((self.s.stage["artifact_id"], self.s.stage["slide"]), (self.a, 1))
        newer = db.save_artifact(self.mid, "slides", "Slide: Review phim Trại buôn người", json.dumps(DECK_B, ensure_ascii=False),
                                 parent_id=self.b)
        await self.ask("quay lại slide điểm trừ")
        self.assertEqual((self.s.stage["artifact_id"], self.s.stage["slide"]), (newer, 2))   # bản mới nhất của bộ đó

    async def test_old_meeting_without_history_uses_latest_other(self):
        await self.s.stage_action("show", artifact_id=self.b)
        self.s.stage["history"].clear()
        await self.ask("em hãy quay lại cái slide trước đó")
        self.assertEqual(self.s.stage["artifact_id"], self.a)

    async def test_single_deck_goes_to_previous_slide(self):
        mid = db.create_meeting("Họp một bộ")
        aid = db.save_artifact(mid, "slides", "Slide: Sự học khai phóng P1", json.dumps(DECK_A, ensure_ascii=False))
        s = await live.get_session(mid)
        await s.stage_action("show", artifact_id=aid, slide=2)
        await s._handle_ai_activation("em hãy quay lại cái slide trước đó", "Bông ơi, em hãy quay lại cái slide trước đó", "Bông")
        self.assertEqual((s.stage["artifact_id"], s.stage["slide"]), (aid, 1))

    async def test_unknown_topic_goes_to_assistant_which_reopens_instead_of_creating(self):
        await self.s.stage_action("show", artifact_id=self.a)
        await self.s.stage_action("show", artifact_id=self.d)
        prompts = []

        async def fake(system, prompt, max_tokens=4000):
            prompts.append(str(prompt))
            self.assertIn("em điều khiển được", str(system))
            return json.dumps({"thought": "người dùng muốn xem lại bộ slide đã chiếu", "artifact_needed": "slides",
                               "show": {"artifact_id": self.a, "slide": 2}, "chat_response": "Dạ, em mở lại bộ slide sự học khai phóng."},
                              ensure_ascii=False)
        n_before = len(db.get_artifacts(self.mid))
        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            await self.ask("em quay lại cái spotlight")
        self.assertIn("Nội dung đã có trong cuộc họp", prompts[0])
        self.assertIn(f"[{self.a}] bộ slide", prompts[0])
        self.assertEqual((self.s.stage["artifact_id"], self.s.stage["slide"]), (self.a, 1))
        self.assertEqual(len(db.get_artifacts(self.mid)), n_before)               # không tạo slide mới

    async def test_assistant_show_with_unknown_id_is_ignored(self):
        lib = await self.s._library_summary()
        self.assertIsNone(llm._pick_show({"artifact_id": 99999}, lib))
        self.assertEqual(llm._pick_show({"artifact_id": self.a, "slide": 9}, lib)["slide"], 2)   # quá số slide: slide cuối

    async def test_present_again_reopens_deck_from_dashboard(self):
        await self.s.stage_action("show", artifact_id=self.b)
        await self.s.stage_action("show", artifact_id=self.d)

        async def fake(system, prompt, max_tokens=4000):
            return json.dumps({"scripts": [("lời trình bày " * 15).strip()] * 3}, ensure_ascii=False)
        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            await self.ask("thuyết trình lại cái slide hồi nãy")
        art = db.get_artifact(self.s.stage["artifact_id"])
        self.assertEqual((art["kind"], art["title"]), ("slides", "Slide: Review phim Trại buôn người"))


if __name__ == "__main__":
    unittest.main()
