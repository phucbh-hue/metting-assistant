"""Giai đoạn 2 (bản 3.18): Lịch Google, bot Recall vào Google Meet theo lịch, nhận âm thanh từng người trong Meet,
họp nửa online nửa trực tiếp. Google và Recall đều là bản giả (httpx.MockTransport), không gọi dịch vụ thật."""
import asyncio
import base64
import json
import os
import re
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import httpx
import numpy as np
from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import auth, db, gcalendar, google_oauth, groups, live, meet_bots, recall, user_prefs

WEB = dict(ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS=set(),
           CLIENT_ID="123-test.apps.googleusercontent.com", TTL_S=3600)
ENV = {"RECALLAI_API_KEY": "rk-test", "PUBLIC_BASE_URL": "https://meet-api.example.com", "RECALL_REGION": "",
       "GOOGLE_OAUTH_CLIENT_ID": "123-test.apps.googleusercontent.com", "GOOGLE_OAUTH_CLIENT_SECRET": "s3cret",
       "AUTH_SECRET": "a-stable-secret-for-tests-0123456789"}
NOW = 1_800_000_000.0
MEET = "https://meet.google.com/abc-defg-hij"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def event(eid="e1", start=NOW + 20 * 60, meet=MEET, series="", declined=False, title="Họp sprint thanh toán"):
    return {"id": eid, "series": series, "title": title, "start": start, "end": start + 3600, "all_day": False,
            "meet_url": meet, "declined": declined, "organizer": "an@urbox.vn", "is_organizer": True,
            "attendees": [{"name": "Nguyễn Văn An", "email": "an@urbox.vn", "response": "accepted", "organizer": True},
                          {"name": "Trần Thị Lan", "email": "lan@urbox.vn", "response": "accepted", "organizer": False}],
            "description": "- Báo cáo tiến độ\n- Kế hoạch tuần sau\nhttps://meet.google.com/abc-defg-hij",
            "link": "", "kind": "default"}


def connect_google(email, calendar=True):
    scopes = ["openid", "email", google_oauth.DRIVE_SCOPE] + ([google_oauth.CALENDAR_SCOPE] if calendar else [])
    db._get_db()["google_tokens"].insert_one({"email": email, "refresh_enc": "x", "scopes": scopes,
                                              "google_email": email, "connected_at": NOW})


class FakeRecall:
    """Recall giả: tạo / đọc / xóa bot, rời cuộc họp, xóa media; key chỉ dùng được ở một vùng."""

    def __init__(self, region="ap-northeast-1"):
        self.region, self.bots, self.calls, self.busy, self.n = region, {}, [], 0, 0

    def status(self, bid, code, sub_code=None):
        self.bots[bid]["status_changes"].append({"code": code, "sub_code": sub_code, "message": "",
                                                 "created_at": iso(NOW)})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        self.calls.append((req.method, host.split(".")[0], path))
        if not host.startswith(self.region + ".") or req.headers.get("authorization") != "Token rk-test":
            return httpx.Response(401, json={"detail": "Invalid token."})
        if path == "/api/v1/bot/" and req.method == "GET":
            return httpx.Response(200, json={"results": [], "next": None})
        if path == "/api/v1/bot/" and req.method == "POST":
            if self.busy:
                self.busy -= 1
                return httpx.Response(507, json={"detail": "No ad-hoc bots available"})
            self.n += 1
            bid = f"bot-{self.n}"
            self.bots[bid] = {"id": bid, "body": json.loads(req.content), "deleted": False, "left": False,
                              "media_deleted": False, "status_changes": []}
            return httpx.Response(201, json={"id": bid, "status_changes": []})
        m = re.fullmatch(r"/api/v1/bot/([^/]+)/(leave_call/|delete_media/)?", path)
        bot = self.bots.get(m.group(1)) if m else None
        if bot is None:
            return httpx.Response(404, json={"detail": "Not found."})
        if req.method == "GET":
            return httpx.Response(200, json={"id": bot["id"], "status_changes": bot["status_changes"]})
        if req.method == "DELETE":
            if any(c["code"] not in ("ready",) for c in bot["status_changes"]):
                return httpx.Response(400, json={"detail": "Bot already dispatched"})
            bot["deleted"] = True
            return httpx.Response(204)
        if m.group(2) == "leave_call/":
            bot["left"] = True
        else:
            bot["media_deleted"] = True
        return httpx.Response(200, json={})


class MeetBase(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.recall = FakeRecall()
        self.events = {}                     # email -> danh sách buổi trên lịch

        async def upcoming(email, refresh=False, now=None):
            if email not in self.events:
                raise google_oauth.NeedReconnect(f"{email} chưa kết nối Google")
            return self.events[email]
        self.patches = [mock.patch.multiple(auth, **WEB), mock.patch.dict(os.environ, ENV),
                        mock.patch.object(recall, "_transport", httpx.MockTransport(self.recall)),
                        mock.patch.object(gcalendar, "upcoming", upcoming)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def autojoin(self, email, on=True, events=None):
        user_prefs.update(email, calendar_autojoin=on, name=email.split("@")[0].title())
        if events is not None:
            self.events[email] = events

    def records(self, **match):
        return [meet_bots.strip(r, True) for r in db._get_db()["meet_bots"].find(match)]

    def tick(self, now=NOW):
        asyncio.run(meet_bots.tick(now))


class SchedulingTests(MeetBase):
    def test_bot_is_scheduled_ahead_with_one_audio_stream_per_person(self):
        self.autojoin("an@urbox.vn", events=[event()])
        self.tick()
        [rec] = self.records()
        bot = self.recall.bots[rec["bot_id"]]["body"]
        self.assertEqual(bot["meeting_url"], MEET)
        self.assertEqual(bot["join_at"], iso(NOW + 20 * 60))                  # hẹn trước (còn hơn 10 phút)
        cfg = bot["recording_config"]
        self.assertEqual(cfg["audio_separate_raw"], {})
        ep = cfg["realtime_endpoints"][0]
        self.assertEqual((ep["type"], ep["events"]), ("websocket", ["audio_separate_raw.data"]))
        self.assertEqual(ep["url"], f"wss://meet-api.example.com/ws/recall/{rec['rid']}/?token={rec['token']}")
        self.assertEqual(cfg["retention"], {"type": "timed", "hours": 1})    # Recall không giữ bản ghi lâu
        msg = bot["chat"]["on_bot_join"]
        self.assertEqual(msg["send_to"], "everyone")
        self.assertLessEqual(len(msg["message"]), 500)
        self.assertIn("chép lời", msg["message"])
        self.assertEqual(rec["status"], "scheduled")
        # vùng Recall tự dò: us-west-2, us-east-1, eu-central-1 báo key sai, ap-northeast-1 đúng; lần sau không dò lại
        self.assertEqual([c[1] for c in self.recall.calls if c[0] == "GET"],
                         ["us-west-2", "us-east-1", "eu-central-1", "ap-northeast-1"])
        self.assertEqual(db.get_setting("recall_region"), "ap-northeast-1")
        self.tick(NOW + 60)
        self.assertEqual(len(self.recall.bots), 1)                             # không hẹn trùng

    def test_meeting_soon_or_running_joins_now_far_meeting_waits(self):
        self.autojoin("an@urbox.vn", events=[event("soon", NOW + 5 * 60), event("far", NOW + 45 * 60,
                                                                                 meet="https://meet.google.com/xyz-abcd-efg"),
                                             event("zoom", NOW + 60, meet=""), event("no", NOW + 60, declined=True,
                                                                                    meet="https://meet.google.com/qqq-wwww-eee")])
        self.tick()
        [rec] = self.records()
        self.assertEqual(rec["event_id"], "soon")
        self.assertNotIn("join_at", self.recall.bots[rec["bot_id"]]["body"])   # còn dưới 10 phút: vào ngay

    def test_one_bot_per_meet_link_even_if_two_people_turn_it_on(self):
        self.autojoin("an@urbox.vn", events=[event()])
        self.autojoin("lan@urbox.vn", events=[event()])
        self.tick()
        [rec] = self.records()
        self.assertEqual(len(self.recall.bots), 1)
        self.assertEqual(rec["owner"], "an@urbox.vn")
        self.assertEqual(rec["also"], ["lan@urbox.vn"])

    def test_skip_hands_over_then_turning_off_deletes_the_bot(self):
        self.autojoin("an@urbox.vn", events=[event()])
        self.autojoin("lan@urbox.vn", events=[event()])
        self.tick()
        meet_bots.skip_event("an@urbox.vn", "e1", True)
        self.tick(NOW + 60)
        [rec] = self.records()
        self.assertEqual((rec["owner"], rec["status"]), ("lan@urbox.vn", "scheduled"))   # Lan vẫn muốn bot vào
        self.assertFalse(self.recall.bots[rec["bot_id"]]["deleted"])
        self.autojoin("lan@urbox.vn", on=False)
        self.tick(NOW + 120)
        [rec] = self.records()
        self.assertEqual(rec["status"], "cancelled")
        self.assertTrue(self.recall.bots[rec["bot_id"]]["deleted"])

    def test_skipping_a_running_meeting_makes_the_bot_leave_but_turning_off_does_not(self):
        self.autojoin("an@urbox.vn", events=[event("e1", NOW + 3 * 60)])
        self.tick()
        [rec] = self.records()
        self.recall.status(rec["bot_id"], "in_call_recording")
        self.tick(NOW + 4 * 60)
        self.autojoin("an@urbox.vn", on=False)                                  # tắt công tắc: chỉ cho các buổi sau
        self.tick(NOW + 5 * 60)
        self.assertFalse(self.recall.bots[rec["bot_id"]]["left"])
        meet_bots.skip_event("an@urbox.vn", "e1", True)                         # bỏ qua chính buổi này: bot rời ngay
        self.tick(NOW + 6 * 60)
        [rec] = self.records()
        self.assertTrue(self.recall.bots[rec["bot_id"]]["left"])
        self.assertTrue(rec["left_at"])

    def test_moved_meeting_is_rescheduled(self):
        self.autojoin("an@urbox.vn", events=[event()])
        self.tick()
        self.events["an@urbox.vn"] = [event(start=NOW + 28 * 60)]               # dời giờ
        self.tick(NOW + 60)
        old, new = sorted(self.records(), key=lambda r: r["created_at"])
        self.assertEqual(old["status"], "cancelled")
        self.assertTrue(self.recall.bots[old["bot_id"]]["deleted"])
        self.assertEqual(self.recall.bots[new["bot_id"]]["body"]["join_at"], iso(NOW + 28 * 60))

    def test_recall_busy_is_retried_next_minute(self):
        self.recall.busy = 1
        self.autojoin("an@urbox.vn", events=[event("e1", NOW + 3 * 60)])
        self.tick()
        [rec] = self.records()
        self.assertEqual((rec["status"], rec["bot_id"]), ("pending", ""))
        self.tick(NOW + 60)
        [rec] = self.records()
        self.assertTrue(rec["bot_id"])

    def test_status_around_join_time_and_why_it_failed(self):
        self.autojoin("an@urbox.vn", events=[event("e1", NOW + 5 * 60)])
        self.tick()
        [rec] = self.records()
        self.recall.status(rec["bot_id"], "joining_call")
        self.recall.status(rec["bot_id"], "in_waiting_room")
        self.tick(NOW + 5 * 60)
        [rec] = self.records()
        self.assertEqual(rec["status"], "waiting")
        self.assertIn("Cho vào", rec["status_msg"])
        self.recall.status(rec["bot_id"], "fatal", "google_meet_organisation_restricted")
        self.tick(NOW + 7 * 60)
        [rec] = self.records()
        self.assertEqual(rec["status"], "failed")
        self.assertIn("người trong tổ chức", rec["status_msg"])
        self.assertTrue(self.recall.bots[rec["bot_id"]]["media_deleted"])


class FakeWS:
    def __init__(self, token, frames):
        self.query_params = {"token": token}
        self.frames = list(frames)
        self.accepted, self.closed = False, None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed = code

    async def receive(self):
        if self.frames:
            return {"type": "websocket.receive", "text": self.frames.pop(0)}
        return {"type": "websocket.disconnect"}


def tone(seconds, amp=4000):
    t = np.arange(int(16000 * seconds)) / 16000
    return (np.sin(2 * np.pi * 220 * t) * amp).astype("<i2").tobytes()


def frame(pid, name, pcm, email=None):
    return json.dumps({"event": "audio_separate_raw.data", "data": {"data": {
        "buffer": base64.b64encode(pcm).decode(), "timestamp": {"relative": 1.0, "absolute": iso(NOW)},
        "participant": {"id": pid, "name": name, "is_host": pid == 1, "platform": "desktop", "extra_data": {},
                        "email": email}}}})


class ReceiverTests(MeetBase):
    def setUp(self):
        super().setUp()
        self.fed, self.opened = [], []

        async def ensure_open(st):
            self.opened.append(st.name)
            st.ws, st.down = "fake", False

        async def feed(st, pcm):
            self.fed.append((st.name, len(pcm)))
        self.patches += [mock.patch.object(live.MeetingStream, "ensure_open", ensure_open),
                         mock.patch.object(live.MeetingStream, "feed", feed)]
        for p in self.patches[-2:]:
            p.start()
        self.autojoin("an@urbox.vn", events=[event("e1", NOW + 2 * 60, series="s-sprint")])
        g = groups.create_group("Sprint Payment", "an@urbox.vn")
        self.gid = g["id"]
        meet_bots.set_series_group("an@urbox.vn", "s-sprint", self.gid)
        self.tick()
        [self.rec] = self.records()

    def serve(self, frames, token=None):
        ws = FakeWS(self.rec["token"] if token is None else token, frames)
        asyncio.run(meet_bots.serve(ws, self.rec["rid"]))
        return ws

    def test_wrong_token_is_refused(self):
        ws = self.serve([frame(1, "Trần Thị Lan", tone(1))], token="sai")
        self.assertEqual((ws.accepted, ws.closed), (False, 4403))
        self.assertIsNone(self.records()[0]["meeting_id"])

    def test_first_audio_creates_the_meeting_and_each_person_gets_a_stream(self):
        silence = bytes(3200)
        ws = self.serve([frame(1, "Trần Thị Lan", silence), frame(1, "Trần Thị Lan", tone(0.5)),
                         frame(2, "Lê Minh", silence), frame(2, "Lê Minh", silence)])
        self.assertTrue(ws.accepted)
        [rec] = self.records()
        m = db.get_meeting(rec["meeting_id"])
        self.assertEqual((m["title"], m["owner"], m["source"], m["group_id"]),
                         ("Họp sprint thanh toán", "an@urbox.vn", "meet", self.gid))
        self.assertEqual(m["agenda"], ["Báo cáo tiến độ", "Kế hoạch tuần sau"])
        self.assertEqual(m["expected_attendees"], ["Nguyễn Văn An", "Trần Thị Lan"])
        self.assertEqual(m["meet"]["url"], MEET)
        # Lan nói: luồng meet1 mở, nhận cả 0,1 giây im lặng ngay trước câu nói; Minh chỉ im lặng: không mở, không tốn phí
        self.assertEqual(self.opened, ["meet1"])
        self.assertEqual(self.fed, [("meet1", 3200), ("meet1", 16000)])
        s = live.SESSIONS[rec["meeting_id"]]
        self.assertEqual({p["name"] for p in s.meet_public()["people"]}, {"Trần Thị Lan", "Lê Minh"})
        self.assertFalse(rec["ws_open"])                                        # bot đã ngắt (hết gói tin)
        self.assertTrue(rec["ws_closed_at"])

    def test_room_device_is_not_transcribed_twice(self):
        mid = asyncio.run(meet_bots.meeting_for(self.rec))

        async def go():
            s = await live.get_session(mid)
            s.audio_owner = object()                                            # mic trong phòng đang bật
            s.set_mic_user({"email": "an@urbox.vn", "name": "Nguyễn Văn An"})
            await s.feed_meet({"id": "7", "name": "Nguyen Van An", "email": "", "is_host": True}, tone(0.5))
            first = list(self.fed)
            await s.set_meet_mode("meet7", "listen")                            # tự đánh dấu: vẫn chép
            await s.feed_meet({"id": "7", "name": "Nguyen Van An", "email": "", "is_host": True}, tone(0.5))
            return first, s.meet_public()
        first, pub = asyncio.run(go())
        self.assertEqual(first, [])
        self.assertEqual(self.fed, [("meet7", 16000)])
        self.assertEqual(pub["people"][0]["mode"], "listen")
        self.assertEqual(db.get_meeting(mid)["meet"]["modes"], {"meet7": "listen"})

    def test_bot_leaving_ends_the_meeting_and_media_is_deleted(self):
        self.serve([frame(1, "Trần Thị Lan", tone(0.5))])
        [rec] = self.records()
        self.recall.status(rec["bot_id"], "in_call_recording")
        self.recall.status(rec["bot_id"], "call_ended", "timeout_exceeded_everyone_left")

        left = NOW + 5000            # bot ngắt âm thanh lúc này (ngoài khoảng theo dõi quanh giờ vào): chờ 2 phút mới hỏi
        db._get_db()["meet_bots"].update_one({"rid": rec["rid"]}, {"$set": {"ws_closed_at": left, "polled_at": 0}})

        async def go():
            await meet_bots.watch(left + 30)                                    # bot mới rời 30 giây: chờ thêm
            mid_status = db.get_meeting(rec["meeting_id"])["status"]
            await meet_bots.watch(left + 150)
            return mid_status
        self.assertEqual(asyncio.run(go()), "live")
        self.assertEqual(db.get_meeting(rec["meeting_id"])["status"], "ended")
        [rec] = self.records()
        self.assertEqual(rec["status"], "ended")
        self.assertTrue(self.recall.bots[rec["bot_id"]]["media_deleted"])


class MeetSpeakerTests(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_voice_gate_keeps_the_start_of_a_sentence_and_pauses_on_silence(self):
        g = live.VoiceGate()
        silence = bytes(6400)                                                   # 0,2 giây
        for i in range(5):
            self.assertEqual(g.push(silence, i * 0.2), ([], False))
        out, stop = g.push(tone(0.2), 1.0)
        self.assertFalse(stop)
        self.assertEqual(sum(len(c) for c in out), int(0.6 * 32000) + 6400)     # 0,6 giây trước câu nói + câu nói
        self.assertEqual(g.push(silence, 2.0), ([silence], False))
        self.assertEqual(g.push(silence, 1.0 + live.VoiceGate.IDLE_S + 1), ([], True))

    def test_meet_stream_is_one_named_person_and_never_absorbs_the_room(self):
        async def go():
            mid = db.create_meeting("Họp", owner="an@urbox.vn")
            s = await live.get_session(mid)
            s.meet_people["meet7"] = {"name": "Trần Thị Lan", "email": "", "is_host": False, "speaking": True}
            await s.on_segment_finalized(1.0, 3.0, None, "Em báo cáo tiến độ tuần này", stream="meet7")
            await s.on_segment_finalized(4.0, 6.0, "1", "Anh thấy ổn", stream="mic")
            await s.on_segment_finalized(7.0, 9.0, None, "Tuần sau em làm tiếp", stream="meet7")
            # máy phòng họp nhiều người vào Meet (MEET_DIARIZE=1): hai nhãn Soniox trong một luồng -> tách người
            s.meet_people["meet8"] = {"name": "Phòng họp tầng 3", "email": "", "is_host": False, "speaking": True}
            await s.on_segment_finalized(10.0, 12.0, "1", "Bên phòng họp xin chào", stream="meet8")
            await s.on_segment_finalized(13.0, 15.0, "2", "Tôi ngồi cùng phòng", stream="meet8")
            await s.drain()
            return s
        s = asyncio.run(go())
        labels = [seg["speaker_label"] for seg in s.segments]
        self.assertEqual(labels[0], "Trần Thị Lan")
        self.assertNotEqual(labels[1], "Trần Thị Lan")                          # người trong phòng không thành Lan
        self.assertEqual(labels[2], "Trần Thị Lan")
        self.assertEqual(labels[3], "Phòng họp tầng 3")
        self.assertNotIn(labels[4], ("Phòng họp tầng 3", "Trần Thị Lan", labels[1]))   # người thứ hai trong máy phòng họp
        p = s.speakers.pinned("meet7")
        self.assertEqual((p.origin, p.pin, p.n_segments), ("meet", "meet7", 2))

    def test_echo_of_meet_audio_in_the_room_mic_is_dropped(self):
        async def go():
            mid = db.create_meeting("Họp", owner="an@urbox.vn")
            s = await live.get_session(mid)
            g = s._gates.setdefault("meet7", live.VoiceGate())
            g.active, g.last_voice = True, time.monotonic()                     # người trong Meet đang nói
            s.meet_people["meet7"] = {"name": "Trần Thị Lan", "email": "", "is_host": False, "speaking": True}
            with mock.patch.object(live, "ECHO_HOLD_S", 0.05):
                await s.on_segment_finalized(1.0, 4.0, "1", "Bên em đã chốt phát hành ngày 15 tháng 12", stream="meet7")
                await s.on_segment_finalized(1.5, 4.2, "1", "bên em đã chốt phát hành ngày 15 tháng 12", stream="mic")
                await s.on_segment_finalized(5.0, 7.0, "1", "Vậy mình chốt luôn nhé", stream="mic")
                await s.drain()
            return [seg["text"] for seg in s.segments]
        self.assertEqual(asyncio.run(go()), ["Bên em đã chốt phát hành ngày 15 tháng 12", "Vậy mình chốt luôn nhé"])


class CalendarApiTests(MeetBase):
    def setUp(self):
        super().setUp()
        self.soon = []
        self.patches += [mock.patch.object(meet_bots, "run_soon", lambda: self.soon.append(1)),
                         mock.patch.object(meet_bots, "start", lambda: None)]
        for p in self.patches[-2:]:
            p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()
        self.h = {"Authorization": f"Bearer {auth.issue({'email': 'an@urbox.vn', 'name': 'Nguyễn Văn An'})['token']}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_upcoming_list_switch_skip_and_group(self):
        connect_google("an@urbox.vn", calendar=False)
        self.events["an@urbox.vn"] = [event(series="s-sprint")]
        r = self.client.get("/api/calendar", headers=self.h).json()
        self.assertEqual((r["connected"], r["calendar"], r["events"]), (True, False, []))
        self.assertEqual(self.client.put("/api/calendar/autojoin", json={"enabled": True}, headers=self.h).status_code, 400)
        db._get_db()["google_tokens"].update_one({"email": "an@urbox.vn"},
                                                 {"$push": {"scopes": google_oauth.CALENDAR_SCOPE}})
        st = self.client.get("/api/google/status", headers=self.h).json()
        self.assertEqual((st["drive"], st["calendar"]), (True, True))
        r = self.client.put("/api/calendar/autojoin", json={"enabled": True}, headers=self.h).json()
        self.assertTrue(r["autojoin"] and r["bot_ready"])
        self.assertEqual(user_prefs.get("an@urbox.vn")["name"], "Nguyễn Văn An")
        self.assertEqual(self.soon, [1])
        [ev] = r["events"]
        self.assertEqual((ev["title"], ev["attendees"], ev["skipped"], ev["bot"]), ("Họp sprint thanh toán", 2, False, None))
        gcalendar._CACHE["an@urbox.vn"] = (time.time(), self.events["an@urbox.vn"])
        r = self.client.put("/api/calendar/events/e1/skip", json={"skip": True}, headers=self.h).json()
        self.assertTrue(r["events"][0]["skipped"])
        other = groups.create_group("Nhóm của Bình", "binh@urbox.vn")
        self.assertEqual(self.client.put("/api/calendar/events/e1/group", json={"group_id": other["id"]},
                                         headers=self.h).status_code, 404)              # không phải nhóm của mình
        mine = groups.create_group("Sprint Payment", "an@urbox.vn")
        r = self.client.put("/api/calendar/events/e1/group", json={"group_id": mine["id"]}, headers=self.h).json()
        self.assertEqual(r["events"][0]["group_id"], mine["id"])
        self.assertEqual(user_prefs.get("an@urbox.vn")["series_groups"], {"s-sprint": mine["id"]})

    def test_recall_socket_is_public_but_needs_the_token(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect("/ws/recall/abc/?token=sai") as ws:
                ws.receive_text()
        self.assertEqual(cm.exception.code, 4403)                               # qua được đăng nhập, bị chặn bởi mã


class GoogleCalendarTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        connect_google("an@urbox.vn")
        self.reply = None
        self.tokens = []

        async def access_token(email):
            self.tokens.append(email)
            return f"at{len(self.tokens)}"

        def handler(req: httpx.Request) -> httpx.Response:
            self.assertTrue(req.url.path.endswith("/calendars/primary/events"))
            self.seen = req
            return self.reply(req) if callable(self.reply) else self.reply
        self.patches = [mock.patch.object(google_oauth, "access_token", access_token),
                        mock.patch.object(google_oauth, "_transport", httpx.MockTransport(handler))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_events_are_normalized(self):
        self.reply = httpx.Response(200, json={"items": [
            {"id": "a", "summary": "Họp sprint", "start": {"dateTime": "2026-10-09T14:00:00+07:00"},
             "end": {"dateTime": "2026-10-09T15:00:00+07:00"}, "hangoutLink": "https://meet.google.com/abc-defg-hij",
             "recurringEventId": "s1", "description": "<p>Chương trình:</p><ul><li>Báo cáo</li><li>Kế hoạch &amp; rủi ro</li></ul>",
             "attendees": [{"email": "AN@urbox.vn", "displayName": "An", "self": True, "responseStatus": "accepted"},
                           {"email": "phong-hop-1@resource.calendar.google.com", "resource": True},
                           {"email": "lan@urbox.vn", "responseStatus": "declined"}],
             "organizer": {"email": "an@urbox.vn", "self": True}},
            {"id": "b", "summary": "Gặp đối tác", "start": {"dateTime": "2026-10-09T16:00:00Z"},
             "end": {"dateTime": "2026-10-09T17:00:00Z"},
             "conferenceData": {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+84"},
                                                {"entryPointType": "video", "uri": "https://meet.google.com/xyz-abcd-efg?hs=1"}]},
             "attendees": [{"email": "an@urbox.vn", "self": True, "responseStatus": "declined"}]},
            {"id": "c", "summary": "Zoom", "start": {"dateTime": "2026-10-09T16:00:00Z"}, "end": {"dateTime": "2026-10-09T17:00:00Z"},
             "location": "https://zoom.us/j/1"},
            {"id": "d", "summary": "Nghỉ lễ", "start": {"date": "2026-10-10"}, "end": {"date": "2026-10-11"}},
            {"id": "e", "status": "cancelled", "start": {"dateTime": "2026-10-09T16:00:00Z"}},
            {"id": "f", "eventType": "workingLocation", "start": {"date": "2026-10-10"}, "end": {"date": "2026-10-11"}},
        ]})
        evs = {e["id"]: e for e in asyncio.run(gcalendar.upcoming("an@urbox.vn", now=NOW))}
        self.assertEqual(sorted(evs), ["a", "b", "c", "d"])
        a = evs["a"]
        self.assertEqual((a["meet_url"], a["series"], a["declined"], a["is_organizer"]),
                         ("https://meet.google.com/abc-defg-hij", "s1", False, True))
        self.assertEqual(a["start"], datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual([p["email"] for p in a["attendees"]], ["an@urbox.vn", "lan@urbox.vn"])   # bỏ phòng họp
        self.assertEqual(a["description"], "Chương trình:\n- Báo cáo\n- Kế hoạch & rủi ro")
        self.assertEqual((evs["b"]["meet_url"], evs["b"]["declined"]), ("https://meet.google.com/xyz-abcd-efg", True))
        self.assertEqual(evs["c"]["meet_url"], "")
        self.assertTrue(evs["d"]["all_day"])
        q = dict(self.seen.url.params)
        self.assertEqual((q["singleEvents"], q["orderBy"]), ("true", "startTime"))
        self.assertEqual(q["timeMin"], datetime.fromtimestamp(NOW - 900, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.reply = httpx.Response(500)
        self.assertEqual(len(asyncio.run(gcalendar.upcoming("an@urbox.vn"))), 4)   # nhớ 2 phút: không gọi lại

    def test_missing_permission_or_api_off_and_token_refresh(self):
        self.reply = httpx.Response(403, json={"error": {"message": "Request had insufficient authentication scopes.",
                                                         "status": "PERMISSION_DENIED",
                                                         "errors": [{"reason": "insufficientPermissions"}]}})
        with self.assertRaises(google_oauth.NeedReconnect):
            asyncio.run(gcalendar.upcoming("an@urbox.vn", refresh=True))
        self.reply = httpx.Response(403, json={"error": {"message": "Google Calendar API has not been used in project 1",
                                                         "errors": [{"reason": "accessNotConfigured"}]}})
        with self.assertRaisesRegex(gcalendar.CalendarError, "chưa được bật"):
            asyncio.run(gcalendar.upcoming("an@urbox.vn", refresh=True))
        replies = [httpx.Response(401, json={"error": {"message": "Invalid Credentials"}}),
                   httpx.Response(200, json={"items": []})]
        self.reply = lambda req: replies.pop(0)
        self.tokens.clear()
        self.assertEqual(asyncio.run(gcalendar.upcoming("an@urbox.vn", refresh=True)), [])
        self.assertEqual(len(self.tokens), 2)                                   # token cũ bị từ chối: lấy token mới
        db._get_db()["google_tokens"].update_one({"email": "an@urbox.vn"}, {"$set": {"scopes": [google_oauth.DRIVE_SCOPE]}})
        with self.assertRaises(google_oauth.NeedReconnect):                     # chỉ cho quyền Drive
            asyncio.run(gcalendar.upcoming("an@urbox.vn", refresh=True))


if __name__ == "__main__":
    unittest.main()
