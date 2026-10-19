"""Nhiều người dùng Copilot cùng một cuộc họp Google Meet (bản 3.21): vẫn một bot. Cuộc họp nhận âm thanh của người hẹn
bot gửi bản sao lời thoại sang cuộc họp riêng của từng người khác (phòng độc lập: trợ lý, sản phẩm AI, biên bản, Drive của
chính người đó). Ai xem được qua nhóm thì dùng chung cuộc họp của nhóm. Một người kết thúc chỉ dừng cuộc họp của mình.
Recall, Soniox, Google và AI đều là bản giả."""
import asyncio
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.test_calendar_meet import MEET, NOW, MeetBase, connect_google, event, frame, tone
from meeting import app as appmod
from meeting import artifacts, auth, db, drive_sync, group_memory, groups, live, meet_bots, user_prefs

AN, LAN, BINH, CUONG, DUNG = "an@urbox.vn", "lan@urbox.vn", "binh@urbox.vn", "cuong@urbox.vn", "dung@urbox.vn"
NAME = {AN: "Nguyễn Văn An", LAN: "Trần Thị Lan", BINH: "Lê Văn Bình", CUONG: "Phạm Cường", DUNG: "Đỗ Dung"}
MINUTES = "# Biên bản\n\n- Chốt phát hành ngày 15/12/2026"


class FakeWS:
    """Recall nối vào và gửi lần lượt các gói tin; phần tử là hàm async thì chạy nó (ai đó bấm nút giữa cuộc họp)."""

    def __init__(self, token, frames):
        self.query_params = {"token": token}
        self.frames = list(frames)
        self.accepted, self.closed = False, None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed = code

    async def receive(self):
        while self.frames:
            f = self.frames.pop(0)
            if callable(f):
                await f()
                continue
            return {"type": "websocket.receive", "text": f}
        return {"type": "websocket.disconnect"}


class SharedBase(MeetBase):
    def setUp(self):
        super().setUp()
        self.fed, self.handled, self.calls, self.saved = [], [], [], []

        async def ensure_open(st):
            st.ws, st.down = "fake", False

        async def feed(st, pcm):
            self.fed.append((st.session.id, st.name))

        async def activation(s, command, full_sentence, name="", source="voice"):
            self.handled.append(s.id)

        async def call_llm(system, prompt, max_tokens=4000):
            self.calls.append(prompt)
            await asyncio.sleep(0.05)
            return MINUTES

        async def same(mid, meeting, content):
            return content
        self.patches += [mock.patch.object(live.MeetingStream, "ensure_open", ensure_open),
                         mock.patch.object(live.MeetingStream, "feed", feed),
                         mock.patch.object(live.MeetingSession, "_handle_ai_activation", activation),
                         mock.patch.object(artifacts, "_call_llm", call_llm),
                         mock.patch.object(artifacts, "llm_available", lambda: True),
                         mock.patch.object(group_memory, "with_changes", same),
                         mock.patch.object(drive_sync, "schedule", lambda mid: self.saved.append(mid) or True)]
        for p in self.patches[-7:]:
            p.start()
        # An hẹn bot, gắn buổi này vào nhóm Sprint của An (Cường là thành viên). Bình gắn buổi này vào nhóm của Bình.
        # Lan không chọn nhóm. Cả bốn người đều bật "tự cho bot vào".
        self.g = groups.create_group("Sprint Payment", AN)
        groups.update_group(self.g["id"], members=[CUONG])
        self.h = groups.create_group("Đội của Bình", BINH)
        for email in (AN, LAN, BINH, CUONG):
            self.autojoin(email, events=[event("e1", NOW + 2 * 60, series="s-sprint")])
            user_prefs.update(email, name=NAME[email])              # tên Google, trùng tên hiện trong Meet
        meet_bots.set_series_group(AN, "s-sprint", self.g["id"])
        meet_bots.set_series_group(BINH, "s-sprint", self.h["id"])
        self.tick()
        [self.rec] = self.records()

    def rec_now(self):
        return self.records()[0]

    def copies(self):
        return {c["email"]: c["meeting_id"] for c in self.rec_now().get("copies") or []}

    async def start(self, frames=None):
        """Bot gửi gói âm thanh đầu tiên (Lan nói): tạo cuộc họp nhận âm thanh và cuộc họp riêng."""
        ws = FakeWS(self.rec["token"], frames or [frame(2, NAME[LAN], tone(0.5))])
        await meet_bots.serve(ws, self.rec["rid"])
        src = await live.get_session(self.rec_now()["meeting_id"])
        src.meet_people.setdefault("meet2", {"name": NAME[LAN], "email": "", "is_host": False, "speaking": True})
        src.meet_people.setdefault("meet3", {"name": NAME[BINH], "email": "", "is_host": False, "speaking": True})
        return src

    async def say(self, src, t, text, stream):
        await src.on_segment_finalized(t, t + 2.0, None, text, stream=stream)
        await self.settle()

    async def settle(self):
        for _ in range(3):
            for s in list(live.SESSIONS.values()):
                if not s.disposed:
                    await s.drain()
            await asyncio.sleep(0.01)

    @staticmethod
    def lines(s):
        return [(seg["speaker_label"], seg["text"]) for seg in s.segments]


class SharedBotTests(SharedBase):
    def test_one_bot_each_person_a_private_meeting_group_members_share(self):
        async def go():
            src = await self.start()
            return src, self.copies()
        src, copies = asyncio.run(go())
        self.assertEqual(len(self.recall.bots), 1)                              # vẫn chỉ một bot trong Meet
        self.assertEqual(sorted(copies), [BINH, LAN])                           # Cường xem qua nhóm Sprint: dùng chung
        m0 = db.get_meeting(src.id)
        self.assertEqual((m0["owner"], m0["group_id"]), (AN, self.g["id"]))
        lan, binh = db.get_meeting(copies[LAN]), db.get_meeting(copies[BINH])
        self.assertEqual((lan["owner"], lan.get("group_id"), lan["meet"]["copy_of"]), (LAN, None, src.id))
        self.assertEqual((binh["owner"], binh["group_id"]), (BINH, self.h["id"]))   # nhóm do chính Bình chọn
        self.assertEqual((lan["title"], lan["started_at"], lan["agenda"]), (m0["title"], m0["started_at"], m0["agenda"]))
        rec = self.rec_now()
        self.assertEqual([meet_bots.meeting_of(rec, e) for e in (AN, LAN, BINH, CUONG, DUNG)],
                         [src.id, copies[LAN], copies[BINH], src.id, None])

    def test_lines_names_and_speaker_fixes_follow_but_each_room_stays_independent(self):
        async def go():
            src = await self.start()
            lan = await live.get_session(self.copies()[LAN])
            binh = await live.get_session(self.copies()[BINH])
            await self.say(src, 1.0, "Em báo cáo tiến độ tuần này.", "meet2")
            await self.say(src, 4.0, "Bên anh xong phần thanh toán rồi.", "meet3")
            first = (self.lines(src), self.lines(lan), self.lines(binh))
            db_rows = db.get_segments(lan.id)
            # An đổi tên người nói trong cuộc họp của An: các cuộc họp riêng đổi theo
            sid_lan = src.speakers.pinned("meet2").sid
            await src.rename_speaker(sid_lan, "Lan Trần")
            await self.settle()
            renamed = (self.lines(lan)[0][0], self.lines(binh)[0][0])
            # Lan tự sửa tên trong cuộc họp của mình: An đổi tên lần nữa thì cuộc họp của Lan giữ tên Lan đã sửa
            await lan.rename_speaker(lan.speakers.pinned("meet2").sid, "Chị Lan")
            await src.rename_speaker(sid_lan, "Trần Thị Lan")
            await self.settle()
            kept = (self.lines(lan)[0][0], self.lines(binh)[0][0])
            # An sửa người nói của câu thứ hai: cuộc họp riêng sửa theo
            await src.reassign_segment(src.segments[1]["seq"], sid_lan)
            await self.settle()
            moved = (self.lines(lan)[1][0], self.lines(binh)[1][0])
            try:
                await lan.reanalyze()
                refused = False
            except ValueError:
                refused = True                                              # không có dữ liệu giọng để phân tích lại
            return first, db_rows, renamed, kept, moved, src, lan, refused
        first, db_rows, renamed, kept, moved, src, lan, refused = asyncio.run(go())
        self.assertTrue(refused)
        self.assertEqual(first[0], first[1])
        self.assertEqual(first[0], first[2])
        self.assertEqual(first[0], [(NAME[LAN], "Em báo cáo tiến độ tuần này."), (NAME[BINH], "Bên anh xong phần thanh toán rồi.")])
        self.assertEqual([r["src_seq"] for r in db_rows], [s["seq"] for s in src.segments])
        self.assertEqual([s["speaker_key"] for s in lan.segments], [s["speaker_key"] for s in src.segments])   # cùng số
        self.assertEqual(renamed, ("Lan Trần", "Lan Trần"))
        self.assertEqual(kept, ("Chị Lan", "Trần Thị Lan"))
        self.assertEqual(moved, ("Chị Lan", "Trần Thị Lan"))

    def test_whoever_calls_the_assistant_gets_the_answer_in_their_own_open_room(self):
        async def go():
            src = await self.start()
            lan = await live.get_session(self.copies()[LAN])
            await lan.subscribe()                                               # Lan đang mở cuộc họp riêng
            await self.say(src, 1.0, "Jarvis ơi, tóm tắt giúp em phần vừa rồi.", "meet2")
            await asyncio.sleep(0.05)
            by_lan = list(self.handled)
            await self.say(src, 4.0, "Jarvis ơi, vẽ sơ đồ quy trình thanh toán.", "meet3")   # Bình không mở phòng
            await asyncio.sleep(0.05)
            return src, lan, by_lan, list(self.handled)
        src, lan, by_lan, both = asyncio.run(go())
        self.assertEqual(by_lan, [lan.id])                                      # phòng của Lan trả lời, phòng An thì không
        self.assertEqual(both, [lan.id, src.id])                                # Bình không mở phòng: phòng của An trả lời

    def test_bot_leaves_every_meeting_ends_one_ai_call_each_recap_in_its_own_drive(self):
        async def go():
            src = await self.start()
            await self.say(src, 1.0, "Chốt phát hành ngày 15 tháng 12.", "meet2")
            await self.say(src, 4.0, "Bên anh đồng ý.", "meet3")
            await meet_bots.finish(self.rec_now())                             # mọi người rời Meet, bot rời theo
            mids = [src.id] + list(self.copies().values())
            for _ in range(200):
                if all(db.get_meeting(m)["minutes_status"] in ("done", "failed") for m in mids):
                    break
                await asyncio.sleep(0.02)
            return mids
        mids = asyncio.run(go())
        self.assertEqual(len(self.calls), 1)                                    # cùng lời thoại, cùng mẫu: gọi AI một lần
        for mid in mids:
            m = db.get_meeting(mid)
            self.assertEqual((m["status"], m["minutes_status"]), ("ended", "done"))
            [minutes] = [a for a in db.get_artifacts(mid) if a["kind"] == "minutes"]
            self.assertEqual(minutes["content"], MINUTES)
        self.assertEqual(sorted(self.saved), sorted(mids))
        keepers = {mid: drive_sync.keeper_of(db.get_meeting(mid), groups.get_group(db.get_meeting(mid).get("group_id")))
                   for mid in mids}
        self.assertEqual(keepers, {mids[0]: AN, self.copies()[LAN]: LAN, self.copies()[BINH]: BINH})

    def test_owner_ending_early_hands_the_bot_over_and_the_others_keep_going(self):
        state = {}

        async def owner_ends():
            src = live.SESSIONS[self.rec_now()["meeting_id"]]
            state["src"] = src.id
            await src.finish(generate_minutes=True)
            await meet_bots.on_meeting_ended(src.id)

        async def go():
            await self.start([frame(2, NAME[LAN], tone(0.5)), owner_ends, frame(3, NAME[BINH], tone(0.5)),
                              frame(3, NAME[BINH], tone(0.5))])
            await self.settle()
        asyncio.run(go())
        rec = self.rec_now()
        copies = self.copies()
        self.assertEqual(rec["status"], "in_call")                             # bot ở lại
        self.assertFalse(self.recall.bots[rec["bot_id"]]["left"])
        self.assertEqual(rec["owner"], BINH)                                    # cuộc họp riêng của Bình nhận âm thanh
        new_src = rec["meeting_id"]
        self.assertNotEqual(new_src, state["src"])
        self.assertIsNone(db.get_meeting(new_src)["meet"]["copy_of"])
        # Lan chép theo cuộc họp mới; Cường trước xem qua nhóm Sprint của An (đã kết thúc) nay có cuộc họp riêng
        self.assertEqual(sorted(copies), [CUONG, LAN])
        self.assertEqual(db.get_meeting(copies[LAN])["meet"]["copy_of"], new_src)
        self.assertEqual(db.get_meeting(copies[CUONG])["owner"], CUONG)
        self.assertIn((new_src, "meet3"), self.fed)                             # âm thanh sau đó vào cuộc họp mới
        self.assertEqual(db.get_meeting(state["src"])["status"], "ended")
        self.assertIn(AN, rec["detached"])                                      # không tự tạo lại cuộc họp cho An

    def test_someone_ending_their_own_meeting_stops_only_theirs(self):
        async def go():
            src = await self.start()
            lan = await live.get_session(self.copies()[LAN])
            await self.say(src, 1.0, "Câu thứ nhất.", "meet2")
            await lan.finish(generate_minutes=True)
            await meet_bots.on_meeting_ended(lan.id)
            await self.say(src, 4.0, "Câu sau khi Lan kết thúc.", "meet3")
            binh = await live.get_session(self.copies()[BINH])
            return src, lan, binh
        src, lan, binh = asyncio.run(go())
        rec = self.rec_now()
        self.assertEqual(sorted(self.copies()), [BINH])
        self.assertIn(LAN, rec["detached"])
        self.assertFalse(self.recall.bots[rec["bot_id"]]["left"])
        self.assertEqual([t for _, t in self.lines(lan)], ["Câu thứ nhất."])
        self.assertEqual([t for _, t in self.lines(binh)], ["Câu thứ nhất.", "Câu sau khi Lan kết thúc."])
        self.assertEqual(rec["meeting_id"], src.id)

    def test_late_joiner_gets_what_was_said_so_far(self):
        async def go():
            src = await self.start()
            await self.say(src, 1.0, "Jarvis ơi, ghi lại giúp em quyết định này.", "meet2")
            await self.say(src, 4.0, "Chốt phát hành ngày 15 tháng 12.", "meet3")
            self.handled.clear()
            self.autojoin(DUNG, events=[event("e1", NOW + 2 * 60, series="s-sprint")])   # Dũng bật bot muộn
            await meet_bots.tick(NOW + 60)
            await self.settle()
            dung = await live.get_session(self.copies()[DUNG])
            return dung
        dung = asyncio.run(go())
        self.assertEqual([t for _, t in self.lines(dung)],
                         ["Jarvis ơi, ghi lại giúp em quyết định này.", "Chốt phát hành ngày 15 tháng 12."])
        self.assertEqual(self.handled, [])                                      # câu cũ chép bù không gọi trợ lý
        self.assertEqual(db.get_meeting(dung.id)["owner"], DUNG)

    def test_paste_the_link_from_your_own_room_while_the_bot_is_there(self):
        async def go():
            src = await self.start()
            room = db.create_meeting("Phòng của Dũng", owner=DUNG)
            db.update_meeting(room, {"started_at": src.started_at + 100}, True)
            await meet_bots.join_now(DUNG, NAME[DUNG], MEET, meeting_id=room, now=NOW + 3 * 60)
            await self.say(src, 200.0, "Bên phòng Dũng nghe rõ không?", "meet2")
            return room, await live.get_session(room)
        room, s = asyncio.run(go())
        self.assertEqual(len(self.recall.bots), 1)                              # không gọi bot thứ hai
        self.assertEqual(self.copies()[DUNG], room)                             # chính phòng đang mở nhận lời thoại
        self.assertEqual(db.get_meeting(room)["meet"]["copy_of"], self.rec_now()["meeting_id"])
        [seg] = s.segments
        self.assertEqual((seg["text"], seg["t_start"]), ("Bên phòng Dũng nghe rõ không?", 100.0))   # theo giờ của phòng


class SharedBotApiTests(SharedBase):
    def setUp(self):
        super().setUp()
        self.patches += [mock.patch.object(meet_bots, "run_soon", lambda: None),
                         mock.patch.object(meet_bots, "start", lambda: None)]
        for p in self.patches[-2:]:
            p.start()
        for email in (AN, LAN, CUONG):
            connect_google(email)
        asyncio.run(self.start())
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    @staticmethod
    def hdr(email):
        return {"Authorization": f"Bearer {auth.issue({'email': email, 'name': NAME[email]})['token']}"}

    def test_calendar_shows_each_person_the_meeting_they_can_open(self):
        out = {}
        for email in (AN, LAN, CUONG):
            [ev] = self.client.get("/api/calendar", headers=self.hdr(email)).json()["events"]
            out[email] = (ev["bot"]["meeting_id"], ev["bot_mine"], ev["bot"].get("shared_by"))
        src = self.rec_now()["meeting_id"]
        self.assertEqual(out, {AN: (src, True, None), LAN: (self.copies()[LAN], True, NAME[AN]),
                               CUONG: (src, True, NAME[AN])})

    def test_leave_from_a_private_meeting_only_detaches_it(self):
        rid = self.rec["rid"]
        r = self.client.post(f"/api/meet-bots/{rid}/leave", headers=self.hdr(LAN)).json()
        self.assertFalse(r["left"])
        self.assertEqual(sorted(self.copies()), [BINH])
        self.assertFalse(self.recall.bots[self.rec_now()["bot_id"]]["left"])


if __name__ == "__main__":
    unittest.main()
