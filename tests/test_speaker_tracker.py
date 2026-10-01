"""Test bộ theo dõi người nói (voice.MeetingSpeakers) - phân vai, đổi tên, gộp, khôi phục."""
import unittest

from tests.helpers import VoiceBank, label, labels_of, script_add
from meeting import voice


class NewSpeakerDetectionTests(unittest.TestCase):
    def test_two_speakers_alternating_get_two_labels(self):
        """Lỗi cũ: người thứ 2 bị gộp vào 'Người 1'. Hội thoại xen kẽ phải ra đúng 2 người."""
        bank = VoiceBank(seed=1)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [
            (0, "1", 1.4), (1, "2", 1.3), (0, "1", 1.4), (1, "2", 1.2),
            (0, "1", 1.5), (0, "1", 0.2), (1, "2", 5.5), (1, "2", 7.4), (0, "1", 2.0),
        ])
        got = labels_of(sp, keys)
        self.assertEqual(len(sp.visible_profiles()), 2)
        self.assertEqual({got[i] for i in (0, 2, 4, 5, 8)}, {"Người nói 1"})
        self.assertEqual({got[i] for i in (1, 3, 6, 7)}, {"Người nói 2"})

    def test_third_speaker_appears_on_first_utterance(self):
        bank = VoiceBank(seed=2)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 3.0), (1, "2", 2.5), (0, "1", 3.0), (2, "3", 1.4), (1, "2", 3.0)])
        self.assertEqual(label(sp, keys[3]), "Người nói 3")
        self.assertEqual(label(sp, keys[4]), "Người nói 2")

    def test_short_utterance_without_vector_follows_soniox_label(self):
        bank = VoiceBank(seed=3)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 3.0), (1, "2", 3.0), (0, "1", 0.3), (1, "2", 0.2), (0, "1", 0.4)])
        self.assertEqual(labels_of(sp, keys), ["Người nói 1", "Người nói 2", "Người nói 1", "Người nói 2", "Người nói 1"])

    def test_new_soniox_label_without_vector_creates_new_speaker(self):
        bank = VoiceBank(seed=4)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 3.0), (1, "2", 0.3), (1, "2", 3.0)])
        self.assertEqual(labels_of(sp, keys), ["Người nói 1", "Người nói 2", "Người nói 2"])

    def test_same_room_high_similarity_does_not_collapse_everyone(self):
        """Cuộc họp thật cùng phòng/mic: cosine giữa người khác nhau cao (~0.37 câu dài).
        Thuật toán cũ tái dùng nhãn khi centroid >= 0.46 nên dồn tất cả vào 'Người lạ #1'."""
        bank = VoiceBank(seed=5, n=4, channel=0.9)
        sp = voice.MeetingSpeakers()
        script = []
        for r in range(6):
            for k in range(4):
                script.append((k, str(k + 1), 4.5 if r % 2 else 1.6))
        keys = script_add(sp, bank, script)
        self.assertEqual(len(sp.visible_profiles()), 4)
        for i, row in enumerate(script):
            self.assertEqual(label(sp, keys[i]), f"Người nói {row[0] + 1}")

    def test_without_diarization_long_segments_cluster_by_voice(self):
        bank = VoiceBank(seed=6)
        sp = voice.MeetingSpeakers()
        script = [(0, None, 5.0), (1, None, 5.0), (0, None, 4.5), (1, None, 6.0), (0, None, 5.5), (1, None, 4.2)]
        keys = script_add(sp, bank, script)
        got = labels_of(sp, keys)
        self.assertEqual(len(set(got)), 2)
        self.assertEqual(got[0], got[2])
        self.assertEqual(got[1], got[3])
        self.assertNotEqual(got[0], got[1])


class SonioxErrorRecoveryTests(unittest.TestCase):
    def test_soniox_split_label_rejoins_existing_speaker(self):
        """Soniox đổi nhãn của cùng một người (1 -> 3): giọng giống hệt nên phải về lại hồ sơ cũ."""
        bank = VoiceBank(seed=7)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 5.0), (1, "2", 5.0), (0, "1", 4.5), (0, "3", 6.0)])
        self.assertEqual(label(sp, keys[3]), "Người nói 1")
        self.assertEqual(len(sp.visible_profiles()), 2)

    def test_voice_override_when_soniox_confuses_two_people(self):
        bank = VoiceBank(seed=8)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 5.0), (1, "2", 5.0), (0, "1", 5.0), (1, "2", 5.0),
                                     (1, "1", 6.0)])   # người 2 nói nhưng Soniox gắn nhãn của người 1
        self.assertEqual(label(sp, keys[4]), "Người nói 2")
        self.assertEqual(sp.by_key[keys[4]]["reason"], "voice_override")

    def test_reconnect_new_epoch_reidentifies_by_voice(self):
        """Stream nối lại -> nhãn Soniox bắt đầu lại (thậm chí đảo nhau). CAM++ phải nhận lại đúng người."""
        bank = VoiceBank(seed=9)
        sp = voice.MeetingSpeakers()
        script_add(sp, bank, [(0, "1", 5.0), (1, "2", 5.0), (0, "1", 4.5), (1, "2", 4.5)], epoch=0)
        keys = script_add(sp, bank, [(1, "1", 4.5), (0, "2", 4.5), (1, "1", 1.2), (0, "2", 0.3)],
                          start_key=100, epoch=1, t0=100)
        self.assertEqual(labels_of(sp, keys), ["Người nói 2", "Người nói 1", "Người nói 2", "Người nói 1"])
        self.assertEqual(len(sp.visible_profiles()), 2)

    def test_short_utterances_after_reconnect_are_resolved_later(self):
        """Sau khi nối lại, câu ngắn mang nhãn Soniox mới không được tạo người mới; khi nhãn đó có câu dài
        thì các câu ngắn được chuyển về đúng người."""
        bank = VoiceBank(seed=14)
        sp = voice.MeetingSpeakers()
        script_add(sp, bank, [(0, "1", 5.0), (1, "2", 5.0)], epoch=0)
        keys = script_add(sp, bank, [(1, "1", 0.3), (0, "2", 0.3), (0, "2", 4.0), (1, "1", 4.0)],
                          start_key=100, epoch=1, t0=100)
        self.assertEqual(labels_of(sp, keys), ["Người nói 2", "Người nói 1", "Người nói 1", "Người nói 2"])
        self.assertEqual(len(sp.visible_profiles()), 2)


class NamingTests(unittest.TestCase):
    def test_rename_sticks_for_future_segments(self):
        """Lỗi cũ: đổi tên xong, câu mới của người đó lại hiện nhãn tạm."""
        bank = VoiceBank(seed=10)
        sp = voice.MeetingSpeakers()
        script_add(sp, bank, [(0, "1", 3.0), (1, "2", 3.0)])
        p, keys = sp.rename(2, "Hương", origin="manual")
        self.assertEqual(p.label, "Hương")
        self.assertTrue(p.locked)
        new = script_add(sp, bank, [(1, "2", 1.3), (0, "1", 2.0), (1, "2", 0.4), (1, "2", 6.0)], start_key=10)
        self.assertEqual(labels_of(sp, new), ["Hương", "Người nói 1", "Hương", "Hương"])

    def test_rename_to_existing_name_merges_profiles(self):
        bank = VoiceBank(seed=11)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 3.0), (1, "2", 3.0), (0, "3", 1.2)])
        sp.rename(1, "Nam")
        if label(sp, keys[2]) != "Nam":       # nếu bị tách thành người mới thì người dùng đặt cùng tên để gộp
            sp.rename(sp.sid_of(keys[2]), "Nam")
        self.assertEqual(label(sp, keys[2]), "Nam")
        self.assertEqual(sorted(p.label for p in sp.visible_profiles()), ["Nam", "Người nói 2"])

    def test_manual_merge_moves_segments_and_keeps_name(self):
        bank = VoiceBank(seed=12)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 3.0), (1, "2", 3.0), (1, "3", 3.0)])
        sp.rename(2, "Lan Anh")
        sp.reassign(keys[2], None)            # giả lập bị tách nhầm thành người mới
        self.assertEqual(label(sp, keys[2]), "Người nói 3")
        moved = sp.merge(sp.sid_of(keys[2]), 2)
        self.assertIn(keys[2], moved)
        self.assertEqual(label(sp, keys[2]), "Lan Anh")
        # nhãn Soniox "3" giờ trỏ về Lan Anh
        nxt = script_add(sp, bank, [(1, "3", 0.3)], start_key=50)
        self.assertEqual(label(sp, nxt[0]), "Lan Anh")

    def test_reassign_single_segment(self):
        bank = VoiceBank(seed=13)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 3.0), (1, "2", 3.0), (1, "1", 1.2)])
        dst, _ = sp.reassign(keys[2], 2)
        self.assertEqual(label(sp, keys[2]), "Người nói 2")
        dst, _ = sp.reassign(keys[2], None)   # tách thành người mới
        self.assertEqual(label(sp, keys[2]), "Người nói 3")


class AnchorTests(unittest.TestCase):
    def make(self, bank):
        anchors = {
            101: {"name": "Bùi Hồng Phúc", "vector": bank.enrollment(0), "role": "CTO"},
            102: {"name": "Lê Văn Tuấn", "vector": bank.enrollment(1), "role": "DevOps"},
            103: {"name": "Vector rỗng", "vector": [0.0] * 192},
        }
        return voice.MeetingSpeakers(anchors=anchors, expected_host_id=101)

    def test_known_voice_recognized_and_stranger_stays_unnamed(self):
        bank = VoiceBank(seed=20)
        sp = self.make(bank)
        self.assertNotIn(103, sp.anchors)   # vector rỗng không được dùng làm mẫu
        keys = script_add(sp, bank, [(0, "1", 3.0), (2, "2", 3.0), (1, "3", 2.5), (0, "1", 0.3)])
        self.assertEqual(labels_of(sp, keys), ["Bùi Hồng Phúc", "Người nói 2", "Lê Văn Tuấn", "Bùi Hồng Phúc"])
        self.assertEqual(sp.speaker_ids[keys[0]], 101)

    def test_anchor_is_exclusive_per_meeting(self):
        bank = VoiceBank(seed=21)
        sp = self.make(bank)
        script_add(sp, bank, [(0, "1", 5.0), (2, "2", 5.0), (2, "2", 5.0)])
        names = [p.name for p in sp.visible_profiles() if p.voice_id == 101]
        self.assertEqual(names, ["Bùi Hồng Phúc"])

    def test_update_anchors_names_existing_profile(self):
        """Lưu mẫu giọng giữa buổi họp -> hồ sơ đang có được gắn tên ngay."""
        bank = VoiceBank(seed=22)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(3, "1", 5.0), (3, "1", 4.0)])
        self.assertEqual(label(sp, keys[0]), "Người nói 1")
        sp.update_anchors({7: {"name": "Trần Thu Hằng", "vector": bank.enrollment(3)}})
        self.assertEqual(label(sp, keys[0]), "Trần Thu Hằng")


class PersistenceTests(unittest.TestCase):
    def test_export_and_load_state_roundtrip(self):
        bank = VoiceBank(seed=30)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 3.0), (1, "2", 3.0), (0, "1", 1.4)])
        sp.rename(2, "Hương")
        profiles = sp.export_profiles()
        segments = [{"seq": k, "speaker_key": sp.sid_of(k), "speaker_label": sp.decided[k], "t_start": i * 4.0,
                     "t_end": i * 4.0 + 3, "raw_embedding": sp.by_key[k]["v"].tolist(), "voiced": 3.0}
                    for i, k in enumerate(keys)]
        sp2 = voice.MeetingSpeakers()
        sp2.load_state(profiles, segments)
        self.assertEqual(labels_of(sp2, keys), ["Người nói 1", "Hương", "Người nói 1"])
        self.assertEqual(sp2.next_sid, 3)
        # Phiên Soniox mới (epoch 1): nhận lại Hương bằng giọng
        new = script_add(sp2, bank, [(1, "1", 5.0), (0, "2", 5.0)], start_key=10, epoch=1)
        self.assertEqual(labels_of(sp2, new), ["Hương", "Người nói 1"])

    def test_load_legacy_segments_without_speaker_key(self):
        sp = voice.MeetingSpeakers()
        sp.load_state([], [
            {"seq": 1, "speaker_label": "Nam", "speaker_id": 15, "t_start": 0, "t_end": 2},
            {"seq": 2, "speaker_label": "Người lạ #1", "t_start": 3, "t_end": 5},
            {"seq": 3, "speaker_label": "Nam", "t_start": 6, "t_end": 8},
        ])
        self.assertEqual(sp.decided, {1: "Nam", 2: "Người nói 2", 3: "Nam"})
        self.assertEqual(sp.profile(sp.sid_of(1)).voice_id, 15)


if __name__ == "__main__":
    unittest.main()
