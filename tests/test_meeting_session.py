"""Test MeetingSession: xử lý câu, lưu DB, đổi tên bền vững, lưu giọng, AI đoán tên, kết thúc họp."""
import asyncio
import json
import unittest
from unittest import mock

from tests.helpers import VoiceBank, reset_db
from meeting import artifacts, db, live


class SessionTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.bank = VoiceBank(seed=40)

    async def asyncTearDown(self):
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()

    async def feed(self, s, script, epoch=0, t0=0.0):
        """script: [(speaker_idx, raw, voiced_s) hoặc (speaker_idx, raw, voiced_s, text)]"""
        t, seqs = t0, []
        for row in script:
            k, raw, voiced = row[:3]
            text = row[3] if len(row) > 3 else f"câu của người {k + 1}"
            v = self.bank.vec(k, voiced) if voiced >= 1.0 else None
            seqs.append(await s.on_segment_finalized(t_start=round(t, 2), t_end=round(t + voiced * 1.3, 2),
                                                     raw_speaker=raw, text=text, vector=v, voiced=voiced,
                                                     epoch=epoch))
            t += voiced * 1.3 + 0.5
        await s.drain()
        return seqs

    @staticmethod
    def events(q):
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    async def settle(self, s, cond, timeout=3.0):
        for _ in range(int(timeout / 0.02)):
            await s.drain()
            if cond():
                return True
            await asyncio.sleep(0.02)
        return cond()


class SegmentFlowTests(SessionTestCase):
    async def test_segments_and_speakers_are_persisted(self):
        mid = db.create_meeting("Họp test")
        s = await live.get_session(mid)
        q = await s.subscribe()
        await self.feed(s, [(0, "1", 3.0), (1, "2", 3.0), (0, "1", 0.3)])
        rows = db.get_segments(mid)
        self.assertEqual([r["speaker_label"] for r in rows], ["Người nói 1", "Người nói 2", "Người nói 1"])
        self.assertEqual([r["speaker_key"] for r in rows], [1, 2, 1])
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3])
        self.assertEqual([r["raw_speaker"] for r in rows], ["1", "2", "1"])
        self.assertEqual(sorted(p["sid"] for p in db.list_speakers(mid)), [1, 2])
        types = [e["type"] for e in self.events(q)]
        self.assertEqual(types.count("segment"), 3)
        self.assertIn("speakers", types)

    async def test_rename_persists_across_server_restart(self):
        mid = db.create_meeting("Họp test")
        s = await live.get_session(mid)
        await self.feed(s, [(0, "1", 3.0), (1, "2", 3.0), (1, "2", 1.2)])
        res = await s.rename_speaker(2, "Hương", role="Khách mời")
        self.assertEqual(res["updated_segments"], 2)
        await s.drain()
        self.assertEqual([r["speaker_label"] for r in db.get_segments(mid)], ["Người nói 1", "Hương", "Hương"])

        # Server khởi động lại: session mới nạp từ DB
        s.dispose()
        live.SESSIONS.clear()
        s2 = await live.get_session(mid)
        self.assertEqual([p["label"] for p in s2.public_speakers()], ["Người nói 1", "Hương"])
        seqs = await self.feed(s2, [(1, "1", 5.0), (0, "2", 5.0)], epoch=s2.next_epoch(), t0=60)
        self.assertEqual(seqs[0], 4)  # seq tiếp nối, không trùng câu cũ
        self.assertEqual([s2._by_seq[x]["speaker_label"] for x in seqs], ["Hương", "Người nói 1"])

    async def test_legacy_meeting_from_v25_is_upgraded_and_editable(self):
        """Cuộc họp tạo từ bản 2.5: không có seq/speaker_key, nhãn 'Người lạ #1' và tên thật."""
        mid = db.create_meeting("Họp cũ")
        col = db._get_db()["meeting_segments"]
        for i, (lbl, text) in enumerate([("Nam", "Dạ em chào chị"), ("Người lạ #1", "Chào em"),
                                         ("Nam", "Em khỏe ạ"), ("Người lạ #1", "Ừ")], 1):
            col.insert_one({"id": 100 + i, "meeting_id": mid, "t_start": i * 3.0, "t_end": i * 3.0 + 2,
                            "speaker_label": lbl, "speaker_id": 15 if lbl == "Nam" else None, "text": text,
                            "raw_embedding": self.bank.vec(0 if lbl == "Nam" else 1, 3.0).tolist()})
        s = await live.get_session(mid)
        await s.drain()
        rows = db.get_segments(mid)
        self.assertEqual([r["seq"] for r in rows], [101, 102, 103, 104])
        self.assertEqual([r["speaker_key"] for r in rows], [1, 2, 1, 2])
        self.assertEqual([r["speaker_label"] for r in rows], ["Nam", "Người nói 2", "Nam", "Người nói 2"])
        await s.rename_speaker(2, "Hương")
        await s.drain()
        self.assertEqual([r["speaker_label"] for r in db.get_segments(mid)], ["Nam", "Hương", "Nam", "Hương"])
        seqs = await self.feed(s, [(1, "1", 5.0)], epoch=s.next_epoch(), t0=30)
        self.assertEqual(seqs, [105])
        self.assertEqual(s._by_seq[105]["speaker_label"], "Hương")

    async def test_merge_and_reassign_update_db(self):
        mid = db.create_meeting("Họp test")
        s = await live.get_session(mid)
        seqs = await self.feed(s, [(0, "1", 3.0), (1, "2", 3.0), (1, "2", 3.0)])
        await s.reassign_segment(seqs[2], None)
        await s.drain()
        self.assertEqual(db.get_segments(mid)[2]["speaker_label"], "Người nói 3")
        await s.merge_speakers(3, 2)
        await s.drain()
        self.assertEqual([r["speaker_key"] for r in db.get_segments(mid)], [1, 2, 2])
        merged = [p for p in db.list_speakers(mid) if p["sid"] == 3][0]
        self.assertEqual(merged["merged_into"], 2)


class VoiceRegistryTests(SessionTestCase):
    async def test_save_voice_waits_for_enough_audio_then_recognized_next_meeting(self):
        mid = db.create_meeting("Họp 1")
        s = await live.get_session(mid)
        await self.feed(s, [(1, "1", 1.2)])
        await s.rename_speaker(1, "Đỗ Minh Quân")
        res = await s.save_profile_voice(1)
        self.assertTrue(res.get("pending"))
        self.assertIsNone(db.get_voice_by_name("Đỗ Minh Quân"))
        await self.feed(s, [(1, "1", 4.0)], t0=10)
        ok = await self.settle(s, lambda: db.get_voice_by_name("Đỗ Minh Quân") is not None)
        self.assertTrue(ok, "Chưa tự lưu mẫu giọng khi đã đủ dữ liệu")
        v = db.get_voice_by_name("Đỗ Minh Quân")
        self.assertTrue(v["auto_learned"])
        self.assertEqual(v["consent_by"], "meeting_host_confirm")

        mid2 = db.create_meeting("Họp hôm sau")
        s2 = await live.get_session(mid2)
        seqs = await self.feed(s2, [(1, "1", 3.0)])
        self.assertEqual(s2._by_seq[seqs[0]]["speaker_label"], "Đỗ Minh Quân")

    async def test_same_name_different_voice_does_not_pollute_registry(self):
        db.save_voice("Hương", self.bank.enrollment(2).tolist(), consent_by="self_enrolled_mic", mode="replace")
        before = db.get_voice_by_name("Hương")["embedding"]
        mid = db.create_meeting("Họp test")
        s = await live.get_session(mid)
        await self.feed(s, [(0, "1", 5.0)])
        res = await s.rename_speaker(1, "Hương", save_voice=True)
        self.assertEqual(res["new_name"], "Hương")
        self.assertIsNone(res["voice_id"])
        self.assertTrue(res["voice"].get("conflict"))
        self.assertEqual(db.get_voice_by_name("Hương")["embedding"], before)

    async def test_manual_enrollment_not_overwritten_by_auto_learning(self):
        vid = db.save_voice("Bùi Hồng Phúc", self.bank.enrollment(0).tolist(), consent_by="self_enrolled_mic",
                            auto_learned=False, mode="replace")
        before = db.get_voice(vid)["embedding"]
        db.save_voice("Bùi Hồng Phúc", self.bank.vec(1, 3.0).tolist(), consent_by="x", auto_learned=True)
        self.assertEqual(db.get_voice(vid)["embedding"], before)
        vid2 = db.save_voice("Khách", self.bank.vec(3, 3.0).tolist(), auto_learned=True, n_samples=2)
        db.save_voice("Khách", self.bank.vec(3, 3.0).tolist(), auto_learned=True, n_samples=3)
        self.assertEqual(db.get_voice(vid2)["n_samples"], 5)


def fake_llm(predictions):
    async def _call(system, prompt, max_tokens=4000):
        return "Kết quả:\n```json\n" + json.dumps({"predictions": predictions}, ensure_ascii=False) + "\n```"
    return _call


class IdentityEngineTests(SessionTestCase):
    async def test_auto_apply_high_confidence_and_suggest_medium(self):
        mid = db.create_meeting("Họp test")
        s = await live.get_session(mid)
        await self.feed(s, [(0, "1", 3.0, "Chào mọi người, Tuấn ơi em cập nhật nhé"),
                            (1, "2", 3.0, "Dạ em là Tuấn bên DevOps"),
                            (2, "3", 3.0, "Em nghĩ bên payment ổn")])
        preds = [
            {"unknown_label": "Người nói 2", "predicted_name": "anh Lê Văn Tuấn", "predicted_role": "DevOps",
             "confidence": 0.93, "reasoning": "tự giới thiệu", "evidence": ["Dạ em là Tuấn bên DevOps"]},
            {"unknown_label": "Người nói 3", "predicted_name": "Đỗ Minh Quân", "confidence": 0.7,
             "reasoning": "nhắc payment"},
            {"unknown_label": "Người nói 1", "predicted_name": "", "confidence": 0.1},
        ]
        with mock.patch.object(artifacts, "llm_available", lambda: True), \
                mock.patch.object(artifacts, "_call_llm", fake_llm(preds)):
            results = await s.identity.run(force=True)
        await s.drain()
        actions = {r["sid"]: r["action"] for r in results}
        self.assertEqual(actions, {2: "applied", 3: "suggested"})
        self.assertEqual(s.speakers.profile(2).name, "Lê Văn Tuấn")   # đã bỏ kính ngữ "anh"
        self.assertEqual(s.speakers.profile(2).origin, "ai")
        self.assertEqual(db.get_segments(mid)[1]["speaker_label"], "Lê Văn Tuấn")
        self.assertTrue(db.get_segments(mid)[1]["is_inferred"])
        self.assertIsNone(db.get_voice_by_name("Lê Văn Tuấn"))      # mặc định không tự lưu sinh trắc học
        sugg = s.identity.pending_suggestions()
        self.assertEqual([(x["sid"], x["suggested_name"]) for x in sugg], [(3, "Đỗ Minh Quân")])
        statuses = sorted(i["status"] for i in db.get_inferences(mid))
        self.assertEqual(statuses, ["auto_applied", "pending"])

        await s.identity.accept(sugg[0]["inference_id"], save_voice=False)
        self.assertEqual(s.speakers.profile(3).name, "Đỗ Minh Quân")
        self.assertTrue(s.speakers.profile(3).locked)
        self.assertEqual(s.identity.pending_suggestions(), [])

    async def test_same_name_never_assigned_to_two_speakers(self):
        mid = db.create_meeting("Họp test")
        s = await live.get_session(mid)
        await self.feed(s, [(0, "1", 3.0, "em chào chị Hương"), (1, "2", 3.0, "chào em")])
        preds = [
            {"unknown_label": "Người nói 1", "predicted_name": "Hương", "confidence": 0.9},
            {"unknown_label": "Người nói 2", "predicted_name": "Hương", "confidence": 0.95},
        ]
        with mock.patch.object(artifacts, "llm_available", lambda: True), \
                mock.patch.object(artifacts, "_call_llm", fake_llm(preds)):
            results = await s.identity.run(force=True)
        self.assertEqual(s.speakers.profile(2).name, "Hương")
        self.assertEqual(s.speakers.profile(1).name, "")
        conflict = [r for r in results if r["sid"] == 1][0]
        self.assertEqual(conflict["action"], "suggested")
        self.assertTrue(conflict["conflict"])

    async def test_notify_triggers_only_on_name_cues(self):
        mid = db.create_meeting("Họp test")
        s = await live.get_session(mid)
        calls = []

        async def _call(system, prompt, max_tokens=4000):
            calls.append(prompt)
            return json.dumps({"predictions": []})

        with mock.patch.object(artifacts, "llm_available", lambda: True), \
                mock.patch.object(artifacts, "_call_llm", _call), \
                mock.patch("meeting.identity.MIN_INTERVAL_S", 0.0):
            await self.feed(s, [(0, "1", 3.0, "Hôm nay mình bàn về cache"), (1, "2", 3.0, "Đồng ý")])
            await asyncio.sleep(0.05)
            self.assertEqual(calls, [])
            await self.feed(s, [(1, "2", 3.0, "Em là Lan Anh bên marketing")], t0=20)
            await self.settle(s, lambda: bool(calls), timeout=4.0)
        self.assertEqual(len(calls), 1)
        self.assertIn("Người nói 2", calls[0])


class AssistantCallTests(SessionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from meeting import llm
        llm.set_assistant_config("Bông", ["Bong"])
        self.calls = []

        async def fake_activation(command, full_sentence, name=""):
            self.calls.append((name, command))
        self.mid = db.create_meeting("Họp test")
        self.s = await live.get_session(self.mid)
        self.s._handle_ai_activation = fake_activation

    async def test_call_with_request_in_same_sentence(self):
        await self.feed(self.s, [(0, "1", 3.0, "Bông ơi, tóm tắt giúp anh các quyết định.")])
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [("Bông", "tóm tắt giúp anh các quyết định.")])

    async def test_call_then_pause_waits_for_next_sentence(self):
        """Lỗi cũ: "Jarvis ơi." rồi ngừng một nhịp thì yêu cầu ở câu sau bị mất."""
        q = await self.s.subscribe()
        await self.feed(self.s, [(0, "1", 1.0, "Bông ơi.")])
        self.assertIn("ai_listening", [e["type"] for e in self.events(q)])
        await self.feed(self.s, [(0, "1", 3.0, "Tra cứu giúp anh ticket của Tuấn.")], t0=5)
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [("Bông", "Tra cứu giúp anh ticket của Tuấn.")])

    async def test_call_cut_mid_sentence_is_joined(self):
        """Lỗi thật (#37): Soniox cắt "Thanh ơi, em hãy" | "tổng kết lại... báo cáo nhanh cho anh." -> trợ lý từng
        làm theo "em hãy". Phải chờ người gọi nói nốt rồi xử lý cả câu, một lần."""
        q = await self.s.subscribe()
        await self.feed(self.s, [(0, "1", 1.5, "Rồi, ok, Bông ơi, em hãy")])
        self.assertEqual(self.calls, [])
        self.assertIn("ai_listening", [e["type"] for e in self.events(q)])
        await self.feed(self.s, [(0, "1", 4.0, "tổng kết lại cuộc họp và báo cáo nhanh cho anh.")], t0=3)
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [("Bông", "em hãy tổng kết lại cuộc họp và báo cáo nhanh cho anh.")])

    async def test_unpunctuated_call_runs_once_speaker_stops(self):
        with mock.patch.object(live, "CMD_SETTLE_S", 0.05):
            await self.feed(self.s, [(0, "1", 3.0, "Bông ơi, chuyển slide sang slide 3")])
            self.assertEqual(self.calls, [])
            await asyncio.sleep(0.5)
        self.assertEqual(self.calls, [("Bông", "chuyển slide sang slide 3")])

    async def test_unfinished_call_survives_someone_else_talking(self):
        with mock.patch.object(live, "CMD_SETTLE_S", 0.3):
            await self.feed(self.s, [(0, "1", 1.5, "Bông ơi, em hãy tạo một cái"), (1, "2", 1.0, "Ừ.")])
            await self.feed(self.s, [(0, "1", 3.5, "slide báo cáo tiến độ cho anh.")], t0=4)
            await asyncio.sleep(0)
        self.assertEqual(self.calls, [("Bông", "em hãy tạo một cái slide báo cáo tiến độ cho anh.")])

    async def test_call_then_silence_times_out(self):
        q = await self.s.subscribe()
        with mock.patch.object(live, "WAKE_FOLLOWUP_S", 0.05):
            await self.feed(self.s, [(0, "1", 1.0, "Bông ơi.")])
            await asyncio.sleep(0.7)
            await self.feed(self.s, [(1, "2", 3.0, "Mình bàn tiếp phần ngân sách nhé")], t0=20)
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [])
        self.assertIn("ai_listening_end", [e["type"] for e in self.events(q)])

    async def test_mentioning_name_mid_sentence_does_not_call(self):
        await self.feed(self.s, [(0, "1", 3.0, "Tôi nghĩ Bông làm được việc này"), (1, "2", 3.0, "bông hoa đẹp quá")])
        await asyncio.sleep(0)
        self.assertEqual(self.calls, [])

    async def test_assistant_names_sent_to_soniox_vocabulary(self):
        self.assertEqual(self.s.context_terms()[:2], ["Bông", "Bong"])


class ReanalyzeTests(SessionTestCase):
    async def test_reanalyze_splits_hidden_speaker_and_keeps_names(self):
        """Dữ liệu giống cuộc họp #30: người trong phòng nói chen vào, Soniox gắn cùng nhãn "2" với khách mời,
        bản cũ gộp hết vào khách mời đã được AI đặt tên. Phân tích lại phải ra 3 người, tên ở lại với khách mời."""
        bank = VoiceBank(seed=15, n=3, channel=0.9)
        mid = db.create_meeting("Podcast test")
        rows = [(0, "1", 5.0), (1, "2", 4.0), (0, "1", 3.0), (1, "2", 6.0), (0, "1", 4.0), (1, "2", 5.0),
                (2, "2", 2.5), (2, "2", 4.5), (2, "2", 3.0), (2, "2", 0.5), (2, "2", 2.4)]
        t = 0.0
        for i, (k, raw, voiced) in enumerate(rows, 1):
            key = 1 if raw == "1" else 2
            db.add_segment(mid, t, t + voiced * 1.3, "Người nói 1" if key == 1 else "Đặng Thế Trung", f"câu {i}",
                           raw_embedding=bank.vec(k, voiced).tolist() if voiced >= 1 else None, seq=i,
                           speaker_key=key, raw_speaker=raw, epoch=0, voiced=voiced)
            t += voiced * 1.3 + 0.5
        db.upsert_speakers(mid, [{"sid": 1, "name": "", "label": "Người nói 1", "origin": "new", "n_segments": 3},
                                 {"sid": 2, "name": "Đặng Thế Trung", "label": "Đặng Thế Trung", "origin": "ai",
                                  "confidence": 0.85, "n_segments": 8}])
        s = await live.get_session(mid)
        res = await s.reanalyze()
        labels = [r["speaker_label"] for r in db.get_segments(mid)]
        self.assertEqual(labels[:6], ["Người nói 1", "Đặng Thế Trung"] * 3)
        self.assertEqual(set(labels[6:]), {"Người nói 3"})
        self.assertEqual(sorted(p["label"] for p in res["speakers"]), ["Người nói 1", "Người nói 3", "Đặng Thế Trung"])
        self.assertEqual(sorted(p["sid"] for p in db.list_speakers(mid)), [1, 2, 3])


class LifecycleTests(SessionTestCase):
    async def test_finish_marks_ended_and_generates_minutes_in_background(self):
        mid = db.create_meeting("Họp chốt sprint")
        s = await live.get_session(mid)
        q = await s.subscribe()
        await self.feed(s, [(0, "1", 3.0, "Chốt release thứ sáu")])

        async def _call(system, prompt, max_tokens=4000):
            return "# BIÊN BẢN CUỘC HỌP: Họp chốt sprint\n\n## 1. Tóm Tắt\nChốt release."

        with mock.patch.object(artifacts, "llm_available", lambda: True), \
                mock.patch.object(artifacts, "_call_llm", _call):
            res = await s.finish()
            self.assertEqual(res["minutes_status"], "pending")
            self.assertEqual(db.get_meeting(mid)["status"], "ended")
            ok = await self.settle(s, lambda: db.get_meeting(mid).get("minutes_status") == "done")
        self.assertTrue(ok)
        self.assertEqual([a["kind"] for a in db.get_artifacts(mid)], ["minutes"])
        types = [e["type"] for e in self.events(q)]
        self.assertIn("artifact_created", types)
        self.assertIn("meeting_status", types)


if __name__ == "__main__":
    unittest.main()
