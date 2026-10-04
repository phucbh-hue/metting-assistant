"""Voice Biometrics & Meeting Speaker Tracking.

Module nhận diện và định danh giọng nói cho cuộc họp:
1. Trích xuất vector đặc trưng (192-dim speaker embedding) qua mô hình CAM++ (3D-Speaker)
   chạy trên CPU bằng sherpa-onnx.
2. Kiểm tra chất lượng âm thanh RMS (voiced_s) để loại bỏ khoảng lặng/tiếng xì trước khi tính vector.
3. MeetingSpeakers: theo dõi người nói trực tuyến (online speaker tracking) trong một buổi họp.
   - Mỗi người nói là một HỒ SƠ ỔN ĐỊNH (sid = 1, 2, 3...) - tên gắn với hồ sơ chứ không gắn với chuỗi nhãn,
     nên đổi tên một lần là giữ nguyên cho mọi câu sau, kể cả khi server khởi động lại (export/load state).
   - Kết hợp 2 nguồn tín hiệu: nhãn diarization của Soniox (rất tốt để phân biệt người nói trong cùng một
     phiên stream) và vector CAM++ (để nhận lại người quen, nối lại sau khi stream kết nối lại, sửa lỗi Soniox).
   - Câu quá ngắn ("dạ", "ừ") không đủ để tính vector: dựa vào nhãn Soniox, nếu không có thì kế thừa người nói gần nhất.
   - Tự gộp hai hồ sơ khi giọng trùng khớp mạnh; tự gắn tên khi khớp mẫu giọng đã lưu (anchor).
"""
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np

log = logging.getLogger("meeting.voice")

HERE = Path(__file__).resolve().parent
MODEL_PATH = Path(os.getenv("VOICE_MODEL_PATH", str(HERE.parent / "models" / "speaker.onnx")))
RATE = 16000
DIM = 192
MIN_SEG_S = 1.0          # Câu có ít hơn ngần này giây tiếng nói thực: không tính vector
MIN_ANCHOR_S = 3.0       # Mẫu lưu cần ít nhất ngần này giây tiếng nói thực
VOICED_RMS = 300         # Khung 30ms to hơn ngần này (~ -40 dBFS) mới tính là tiếng nói

_ex = None
_ex_lock = threading.Lock()
_warm = {"running": False, "done": False, "error": ""}


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
        return MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000
    except Exception:
        return False


def ready() -> bool:
    return available() and _warm["done"]


def pending() -> bool:
    return _warm["running"] or (available() and not _warm["done"] and not _warm["error"])


def status() -> str:
    if not available():
        return f"Chưa có file model tại {MODEL_PATH}"
    if _warm["running"]:
        return "Đang nạp model CAM++ vào RAM"
    if _warm["error"]:
        return f"Lỗi nạp model: {_warm['error']}"
    if _warm["done"]:
        return "Sẵn sàng (192D CPU)"
    return "Chưa nạp"


def get_diagnostics() -> Dict[str, Any]:
    return {
        "model_name": "CAM++ (3D-Speaker)",
        "dimension": DIM,
        "status": status(),
        "ready": ready(),
        "model_path": str(MODEL_PATH),
        "model_size_mb": round(MODEL_PATH.stat().st_size / (1024 * 1024), 1) if MODEL_PATH.exists() else 0
    }


def _extractor():
    global _ex
    with _ex_lock:
        if _ex is None:
            import sherpa_onnx
            if not MODEL_PATH.exists():
                raise RuntimeError(f"Chưa có file model nhận diện giọng: {MODEL_PATH}")
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL_PATH), num_threads=1)
            if not cfg.validate():
                raise RuntimeError(f"Config model CAM++ không hợp lệ: {MODEL_PATH}")
            _ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        return _ex


def warmup():
    """Nạp trước model vào RAM và chạy thử 1 frame mồi."""
    if _warm["running"] or _warm["done"]:
        return
    _warm["running"] = True
    t0 = time.monotonic()
    try:
        ex = _extractor()
        st = ex.create_stream()
        dummy = np.sin(np.arange(RATE * 2) * 2 * np.pi * 150 / RATE).astype(np.float32) * 0.3
        st.accept_waveform(RATE, dummy)
        st.input_finished()
        if ex.is_ready(st):
            ex.compute(st)
        _warm["done"] = True
        log.info("meeting.voice: model CAM++ sẵn sàng sau %.2fs", time.monotonic() - t0)
    except Exception as e:
        _warm["error"] = str(e)
        log.error("meeting.voice: nạp model CAM++ thất bại: %s", e)
    finally:
        _warm["running"] = False


def ensure_model_async():
    if not ready() and not _warm["running"]:
        threading.Thread(target=warmup, name="voice-warmup", daemon=True).start()


def voiced_s(pcm: bytes) -> float:
    """Đếm số giây có tiếng nói thực sự dựa trên RMS năng lượng."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    fr = int(RATE * 0.03)  # khung 30ms = 480 samples
    n = len(x) // fr
    if n == 0:
        return 0.0
    rms = np.sqrt(np.mean(x[:n * fr].reshape(n, fr) ** 2, axis=1))
    return float(np.count_nonzero(rms >= VOICED_RMS)) * 0.03


def unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def is_valid_vector(v: Any) -> bool:
    """Vector có đúng 192 chiều và không phải vector rỗng (0.0 * 192)."""
    try:
        arr = np.asarray(v, dtype=np.float32)
    except Exception:
        return False
    return arr.shape == (DIM,) and bool(np.isfinite(arr).all()) and float(np.linalg.norm(arr)) > 1e-3


def embed(pcm: bytes, min_voiced: float = MIN_SEG_S) -> Optional[np.ndarray]:
    """Trích xuất 192-dim L2-normalized vector từ PCM 16kHz mono."""
    if len(pcm) < min_voiced * RATE * 2 or voiced_s(pcm) < min_voiced:
        return None
    try:
        ex = _extractor()
        st = ex.create_stream()
        audio_norm = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        st.accept_waveform(RATE, audio_norm)
        st.input_finished()
        if not ex.is_ready(st):
            return None
        v = np.asarray(ex.compute(st), dtype=np.float32)
        return unit(v)
    except Exception as e:
        log.warning("meeting.voice embed error: %s", e)
        return None


def cosine_sim(v1: np.ndarray, v2: np.ndarray) -> float:
    return float(np.dot(v1, v2))


def merge_vectors(vectors: List[np.ndarray], weights: Optional[List[float]] = None) -> np.ndarray:
    if not vectors:
        return np.zeros(DIM, dtype=np.float32)
    if weights is None:
        weights = [1.0] * len(vectors)
    acc = np.sum([v * max(w, 0.1) for v, w in zip(vectors, weights)], axis=0)
    return unit(acc)


def _two_way_partitions(V: np.ndarray, W: np.ndarray, min_size: int, iters: int = 6):
    """Sinh các phương án chia các vector (theo thứ tự thời gian) thành 2 cụm, tinh chỉnh bằng 2-means cầu.

    Khởi tạo: (a) mọi mốc thời gian "k câu cuối là người khác" - đúng tình huống người mới vào giữa chừng,
    (b) cặp vector khác nhau nhất. Trả về các mảng nhãn 0/1 không trùng nhau."""
    n = len(V)
    inits = []
    for k in range(min_size, n - min_size + 1):
        lab = np.zeros(n, dtype=int)
        lab[n - k:] = 1
        inits.append(lab)
    S = V @ V.T
    i, j = np.unravel_index(np.argmin(S), S.shape)
    inits.append((S[:, j] > S[:, i]).astype(int))
    seen = set()
    for lab in inits:
        for _ in range(iters):
            if lab.sum() in (0, n):
                break
            c0 = unit((V[lab == 0] * W[lab == 0, None]).sum(axis=0))
            c1 = unit((V[lab == 1] * W[lab == 1, None]).sum(axis=0))
            new = (V @ c1 > V @ c0).astype(int)
            if np.array_equal(new, lab):
                break
            lab = new
        key = lab.tobytes() if lab[0] == 0 else (1 - lab).tobytes()
        if key not in seen and 0 < lab.sum() < n:
            seen.add(key)
            yield lab


# ==============================================================================
# THEO DÕI NGƯỜI NÓI TRONG CUỘC HỌP (MeetingSpeakers)
# ==============================================================================
DEFAULT_LABEL_PREFIX = "Người nói"


def default_label(sid: int) -> str:
    return f"{DEFAULT_LABEL_PREFIX} {sid}"


def is_placeholder_name(name: Optional[str]) -> bool:
    """Nhãn tạm do hệ thống sinh ra (chưa phải tên thật)."""
    if not name:
        return True
    low = name.strip().lower()
    return any(low.startswith(p) for p in ("người nói", "người lạ", "speaker", "unknown", "đang nhận diện"))


@dataclass
class SpeakerProfile:
    """Hồ sơ một người nói trong cuộc họp (ổn định suốt buổi họp)."""
    sid: int
    name: str = ""                      # Tên thật (rỗng = chưa định danh)
    voice_id: Optional[int] = None      # Liên kết hồ sơ giọng nói toàn cục (collection voices)
    role: str = ""
    origin: str = "new"                 # new | voiceprint | manual | ai
    locked: bool = False                # Người dùng đã tự đặt tên -> AI không tự đổi nữa
    confidence: float = 0.0
    vsum: Optional[np.ndarray] = None   # Tổng có trọng số các vector (chưa chuẩn hóa)
    weight: float = 0.0                 # Tổng giây tiếng nói đã đóng góp vào centroid
    n_segments: int = 0
    speech_s: float = 0.0               # Tổng thời lượng nói (kể cả câu ngắn không có vector)
    first_t: float = 0.0
    last_t: float = 0.0
    merged_into: Optional[int] = None
    apart: List[int] = field(default_factory=list)   # Người dùng đã tách khỏi các hồ sơ này: không tự gộp lại

    @property
    def label(self) -> str:
        return self.name or default_label(self.sid)

    @property
    def named(self) -> bool:
        return bool(self.name)

    @property
    def active(self) -> bool:
        return self.merged_into is None

    def centroid(self) -> Optional[np.ndarray]:
        if self.vsum is None or self.weight <= 0:
            return None
        return unit(self.vsum)

    def to_dict(self, with_vector: bool = True) -> Dict[str, Any]:
        d = {
            "sid": self.sid,
            "label": self.label,
            "name": self.name,
            "voice_id": self.voice_id,
            "role": self.role,
            "origin": self.origin,
            "locked": self.locked,
            "confidence": round(float(self.confidence), 3),
            "weight": round(float(self.weight), 2),
            "n_segments": self.n_segments,
            "speech_s": round(float(self.speech_s), 2),
            "first_t": self.first_t,
            "last_t": self.last_t,
            "merged_into": self.merged_into,
            "apart": list(self.apart),
            "has_voice": self.vsum is not None and self.weight > 0,
        }
        if with_vector:
            c = self.centroid()
            d["centroid"] = c.tolist() if c is not None else None
        return d


class MeetingSpeakers:
    """Theo dõi người nói trực tuyến cho một buổi họp ($N$ người, 1 mic hoặc nhiều mic).

    Quy tắc phân vai cho mỗi câu mới:
    1. Nhãn Soniox (theo từng phiên stream - epoch) đã gắn với hồ sơ P -> mặc định là P.
       CAM++ chỉ ghi đè khi rất chắc chắn (giống người khác rõ rệt, hoặc câu dài mà khác hẳn P).
    2. Nhãn Soniox mới trong phiên -> Soniox cho rằng đây là người KHÁC những người đang có nhãn trong phiên.
       So vector với các hồ sơ còn lại (ví dụ người nói trước khi stream nối lại); không đủ giống -> hồ sơ mới.
    3. Không có nhãn Soniox (tắt diarization) -> gom cụm trực tuyến thuần CAM++ theo centroid.
    4. Câu không có vector -> theo nhãn Soniox; không có thì kế thừa người nói gần nhất.
    """
    # Ngưỡng cosine (đo trên dữ liệu thật: cùng người câu ngắn 0.3-0.6, câu dài 0.6-0.87; khác người 0.0-0.4)
    T_JOIN_SHORT = 0.42        # Gán câu (<2s tiếng nói) vào hồ sơ đã có khi không có nhãn Soniox hỗ trợ
    T_JOIN = 0.46              # ... câu 2-4s
    T_JOIN_LONG = 0.50         # ... câu >= 4s
    T_JOIN_BLOCKED = 0.64      # Soniox nói "người khác" nhưng CAM++ nói "cùng người" -> cần rất chắc chắn
    JOIN_MARGIN = 0.05         # Chênh lệch tối thiểu giữa hồ sơ giống nhất và nhì
    OVERRIDE_MIN = 0.60        # Ghi đè nhãn Soniox sang hồ sơ khác: cosine tối thiểu
    OVERRIDE_GAP = 0.15        # ... và phải hơn hồ sơ Soniox chỉ định ngần này
    SPLIT_MAX = 0.22           # Câu dài mà cosine với hồ sơ Soniox chỉ định thấp hơn mức này -> người mới
    SPLIT_MIN_W = 2.5          # ... chỉ xét khi câu có >= 2.5s tiếng nói
    # Người khác chen ngang nói DÀI dưới cùng nhãn Soniox: câu càng dài, vector càng ổn định nên ngưỡng tách nới dần.
    # Chỉ áp dụng khi hồ sơ đang nói "chặt" (các câu của họ giống centroid >= SPLIT_COHESION_MIN), tức câu lạ này
    # thực sự lệch khỏi giọng quen chứ không phải người đó nói không đều.
    SPLIT_MAX_LONG = 0.36      # câu >= 4s tiếng nói
    SPLIT_MAX_XLONG = 0.42     # câu >= 8s tiếng nói
    SPLIT_COHESION_MIN = 0.62
    RELIABLE_W = 4.0           # Hồ sơ có >= 4s tiếng nói thì centroid đủ tin cậy
    MERGE_T = 0.68             # Gộp 2 hồ sơ (cả hai đã đủ tin cậy)
    MERGE_T_WEAK = 0.74        # Gộp khi một hồ sơ còn ít dữ liệu
    MERGE_T_DISTINCT = 0.82    # Gộp 2 hồ sơ mà Soniox từng khẳng định là 2 người khác nhau
    ANCHOR_T = 0.52            # Gắn hồ sơ với mẫu giọng đã lưu (centroid đủ tin cậy)
    ANCHOR_T_WEAK = 0.58       # ... khi hồ sơ mới có ít dữ liệu (>= ANCHOR_MIN_W)
    ANCHOR_MARGIN = 0.06
    ANCHOR_MIN_W = 1.0
    # Tách hồ sơ: Soniox đôi khi dùng lại nhãn của một người cho người mới (ví dụ khách mời vừa xem video,
    # rồi người trong phòng nói). Từng câu lẻ không đủ để phân biệt (cùng phòng, cùng mic nên vẫn giống ~0.5),
    # nhưng các câu của người mới rất giống NHAU -> tách được khi đã đủ dữ liệu.
    SPLIT_CROSS_T = 0.65       # Centroid 2 cụm con giống nhau dưới mức này -> 2 người khác nhau
    SPLIT_MIN_SEGS = 3         # Mỗi cụm con cần >= 3 câu có vector
    SPLIT_MIN_W = 6.0          # ... và >= 6 giây tiếng nói
    SPLIT_COHESION_GAP = 0.12  # Mỗi cụm con phải "chặt" hơn độ giống giữa 2 cụm ít nhất ngần này
    SPLIT_WINDOW = 40          # Xét tối đa 40 câu có vector gần nhất của hồ sơ
    SPLIT_SAME_T = 0.86        # Tâm 2 cụm con giống từ mức này -> chắc chắn cùng một người, không tách
    SPLIT_MEMBER_GAP = 0.10    # Tách theo từng câu: câu gần cụm mình hơn cụm kia trung bình ngần này
    SPLIT_MEMBER_AGREE = 0.75  # ... và >= 75% (theo thời lượng) số câu gần cụm mình hơn
    # Đo trên 3 cuộc họp thật có người nói trực tiếp + podcast phát qua loa: đúng 82% -> 87% câu,
    # không đổi kết quả ở các cuộc họp khác (#15, #26, #30).
    # Tách theo từng câu chỉ dùng khi Soniox đã nghe thấy nhiều người trong phiên stream. Một người nói suốt buổi
    # (Soniox chỉ có một nhãn) mà giọng đổi dần (đổi tư thế, xa/gần mic) cũng tạo ra 2 cụm gần nhau như vậy:
    # lỗi thật #44 bị tách đôi từ phút 15 (tâm 2 cụm giống 0.75). Khi đó chỉ tách nếu 2 giọng khác hẳn nhau.
    MAX_W = 8.0                # Trọng số tối đa của một câu khi cộng vào centroid
    # Soniox dùng lại một nhãn cho nhiều người (podcast phát qua loa + người trong phòng). Câu quá ngắn không có
    # vector thì theo NGƯỜI VỪA ĐƯỢC XÁC ĐỊNH BẰNG GIỌNG gần nhất của nhãn đó, không theo tổng phiếu cả buổi.
    RECENT_LABEL_S = 60.0      # ... nếu câu có vector đó cách không quá ngần này giây
    BACKFILL_S = 20.0          # Câu có vector vừa xác định người nói -> các câu ngắn cùng nhãn ngay trước đó theo luôn
    MAX_HISTORY = 2000         # Số câu tối đa giữ trong RAM

    def __init__(self, anchors: Optional[Dict[int, Dict[str, Any]]] = None,
                 expected_host_id: Optional[int] = None):
        """anchors: {voice_id: {"name": str, "vector": np.ndarray, "role": str, "department": str}}"""
        self.anchors: Dict[int, Dict[str, Any]] = {}
        self.update_anchors(anchors or {}, rebind=False)
        self.host_id = expected_host_id if expected_host_id in self.anchors else None
        self.profiles: Dict[int, SpeakerProfile] = {}
        self.next_sid = 1
        self.segs: List[Dict[str, Any]] = []
        self.by_key: Dict[Any, Dict[str, Any]] = {}
        # Phiếu bầu của nhãn Soniox: (stream, epoch, raw) -> {sid: trọng số}
        self.raw_votes: Dict[Tuple[str, int, str], Dict[int, float]] = {}
        self.last_sid: Optional[int] = None
        self.dirty: Set[int] = set()          # Hồ sơ vừa thay đổi (để lưu DB / phát sự kiện)
        self.merges: List[Tuple[int, int, bool]] = []  # (src, dst, tự động?) chưa phát sự kiện
        self.splits: List[Tuple[int, int]] = []        # (hồ sơ gốc, hồ sơ mới tách ra) chưa phát sự kiện
        # Phiên stream mở khi đã có người nói từ trước (nối lại / mở lại mic): nhãn Soniox đánh số lại từ đầu
        self.epoch_fresh: Dict[Tuple[str, int], bool] = {}
        # Câu ngắn của nhãn Soniox chưa xác định trong phiên "fresh": gán tạm, chờ câu có vector để chốt
        self.pending_raw: Dict[Tuple[str, int, str], List[Any]] = {}
        self._now = 0.0                      # thời điểm (giây) của câu đang xử lý

    # ------------------------------------------------------------- anchors ---
    def update_anchors(self, new_anchors: Dict[int, Dict[str, Any]], rebind: bool = True):
        for vid, info in new_anchors.items():
            vec = info.get("vector")
            if vec is None or not is_valid_vector(vec):
                continue
            self.anchors[vid] = {**info, "vector": unit(np.asarray(vec, dtype=np.float32))}
        if rebind:
            for p in self.active_profiles():
                self._bind_anchor(p)

    def remove_anchor(self, voice_id: int):
        self.anchors.pop(voice_id, None)

    # ------------------------------------------------------------ helpers ---
    def active_profiles(self) -> List[SpeakerProfile]:
        return [p for p in self.profiles.values() if p.active]

    def visible_profiles(self) -> List[SpeakerProfile]:
        return [p for p in self.active_profiles() if p.n_segments > 0]

    def profile(self, sid: Optional[int]) -> Optional[SpeakerProfile]:
        p = self.profiles.get(sid) if sid is not None else None
        while p is not None and p.merged_into is not None:
            p = self.profiles.get(p.merged_into)
        return p

    def find_by_name(self, name: str, exclude: Optional[int] = None) -> Optional[SpeakerProfile]:
        key = (name or "").strip().casefold()
        if not key:
            return None
        for p in self.active_profiles():
            if p.sid != exclude and p.name and p.name.strip().casefold() == key:
                return p
        return None

    def find_by_label(self, label: str) -> Optional[SpeakerProfile]:
        for p in self.active_profiles():
            if p.label == label:
                return p
        return None

    def _new_profile(self, t: float = 0.0) -> SpeakerProfile:
        p = SpeakerProfile(sid=self.next_sid, first_t=t, last_t=t)
        self.profiles[p.sid] = p
        self.next_sid += 1
        self.dirty.add(p.sid)
        return p

    @staticmethod
    def _rkey(stream: str, epoch: int, raw: Optional[str]) -> Optional[Tuple[str, int, str]]:
        if raw is None or raw == "":
            return None
        return (str(stream), int(epoch), str(raw))

    def _mapped_sid(self, rk: Optional[Tuple[str, int, str]]) -> Optional[int]:
        if rk is None or rk not in self.raw_votes:
            return None
        votes = {self.profile(s).sid: 0.0 for s in self.raw_votes[rk] if self.profile(s)}
        for s, w in self.raw_votes[rk].items():
            p = self.profile(s)
            if p:
                votes[p.sid] += w
        if not votes:
            return None
        return max(votes.items(), key=lambda kv: kv[1])[0]

    def _blocked_sids(self, rk: Optional[Tuple[str, int, str]]) -> Set[int]:
        """Các hồ sơ mà Soniox đang gắn cho nhãn KHÁC trong cùng phiên stream (=> người khác)."""
        if rk is None:
            return set()
        out = set()
        for other in self.raw_votes:
            if other[0] == rk[0] and other[1] == rk[1] and other[2] != rk[2]:
                m = self._mapped_sid(other)
                if m is not None:
                    out.add(m)
        return out

    def _vote(self, rk, sid: int, w: float):
        if rk is None:
            return
        self.raw_votes.setdefault(rk, {})
        self.raw_votes[rk][sid] = self.raw_votes[rk].get(sid, 0.0) + max(w, 0.3)

    def _split_limit(self, p: "SpeakerProfile", w: float) -> float:
        """Ngưỡng "khác hẳn giọng hồ sơ p" cho một câu dài w giây: nới theo độ dài câu nếu p là hồ sơ chặt."""
        if w < 4.0 or self._cohesion(p) < self.SPLIT_COHESION_MIN:
            return self.SPLIT_MAX
        return self.SPLIT_MAX_XLONG if w >= 8.0 else self.SPLIT_MAX_LONG

    def _cohesion(self, p: "SpeakerProfile", n: int = 8) -> float:
        """Độ giống trung bình giữa các câu gần nhất của p và centroid của p (loại chính câu đó ra)."""
        c = p.centroid()
        if c is None or p.vsum is None:
            return 0.0
        vals = []
        for s in reversed(self.segs):
            if s["v"] is not None and s["w"] > 0 and self.profile(s["sid"]) is p:
                rest = p.vsum - s["v"] * s["w"]
                if float(np.linalg.norm(rest)) > 1e-6:
                    vals.append(float(s["v"] @ unit(rest)))
                if len(vals) >= n:
                    break
        return float(np.mean(vals)) if len(vals) >= 2 else 0.0

    def _t_join(self, w: float) -> float:
        if w < 2.0:
            return self.T_JOIN_SHORT
        if w < 4.0:
            return self.T_JOIN
        return self.T_JOIN_LONG

    # ------------------------------------------------------------ decide ---
    def _is_fresh(self, rk) -> bool:
        if rk is None:
            return False
        key = (rk[0], rk[1])
        if key not in self.epoch_fresh:
            self.epoch_fresh[key] = any(p.n_segments > 0 for p in self.active_profiles())
        return self.epoch_fresh[key]

    def _decide(self, v: Optional[np.ndarray], w: float, rk) -> Tuple[Optional[int], str]:
        """Trả về (sid hoặc None = tạo hồ sơ mới, lý do)."""
        mapped = self._mapped_sid(rk)
        fresh = self._is_fresh(rk)
        if v is None:
            recent = self._recent_voice_sid(rk)
            if recent is not None:
                return recent, "soniox_recent"
            if mapped is not None:
                return mapped, "soniox"
            if rk is not None and fresh:
                # Nhãn Soniox đã đánh số lại: chưa biết là ai -> tạm gán người không bị chặn gần nhất
                blocked = self._blocked_sids(rk)
                cands = sorted((p for p in self.active_profiles() if p.n_segments and p.sid not in blocked),
                               key=lambda p: p.last_t, reverse=True)
                if cands:
                    return cands[0].sid, "pending_soniox"
                return None, "soniox_new"
            if rk is not None:
                # Soniox gắn nhãn chưa từng gặp trong phiên -> người mới
                return None, "soniox_new"
            if self.last_sid is not None and self.profile(self.last_sid):
                return self.profile(self.last_sid).sid, "continuity"
            return None, "first"

        sims: Dict[int, float] = {}
        for p in self.active_profiles():
            c = p.centroid()
            if c is not None:
                sims[p.sid] = cosine_sim(v, c)
        ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)

        if mapped is not None:
            s_m = sims.get(mapped)
            if s_m is None:
                return mapped, "soniox"  # hồ sơ chưa có giọng: nhận vector đầu tiên
            best_sid, best = ranked[0]
            if best_sid != mapped and best >= max(self.OVERRIDE_MIN, s_m + self.OVERRIDE_GAP):
                return best_sid, "voice_override"
            p_m = self.profiles[mapped]
            if (w >= self.SPLIT_MIN_W and s_m < self._split_limit(p_m, w) and p_m.weight >= self.RELIABLE_W
                    and best < self._t_join(w)):
                return None, "voice_split"
            if w < 2.0 and s_m < self._t_join(w):
                # Câu ngắn, vector nhiễu, không giống người mà nhãn chỉ định: theo người vừa nói bằng nhãn này
                recent = self._recent_voice_sid(rk)
                if recent is not None and recent != mapped and sims.get(recent, -1.0) >= s_m:
                    return recent, "soniox_recent"
            return mapped, "soniox"

        if not ranked:
            # Chưa hồ sơ nào có giọng: không có diarization thì coi như người vừa nói đang nói tiếp
            if rk is None and self.last_sid is not None and self.profile(self.last_sid):
                return self.profile(self.last_sid).sid, "continuity"
            return None, "first"

        blocked = self._blocked_sids(rk)
        thr = self._t_join(w)
        free = [(s, x) for s, x in ranked if s not in blocked]
        if free:
            f_sid, f_sim = free[0]
            f_second = free[1][1] if len(free) > 1 else -1.0
            # Hồ sơ bị chặn giống hơn hẳn thì không gán nhầm sang hồ sơ tự do
            b_best = max((x for s, x in ranked if s in blocked), default=-1.0)
            if f_sim >= thr and (f_sim - f_second) >= self.JOIN_MARGIN and f_sim >= b_best - 0.02:
                return f_sid, "voice"
        b_ranked = [(s, x) for s, x in ranked if s in blocked]
        if b_ranked and b_ranked[0][1] >= self.T_JOIN_BLOCKED:
            return b_ranked[0][0], "voice_strong"
        return None, "new_voice"

    # --------------------------------------------------------------- add ---
    def add(self, key: Any, v: Optional[np.ndarray], raw_label: Any = None, t: float = 0.0,
            voiced: float = 1.0, text: str = "", epoch: int = 0, dur: Optional[float] = None,
            stream: str = "mic") -> Dict[Any, Tuple[str, Optional[int]]]:
        """Thêm câu mới và phân vai.

        Trả về {key: (speaker_label, voice_id)} cho câu mới và mọi câu cũ bị đổi nhãn (do gộp hồ sơ...).
        """
        if v is not None and not is_valid_vector(v):
            v = None
        if v is not None:
            v = unit(np.asarray(v, dtype=np.float32))
        w = 0.0 if v is None else min(max(float(voiced), 0.5), self.MAX_W)
        raw = None if raw_label is None or str(raw_label) in ("", "None") else str(raw_label)
        rk = self._rkey(stream, epoch, raw)
        before = {s["key"]: self._label_of(s) for s in self.segs}
        self._now = float(t)

        sid, reason = self._decide(v, w, rk)
        p = self.profile(sid) if sid is not None else None
        if p is None:
            p = self._new_profile(t)
        seg = {"key": key, "v": v, "w": w, "raw": raw, "rk": rk, "t": float(t),
               "dur": float(dur if dur is not None else max(voiced, 0.0)), "text": text,
               "sid": p.sid, "reason": reason}
        self._attach(seg, p)
        self.segs.append(seg)
        self.by_key[key] = seg
        if len(self.segs) > self.MAX_HISTORY:
            old = self.segs.pop(0)
            self.by_key.pop(old["key"], None)
        if reason == "pending_soniox":
            self.pending_raw.setdefault(rk, []).append(key)
        else:
            self._vote(rk, p.sid, w if w > 0 else 0.5)
            if rk in self.pending_raw:
                # Nhãn này vừa được chốt bằng giọng/Soniox -> chuyển các câu gán tạm về đúng người
                for k in self.pending_raw.pop(rk):
                    s = self.by_key.get(k)
                    if s is not None and self.profile(s["sid"]) is not p:
                        old_p = self.profile(s["sid"])
                        if old_p is not None:
                            self._detach(s, old_p)
                        s["sid"] = p.sid
                        self._attach(s, p)
                    if s is not None:
                        self._vote(rk, p.sid, 0.3)
        if v is not None and rk is not None:
            self._backfill_short(seg, p)
        self.last_sid = p.sid
        log.debug("meeting.voice: seg %s raw=%s -> %s (%s)", key, raw, p.label, reason)

        if v is not None:
            q = self._maybe_split(p)
            if q is not None:
                self.last_sid = self.profile(seg["sid"]).sid
                self._bind_anchor(q)
        self._bind_anchor(p)
        self._maybe_merge(p)

        changes: Dict[Any, Tuple[str, Optional[int]]] = {}
        for s in self.segs:
            lbl = self._label_of(s)
            if s["key"] == key or before.get(s["key"]) != lbl:
                prof = self.profile(s["sid"])
                changes[s["key"]] = (lbl, prof.voice_id if prof else None)
        return changes

    def _label_of(self, seg: Dict[str, Any]) -> str:
        p = self.profile(seg["sid"])
        return p.label if p else default_label(seg["sid"])

    def _attach(self, seg: Dict[str, Any], p: SpeakerProfile):
        v, w = seg["v"], seg["w"]
        if v is not None and w > 0:
            p.vsum = v * w if p.vsum is None else p.vsum + v * w
            p.weight += w
        p.n_segments += 1
        p.speech_s += seg.get("dur", 0.0)
        p.last_t = max(p.last_t, seg["t"])
        if p.n_segments == 1:
            p.first_t = seg["t"]
        self.dirty.add(p.sid)

    def _detach(self, seg: Dict[str, Any], p: SpeakerProfile):
        v, w = seg["v"], seg["w"]
        if v is not None and w > 0 and p.vsum is not None:
            p.vsum = p.vsum - v * w
            p.weight = max(0.0, p.weight - w)
            if p.weight <= 1e-6:
                p.vsum, p.weight = None, 0.0
        p.n_segments = max(0, p.n_segments - 1)
        p.speech_s = max(0.0, p.speech_s - seg.get("dur", 0.0))
        self.dirty.add(p.sid)

    # ------------------------------------------------------ anchor binding ---
    def _bind_anchor(self, p: SpeakerProfile):
        if not p.active or p.locked or p.voice_id is not None or not self.anchors:
            return
        c = p.centroid()
        if c is None or p.weight < self.ANCHOR_MIN_W:
            return
        bound = {q.voice_id: q for q in self.active_profiles() if q.voice_id is not None and q.sid != p.sid}
        ranked = sorted(((cosine_sim(c, a["vector"]), vid) for vid, a in self.anchors.items()), reverse=True)
        best, vid = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else -1.0
        thr = self.ANCHOR_T if p.weight >= self.RELIABLE_W else self.ANCHOR_T_WEAK
        if best < thr or (best - second) < self.ANCHOR_MARGIN:
            return
        if vid in bound:
            # Mẫu giọng đã gắn với hồ sơ khác: có thể là cùng một người bị tách đôi -> gộp nếu giọng gần nhau
            q = bound[vid]
            qc = q.centroid()
            if qc is not None and cosine_sim(c, qc) >= 0.45 and not (p.name and q.name and p.name != q.name):
                self.merge(p.sid, q.sid, auto=True)
            return
        if p.name and p.origin in ("manual", "ai") and p.name.casefold() != self.anchors[vid]["name"].casefold():
            return
        a = self.anchors[vid]
        p.voice_id = vid
        p.name = a["name"]
        p.role = a.get("role", "") or p.role
        p.origin = "voiceprint"
        p.confidence = best
        self.dirty.add(p.sid)
        log.info("meeting.voice: hồ sơ %d khớp mẫu giọng '%s' (cos=%.2f)", p.sid, p.name, best)

    # -------------------------------------------------------------- split ---
    def _recent_voice_sid(self, rk) -> Optional[int]:
        """Hồ sơ của câu có vector gần nhất mang nhãn Soniox rk (trong RECENT_LABEL_S giây)."""
        if rk is None:
            return None
        for s in reversed(self.segs):
            if self._now - s["t"] > self.RECENT_LABEL_S:
                return None
            if s["rk"] == rk and s["v"] is not None:
                prof = self.profile(s["sid"])
                return prof.sid if prof is not None else None
        return None

    def _backfill_short(self, seg: Dict[str, Any], p: "SpeakerProfile") -> None:
        """Câu có vector vừa xác định là người p: các câu ngắn liền trước cùng nhãn Soniox (đã gán theo nhãn,
        chưa có giọng) là cùng một lượt nói -> chuyển sang p."""
        for s in reversed(self.segs[:-1]):
            if s["rk"] != seg["rk"] or seg["t"] - s["t"] > self.BACKFILL_S:
                return
            if s["v"] is not None or s["reason"] != "soniox":   # câu đã theo người vừa nói (soniox_recent) thì giữ
                return
            old = self.profile(s["sid"])
            if old is not None and old is not p:
                self._detach(s, old)
                votes = self.raw_votes.get(s["rk"], {})
                votes[old.sid] = max(0.0, votes.get(old.sid, 0.0) - 0.5)
                s["sid"] = p.sid
                self._attach(s, p)
                self._vote(s["rk"], p.sid, 0.5)

    @staticmethod
    def _vote_w(seg: Dict[str, Any]) -> float:
        return max(seg["w"], 0.3) if seg["w"] > 0 else 0.5

    def _maybe_split(self, p: SpeakerProfile) -> Optional[SpeakerProfile]:
        """Tìm trong hồ sơ p hai cụm giọng khác hẳn nhau; cụm mới hơn trở thành người nói mới."""
        if not p.active:
            return None
        members = [s for s in self.segs if s["v"] is not None and s["reason"] != "manual"
                   and self.profile(s["sid"]) is p][-self.SPLIT_WINDOW:]
        n = len(members)
        if n < 2 * self.SPLIT_MIN_SEGS:
            return None
        V = np.stack([s["v"] for s in members]).astype(np.float64)
        W = np.array([s["w"] for s in members], dtype=np.float64)
        close_ok = not self._soniox_one_voice(members)
        best = None
        for lab in _two_way_partitions(V, W, self.SPLIT_MIN_SEGS):
            res = self._eval_partition(V, W, lab, close_ok)
            if res is not None and (best is None or res[0] < best[0]):
                best = (res[0], lab)
        if best is None:
            return None
        lab = best[1]
        t = np.array([s["t"] for s in members])
        newer = 1 if t[lab == 1].mean() > t[lab == 0].mean() else 0
        newer_keys = {members[i]["key"] for i in range(n) if lab[i] == newer}
        q = self._new_profile(float(t[lab == newer].min()))
        member_new = {members[i]["key"]: bool(lab[i] == newer) for i in range(n)}
        # Chia theo LƯỢT NÓI (chuỗi câu liền nhau của hồ sơ p, không bị người khác chen vào):
        # câu không có vector chỉ theo người mới khi cùng lượt với câu có vector của người mới.
        runs, cur = [], []
        for s in self.segs:
            if self.profile(s["sid"]) is p:
                cur.append(s)
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
        moved = []
        for run in runs:
            voted = [(s["t"], member_new[s["key"]]) for s in run if s["key"] in member_new]
            if not voted or not any(x for _, x in voted):
                continue
            for s in run:
                if s["reason"] == "manual":
                    continue
                to_new = member_new[s["key"]] if s["key"] in member_new else \
                    min(voted, key=lambda a: abs(a[0] - s["t"]))[1]
                if to_new:
                    self._detach(s, p)
                    s["sid"] = q.sid
                    self._attach(s, q)
                    moved.append(s)
        # Tính lại phiếu Soniox của các nhãn liên quan theo người nói thực tế của từng câu
        for rk in {s["rk"] for s in moved if s["rk"] is not None}:
            votes: Dict[int, float] = {}
            same_rk = [s for s in self.segs if s["rk"] == rk]
            for s in same_rk:
                sid = self.profile(s["sid"]).sid
                votes[sid] = votes.get(sid, 0.0) + self._vote_w(s)
            # Soniox đang dùng nhãn này cho người mới -> câu ngắn tiếp theo mặc định là người mới
            if same_rk and self.profile(max(same_rk, key=lambda s: s["t"])["sid"]) is q:
                votes[q.sid] = max(votes.get(q.sid, 0.0), votes.get(p.sid, 0.0) + 1.0)
            self.raw_votes[rk] = votes
        # Tách dựa trên bằng chứng từng câu (mạnh hơn so tâm cụm khi tự gộp): không tự gộp lại, tránh vòng tách-gộp
        p.apart.append(q.sid)
        q.apart.append(p.sid)
        self.splits.append((p.sid, q.sid))
        self.dirty.update({p.sid, q.sid})
        log.info("meeting.voice: tách %d câu khỏi hồ sơ %d thành người nói mới %d (cos=%.2f)",
                 len(moved), p.sid, q.sid, best[0])
        return q

    def _soniox_one_voice(self, members: List[Dict[str, Any]]) -> bool:
        """Soniox chưa từng nghe thấy người thứ hai trong các phiên stream chứa các câu đang xét: mọi câu của các
        phiên đó (cả câu ngắn không có vector) chỉ mang một nhãn."""
        if not members or any(s["rk"] is None for s in members):
            return False
        sessions = {s["rk"][:2] for s in members}
        seen: Dict[Tuple[str, int], Set[str]] = {}
        for s in self.segs:
            rk = s["rk"]
            if rk is not None and rk[:2] in sessions:
                seen.setdefault(rk[:2], set()).add(rk[2])
        return all(len(x) == 1 for x in seen.values())

    def _eval_partition(self, V: np.ndarray, W: np.ndarray, lab: np.ndarray,
                        close_ok: bool = True) -> Optional[Tuple[float, float]]:
        """Trả về (độ giống giữa 2 cụm, độ chặt nhỏ nhất) nếu phân chia hợp lệ.
        close_ok=False: chỉ chấp nhận 2 giọng khác hẳn nhau (a), không tách 2 giọng gần nhau (b)."""
        idx = [np.where(lab == c)[0] for c in (0, 1)]
        if min(len(i) for i in idx) < self.SPLIT_MIN_SEGS or min(W[i].sum() for i in idx) < self.SPLIT_MIN_W:
            return None
        sums = [(V[i] * W[i, None]).sum(axis=0) for i in idx]
        cents = [unit(s) for s in sums]
        cross = float(cents[0] @ cents[1])
        if cross >= self.SPLIT_SAME_T:
            return None
        # (a) Hai cụm khác hẳn nhau ở mức tâm cụm
        if cross < self.SPLIT_CROSS_T:
            coh = min(float(np.mean([V[j] @ unit(sums[c] - V[j] * W[j]) for j in idx[c]])) for c in (0, 1))
            if coh >= cross + self.SPLIT_COHESION_GAP:
                return cross, coh
        if not close_ok:
            return None
        # (b) Hai giọng gần nhau (tâm cụm vẫn giống ~0.7-0.8, ví dụ cùng phát qua loa) nhưng TỪNG CÂU vẫn gần
        # cụm của mình hơn hẳn cụm kia. Tâm cụm lớn bị "làm mượt" nên so tâm-tâm đánh giá quá cao độ giống.
        gaps, agrees = [], []
        for c in (0, 1):
            own = np.array([V[j] @ unit(sums[c] - V[j] * W[j]) for j in idx[c]])
            other = V[idx[c]] @ cents[1 - c]
            gaps.append(float(np.average(own - other, weights=W[idx[c]])))
            agrees.append(float(np.average(own > other, weights=W[idx[c]])))
        if min(gaps) >= self.SPLIT_MEMBER_GAP and min(agrees) >= self.SPLIT_MEMBER_AGREE:
            return cross, min(gaps)
        return None

    def pop_splits(self) -> List[Tuple[int, int]]:
        out, self.splits = self.splits, []
        return out

    # -------------------------------------------------------------- merge ---
    def _soniox_distinct(self, a: int, b: int) -> bool:
        """Soniox từng gắn 2 hồ sơ này cho 2 nhãn khác nhau trong cùng một phiên stream."""
        seen: Dict[Tuple[str, int], Set[int]] = {}
        for rk in self.raw_votes:
            m = self._mapped_sid(rk)
            if m in (a, b):
                seen.setdefault((rk[0], rk[1]), set()).add(m)
        return any(len(s) == 2 for s in seen.values())

    def _maybe_merge(self, p: SpeakerProfile):
        if not p.active:
            return
        c = p.centroid()
        if c is None:
            return
        for q in self.active_profiles():
            if q.sid == p.sid:
                continue
            qc = q.centroid()
            if qc is None:
                continue
            if p.name and q.name and p.name.casefold() != q.name.casefold():
                continue
            if p.voice_id is not None and q.voice_id is not None and p.voice_id != q.voice_id:
                continue
            if q.sid in p.apart or p.sid in q.apart:
                continue
            s = cosine_sim(c, qc)
            thr = self.MERGE_T if min(p.weight, q.weight) >= self.RELIABLE_W else self.MERGE_T_WEAK
            if self._soniox_distinct(p.sid, q.sid):
                thr = max(thr, self.MERGE_T_DISTINCT)
            if s >= thr:
                src, dst = (p, q) if p.sid > q.sid else (q, p)
                log.info("meeting.voice: tự gộp hồ sơ %d vào %d (cos=%.2f)", src.sid, dst.sid, s)
                self.merge(src.sid, dst.sid, auto=True)
                return

    def merge(self, src_sid: int, dst_sid: int, auto: bool = False) -> List[Any]:
        """Gộp hồ sơ src vào dst (auto=True: hệ thống tự gộp do giọng trùng). Trả về key các câu bị đổi người nói."""
        src, dst = self.profile(src_sid), self.profile(dst_sid)
        if src is None or dst is None or src.sid == dst.sid:
            return []
        moved = []
        for s in self.segs:
            if self.profile(s["sid"]) is src:
                s["sid"] = dst.sid
                moved.append(s["key"])
        if src.vsum is not None:
            dst.vsum = src.vsum.copy() if dst.vsum is None else dst.vsum + src.vsum
            dst.weight += src.weight
        dst.n_segments += src.n_segments
        dst.speech_s += src.speech_s
        dst.first_t = min(dst.first_t, src.first_t) if dst.n_segments else src.first_t
        dst.last_t = max(dst.last_t, src.last_t)
        if not dst.name and src.name:
            dst.name, dst.voice_id, dst.role = src.name, src.voice_id, src.role
            dst.origin, dst.locked, dst.confidence = src.origin, src.locked, src.confidence
        elif dst.voice_id is None and src.voice_id is not None:
            dst.voice_id = src.voice_id
        dst.locked = dst.locked or src.locked
        for rk, votes in self.raw_votes.items():
            if src.sid in votes:
                votes[dst.sid] = votes.get(dst.sid, 0.0) + votes.pop(src.sid)
        src.merged_into = dst.sid
        src.vsum, src.weight, src.n_segments, src.speech_s = None, 0.0, 0, 0.0
        if self.last_sid == src.sid:
            self.last_sid = dst.sid
        self.dirty.update({src.sid, dst.sid})
        self.merges.append((src.sid, dst.sid, auto))
        return moved

    # ------------------------------------------------------ user actions ---
    def rename(self, sid: int, name: str, voice_id: Optional[int] = None, role: str = "",
               origin: str = "manual", confidence: float = 1.0) -> Tuple[Optional[SpeakerProfile], List[Any]]:
        """Đặt tên cho hồ sơ. Nếu tên trùng một hồ sơ khác trong buổi họp -> gộp (cùng một người).

        Trả về (hồ sơ sau cùng, danh sách key câu bị đổi nhãn)."""
        p = self.profile(sid)
        name = (name or "").strip()
        if p is None or not name:
            return None, []
        twin = self.find_by_name(name, exclude=p.sid)
        moved: List[Any] = []
        if twin is not None:
            moved = self.merge(p.sid, twin.sid)
            p = twin
        p.name = name
        if voice_id is not None:
            p.voice_id = voice_id
        if role:
            p.role = role
        p.origin = origin
        p.confidence = confidence
        p.locked = p.locked or origin == "manual"
        self.dirty.add(p.sid)
        keys = [s["key"] for s in self.segs if self.profile(s["sid"]) is p]
        return p, sorted(set(keys) | set(moved), key=lambda k: (str(type(k)), k))

    def reassign(self, key: Any, target_sid: Optional[int]) -> Tuple[Optional[SpeakerProfile], List[int]]:
        """Người dùng sửa người nói của MỘT câu. target_sid=None -> tạo người nói mới.

        Trả về (hồ sơ đích, danh sách sid bị ảnh hưởng)."""
        seg = self.by_key.get(key)
        if seg is None:
            return None, []
        old = self.profile(seg["sid"])
        dst = self.profile(target_sid) if target_sid is not None else self._new_profile(seg["t"])
        if dst is None or (old is not None and old.sid == dst.sid):
            return dst, []
        if old is not None:
            self._detach(seg, old)
            if seg["rk"] is not None and seg["rk"] in self.raw_votes:
                votes = self.raw_votes[seg["rk"]]
                votes[old.sid] = max(0.0, votes.get(old.sid, 0.0) - max(seg["w"], 0.3))
        seg["sid"] = dst.sid
        seg["reason"] = "manual"
        self._attach(seg, dst)
        self._vote(seg["rk"], dst.sid, seg["w"] if seg["w"] > 0 else 0.5)
        affected = [dst.sid] + ([old.sid] if old else [])
        return dst, affected

    def split_from(self, key: Any) -> Tuple[Optional[SpeakerProfile], List[Any]]:
        """Người dùng chỉ ra "từ câu này trở đi là người khác": mọi câu từ đây về sau của hồ sơ sang người nói mới.

        Dùng khi hai người có giọng quá giống nhau (ví dụ cùng phát qua loa) bị gộp chung một hồ sơ.
        Trả về (hồ sơ mới, các câu đã chuyển); (None, []) nếu đây là câu đầu tiên của hồ sơ."""
        seg = self.by_key.get(key)
        p = self.profile(seg["sid"]) if seg else None
        if p is None:
            return None, []
        mine = [s for s in self.segs if self.profile(s["sid"]) is p]
        moved = [s for s in mine if s["t"] >= seg["t"]]
        if not moved or len(moved) == len(mine):
            return None, []
        q = self._new_profile(seg["t"])
        for s in moved:
            self._detach(s, p)
            s["sid"] = q.sid
            s["reason"] = "manual"
            self._attach(s, q)
        p.apart.append(q.sid)
        q.apart.append(p.sid)
        # Nhãn Soniox các câu vừa chuyển: tính lại phiếu theo người nói thực tế, câu gần nhất quyết định câu ngắn sắp tới
        for rk in {s["rk"] for s in moved if s["rk"] is not None}:
            same = [s for s in self.segs if s["rk"] == rk]
            votes: Dict[int, float] = {}
            for s in same:
                sid = self.profile(s["sid"]).sid
                votes[sid] = votes.get(sid, 0.0) + self._vote_w(s)
            latest = self.profile(max(same, key=lambda s: s["t"])["sid"])
            if latest is q:
                votes[q.sid] = max(votes.get(q.sid, 0.0), votes.get(p.sid, 0.0) + 1.0)
            self.raw_votes[rk] = votes
        self.dirty.update({p.sid, q.sid})
        return q, [s["key"] for s in moved]

    def forget_voice(self, voice_id: int):
        """Hồ sơ giọng toàn cục bị xóa: bỏ liên kết nhưng giữ tên trong buổi họp."""
        self.remove_anchor(voice_id)
        for p in self.active_profiles():
            if p.voice_id == voice_id:
                p.voice_id = None
                self.dirty.add(p.sid)

    # ---------------------------------------------------------- queries ---
    def peek(self, raw_label: Any, epoch: int = 0, stream: str = "mic") -> Optional[SpeakerProfile]:
        """Đoán nhanh hồ sơ theo nhãn Soniox (dùng cho chữ đang nói - interim)."""
        raw = None if raw_label is None else str(raw_label)
        sid = self._mapped_sid(self._rkey(stream, epoch, raw))
        return self.profile(sid) if sid is not None else None

    def sid_of(self, key: Any) -> Optional[int]:
        seg = self.by_key.get(key)
        p = self.profile(seg["sid"]) if seg else None
        return p.sid if p else None

    def keys_of(self, sid: int) -> List[Any]:
        p = self.profile(sid)
        return [s["key"] for s in self.segs if p is not None and self.profile(s["sid"]) is p]

    def vectors_of(self, sid: int) -> Tuple[List[np.ndarray], List[float]]:
        p = self.profile(sid)
        vs, ws = [], []
        for s in self.segs:
            if p is not None and s["v"] is not None and self.profile(s["sid"]) is p:
                vs.append(s["v"])
                ws.append(s["w"])
        return vs, ws

    def pop_dirty(self) -> List[SpeakerProfile]:
        out = [self.profiles[s] for s in sorted(self.dirty) if s in self.profiles]
        self.dirty.clear()
        return out

    def pop_merges(self) -> List[Tuple[int, int, bool]]:
        out, self.merges = self.merges, []
        return out

    # ------------------------------------------------ persistence (DB) ---
    def export_profiles(self) -> List[Dict[str, Any]]:
        return [p.to_dict(with_vector=True) for p in self.profiles.values()]

    def load_state(self, profiles: Iterable[Dict[str, Any]], segments: Iterable[Dict[str, Any]]):
        """Khôi phục trạng thái từ DB (server khởi động lại / mở lại phòng họp).

        profiles: bản ghi meeting_speakers. segments: bản ghi meeting_segments (theo thứ tự thời gian).
        Phiên Soniox cũ đã đóng nên nhãn raw không được dùng lại (mỗi lần mở stream là một epoch mới)."""
        for d in profiles:
            sid = int(d["sid"])
            p = SpeakerProfile(
                sid=sid, name=d.get("name") or "", voice_id=d.get("voice_id"), role=d.get("role") or "",
                origin=d.get("origin") or "new", locked=bool(d.get("locked")),
                confidence=float(d.get("confidence") or 0.0), merged_into=d.get("merged_into"),
                apart=[int(x) for x in (d.get("apart") or [])],
                first_t=float(d.get("first_t") or 0.0), last_t=float(d.get("last_t") or 0.0),
            )
            self.profiles[sid] = p
            self.next_sid = max(self.next_sid, sid + 1)
        legacy: Dict[str, int] = {}
        for s in segments:
            sid = s.get("speaker_key")
            if sid is None:
                # Dữ liệu cũ (trước khi có hồ sơ): mỗi nhãn là một người
                lbl = s.get("speaker_label") or default_label(0)
                if lbl not in legacy:
                    p = self.find_by_name(lbl) if not is_placeholder_name(lbl) else None
                    if p is None:
                        p = self._new_profile(float(s.get("t_start") or 0.0))
                        if not is_placeholder_name(lbl):
                            p.name, p.voice_id, p.origin = lbl, s.get("speaker_id"), "manual"
                    legacy[lbl] = p.sid
                sid = legacy[lbl]
            sid = int(sid)
            if sid not in self.profiles:
                self.profiles[sid] = SpeakerProfile(sid=sid)
                self.next_sid = max(self.next_sid, sid + 1)
            p = self.profile(sid) or self.profiles[sid]
            emb = s.get("raw_embedding")
            v = unit(np.asarray(emb, dtype=np.float32)) if emb and is_valid_vector(emb) else None
            dur = max(0.0, float(s.get("t_end") or 0.0) - float(s.get("t_start") or 0.0))
            w = 0.0 if v is None else min(max(float(s.get("voiced") or dur * 0.7), 0.5), self.MAX_W)
            seg = {"key": s.get("seq", s.get("id")), "v": v, "w": w, "raw": None, "rk": None,
                   "t": float(s.get("t_start") or 0.0), "dur": dur, "text": s.get("text", ""),
                   "sid": p.sid, "reason": "restored"}
            self._attach(seg, p)
            self.segs.append(seg)
            self.by_key[seg["key"]] = seg
            self.last_sid = p.sid
        self.dirty.clear()
        self.merges.clear()

    # ------------------------------------------------ compat (API cũ) ---
    @property
    def decided(self) -> Dict[Any, str]:
        return {s["key"]: self._label_of(s) for s in self.segs}

    @property
    def speaker_ids(self) -> Dict[Any, Optional[int]]:
        out = {}
        for s in self.segs:
            p = self.profile(s["sid"])
            out[s["key"]] = p.voice_id if p else None
        return out

    def override_speaker(self, old_label: str, new_name: str, new_vid: Optional[int] = None):
        p = self.find_by_label(old_label)
        if p is not None:
            self.rename(p.sid, new_name, voice_id=new_vid, origin="manual")

    def get_cluster_vectors(self, label: str) -> List[np.ndarray]:
        p = self.find_by_label(label)
        return self.vectors_of(p.sid)[0] if p else []
