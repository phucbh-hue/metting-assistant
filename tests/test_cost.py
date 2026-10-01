"""Test tối ưu chi phí gọi AI.

- Prompt cache của trợ lý: phần đầu (hướng dẫn, transcript, kết quả tra cứu cũ) giữ nguyên từng byte giữa các vòng và
  các lần hỏi, có điểm cache đúng chỗ, gửi dạng khối cho Claude.
- Tính tiền đúng: ghi cache 1,25 lần, đọc cache 0,1 lần (Opus 5.5: 0,05 lần), web_search của Claude 0,01 USD / lượt.
- Đoán tên người nói chỉ chạy khi có tên mới được nhắc; câu gọi dở dang không gửi cho AI.
- Lệnh thuyết trình nhận đúng các cách nói thật trong cuộc họp #40.
Không gọi API thật.
"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.helpers import reset_db
from tests.test_meeting_session import SessionTestCase
from meeting import artifacts, db, identity, live, llm, mcp


def segs(n, prefix="Câu"):
    return [{"seq": i, "speaker_label": f"Người nói {i % 2 + 1}", "text": f"{prefix} số {i} về tiến độ dự án."}
            for i in range(n)]


class FakeClient:
    """Thay anthropic.AsyncAnthropic: ghi lại tham số gọi, trả lời theo kịch bản."""

    def __init__(self, replies, usage=None):
        self.replies, self.calls = list(replies), []
        self.usage = usage or {}
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        text = self.replies.pop(0)
        fields = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "server_tool_use": None}
        fields.update(self.usage)
        u = SimpleNamespace(**fields)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text, citations=None)], usage=u)


def cache_marks(blocks):
    return [i for i, b in enumerate(blocks) if isinstance(b, dict) and "cache_control" in b]


class TranscriptBlockTests(unittest.TestCase):
    def test_fixed_chunks_and_cache_on_last_full_chunk(self):
        b = llm.transcript_blocks(segs(75))                       # 30 + 30 + 15 câu
        self.assertEqual(len(b), 3)
        self.assertEqual(cache_marks(b), [1])
        self.assertTrue(b[0]["text"].startswith("## Nội dung cuộc họp (toàn bộ"))
        more = llm.transcript_blocks(segs(75) + [{"speaker_label": "A", "text": "câu mới"}])
        self.assertEqual([x["text"] for x in more[:2]], [x["text"] for x in b[:2]])   # câu mới không đổi phần đầu
        self.assertEqual(cache_marks(llm.transcript_blocks(segs(10))), [])           # chưa đủ 30 câu: không đánh dấu

    def test_long_meeting_drops_whole_chunks_so_prefix_stays_stable(self):
        long = [{"speaker_label": "A", "text": "x" * 300} for _ in range(120)]
        b = llm.transcript_blocks(long, max_chars=24000)
        self.assertIn("phần đầu đã lược bớt", b[0]["text"])
        self.assertLessEqual(sum(len(x["text"]) for x in b), 24000 + 200)
        b2 = llm.transcript_blocks(long + [{"speaker_label": "A", "text": "y"}], max_chars=24000)
        self.assertEqual(b2[0]["text"], b[0]["text"])

    def test_blocks_string_behaves_like_text(self):
        p = llm._agent_request("tóm tắt", llm.transcript_blocks(segs(3)), {}, [], None, False)
        self.assertIsInstance(p, str)
        self.assertIn("## Dữ liệu đã tra cứu:\n(chưa tra cứu)", p)
        self.assertIn('## Yêu cầu:\n"tóm tắt"', p)
        self.assertEqual(p.blocks[-1]["text"], '## Yêu cầu:\n"tóm tắt"')


class AgentCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        mcp.seed_mock_data()
        self.mid = db.create_meeting("Review")

    async def test_rounds_share_prefix_and_claude_gets_cache_markers(self):
        final = json.dumps({"chat_response": "Có 6 ticket, URBOX-102 quá hạn.", "artifact_needed": None}, ensure_ascii=False)
        fake = FakeClient(['TOOL_CALL: {"tool": "query_jira_issues", "arguments": {}}', final],
                          usage={"cache_read_input_tokens": 0})
        with mock.patch.object(artifacts, "_anthropic", lambda: fake), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x", "GEMINI_API_KEY": ""}), \
                mock.patch.object(artifacts, "PROVIDER", "claude"):
            res = await llm.think_and_act(self.mid, "tình hình ticket thế nào", segs(35))
        self.assertEqual(res["chat_response"], "Có 6 ticket, URBOX-102 quá hạn.")
        self.assertEqual(len(fake.calls), 2)
        s0, s1 = fake.calls[0]["system"], fake.calls[1]["system"]
        self.assertEqual(s0, s1)                                                  # hướng dẫn giống hệt
        self.assertEqual(s0[0]["cache_control"], {"type": "ephemeral"})
        c0, c1 = fake.calls[0]["messages"][0]["content"], fake.calls[1]["messages"][0]["content"]
        self.assertIsInstance(c0, list)
        # vòng 2 = vòng 1 bỏ 2 khối cuối (chưa tra cứu / yêu cầu) + kết quả tra cứu + yêu cầu mới
        self.assertEqual(c1[:len(c0) - 2], c0[:len(c0) - 2])
        self.assertEqual(cache_marks(c0), [0])                                   # khối 30 câu đầy đủ (35 câu = 30 + 5)
        self.assertEqual(cache_marks(c1), [0, len(c1) - 2])                      # + kết quả tra cứu cuối cùng
        self.assertLessEqual(1 + len(cache_marks(c1)), 4)                        # tối đa 4 điểm cache mỗi lần gọi
        self.assertIn("Dữ liệu đã tra cứu (1)", c1[-2]["text"])

    async def test_usage_records_cache_tokens_and_cost(self):
        fake = FakeClient(["xin chào"], usage={"cache_read_input_tokens": 9000, "cache_creation_input_tokens": 1000})
        with mock.patch.object(artifacts, "_anthropic", lambda: fake), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x", "GEMINI_API_KEY": ""}), \
                mock.patch.object(artifacts, "PROVIDER", "claude"), mock.patch.object(artifacts, "CLAUDE_MODEL", "claude-opus-4-7"):
            artifacts.set_meeting(self.mid, "trợ lý")
            await artifacts._call_llm(artifacts.Blocks([artifacts.text_block("hệ thống", cache=True)]), "hỏi")
        row = db._get_db()["llm_usage"].find_one({}, {"_id": 0})
        self.assertEqual((row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]), (10100, 9000, 1000))
        self.assertAlmostEqual(row["cost_usd"], (100 * 5 + 1000 * 5 * 1.25 + 9000 * 5 * 0.1 + 20 * 25) / 1e6, places=7)
        self.assertEqual(db.usage_summary()["total"]["cache_read_tokens"], 9000)
        sent = fake.calls[0]
        self.assertEqual(sent["system"], [{"type": "text", "text": "hệ thống", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(sent["messages"][0]["content"], "hỏi")                  # chuỗi thường giữ nguyên


class PricingTests(unittest.TestCase):
    def test_cache_multipliers_from_pricing_page(self):
        self.assertAlmostEqual(artifacts._cost_usd("claude-opus-4-7", 1000, 100, cache_read=10000, cache_write=2000),
                               (1000 * 5 + 2000 * 5 * 1.25 + 10000 * 5 * 0.1 + 100 * 25) / 1e6)
        self.assertAlmostEqual(artifacts._cost_usd("claude-opus-5-5", 0, 0, cache_read=1_000_000), 0.20)
        self.assertAlmostEqual(artifacts._cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000), 12.0)
        self.assertEqual(artifacts._cost_usd("model-la", 1000, 1000), 0.0)


class ClaudeSearchCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_search_requests_are_billed(self):
        reset_db()
        mid = db.create_meeting("Họp")
        fake = FakeClient(["# Tra cứu: x\n\nKết quả"],
                          usage={"server_tool_use": SimpleNamespace(web_search_requests=3)})
        with mock.patch.object(artifacts, "_anthropic", lambda: fake), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x"}), mock.patch.object(artifacts, "CLAUDE_MODEL", "claude-opus-4-7"):
            artifacts.set_meeting(mid, "tra cứu web")
            await artifacts._claude_search("giá vàng")
        row = db._get_db()["llm_usage"].find_one({}, {"_id": 0})
        self.assertAlmostEqual(row["cost_usd"], (100 * 5 + 20 * 25) / 1e6 + 0.03, places=7)


class IdentityTriggerTests(SessionTestCase):
    async def test_fresh_names_skip_assistant_known_and_sentence_start(self):
        llm.set_assistant_config("Bông", ["Bong"])
        mid = db.create_meeting("Họp")
        s = await live.get_session(mid)
        e = s.identity
        self.assertEqual(e.fresh_names("Anh Tuấn ơi, xong chưa? Hôm nay bàn Jira với UrBox."), {"tuấn"})
        self.assertEqual(e.fresh_names("Ok Bông ơi, mở slide giúp anh."), set())               # tên trợ lý
        self.assertEqual(e.fresh_names("cảm ơn chị Lan Anh nhé"), {"lan"})
        self.assertEqual(e.fresh_names("Xin tự giới thiệu, tôi là Bùi Hồng Phúc."), {"bùi hồng phúc"})
        self.assertEqual(e.fresh_names("Trời ơi, dự án EduStation chạy trên Redis"), set())      # không phải tên người
        await self.feed(s, [(0, "1", 3.0, "Xin chào mọi người"), (1, "2", 3.0, "Chào anh")])
        prof = s.speakers.visible_profiles()[0]
        prof.name = "Phúc"
        self.assertEqual(e.fresh_names("anh Phúc nói đúng"), set())                          # người đã biết tên

    async def test_repeated_name_stops_counting_and_fallback_is_sparse(self):
        mid = db.create_meeting("Podcast")
        s = await live.get_session(mid)
        calls = []

        async def _call(system, prompt, max_tokens=4000):
            calls.append(prompt)
            return json.dumps({"predictions": []})
        with mock.patch.object(artifacts, "llm_available", lambda: True), mock.patch.object(artifacts, "_call_llm", _call), \
                mock.patch("meeting.identity.MIN_INTERVAL_S", 0.0):
            script = [(i % 2, "1" if i % 2 == 0 else "2", 2.0, "thầy Minh nói tiếp đi ạ") for i in range(8)]
            for row in script:
                await self.feed(s, [row])
                await self.settle(s, lambda: not s.identity._running and s.identity._timer is None, timeout=2.0)
        # "Minh" chỉ là thông tin mới ở 3 lần nhắc đầu; sau đó không gọi lại (dự phòng 40 câu chưa tới)
        self.assertLessEqual(len(calls), 3)
        self.assertGreaterEqual(len(calls), 1)


class FragmentCallTests(SessionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        llm.set_assistant_config("Bông", ["Bong"])
        self.calls = []

        async def fake_activation(command, full_sentence, name=""):
            self.calls.append((name, command))
        self.mid = db.create_meeting("Họp test")
        self.s = await live.get_session(self.mid)
        self.s._handle_ai_activation = fake_activation

    async def test_fragment_waits_for_the_real_request(self):
        """Lỗi thật (#40): "em có thể" bị gửi cho AI thành một lượt gọi riêng."""
        with mock.patch.object(live, "CMD_SETTLE_S", 0.05):
            await self.feed(self.s, [(0, "1", 1.5, "Bông ơi, em có thể")])
            await asyncio.sleep(0.4)
            self.assertEqual(self.calls, [])
            await self.feed(self.s, [(0, "1", 3.0, "tóm tắt giúp anh các quyết định.")], t0=4)
            await asyncio.sleep(0)
        self.assertEqual(self.calls, [("Bông", "em có thể tóm tắt giúp anh các quyết định.")])

    def test_is_fragment(self):
        self.assertTrue(live._is_fragment("em có thể"))
        self.assertTrue(live._is_fragment("em hãy giúp"))
        self.assertFalse(live._is_fragment("tóm tắt cuộc họp"))
        self.assertFalse(live._is_fragment("em có thể tóm tắt giúp anh cuộc họp được không"))


class PresentIntentTests(unittest.TestCase):
    def test_real_phrasings_from_meeting_40(self):
        for t in ("em trình bày cái nội dung trong slide đi.",
                  "không phải anh muốn em kéo slide rồi trình bày luôn nội dung ở trong cái slide đó",
                  "đọc nội dung slide giúp anh", "resend cho anh xem nhé", "em hãy present về cái slide em đã nói đi"):
            self.assertEqual((llm.stage_intent(t) or {}).get("action"), "present", t)
        for t in ("làm slide thuyết trình về kế hoạch Q4", "em tạo một bộ slide trình bày về sprint"):
            self.assertNotEqual((llm.stage_intent(t) or {}).get("action"), "present", t)


if __name__ == "__main__":
    unittest.main()


DECK_A = {"title": "Phim Duy Leo", "slides": [{"title": "Tổng quan", "layout": "title", "bullets": ["Review phim"]},
                                              {"title": "Điểm cộng", "layout": "bullets", "bullets": ["Đầu tư bối cảnh lớn"]}]}
DECK_B = {"title": "Kế hoạch Q4", "slides": [{"title": "Mục tiêu", "layout": "bullets", "bullets": ["Doanh số"]}]}


class NavigationWithoutAITests(unittest.IsolatedAsyncioTestCase):
    """Các câu điều hướng thật ở #40 từng bị gửi cho AI và soạn lại slide mới; giờ xử lý tại chỗ."""

    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Họp")
        self.a1 = db.save_artifact(self.mid, "slides", "Slide: Phim Duy Leo", json.dumps(DECK_A, ensure_ascii=False))
        self.a2 = db.save_artifact(self.mid, "slides", "Slide: Phim Duy Leo", json.dumps(DECK_A, ensure_ascii=False))
        self.b = db.save_artifact(self.mid, "slides", "Slide: Kế hoạch Q4", json.dumps(DECK_B, ensure_ascii=False))
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

    async def run_cmd(self, text):
        async def no_ai(*a, **k):
            raise AssertionError("không được gọi AI")
        with mock.patch.object(artifacts, "_call_llm", no_ai), mock.patch.object(artifacts, "llm_available", lambda: True):
            await self.s._handle_ai_activation(text, text, "Thanh")

    async def test_latest_version_topic_and_back(self):
        await self.run_cmd("chuyển lại cái slide gần nhất mà em đã tạo.")
        self.assertEqual(self.s.stage["artifact_id"], self.b)                     # bộ mới tạo gần nhất
        await self.run_cmd("em quay lại cái slide điểm cộng.")
        self.assertEqual((self.s.stage["artifact_id"], self.s.stage["slide"]), (self.a2, 1))   # tìm cả bộ khác
        await self.run_cmd("em quay lại cái slide v1 giúp anh nha.")
        self.assertEqual(self.s.stage["artifact_id"], self.a1)                    # bản v1 của bộ đang xem
        await self.run_cmd("em quay lại cái slide v5 giúp anh nha.")
        self.assertIn("chỉ có bản v1, v2", self.says()[-1])
        await self.run_cmd("em quay lại cái slide mà em trình bày hồi nãy coi.")
        self.assertEqual(self.s.stage["artifact_id"], self.a2)                    # lịch sử trình bày
        await self.run_cmd("mở slide về chuyện hoàn toàn khác")
        self.assertIn("chưa thấy slide nào", self.says()[-1])

    async def test_cancel_stops_running_request(self):
        async def slow():
            await asyncio.sleep(30)
        job = asyncio.create_task(slow())
        self.s._ai_tasks.add(job)
        await self.run_cmd("thôi em đừng có soạn slide nữa.")
        await asyncio.sleep(0)
        self.assertTrue(job.cancelled())
        ev = []
        while not self.q.empty():
            ev.append(self.q.get_nowait())
        self.assertIn(("ai_cancelled", 1), [(e["type"], e.get("count")) for e in ev if e["type"] == "ai_cancelled"])
        self.assertIn("Dạ, em dừng lại ạ.", [e["text"] for e in ev if e["type"] == "ai_say"])
