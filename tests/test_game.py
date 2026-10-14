"""Trò chơi có thưởng (14/10/2026): trợ lý thiết kế và dẫn; trả lời bằng giọng nói (nhận ra người nói) hoặc trên điện
thoại; Đấu trường tri thức (điểm + tốc độ) và Rung chuông vàng (loại trực tiếp). AI là bản giả, thời gian chờ rút ngắn."""
import asyncio
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import artifacts, auth, db, game, live, llm

GAME = {"title": "Ai hiểu UrBox nhất?", "format": "classic", "theme": "Quà tặng và voucher", "intro": "Chào cả nhà!",
        "rules": ["Nói đáp án to rõ", "Đúng và nhanh được nhiều điểm"], "prize": "Phần quà bí mật từ UrBox",
        "questions": [{"q": "E-voucher là gì?", "options": ["Phiếu quà điện tử", "Thẻ ATM", "Vé xe", "Tem thư"], "answer": 0,
                       "explain": "E-voucher là phiếu quà tặng điện tử.", "type": "choice"},
                      {"q": "Voucher giấy luôn tốt hơn e-voucher?", "options": ["Đúng", "Sai"], "answer": 1,
                       "explain": "E-voucher gửi ngay, không lo thất lạc.", "type": "truefalse"}],
        "outro": "Chúc mừng!"}


class FakeSpeakers:
    def profile(self, sid):
        return SimpleNamespace(label={1: "Phúc", 2: "Lan"}.get(sid, f"Người nói {sid}"))


class FakeSession:
    def __init__(self):
        self.events, self.said, self.speakers = [], [], FakeSpeakers()

    async def emit(self, ev):
        self.events.append(ev)

    async def _say(self, text, mood="happy", quiet=False):
        self.said.append(text)


def fast():
    return [mock.patch.object(game, "ANSWER_S", {"choice": 0.6, "truefalse": 0.6}), mock.patch.object(game, "GRACE_S", 0.05),
            mock.patch.object(game, "REVEAL_S", 0.0), mock.patch.object(game, "speech_s", lambda text: 0.0)]


class ParseTests(unittest.TestCase):
    def test_normalize_drops_broken_questions(self):
        g = game.normalize({**GAME, "format": "lạ", "questions": GAME["questions"] + [
            {"q": "Thiếu phương án", "options": ["Một"], "answer": 0}, {"q": "Đáp án sai chỗ", "options": ["A", "B"], "answer": 5},
            {"q": "  ", "options": ["A", "B"], "answer": 0}]})
        self.assertEqual((len(g["questions"]), g["format"], g["questions"][1]["type"]), (2, "classic", "truefalse"))
        with self.assertRaises(ValueError):
            game.normalize({"title": "x", "questions": []})

    def test_voice_answers_like_people_really_say_them(self):
        cases = {"Đáp án B": 1, "B": 1, "bê ạ": 1, "câu C nhé": 2, "em chọn đê": 3, "chọn số 3": 2, "A": 0, "a.": 0,
                 "à": None, "ạ": None, "dạ vâng": None, "câu này khó quá mọi người ơi chắc là a hay b gì đó": None,
                 "đáp án e": None}
        for text, want in cases.items():
            self.assertEqual(game.parse_answer(text, 4), want, text)
        self.assertEqual(game.parse_answer("Sai rồi", 2), 1)
        self.assertEqual(game.parse_answer("đúng", 2), 0)
        self.assertEqual(game.parse_answer("đáp án C", 2), None)                # câu Đúng/Sai chỉ có A, B
        self.assertEqual(game.parse_answer("Jarvis ơi đáp án B", 4), 1)

    def test_commands(self):
        for text, want in {"bắt đầu đi": "start", "câu tiếp theo": "next", "dừng trò chơi": "stop", "xem bảng điểm": "scores",
                           "kết thúc game": "stop", "hôm nay trời đẹp": None}.items():
            self.assertEqual(game.command_of(text), want, text)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.patches = fast()
        for p in self.patches:
            p.start()
        self.mid = db.create_meeting("Demo")
        self.aid = db.save_artifact(self.mid, "game", "Trò chơi: Ai hiểu UrBox nhất?", json.dumps(GAME, ensure_ascii=False))

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        game.GAMES.clear()

    async def wait_phase(self, run, phase, timeout=5.0):
        end = time.monotonic() + timeout
        while run.phase != phase and time.monotonic() < end:
            await asyncio.sleep(0.01)
        self.assertEqual(run.phase, phase)

    async def test_classic_round_voice_and_phone_players_share_one_scoreboard(self):
        s = FakeSession()
        run = game.GameRun(s, self.aid, game.normalize(GAME))
        run.task = asyncio.create_task(run.run())
        for _ in range(100):
            if s.said:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(run.phase, "lobby")
        self.assertIn("Phần thưởng là Phần quà bí mật từ UrBox", s.said[0])
        self.assertLess(len(s.said[0].split()), 70)                             # mở màn ngắn: luật chơi đã có trên màn hình
        self.assertNotIn("?.", s.said[0])
        self.assertIs(game.by_code(run.code), run)
        pid = run.join("Hoà - BD")
        run.start()
        await self.wait_phase(run, "answering")
        run.on_segment({"text": "dạ vâng", "speaker_key": 2, "speaker_label": "Lan"})        # không phải câu trả lời
        run.on_segment({"text": "Đáp án A", "speaker_key": 1, "speaker_label": "Phúc"})      # đúng, nhanh nhất
        await asyncio.sleep(0.05)
        self.assertTrue(run.answer_phone(pid, 0))                                             # đúng, chậm hơn
        run.on_segment({"text": "B", "speaker_key": 2, "speaker_label": "Lan"})               # sai
        self.assertFalse(run.answer_phone(pid, 1))                                            # đã chốt thì không đổi
        self.assertEqual(run.public()["n_answered"], 3)
        self.assertNotIn("reveal", run.public())                                              # chưa lộ đáp án
        await self.wait_phase(run, "reveal")
        rv = run.public()["reveal"]
        self.assertEqual((rv["answer"], rv["counts"], rv["fastest"]), (0, [2, 1, 0, 0], "Phúc"))
        self.assertIn("Chúc mừng Phúc, Hoà - BD!", s.said[-1])
        board = run.leaderboard()
        self.assertEqual([r["name"] for r in board[:2]], ["Phúc", "Hoà - BD"])
        self.assertGreater(board[0]["points"], board[1]["points"])                           # nhanh hơn được thêm điểm
        self.assertGreaterEqual(board[1]["points"], 100)
        me = run.public(pid)["me"]
        self.assertEqual((me["joined"], me["rank"], me["choice"]), (True, 2, 0))
        await asyncio.wait_for(run.task, 10)
        self.assertEqual(run.phase, "final")
        self.assertEqual(run.winners[0]["name"], "Phúc")
        ended = game.by_code(run.code)                                                       # hết lượt: chỉ còn xem kết quả
        self.assertIsInstance(ended, game.EndedGame)
        self.assertEqual((ended.public()["ended"], ended.public(pid)["me"]["rank"]), (True, 2))
        with self.assertRaises(ValueError):
            ended.join("Đến muộn")
        meta = db.get_artifact(self.aid)["meta"]["results"]
        self.assertEqual((meta["players"], meta["winners"][0]["name"]), (3, "Phúc"))

    async def test_golden_bell_eliminates_and_revives_when_everyone_is_wrong(self):
        g = game.normalize({**GAME, "format": "golden_bell"})
        s = FakeSession()
        run = game.GameRun(s, self.aid, g)
        a, b = run.join("An"), run.join("Bình")
        run.task = asyncio.create_task(run.run())
        await self.wait_phase(run, "lobby")
        run.start()
        await self.wait_phase(run, "answering")
        run.answer_phone(a, 1)
        run.answer_phone(b, 2)                                                                # cả hai cùng sai câu 1
        await self.wait_phase(run, "reveal")
        self.assertTrue(run.public()["reveal"]["revived"])
        self.assertEqual(run.public()["alive"], 2)
        await self.wait_phase(run, "answering")
        run.answer_phone(a, 1)                                                                # An đúng câu 2, Bình không trả lời
        await asyncio.wait_for(run.task, 10)
        self.assertEqual([w["name"] for w in run.winners], ["An"])
        self.assertFalse(run.players[b]["alive"])


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.patches = fast()
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()
        game.GAMES.clear()

    async def test_assistant_designs_the_game_then_hosts_it_on_stage(self):
        mid = db.create_meeting("Demo Meeting Copilot")
        s = await live.get_session(mid)
        q = await s.subscribe()
        seen = []

        async def fake(system, prompt, max_tokens=4000):
            seen.append(str(system))
            if "thiết kế và dẫn trò chơi" in str(system):
                return json.dumps(GAME, ensure_ascii=False)
            return json.dumps({"chat_response": "Dạ!", "artifact_needed": "game",
                               "artifact_prompt": "Trò chơi có thưởng về quà tặng"}, ensure_ascii=False)
        with mock.patch.object(artifacts, "_call_llm", fake):
            await s._handle_ai_activation("em có thể tạo ra một trò chơi có thưởng để mọi người tương tác với em",
                                          "Jarvis ơi em có thể tạo ra một trò chơi có thưởng để mọi người tương tác với em")
        self.assertIsNotNone(s.game)
        self.assertEqual(s.stage["artifact_id"], s.game.artifact_id)
        self.assertEqual(db.get_artifact(s.game.artifact_id)["kind"], "game")
        await asyncio.sleep(0.05)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        self.assertIn("game_state", [e["type"] for e in events])
        self.assertTrue(any(e.get("type") == "stage_command" and e.get("action") == "open" for e in events))
        self.assertEqual(s.snapshot()["game"]["phase"], "lobby")
        await s._handle_ai_activation("bắt đầu đi", "Jarvis ơi bắt đầu đi")                  # lệnh gọi kèm tên trợ lý
        for _ in range(200):
            if s.game.phase in ("reading", "answering"):
                break
            await asyncio.sleep(0.01)
        self.assertIn(s.game.phase, ("reading", "answering"))
        n = len(seen)
        await s._handle_ai_activation("đáp án A", "Jarvis ơi đáp án A")                       # đang chơi: không gọi AI
        self.assertEqual(len(seen), n)
        await s.game_command("stop")
        self.assertFalse(s.game.active)


class PlayApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.p = mock.patch.multiple(auth, ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS=set(),
                                     CLIENT_ID="x.apps.googleusercontent.com", TTL_S=3600)
        self.p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.p.stop()
        game.GAMES.clear()

    def test_phone_players_need_no_login_only_the_code(self):
        run = game.GameRun(FakeSession(), 1, game.normalize(GAME))
        game.GAMES[run.code] = run
        self.assertEqual(self.client.get("/play/sai-ma/state").status_code, 404)
        page = self.client.get(f"/play/{run.code}")
        self.assertEqual(page.status_code, 200)                                              # không cần token đăng nhập
        self.assertIn("Vào chơi", page.text)
        pid = self.client.post(f"/play/{run.code}/join", json={"name": "Lan"}).json()["pid"]
        st = self.client.get(f"/play/{run.code}/state?pid={pid}").json()
        self.assertEqual((st["phase"], st["me"]["name"], st["players"]), ("lobby", "Lan", 1))
        self.assertEqual(self.client.post(f"/play/{run.code}/answer", json={"pid": pid, "choice": 0}).status_code, 409)
        run.qi, run.phase, run.opened_at, run.deadline = 0, "answering", time.time(), time.time() + 20
        self.assertEqual(self.client.post(f"/play/{run.code}/answer", json={"pid": pid, "choice": 0}).status_code, 200)
        self.assertEqual(self.client.post(f"/play/{run.code}/answer", json={"pid": pid, "choice": 9}).status_code, 422)
        self.assertNotIn("answer", json.dumps(self.client.get(f"/play/{run.code}/state").json()["question"]))

    def test_phone_still_shows_the_result_after_the_game_ends(self):
        run = game.GameRun(FakeSession(), 1, game.normalize(GAME))
        game.GAMES[run.code] = run
        pid = self.client.post(f"/play/{run.code}/join", json={"name": "Lan"}).json()["pid"]
        self.assertFalse(self.client.get(f"/play/{run.code}/state").json()["ended"])
        run.phase = "final"
        run._close()                                                                         # trò chơi khép lại
        st = self.client.get(f"/play/{run.code}/state?pid={pid}").json()
        self.assertEqual((st["phase"], st["ended"], st["me"]["name"], st["me"]["rank"]), ("final", True, "Lan", 1))
        self.assertFalse(self.client.get(f"/play/{run.code}/state?pid=pkhac").json()["me"]["joined"])
        late = self.client.post(f"/play/{run.code}/join", json={"name": "Đến muộn"})
        self.assertEqual((late.status_code, late.json()["detail"]), (400, "Trò chơi đã kết thúc"))
        self.assertEqual(self.client.post(f"/play/{run.code}/answer", json={"pid": pid, "choice": 0}).status_code, 409)
        game.GAMES[run.code].ended_at -= game.KEEP_ENDED_S + 1                               # quá 30 phút: mã đóng hẳn
        self.assertEqual(self.client.get(f"/play/{run.code}/state").status_code, 404)
        self.assertNotIn(run.code, game.GAMES)


if __name__ == "__main__":
    unittest.main()
