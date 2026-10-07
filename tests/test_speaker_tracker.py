"""Test bộ theo dõi người nói (voice.MeetingSpeakers) - phân vai, đổi tên, gộp, khôi phục."""
import unittest

import numpy as np

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

    def test_new_person_hidden_under_reused_soniox_label_is_split_out(self):
        """Lỗi thật (cuộc họp #30): đang phát podcast 2 người, người trong phòng nói chen vào nhưng Soniox
        gắn cùng nhãn với khách mời. Phải tách thành người nói thứ 3 và chuyển ngược các câu của người đó."""
        bank = VoiceBank(seed=15, n=3, channel=0.9)   # cùng phòng/mic: giọng khác người vẫn giống ~0.4-0.6
        sp = voice.MeetingSpeakers()
        podcast = script_add(sp, bank, [(0, "1", 5.0), (1, "2", 4.0), (0, "1", 3.0), (1, "2", 6.0),
                                        (1, "2", 0.4), (0, "1", 4.0), (1, "2", 5.0)])
        newcomer = script_add(sp, bank, [(2, "2", 0.6), (2, "2", 2.5), (2, "2", 0.9), (2, "2", 4.5),
                                         (2, "2", 3.0), (2, "2", 0.5), (2, "2", 2.4)], start_key=50, t0=100)
        self.assertEqual(sp.pop_splits(), [(2, 3)])
        self.assertEqual(set(labels_of(sp, newcomer)), {"Người nói 3"})
        self.assertEqual(labels_of(sp, podcast), [f"Người nói {i}" for i in (1, 2, 1, 2, 2, 1, 2)])
        # Khách mời nói lại (Soniox vẫn gắn "2"): giọng khớp hẳn khách mời -> trả về đúng người
        back = script_add(sp, bank, [(1, "2", 5.0)], start_key=90, t0=200)
        self.assertEqual(label(sp, back[0]), "Người nói 2")

    @staticmethod
    def _similar_bank(r: float, seed: int = 42, n: int = 2) -> VoiceBank:
        """Hai giọng gần nhau (ví dụ cùng phát qua loa): tâm giọng giống ~0.7-0.8 dù là hai người."""
        bank = VoiceBank(seed=seed, n=n, channel=0.7)
        rng = np.random.default_rng(seed + 100)
        bank.base[1] = voice.unit(r * bank.base[0] + np.sqrt(1 - r * r) * voice.unit(rng.standard_normal(voice.DIM)))
        return bank

    def test_similar_voices_under_one_label_split_once(self):
        """Lỗi thật (#31, #32): người dẫn podcast và khách mời giống giọng, Soniox gắn chung nhãn (người trong phòng
        nói trước đó có nhãn khác). Từng câu vẫn gần cụm của mình hơn hẳn cụm kia -> tách một lần, không tách-gộp."""
        bank = self._similar_bank(0.65, n=3)
        sp = voice.MeetingSpeakers()
        user = script_add(sp, bank, [(2, "2", 3.0), (2, "2", 2.5)])
        a = script_add(sp, bank, [(0, "1", v) for v in (4.0, 5.0, 3.5, 6.0, 4.5, 5.0)], start_key=10, t0=20)
        b = script_add(sp, bank, [(1, "1", v) for v in (4.0, 5.5, 3.0, 6.0, 4.0, 5.0, 4.5)], start_key=50, t0=80)
        self.assertEqual(sp.pop_splits(), [(2, 3)])
        self.assertEqual(sp.pop_merges(), [])
        self.assertEqual(set(labels_of(sp, user)), {"Người nói 1"})
        self.assertEqual(set(labels_of(sp, a)), {"Người nói 2"})
        self.assertEqual(set(labels_of(sp, b)), {"Người nói 3"})

    def test_one_voice_drifting_under_the_only_label_is_not_split(self):
        """Lỗi thật #44: một người nói suốt buổi, Soniox chỉ có một nhãn; giọng đổi dần (tư thế, khoảng cách tới mic)
        thành 2 cụm gần nhau như 2 người. Soniox chưa nghe thấy ai khác -> vẫn là một người."""
        bank = self._similar_bank(0.65)
        sp = voice.MeetingSpeakers()
        early = script_add(sp, bank, [(0, "1", v) for v in (4.0, 5.0, 3.5, 6.0, 4.5, 5.0)])
        late = script_add(sp, bank, [(1, "1", v) for v in (4.0, 5.5, 3.0, 6.0, 4.0, 5.0, 4.5)], start_key=50, t0=60)
        self.assertEqual(sp.pop_splits(), [])
        self.assertEqual(set(labels_of(sp, early + late)), {"Người nói 1"})
        # Giọng khác hẳn dưới cùng nhãn thì vẫn tách như cũ
        other = VoiceBank(seed=43, n=1, channel=0.7)
        other.channel = bank.channel
        newcomer = script_add(sp, other, [(0, "1", v) for v in (5.0, 4.5, 6.0, 5.0)], start_key=80, t0=150)
        self.assertEqual(set(labels_of(sp, newcomer)), {"Người nói 2"})
        self.assertEqual(set(labels_of(sp, early + late)), {"Người nói 1"})

    def test_nearly_identical_voices_are_not_split(self):
        bank = self._similar_bank(0.9)
        sp = voice.MeetingSpeakers()
        script_add(sp, bank, [(i % 2, "1", 4.0 + (i % 3)) for i in range(14)])
        self.assertEqual(sp.pop_splits(), [])
        self.assertEqual(len(sp.visible_profiles()), 1)

    def test_user_split_from_segment(self):
        """Người dùng: "từ câu này trở đi là người khác" (hai giọng giống hệt nhau, máy không tự tách được)."""
        bank = VoiceBank(seed=46, n=1)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", 4.0)] * 8)
        self.assertEqual(sp.split_from(keys[0]), (None, []))            # câu đầu tiên: cả hồ sơ là một người
        q, moved = sp.split_from(keys[4])
        self.assertEqual(moved, keys[4:])
        self.assertEqual(labels_of(sp, keys), ["Người nói 1"] * 4 + ["Người nói 2"] * 4)
        # Câu mới cùng nhãn Soniox theo người mới; giọng trùng khớp nhưng không tự gộp lại
        more = script_add(sp, bank, [(0, "1", 0.6), (0, "1", 4.0), (0, "1", 5.0)], start_key=20, t0=200)
        self.assertEqual(set(labels_of(sp, more)), {"Người nói 2"})
        self.assertEqual(sp.pop_merges(), [])
        # Trạng thái "không tự gộp" được lưu lại qua export/load
        sp2 = voice.MeetingSpeakers()
        sp2.load_state(sp.export_profiles(), [])
        self.assertEqual(sp2.profile(q.sid).apart, [1])

    def test_long_interruption_under_same_label_becomes_new_speaker(self):
        """Người đang nói bị người khác chen ngang nói dài; Soniox vẫn dùng nhãn cũ. Câu dài của người chen phải
        thành người nói mới ngay, và người cũ nói tiếp vẫn là người cũ."""
        bank = VoiceBank(seed=47, n=2, channel=0.7)
        sp = voice.MeetingSpeakers()
        a1 = script_add(sp, bank, [(0, "1", v) for v in (5.0, 4.0, 6.0, 3.5, 5.0)])
        b = script_add(sp, bank, [(1, "1", 6.0), (1, "1", 5.0), (1, "1", 0.4)], start_key=20, t0=40)
        a2 = script_add(sp, bank, [(0, "1", 4.0), (0, "1", 0.5), (0, "1", 5.0)], start_key=30, t0=60)
        self.assertEqual(set(labels_of(sp, a1)), {"Người nói 1"})
        self.assertEqual(set(labels_of(sp, b)), {"Người nói 2"})
        self.assertEqual(set(labels_of(sp, a2)), {"Người nói 1"})
        self.assertEqual(len(sp.visible_profiles()), 2)

    def test_uneven_speaker_is_not_split_by_long_utterance(self):
        """Một người nói giọng không đều (hồ sơ không chặt) thì câu dài hơi lệch không được tách thành người mới."""
        bank = VoiceBank(seed=48, n=1, channel=0.7)
        sp = voice.MeetingSpeakers()
        keys = script_add(sp, bank, [(0, "1", v) for v in (1.2, 1.5, 4.0, 1.1, 6.0, 1.3, 9.0, 1.4, 5.0)])
        self.assertEqual(len(sp.visible_profiles()), 1, labels_of(sp, keys))

    def test_same_speaker_is_not_split(self):
        """Một người nói lâu, nhiều câu ngắn dài khác nhau: không được tự tách thành 2 người."""
        bank = VoiceBank(seed=16, n=2, channel=0.9)
        sp = voice.MeetingSpeakers()
        script = [(0, "1", [1.2, 4.0, 2.5, 6.0, 1.5, 3.0, 5.0, 2.0, 1.1, 4.4][i % 10]) for i in range(30)]
        script_add(sp, bank, script)
        self.assertEqual(sp.pop_splits(), [])
        self.assertEqual(len(sp.visible_profiles()), 1)

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

    def test_vague_voiceprint_does_not_pull_everyone_into_one_person(self):
        """Lỗi thật #57: mẫu giọng thu ở máy khác giống cả 2 người như nhau, cả buổi bị dồn vào hồ sơ có mẫu giọng."""
        bank = VoiceBank(seed=23, n=2, channel=0.9)
        vague = voice.unit(bank.enrollment(0) + bank.enrollment(1))
        sp = voice.MeetingSpeakers(anchors={26: {"name": "Bùi Hồng Phúc", "vector": vague}})
        keys = script_add(sp, bank, [(i % 2, str(i % 2 + 1), 4.0) for i in range(12)])
        self.assertEqual(len(sp.visible_profiles()), 2)
        self.assertEqual(sp.pop_merges(), [])
        self.assertEqual(len({label(sp, k) for k in keys[0::2]}), 1)
        self.assertEqual(len({label(sp, k) for k in keys[1::2]}), 1)
        self.assertNotEqual(label(sp, keys[0]), label(sp, keys[1]))

    def test_voiceprint_profile_does_not_swallow_other_people(self):
        """Lỗi thật #48: cùng phòng, cùng mic nên ai cũng hơi giống mẫu giọng; hồ sơ mang mẫu giọng gộp mọi hồ sơ
        mới giống nó từ 0.45, bỏ qua nhãn Soniox -> 4 người còn 2."""
        bank = VoiceBank(seed=24, n=3, channel=1.4)
        sp = voice.MeetingSpeakers(anchors={26: {"name": "Bùi Hồng Phúc", "vector": bank.enrollment(0)}})
        script = [(k, str(k + 1), 4.0) for _ in range(5) for k in range(3)]
        keys = script_add(sp, bank, script)
        self.assertEqual(len(sp.visible_profiles()), 3)
        for who in range(3):
            got = {label(sp, k) for i, k in enumerate(keys) if script[i][0] == who}
            self.assertEqual(len(got), 1, f"người {who}: {got}")
        self.assertEqual(label(sp, keys[0]), "Bùi Hồng Phúc")

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
