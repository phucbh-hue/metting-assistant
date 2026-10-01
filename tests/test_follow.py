"""Test tự chuyển slide theo lời trình bày."""
import json
import unittest
from unittest import mock

from tests.helpers import reset_db
from tests.test_stage import DECK
from meeting import db, follow, live


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.m = follow.SlideMatcher(DECK["slides"])

    def test_speech_matches_the_right_slide(self):
        cases = {
            "về tiến độ thì landing page đã xong bản desktop, bản mobile còn phần banner": 1,
            "ngân sách quý này mình đã chi khoảng một trăm hai mươi triệu": 2,
            "rủi ro lớn nhất là migrate Postgres đang chậm": 3,
        }
        for text, idx in cases.items():
            with self.subTest(text=text):
                sc = self.m.scores(text)
                self.assertEqual(max(range(len(sc)), key=lambda i: sc[i]), idx)

    def test_small_talk_matches_nothing(self):
        self.assertEqual(max(self.m.scores("ừ ok mọi người nghe rõ không, mình bắt đầu nhé")), 0.0)

    def test_best_bullet_of_current_slide(self):
        slide = DECK["slides"][1]
        self.assertEqual(self.m.best_bullet(slide, "bản mobile thì còn thiếu banner"), 1)
        self.assertIsNone(self.m.best_bullet(slide, "chào mọi người"))


class FollowerTests(unittest.TestCase):
    def test_forward_now_backward_needs_confirmation(self):
        f = follow.SlideFollower()
        self.assertEqual(f.decide([0, 8, 0, 0], cur=0, now=100, changed_at=0, manual_at=0), 1)   # ý tiếp theo
        self.assertIsNone(f.decide([9, 0, 1, 0], cur=2, now=100, changed_at=0, manual_at=0))     # lùi: chờ xác nhận
        self.assertEqual(f.decide([9, 0, 1, 0], cur=2, now=101, changed_at=0, manual_at=0), 0)
        self.assertIsNone(f.decide([0, 0, 0, 9], cur=0, now=100, changed_at=0, manual_at=0))     # nhảy xa: chờ
        self.assertIsNone(f.decide([0, 2, 0, 0], cur=0, now=101, changed_at=0, manual_at=0))     # khớp yếu: bỏ chờ
        self.assertIsNone(f.decide([0, 0, 0, 9], cur=0, now=102, changed_at=0, manual_at=0))

    def test_thresholds_cooldown_and_manual_hold(self):
        f = follow.SlideFollower()
        self.assertIsNone(f.decide([0, 2, 0], cur=0, now=100, changed_at=0, manual_at=0))         # khớp quá yếu
        self.assertIsNone(f.decide([5, 7, 0], cur=0, now=100, changed_at=0, manual_at=0))         # không hơn hẳn
        self.assertIsNone(f.decide([0, 9, 0], cur=0, now=100, changed_at=97, manual_at=0))        # vừa đổi slide
        self.assertIsNone(f.decide([0, 9, 0], cur=0, now=100, changed_at=0, manual_at=95))        # người dùng vừa chuyển
        self.assertEqual(f.decide([0, 9, 0], cur=0, now=120, changed_at=97, manual_at=95), 1)


class SessionFollowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Báo cáo sprint")
        self.s = await live.get_session(self.mid)
        self.q = await self.s.subscribe()
        self.aid = db.save_artifact(self.mid, "slides", "Slide: Báo cáo Sprint 39", json.dumps(DECK, ensure_ascii=False))
        await self.s.stage_action("show", artifact_id=self.aid)
        self.t = 0.0
        p1 = mock.patch.object(follow.SlideFollower, "COOLDOWN_S", 0.0)
        p2 = mock.patch.object(follow.SlideFollower, "MANUAL_HOLD_S", 0.0)
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    async def asyncTearDown(self):
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()

    async def say(self, text):
        await self.s.on_segment_finalized(self.t, self.t + 3, "1", text, voiced=2.0)
        self.t += 4
        await self.s.drain()

    def events(self, kind):
        out = []
        while not self.q.empty():
            e = self.q.get_nowait()
            if e["type"] == kind:
                out.append(e)
        return out

    async def test_presenter_speech_moves_slides(self):
        await self.say("Chào mọi người, hôm nay mình báo cáo sprint 39.")
        self.assertEqual(self.s.stage["slide"], 0)
        await self.say("Đầu tiên là tiến độ Mega Sale, landing page đã xong bản desktop.")
        self.assertEqual(self.s.stage["slide"], 1)
        await self.say("Tiếp theo là ngân sách quý 4, mình đã chi một trăm hai mươi triệu.")
        self.assertEqual(self.s.stage["slide"], 2)
        auto = [e for e in self.events("stage_state") if e.get("auto")]
        self.assertEqual([e["stage"]["slide"] for e in auto], [1, 2])
        self.assertTrue(all(e["stage"]["follow"] for e in auto))

    async def test_next_command_right_after_auto_switch_does_not_skip(self):
        """Lỗi thấy khi chạy demo: slide vừa tự sang "Tiến độ Mega Sale" thì người nói bảo "chuyển slide"
        (ý là sang đúng slide đó) -> từng nhảy thêm một slide."""
        await self.say("Hôm nay em báo cáo chiến dịch Mega Sale, landing page đã xong bản desktop.")
        self.assertEqual(self.s.stage["slide"], 1)
        await self.s._handle_ai_activation("chuyển slide", "Jarvis ơi, chuyển slide.", "Jarvis")
        self.assertEqual(self.s.stage["slide"], 1)
        self.assertIn("Đang ở slide 2: Tiến độ Mega Sale rồi ạ.", [e["text"] for e in self.events("ai_say")])
        await self.s._handle_ai_activation("chuyển slide", "Jarvis ơi, chuyển slide.", "Jarvis")
        self.assertEqual(self.s.stage["slide"], 2)          # lần sau là chuyển thật

    async def test_follow_off_and_assistant_calls_do_not_move_slides(self):
        await self.s.stage_action("follow", follow=False)
        await self.say("Đầu tiên là tiến độ Mega Sale, landing page đã xong bản desktop.")
        self.assertEqual(self.s.stage["slide"], 0)
        await self.s.stage_action("follow", follow=True)
        with mock.patch.object(self.s, "_handle_ai_activation", mock.AsyncMock()):
            await self.say("Jarvis ơi, mở slide về ngân sách quý 4 và tiến độ Mega Sale.")
        self.assertEqual(self.s.stage["slide"], 0)        # câu gọi trợ lý để trợ lý xử lý, không tự chuyển


if __name__ == "__main__":
    unittest.main()
