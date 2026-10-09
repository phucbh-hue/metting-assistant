"""Tiện ích dùng chung cho test: sinh vector giọng tổng hợp và dọn DB in-memory.

Vector tổng hợp mô phỏng phân bố cosine đo được trên dữ liệu thật của model CAM++:
cùng người câu ngắn ~0.47, câu dài ~0.80; khác người ~0.15-0.26 (cùng phòng/mic tạo thành phần chung).
Không dùng vector giọng của người thật trong repo (dữ liệu sinh trắc học).
"""
from typing import List, Optional

import numpy as np

import tests  # noqa: F401  (đặt biến môi trường trước khi import meeting.*)
from meeting import db, live, voice

D = voice.DIM


def unit(x: np.ndarray) -> np.ndarray:
    return (x / np.linalg.norm(x)).astype(np.float32)


class VoiceBank:
    """Ngân hàng giọng tổng hợp: mỗi người một vector gốc + thành phần kênh chung + nhiễu theo độ dài câu."""

    def __init__(self, seed: int = 0, n: int = 4, channel: float = 0.7):
        self.rng = np.random.default_rng(seed)
        self.channel = unit(self.rng.standard_normal(D))
        self.alpha = channel
        self.base = [unit(self.rng.standard_normal(D)) for _ in range(n)]

    def vec(self, k: int, voiced: float = 3.0) -> np.ndarray:
        sigma = 1.3 if voiced < 2 else 0.85 if voiced < 4 else 0.6
        return unit(self.alpha * self.channel + self.base[k] + sigma * unit(self.rng.standard_normal(D)))

    def enrollment(self, k: int) -> np.ndarray:
        """Mẫu giọng thu 10 giây (sạch hơn câu nói trong họp)."""
        return unit(self.alpha * self.channel + self.base[k] + 0.3 * unit(self.rng.standard_normal(D)))


def reset_db():
    d = db._get_db()
    assert db.is_mock(), "Test chỉ được chạy trên DB in-memory (MEETING_DB=mock)"
    for name in d.list_collection_names():
        d[name].drop()
    db.init()
    from meeting import llm
    llm._assistant_cache.update({"value": None, "at": 0.0})
    from meeting import artifacts
    artifacts._provider_cache.clear()          # nguồn AI đã chọn ở test trước không được dính sang test sau
    from meeting import tts
    tts._settings_cache["value"] = None        # nguồn giọng đọc đã chọn ở test trước cũng vậy
    import sys
    from meeting import groups
    groups._CACHE.clear()                      # nhóm cũ của test trước (mã nhóm được dùng lại sau khi dựng lại DB)
    appmod = sys.modules.get("meeting.app")
    if appmod is not None:
        appmod._MEETING_KEYS.clear()           # id cuộc họp được dùng lại sau khi dựng lại DB
    for s in list(live.SESSIONS.values()):
        s.dispose()
    live.SESSIONS.clear()
    live._session_locks.clear()


def script_add(sp: voice.MeetingSpeakers, bank: VoiceBank, script: List[tuple], start_key: int = 1,
               epoch: int = 0, t0: float = 0.0) -> List[int]:
    """script: [(speaker_idx, raw_label hoặc None, voiced_s, has_vector=True)]. Trả về danh sách key."""
    keys, t = [], t0
    for i, row in enumerate(script):
        k, raw, voiced = row[0], row[1], row[2]
        has_vec = row[3] if len(row) > 3 else voiced >= voice.MIN_SEG_S
        key = start_key + i
        sp.add(key=key, v=bank.vec(k, voiced) if has_vec else None, raw_label=raw, t=t,
               voiced=voiced, text=f"câu {key}", epoch=epoch, dur=voiced * 1.3)
        keys.append(key)
        t += voiced * 1.3 + 0.5
    return keys


def label(sp: voice.MeetingSpeakers, key: int) -> str:
    return sp.decided[key]


def labels_of(sp: voice.MeetingSpeakers, keys: List[int]) -> List[str]:
    return [sp.decided[k] for k in keys]


def first(xs, pred) -> Optional[object]:
    for x in xs:
        if pred(x):
            return x
    return None
