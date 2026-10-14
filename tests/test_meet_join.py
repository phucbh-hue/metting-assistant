"""Bot Google Meet (14/10/2026): tài khoản Recall hết credit, lý do bot không vào được, mời bot bằng link Meet.

Dữ liệu thật 12-14/10/2026: từ 13/10 15:00 mọi lần tạo bot bị Recall trả 402 insufficient_credit_balance; trước đó 18
bot kết thúc "Bot đã xong" mà chưa từng gửi âm thanh (lý do thật nằm ở bước call_ended trước "done"). Recall và Google
là bản giả (httpx.MockTransport), không gọi dịch vụ thật."""
import asyncio
import time
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_calendar_meet import MEET, NOW, MeetBase, event
from meeting import app as appmod
from meeting import auth, db, meet_bots, recall


class AccountProblemTests(MeetBase):
    def setUp(self):
        super().setUp()
        recall._account_problem.clear()

    def test_no_credit_is_explained_and_shown_until_a_bot_is_created(self):
        self.recall.no_credit = True
        self.autojoin("an@urbox.vn", events=[event("e1", NOW + 3 * 60)])
        self.tick()
        [rec] = self.records()
        self.assertEqual(rec["status"], "failed")
        self.assertIn("hết credit", rec["status_msg"])
        self.assertIn("hết credit", recall.account_problem()["text"])
        self.tick(NOW + 60)                                    # buổi đã lỗi: không gọi Recall lại mỗi phút
        self.assertEqual(sum(1 for c in self.recall.calls if c[0] == "POST"), 1)
        self.recall.no_credit = False                          # quản trị viên nạp credit, người dùng bấm Mời bot vào lại
        asyncio.run(meet_bots.retry("an@urbox.vn", event("e1", NOW + 3 * 60)))
        self.assertIsNone(recall.account_problem())


class NotJoinedReasonTests(MeetBase):
    def test_done_after_waiting_room_timeout_says_why(self):
        self.autojoin("an@urbox.vn", events=[event("e1", NOW + 3 * 60)])
        self.tick()
        [rec] = self.records()
        for code, sub in (("joining_call", None), ("in_waiting_room", None),
                          ("call_ended", "timeout_exceeded_waiting_room"), ("done", None)):
            self.recall.status(rec["bot_id"], code, sub)
        self.tick(NOW + 15 * 60)
        [rec] = self.records()
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(rec["status_msg"], "Bot không vào được cuộc họp (chờ ở phòng chờ quá lâu, chưa ai cho vào)")
        self.assertEqual(rec["sub_code"], "timeout_exceeded_waiting_room")

    def test_waiting_room_without_reason_and_joined_flag(self):
        st = recall.last_status({"status_changes": [{"code": "joining_call"}, {"code": "in_waiting_room"},
                                                    {"code": "done"}]})
        self.assertFalse(st["joined"])
        self.assertEqual(recall.not_joined_text(st),
                         "Bot không vào được cuộc họp (bot đã chờ ở phòng chờ nhưng chưa ai bấm Cho vào)")
        ok = recall.last_status({"status_changes": [{"code": "in_call_recording"}, {"code": "call_ended",
                                                                                      "sub_code": "call_ended_by_host"},
                                                    {"code": "done"}]})
        self.assertTrue(ok["joined"])


class ManualJoinTests(MeetBase):
    def setUp(self):
        super().setUp()
        recall._account_problem.clear()
        self.patches += [mock.patch.object(meet_bots, "run_soon", lambda: None),
                         mock.patch.object(meet_bots, "start", lambda: None)]
        for p in self.patches[-2:]:
            p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()
        self.h = self.hdr("an@urbox.vn", "Nguyễn Văn An")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    @staticmethod
    def hdr(email, name):
        return {"Authorization": f"Bearer {auth.issue({'email': email, 'name': name})['token']}"}

    def join(self, headers=None, **body):
        return self.client.post("/api/meet-bots/join", json=body, headers=headers or self.h)

    def test_paste_a_link_bot_joins_now_without_calendar(self):
        self.assertEqual(self.join(meet_url="https://zoom.us/j/123").status_code, 400)
        bot = self.join(meet_url="meet.google.com/ABC-defg-hij?authuser=0").json()["bot"]
        self.assertEqual((bot["meet_url"], bot["title"], bot["manual"]), (MEET, "Google Meet abc-defg-hij", True))
        [rec] = self.records()
        body = self.recall.bots[rec["bot_id"]]["body"]
        self.assertNotIn("join_at", body)                                       # không hẹn giờ: vào ngay
        self.assertEqual(body["meeting_url"], MEET)
        self.tick(time.time())                    # công tắc tự vào đang tắt, không có trên lịch: bot không bị hủy
        [rec] = self.records()
        self.assertNotEqual(rec["status"], "cancelled")
        self.assertEqual(self.join(meet_url=MEET).json()["bot"]["rid"], rec["rid"])      # bấm lại: không gọi bot thứ hai
        other = self.join(self.hdr("binh@urbox.vn", "Bình"), meet_url=MEET)
        self.assertEqual(other.status_code, 400)
        self.assertIn("đã được mời", other.json()["detail"])
        mine = self.client.get("/api/meet-bots", headers=self.h).json()
        self.assertEqual([b["rid"] for b in mine["items"]], [rec["rid"]])
        self.assertEqual(len(self.recall.bots), 1)

    def test_calendar_bot_reuses_a_pasted_link_bot(self):
        asyncio.run(meet_bots.join_now("an@urbox.vn", "An", MEET))
        now = time.time()
        self.autojoin("lan@urbox.vn", events=[event("e9", now + 60)])
        self.tick(now)
        self.assertEqual(len(self.recall.bots), 1)
        [rec] = self.records()
        self.assertIn("lan@urbox.vn", rec["also"])

    def test_bot_invited_from_a_room_feeds_that_meeting_and_a_failed_join_keeps_it_open(self):
        mid = db.create_meeting("Họp kết hợp", owner="an@urbox.vn")
        self.assertEqual(self.join(self.hdr("binh@urbox.vn", "Bình"), meet_url=MEET, meeting_id=mid).status_code, 404)
        bot = self.join(meet_url=MEET, meeting_id=mid).json()["bot"]
        self.assertEqual(bot["meeting_id"], mid)
        [rec] = self.records()
        self.assertEqual(asyncio.run(meet_bots.meeting_for(rec)), mid)          # âm thanh vào đúng cuộc họp đang mở
        for code, sub in (("joining_call", None), ("call_ended", "bot_kicked_from_waiting_room"), ("done", None)):
            self.recall.status(rec["bot_id"], code, sub)
        self.tick(time.time() + 5 * 60)
        [rec] = self.records()
        self.assertEqual(rec["status"], "failed")
        self.assertIn("từ chối cho bot vào", rec["status_msg"])
        self.assertNotEqual(db.get_meeting(mid)["status"], "ended")              # cuộc họp trong phòng vẫn tiếp tục

    def test_leave_removes_the_bot(self):
        rid = self.join(meet_url=MEET).json()["bot"]["rid"]
        self.assertEqual(self.client.post(f"/api/meet-bots/{rid}/leave", headers=self.hdr("binh@urbox.vn", "Bình"))
                         .status_code, 404)
        out = self.client.post(f"/api/meet-bots/{rid}/leave", headers=self.h).json()
        self.assertEqual(out["bot"]["status"], "cancelled")
        [rec] = self.records()
        self.assertTrue(self.recall.bots[rec["bot_id"]]["deleted"])


if __name__ == "__main__":
    import unittest
    unittest.main()
