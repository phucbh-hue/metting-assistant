"""Claude mặc định suy nghĩ (Sonnet 5.5, Opus 5.5...): phần suy nghĩ tính vào max_tokens.

Cuộc họp 86 phút #72 (12/10/2026) ra biên bản rỗng 3 lần vì cả 3.500 token đều dành cho suy nghĩ. Test:
- tham số theo model: effort thấp + chừa token cho suy nghĩ; Opus 4.7 giữ nguyên như cũ;
- hết token mà chưa có chữ: gọi lại một lần với suy nghĩ tắt (Sonnet 5.5: between_tools);
- biên bản rỗng không được lưu; bản ghi dài gộp câu liền nhau của cùng một người.
Không gọi API thật.
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.helpers import reset_db
from meeting import artifacts, db


def reply(text, stop="end_turn", out=50, category=None):
    blocks = [SimpleNamespace(type="thinking", thinking="")]
    if text:
        blocks.append(SimpleNamespace(type="text", text=text, citations=None))
    return SimpleNamespace(content=blocks, stop_reason=stop,
                           stop_details=SimpleNamespace(category=category) if category else None,
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=out, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


class FakeClaude:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


class ParamsTests(unittest.TestCase):
    def test_sonnet_55_low_effort_and_room_for_thinking(self):
        p = artifacts.claude_params("claude-sonnet-5-5", 3500)
        self.assertEqual(p, {"max_tokens": 3500 + artifacts.THINK_HEADROOM, "output_config": {"effort": "low"}})
        q = artifacts.claude_params("claude-sonnet-5-5", 3500, quiet=True)
        self.assertEqual(q["thinking"], {"type": "between_tools"})            # "disabled" bị Sonnet 5.5 từ chối (400)

    def test_models_without_default_thinking_unchanged(self):
        for m in ("claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"):
            self.assertEqual(artifacts.claude_params(m, 1500), {"max_tokens": 1500}, m)
            self.assertEqual(artifacts.claude_params(m, 1500, quiet=True), {"max_tokens": 1500}, m)

    def test_models_that_cannot_turn_thinking_off_get_more_room(self):
        for m in ("claude-opus-5-5", "claude-fable-5-1", "claude-fable-5-1[1m]"):
            q = artifacts.claude_params(m, 3000, quiet=True)
            self.assertNotIn("thinking", q, m)
            self.assertEqual(q["max_tokens"], 3000 * 2 + artifacts.THINK_HEADROOM, m)
        self.assertEqual(artifacts.claude_params("claude-sonnet-5", 900, quiet=True)["thinking"], {"type": "disabled"})

    def test_effort_setting_and_streaming_limit(self):
        with mock.patch.dict("os.environ", {"CLAUDE_EFFORT": "medium"}):
            self.assertEqual(artifacts.claude_params("claude-sonnet-5-5", 100)["output_config"], {"effort": "medium"})
        with mock.patch.dict("os.environ", {"CLAUDE_EFFORT": "max"}):        # xhigh/max không tắt được suy nghĩ: bỏ qua
            self.assertEqual(artifacts.claude_params("claude-sonnet-5-5", 100)["output_config"], {"effort": "low"})
        self.assertLessEqual(artifacts.claude_params("claude-sonnet-5-5", 50000)["max_tokens"],
                             artifacts.MAX_NONSTREAM_TOKENS)


class ClaudeTextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Họp dài")
        artifacts.set_meeting(self.mid, "biên bản")

    def patched(self, fake):
        return (mock.patch.object(artifacts, "_anthropic", lambda: fake),
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x", "GEMINI_API_KEY": ""}),
                mock.patch.object(artifacts, "PROVIDER", "claude"),
                mock.patch.object(artifacts, "CLAUDE_MODEL", "claude-sonnet-5-5"))

    async def test_all_thinking_no_text_retries_with_thinking_off(self):
        fake = FakeClaude([reply("", stop="max_tokens", out=9500), reply("# BIÊN BẢN CUỘC HỌP: Họp dài")])
        p1, p2, p3, p4 = self.patched(fake)
        with p1, p2, p3, p4:
            text = await artifacts._call_llm("sys", "transcript", max_tokens=3500)
        self.assertEqual(text, "# BIÊN BẢN CUỘC HỌP: Họp dài")
        self.assertNotIn("thinking", fake.calls[0])
        self.assertEqual(fake.calls[1]["thinking"], {"type": "between_tools"})
        rows = list(db._get_db()["llm_usage"].find({"meeting_id": self.mid}, {"_id": 0}))
        self.assertEqual([r["ok"] for r in rows], [False, True])                # lần chỉ toàn suy nghĩ ghi là lỗi

    async def test_still_empty_raises_instead_of_returning_blank(self):
        fake = FakeClaude([reply("", stop="max_tokens"), reply("", stop="max_tokens")])
        p1, p2, p3, p4 = self.patched(fake)
        with p1, p2, p3, p4, self.assertRaises(RuntimeError):
            await artifacts._call_llm("sys", "transcript")
        self.assertEqual(len(fake.calls), 2)

    async def test_refusal_is_an_error(self):
        fake = FakeClaude([reply("", stop="refusal", category="general_harms")])
        p1, p2, p3, p4 = self.patched(fake)
        with p1, p2, p3, p4, self.assertRaises(RuntimeError) as cm:
            await artifacts._call_llm("sys", "x")
        self.assertIn("general_harms", str(cm.exception))

    async def test_truncated_text_is_kept(self):
        fake = FakeClaude([reply("Biên bản bị cắt giữa chừng", stop="max_tokens")])
        p1, p2, p3, p4 = self.patched(fake)
        with p1, p2, p3, p4:
            self.assertEqual(await artifacts._call_llm("sys", "x"), "Biên bản bị cắt giữa chừng")
        self.assertEqual(len(fake.calls), 1)


class MinutesTests(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_empty_minutes_not_saved(self):
        mid = db.create_meeting("Họp")

        async def blank(system, prompt, max_tokens=4000):
            return "  \n"
        with mock.patch.object(artifacts, "_call_llm", blank), self.assertRaises(RuntimeError):
            asyncio.run(artifacts.generate_meeting_minutes(mid, [{"speaker_label": "An", "text": "Chốt ngày 15/12"}]))
        self.assertEqual(db.get_artifacts(mid), [])

    def test_compact_transcript_merges_runs_of_one_speaker(self):
        segs = [{"speaker_label": "An", "text": "Chào mọi người."}, {"speaker_label": "An", "text": "Bắt đầu nhé."},
                {"speaker_label": "Bình", "text": "Ok."}, {"speaker_label": "Bình", "text": " "},
                {"speaker_label": "An", "text": "Chốt 15/12."}]
        self.assertEqual(artifacts.compact_transcript(segs),
                         "An: Chào mọi người. Bắt đầu nhé.\nBình: Ok.\nAn: Chốt 15/12.")


if __name__ == "__main__":
    unittest.main()
