"""Nhận ra người phỏng vấn bằng GIỌNG, từng câu một.

Vì sao có file này: Soniox realtime chỉ có diarization ẩn danh và hay tráo
nhãn khi hai giọng na ná nhau ngồi chung một mic. Gặp thật 24/09: giọng mẫu
được gán nhãn "1", nhưng khi giọng thứ hai xuất hiện thì Soniox đổi, giọng
người phỏng vấn thành "2" - mọi thứ dựa trên nhãn (kể cả mồi giọng) sai theo.

Nên không tin nhãn nữa. Cắt audio của TỪNG câu vừa chốt, tính vector đặc
trưng giọng (speaker embedding), gom các câu giống nhau thành cụm, rồi dùng
giọng đã lưu để gọi tên từng cụm (xem Speakers). Quyết định theo giọng thì
Soniox tráo nhãn giữa chừng cũng không sao.

Model: CAM++ "zh_en common advanced" của 3D-Speaker (huấn luyện trên kho lớn tiếng Trung + tiếng
Anh), chạy qua sherpa-onnx trên CPU, không gọi API nào. Thay bản CAM++ chỉ tiếng Trung từ 07/10/2026:
cùng kích thước và tốc độ, nhưng trên VIVOS qua phòng giả lập EER giọng lưu - câu trong phòng giảm từ
9,2% xuống 4,4% và bench/voice_id.py đúng 97,3% -> 98,0% câu. Thiếu thư viện hoặc file model thì
available() trả False và app lùi về mồi giọng cho Soniox như trước.

Vector của hai model khác nhau KHÔNG so với nhau được, kể cả cùng 192 chiều: mỗi giọng lưu ghi MODEL_ID
của model đã tính nó, và lúc khởi động app xoá giọng của model khác (app.py, db.prune_voices).

Tải model (một lần, ~28MB; Render làm trong buildCommand):
    python -m interview.voice download
"""
import logging
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("interview.voice")

HERE = Path(__file__).resolve().parent
MODEL_URL = os.getenv(
    "VOICE_MODEL_URL",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
    "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")
# PHÂN VAI DÙNG ĐÚNG MODEL ĐANG CHẠY CỦA MEETING: bám theo meeting.voice (trên Render là ERes2NetV2, máy local có thể
# là CAM++). Nhờ vậy vector của buổi phỏng vấn cùng không gian với registry `voices` + cách Meeting nhận giọng, không
# phải đoán. Thiếu file model của Meeting mới tải bản dự phòng.
try:
    from meeting import voice as _mv
    MODEL_PATH = _mv.MODEL_PATH if _mv.MODEL_PATH.exists() else Path(HERE / "models" / MODEL_URL.rsplit("/", 1)[-1])
    MODEL_ID = _mv.MODEL_ID
except Exception:
    MODEL_PATH = Path(HERE / "models" / MODEL_URL.rsplit("/", 1)[-1])
    MODEL_ID = MODEL_PATH.stem
RATE = 16000
MIN_SEG_S = 1.0          # câu có ít tiếng nói hơn ("dạ", "vâng"): vector không đáng tin, để nhãn quyết
MIN_ANCHOR_S = 3.0       # giọng mẫu phải có ít nhất ngần này giây tiếng nói thật
VOICED_RMS = 300         # khung 30ms to hơn ngần này (~ -40 dBFS) mới tính là có tiếng nói

_ex = None
_dl = {"running": False, "error": ""}      # trạng thái tự tải model lúc khởi động
_warm = {"running": False, "done": False, "error": ""}   # nạp model vào RAM
_ex_lock = threading.Lock()


def _lib_error() -> str:
    try:
        import numpy  # noqa: F401
        import sherpa_onnx  # noqa: F401
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return ""


def _have_model() -> bool:
    return MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000


def available() -> bool:
    return not _lib_error() and _have_model()


def status() -> str:
    """Lý do CỤ THỂ khi chưa so giọng được - để trang Giọng nói nói đúng việc
    cần làm, thay vì một câu chung chung bắt người dùng đoán."""
    err = _lib_error()
    if err:
        return f"thiếu thư viện (sherpa-onnx/numpy chưa cài: {err})"
    if _have_model():
        if _warm["error"]:
            return f"nạp model hỏng: {_warm['error']}"
        return "ok" if _warm["done"] else "đang nạp model"
    if _dl["running"]:
        return "đang tải model"
    if _dl["error"]:
        return f"tải model hỏng: {_dl['error']}"
    return "chưa có file model"


def ready() -> bool:
    """Model đã tải VÀ đã nạp vào RAM - câu đầu tiên của buổi so được ngay."""
    return available() and _warm["done"]


def pending() -> bool:
    """Đang tải hoặc đang nạp model - chờ thì sẽ có, khác với hỏng hẳn."""
    if _lib_error():
        return False
    return _dl["running"] or _warm["running"] or (_have_model() and not _warm["done"] and not _warm["error"])


def ensure_model_async():
    """Tải (nếu thiếu) rồi NẠP model ở luồng nền ngay lúc server khởi động.

    Vì sao cần: Render free tắt instance sau ~15 phút không ai gọi, filesystem
    lại là tạm. Mỗi lần thức dậy là một lần khởi động mới: tải lại ~28MB nếu
    Build Command không có bước tải (service tạo tay trên Dashboard không chạy
    buildCommand của render.yaml - gặp thật 24/09), rồi nạp model vào RAM mất
    vài chục giây trên CPU nhỏ. Trước đây việc nạp chờ tới câu đầu tiên của buổi
    đầu tiên: các câu đầu so trễ, còn buổi tạo lúc model đang tải thì tắt hẳn so
    giọng. Giờ làm luôn lúc khởi động, và ws_audio/ws_ext chờ ready() rồi mới mở
    mic (live.wait_voice).
    """
    if _lib_error() or _dl["running"] or _warm["running"] or _warm["done"]:
        return
    if not _have_model() and os.getenv("VOICE_AUTO_DOWNLOAD", "1") == "0":
        return

    def run():
        if not _have_model():
            _dl["running"], _dl["error"] = True, ""
            try:
                download()
                log.info("voice: đã tự tải model %s", MODEL_PATH)
            except Exception as e:
                _dl["error"] = f"{type(e).__name__}: {e}"
                log.warning("voice: tự tải model hỏng (%s)", _dl["error"])
                # Không có gì để nạp: phải hạ cờ, không thì pending() True mãi và mọi
                # buổi đứng chờ một model không bao giờ tới.
                _warm["running"] = False
                return
            finally:
                _dl["running"] = False
        warmup()

    _warm["running"] = True                 # đặt trước khi mở luồng: pending() đúng ngay
    threading.Thread(target=run, name="voice-model-load", daemon=True).start()


def warmup():
    """Nạp model và chạy thử một lần (lần chạy đầu của onnxruntime chậm hơn hẳn)."""
    import numpy as np
    _warm["running"], _warm["error"] = True, ""
    t = time.monotonic()
    try:
        ex = _extractor()
        st = ex.create_stream()
        x = np.sin(np.arange(RATE * 2) * 2 * np.pi * 150 / RATE).astype(np.float32) * 0.3
        st.accept_waveform(RATE, x)
        st.input_finished()
        if ex.is_ready(st):
            ex.compute(st)
        _warm["done"] = True
        log.info("voice: model sẵn sàng sau %.1fs", time.monotonic() - t)
    except Exception as e:
        _warm["error"] = f"{type(e).__name__}: {e}"
        log.warning("voice: nạp model hỏng (%s)", _warm["error"])
    finally:
        _warm["running"] = False


def _extractor():
    global _ex
    with _ex_lock:                          # luồng nạp lúc khởi động và VOICE_POOL có thể gọi cùng lúc
        if _ex is None:
            import sherpa_onnx
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL_PATH), num_threads=1)
            if not cfg.validate():
                raise RuntimeError(f"model nhận diện giọng không hợp lệ: {MODEL_PATH}")
            _ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        return _ex


def voiced_s(pcm: bytes) -> float:
    """Số giây có tiếng nói thật trong đoạn audio.

    Model trả vector cho CẢ im lặng lẫn tiếng xì (đo 24/09) - không lọc thì
    một giọng mẫu thu hỏng sẽ thành hồ sơ người phỏng vấn, và mọi câu sau
    được đem so với rác.
    """
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    fr = int(RATE * 0.03)
    n = len(x) // fr
    if n == 0:
        return 0.0
    rms = np.sqrt(np.mean(x[:n * fr].reshape(n, fr) ** 2, axis=1))
    return float(np.count_nonzero(rms >= VOICED_RMS)) * 0.03


def embed(pcm: bytes, min_voiced: float = MIN_SEG_S):
    """PCM16 16kHz mono -> vector giọng đã chuẩn hoá độ dài, hoặc None nếu có
    quá ít tiếng nói thật để tin.

    Chạy trên CPU và chặn luồng - gọi qua executor, đừng gọi thẳng trong event loop.
    """
    import numpy as np
    if len(pcm) < min_voiced * RATE * 2 or voiced_s(pcm) < min_voiced:
        return None
    ex = _extractor()
    st = ex.create_stream()
    st.accept_waveform(RATE, np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0)
    st.input_finished()
    if not ex.is_ready(st):
        return None
    v = np.asarray(ex.compute(st), dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def _unit(v):
    import numpy as np
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def as_vector(xs):
    """list số lưu trong DB -> vector numpy đã chuẩn hoá."""
    import numpy as np
    return _unit(np.asarray(xs, dtype=np.float32))


def merge(old, n: int, new):
    """Gộp thêm một lần thu mẫu vào vector đã lưu (trung bình có trọng số).

    Thu thêm ở mic khác, phòng khác, lúc khác làm vector bền hơn với điều kiện
    thật - một mẫu đọc duy nhất thì quá "sạch" so với giọng nói chuyện.
    """
    return _unit(old * n + new)


CAND = "?"               # khoá cho giọng KHÔNG có trong danh sách = ứng viên
ANY = "*"                # rõ là một người quen, nhưng không phân được là ai (giọng lưu quá giống nhau)


class Speakers:
    """Ai nói câu nào trong MỘT buổi: gom cụm các câu theo giọng, rồi gọi tên cụm.

    Làm lại từ đầu mỗi khi có câu mới (tối đa MAX câu gần nhất, vài mili giây):

    1. GOM CỤM các câu trong buổi theo độ giống NHAU. Mọi câu cùng qua một mic,
       một phòng, nên so với nhau là công bằng. Bước này không dùng giọng đã lưu.
    2. GỌI TÊN từng cụm bằng giọng đã lưu, so TƯƠNG ĐỐI giữa các cụm: cụm giống
       giọng lưu của X nhất (và đủ giống) là X; cụm kém hẳn mọi người quen là ứng viên.

    Vì sao không so từng câu với giọng lưu như bản trước: giọng lưu thu ở chỗ khác
    (trang Giọng nói, lúc khác, có khi mic khác). Đo trên 19 người thật (VIVOS,
    24/09): mẫu thu sạch so với chính người đó nói trong phòng họp chỉ giống ~0,42,
    trong khi hai câu của cùng người trong phòng giống ~0,64. Hồ sơ nào dựng từ câu
    trong phòng cũng thắng giọng lưu, KỂ CẢ khi đó là giọng chủ phòng - bản trước
    dựng "hồ sơ ứng viên" như thế rồi gán mọi câu của chủ phòng cho ứng viên (buổi
    #22: chủ phòng thử mic một mình, Soniox tách làm hai nhãn). Giả lập 1.560 buổi
    bằng giọng thật (bench/voice_id.py): chỉ chủ phòng nói, Soniox tách nhãn - bản
    trước đúng 45%, bản này 99,9%; tính mọi loại buổi: 91,9% -> 97,3%.

    Nguyên tắc chống gán nhầm:
    - Chỉ gọi ai là ứng viên khi đã nhận ra ít nhất một người quen làm mốc, và cụm
      đó kém mốc rõ ràng. Một giọng duy nhất trong buổi không bao giờ thành ứng viên
      chỉ vì "không giống giọng lưu lắm".
    - Người chắc chắn có mặt (`expected`: chủ phòng trên mic của chính họ) được
      nhận ở ngưỡng thấp. Người có thể vắng (người phỏng vấn cùng, mọi người trên
      luồng tab) cần ngưỡng cao hoặc đối chứng rõ: gọi nhầm ứng viên thành người
      phỏng vấn là mất câu trả lời lúc chấm điểm.
    - Cụm đã gọi tên một người thì không làm đối chứng cho người khác nữa: "giống
      đồng nghiệp hơn cụm của chủ phòng" không có nghĩa là đồng nghiệp.
    - Rõ là người quen nhưng hai giọng lưu quá giống nhau thì là ANY - vẫn là người
      phỏng vấn, vẫn làm mốc để nhận ra ứng viên, chỉ không gọi tên.

    Câu không có vector (quá ngắn) hoặc thuộc cụm chưa chắc thì theo các câu cùng
    nhãn Soniox gần nhất về thời gian: nhãn Soniox hay tráo về lâu dài, nhưng trong
    vài chục giây vẫn tách được người.

    Mọi ngưỡng dưới đây chỉnh bằng bench/voice_id.py (giọng thật + phòng giả lập),
    KHÔNG bằng giọng TTS: giọng TTS giống nhau hơn người thật nhiều, ngưỡng cũ chỉnh
    trên TTS chính là lý do bản trước hỏng. Ngưỡng gắn với MODEL: mỗi model có thang
    cosine riêng. Số hiện tại cho CAM++ zh_en advanced (07/10), quy đổi từ ngưỡng của
    bản CAM++ cũ theo trung bình cùng/khác người đo trên cache VIVOS; quét quanh đó
    chỉ hơn 0,1 điểm (trong mức nhiễu) nên giữ bản quy đổi.
    """
    MERGE = 0.54         # hai cụm giống nhau trung bình ngần này thì gộp. Chung một phòng, hai người
                         # KHÁC nhau trung bình ~0,33, cùng người ~0,70 (kênh thu chung kéo cả hai lên)
    LABEL_PEN = 0.06     # hai câu cách nhau < LABEL_WIN giây mà Soniox gán nhãn khác: bớt
    LABEL_WIN = 60.0     # ngần này độ giống khi gom cụm - tín hiệu phụ, giọng vẫn quyết
    SOLID_S = 4.0        # cụm có >= 2 câu hoặc ngần này giây tiếng nói mới được gọi tên
    FLOOR_HOST = 0.30    # người chắc chắn có mặt: cụm giống giọng lưu ngần này là họ
    FLOOR_OTHER = 0.62   # người có thể vắng: ngần này khi không có cụm nào để đối chứng
    FLOOR_REL = 0.24     # có đối chứng tương đối: mức tối thiểu vẫn phải đạt
    GAP = 0.13           # cụm ứng viên phải kém mốc của người quen ngần này...
    GAP_REF_S = 15.0     # ...và cụm ngắn hơn ngần này giây phải kém nhiều hơn (vector nhiễu hơn)
    GAP_OTHER = 0.19     # đối chứng tương đối cho người có thể vắng phải chênh ngần này
    SPLIT = 0.06         # cụm kém cụm tốt nhất của một người chưa tới ngần này: cũng là người đó
    MARGIN_KNOWN = 0.05  # giữa hai người quen: chênh ngần này mới dám gọi tên
    # Cụm giống giọng lưu của chủ phòng nhất chỉ nhường cho người quen KHÁC khi người đó khớp hẳn (>= FLOOR_OTHER
    # và hơn chủ phòng ngần này). Giọng lưu thu ngay trong phòng / trên máy phỏng vấn giống MỌI người ở đó
    # 0,6-0,8 chỉ vì cùng kênh thu (bench/voice_id.py --channel k_room), trong khi giọng lưu của chủ phòng
    # thu chỗ khác giống chính chủ phòng ~0,73: so tuyệt đối hai giọng lưu thu ở hai chỗ là gọi chủ phòng
    # bằng tên người kia (lỗi thật ở vị trí #23, 14/10: chị Quyên chủ phòng mà hiện tên anh Trí).
    STEAL = 0.15
    # Người quen khác chỉ nhận một cụm khi giọng lưu của họ giống cụm đó hơn hẳn giống cụm của CHÍNH chủ phòng
    # (luật "mẫu giọng phải giống hồ sơ này hơn mọi hồ sơ khác" của meeting-assistant). Giọng lưu chỉ giống
    # do cùng kênh thu thì giống cụm nào trong phòng cũng na ná - kể cả cụm của chủ phòng - nên không qua.
    PROFILE_MARGIN = 0.06
    NEAR_S = 90.0        # câu chưa quyết theo câu cùng nhãn Soniox trong ngần này giây
    W_CAP = 10.0         # câu dài nặng hơn khi tính tâm cụm, nhưng tối đa ngần này giây
    MAX = 150            # số câu gần nhất đem ra gom; câu cũ hơn giữ nguyên quyết định

    def __init__(self, people: dict, expected=None, known_only: bool = False):
        """people: {khoá: vector đã chuẩn hoá} - khoá do bên gọi tự đặt (id giọng lưu, "host").
        expected: khoá của người CHẮC CHẮN có mặt trên luồng này (chủ phòng trên mic), hoặc None.
        known_only: trên luồng này ai nói cũng là một người quen, KHÔNG có ứng viên - mic
        của extension (ứng viên ở trong Meet). Cụm chưa gọi được tên thì lấy người quen
        giống nhất, thay vì thành ứng viên: người phỏng vấn cùng ngồi cạnh, thu qua mic
        laptop từ xa, giống giọng lưu của chính họ kém hẳn - bản đầu gán họ thành ứng viên."""
        if not people:
            raise ValueError("cần ít nhất một giọng quen")
        self.anchors = dict(people)
        self.keys = list(people)
        self.expected = expected if expected in self.anchors else None
        self.known_only = known_only
        self.segs: list = []      # {"key", "v" (None nếu câu quá ngắn), "label", "t", "w"}
        # Các dict dưới được THAY (không sửa tại chỗ) cuối mỗi lần add(): add() chạy ở
        # luồng VOICE_POOL, event loop đọc chúng mà không cần khoá.
        self.decided: dict = {}   # khoá câu -> khoá người | ANY | CAND | None
        self.labels: dict = {}    # nhãn Soniox -> người mà nhãn đang chỉ (theo câu gần nhất)
        self.info: dict = {}      # khoá câu -> (giống giọng lưu nhất của cụm, số câu trong cụm, cụm chắc?)
        # khoá câu -> True khi vai do chính cụm giọng quyết, False khi mượn nhãn Soniox của câu gần đó.
        # rolefix.fuse cho nội dung thắng dễ hơn ở câu mượn nhãn.
        self.strong: dict = {}
        # khoá câu -> mã cụm giọng (khoá nhỏ nhất trong cụm, ổn định giữa các lần gom). live.py dùng để đối
        # chiếu cả CỤM với nội dung: cụm mà số đông câu là câu trả lời thì không thể là người phỏng vấn.
        self.cluster: dict = {}

    def add(self, key, v, label, t: float = 0.0, voiced: float | None = None) -> dict:
        """Nghe thêm một câu (v=None nếu quá ngắn để có vector).
        -> {khoá câu: khoá người | ANY | CAND | None} cho câu này và mọi câu cũ vừa đổi quyết định."""
        w = 0.0 if v is None else min(float(voiced) if voiced is not None else 1.0, self.W_CAP)
        self.segs.append({"key": key, "v": v, "label": str(label), "t": float(t), "w": w})
        if len(self.segs) > self.MAX:
            self.segs = self.segs[-self.MAX:]
        new, info, strong, cluster = self._decide()
        out = {k: p for k, p in new.items() if self.decided.get(k, "-") != p}
        out.setdefault(key, new.get(key))
        self.decided = {**self.decided, **new}
        self.info = {**self.info, **info}
        self.strong = {**self.strong, **strong}
        self.cluster = {**self.cluster, **cluster}
        self.labels = self._label_map()
        return out

    # ------------------------------------------------------------------ gom cụm
    def _clusters(self, idx):
        """Gom cụm phân cấp, liên kết trung bình, trên cosine giữa các câu."""
        import numpy as np
        V = np.stack([self.segs[i]["v"] for i in idx])
        n = len(idx)
        M = (V @ V.T).astype(np.float64)
        if self.LABEL_PEN:
            lab = np.array([self.segs[i]["label"] for i in idx])
            t = np.array([self.segs[i]["t"] for i in idx])
            M -= self.LABEL_PEN * ((lab[:, None] != lab[None, :]) & (np.abs(t[:, None] - t[None, :]) <= self.LABEL_WIN))
        np.fill_diagonal(M, -9.0)
        members = [[i] for i in range(n)]
        size = np.ones(n)
        act = np.ones(n, bool)
        while act.sum() > 1:
            Mm = np.where(act[:, None] & act[None, :], M, -9.0)
            a, b = np.unravel_index(np.argmax(Mm), Mm.shape)
            if Mm[a, b] < self.MERGE:
                break
            M[a, :] = (M[a, :] * size[a] + M[b, :] * size[b]) / (size[a] + size[b])
            M[:, a] = M[a, :]
            M[a, a] = -9.0
            size[a] += size[b]
            act[b] = False
            members[a] += members[b]
            members[b] = []
        return [[idx[i] for i in m] for m in members if m]

    # ------------------------------------------------------------------ gọi tên
    def _floor(self, k):
        return self.FLOOR_HOST if k == self.expected else self.FLOOR_OTHER

    def _known_margin(self, c, k):
        return all(c["A"][k] - c["A"][k2] >= self.MARGIN_KNOWN for k2 in self.keys if k2 != k)

    def _yields(self, c, k):
        """Cụm c có một người quen KHÁC k khớp hẳn: đủ ngưỡng của người có thể vắng VÀ hơn k từ STEAL."""
        return any(c["A"][k2] >= self.FLOOR_OTHER and c["A"][k2] - c["A"][k] >= self.STEAL
                   for k2 in self.keys if k2 != k)

    def _name(self, info):
        """-> {id(cụm): khoá người | ANY | CAND} cho các cụm đủ chắc để quyết."""
        solid = [c for c in info if c["solid"]]
        if not solid:
            return {}
        out, level, free = {}, {}, list(solid)
        unsure = {}                         # khoá người -> mức của cụm qua ngưỡng mà không phân được với người khác
        ref = None                          # cụm chính của chủ phòng: mốc để biết giọng lưu người khác có "riêng" không

        def distinct(c, k):
            return ref is None or c["A"][k] - ref["A"][k] >= self.PROFILE_MARGIN
        # người chắc chắn có mặt trước, rồi tới người có cụm giống mình nhất
        order = sorted(self.keys, key=lambda k: (k != self.expected, -max(c["A"][k] for c in solid)))
        for k in order:
            if not free:
                break
            best = max(free, key=lambda c: c["A"][k])
            L = best["A"][k]
            others = [c["A"][k] for c in free if c is not best]
            gap = self.GAP if k == self.expected else self.GAP_OTHER
            rel_ok = bool(others) and L - max(others) >= gap and L >= self.FLOOR_REL
            if k != self.expected and self.expected is not None:
                # Luồng có chủ phòng (mic phòng, mic extension): người khác có thể VẮNG, nên chỉ gọi tên khi khớp
                # tuyệt đối từ FLOOR_OTHER. Đối chứng tương đối (hơn các cụm khác 0,19, từ 0,24) gọi tên người
                # vắng cho cụm câu trả lời của người lạ khi giọng người lạ tách làm nhiều cụm (buổi #74, 14/10:
                # cả đoạn kể chuyện của "người lạ" hiện tên "khánh"). Luồng tab không có chủ phòng thì giữ.
                rel_ok = False
            if not (L >= self._floor(k) or rel_ok):
                continue
            if k == self.expected:
                # Chủ phòng chắc chắn có mặt: cụm giống giọng lưu của họ nhất là của họ, trừ khi một người quen
                # khác khớp HẲN (_yields). Hơn chút ít hay ngang ngửa là do kênh thu, không phải người khác.
                if self._yields(best, k):
                    continue
                level[k] = L
                out[id(best)] = k
                ref = best
            else:
                if not distinct(best, k):
                    continue                # giọng lưu của k giống chủ phòng ngang cụm này: do kênh thu, không phải k
                rival = max((best["A"][k2] for k2 in self.keys if k2 != k), default=-1.0)
                if rival > L:
                    continue                # cụm này giống một người quen khác hơn: để người đó nhận
                if L - rival < self.MARGIN_KNOWN:
                    out[id(best)] = ANY     # ngang ngửa hai người quen: người phỏng vấn, chưa rõ ai
                    unsure[k] = max(unsure.get(k, -1.0), L)
                    continue
                level[k] = L
            for c in free:
                if c["A"][k] >= L - self.SPLIT and self._known_margin(c, k) and (k == self.expected or distinct(c, k)):
                    out[id(c)] = k
            free = [c for c in free if id(c) not in out]
        free = [c for c in free if id(c) not in out]     # bỏ cả các cụm ANY
        level = {**unsure, **level}
        if self.known_only:
            for c in free:
                ranked = sorted(self.keys, key=lambda k: -c["A"][k])
                top, second = ranked[0], (ranked[1] if len(ranked) > 1 else None)
                if self.expected is not None and top != self.expected and not distinct(c, top):
                    out[id(c)] = self.expected  # mic của chủ phòng: giọng lưu người kia không "riêng" cho cụm này
                    continue
                out[id(c)] = top if second is None or c["A"][top] - c["A"][second] >= self.MARGIN_KNOWN else ANY
            return out
        if not level:
            return out                      # chưa có mốc nào: không ai bị gọi là ứng viên
        for c in free:
            g = self.GAP * max(1.0, (self.GAP_REF_S / max(c["dur"], 0.5)) ** 0.5)
            if all((c["A"][k] <= level[k] - g) if k in level else (c["A"][k] < self._floor(k))
                   for k in self.keys):
                out[id(c)] = CAND
        return out

    def _decide(self):
        import numpy as np
        out, info, cluster = {}, {}, {}
        vec = [i for i, s in enumerate(self.segs) if s["v"] is not None]
        if vec:
            clusters = []
            for m in self._clusters(vec):
                dur = sum(self.segs[i]["w"] for i in m)
                u = _unit(np.sum([self.segs[i]["v"] * max(self.segs[i]["w"], 0.5) for i in m], axis=0))
                clusters.append({"m": m, "dur": dur, "solid": len(m) >= 2 or dur >= self.SOLID_S,
                                 "A": {k: float(u @ self.anchors[k]) for k in self.keys}})
            named = self._name(clusters)
            for c in clusters:
                cid = min(self.segs[i]["key"] for i in c["m"])
                for i in c["m"]:
                    key = self.segs[i]["key"]
                    out[key] = named.get(id(c))
                    info[key] = (max(c["A"].values()), len(c["m"]), c["solid"])
                    cluster[key] = cid
        strong = {s["key"]: out.get(s["key"]) is not None for s in self.segs}
        # Câu chưa quyết: theo tối đa 3 câu cùng nhãn gần nhất về thời gian, phải quá bán.
        base = dict(out)
        for s in self.segs:
            if base.get(s["key"]) is not None:
                continue
            near = sorted((x for x in self.segs if x is not s and x["label"] == s["label"]
                           and base.get(x["key"]) is not None and abs(x["t"] - s["t"]) <= self.NEAR_S),
                          key=lambda x: abs(x["t"] - s["t"]))[:3]
            votes = [base[x["key"]] for x in near]
            top = max(set(votes), key=votes.count) if votes else None
            out[s["key"]] = top if votes and votes.count(top) * 2 > len(votes) else None
        return out, info, strong, cluster

    def _label_map(self) -> dict:
        """nhãn Soniox -> người, theo 5 câu gần nhất đã quyết của nhãn đó (phải quá bán)."""
        votes: dict = {}
        for s in reversed(self.segs):
            p = self.decided.get(s["key"])
            if p is not None and len(votes.setdefault(s["label"], [])) < 5:
                votes[s["label"]].append(p)
        out = {}
        for lab, vs in votes.items():
            top = max(set(vs), key=vs.count)
            if vs.count(top) * 2 > len(vs):
                out[lab] = top
        return out


def download():
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 1_000_000:
        print(f"đã có {MODEL_PATH} ({MODEL_PATH.stat().st_size / 1e6:.1f}MB)")
        return
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".part")
    print(f"tải {MODEL_URL}")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    tmp.replace(MODEL_PATH)             # đổi tên sau cùng: tải dở không bao giờ bị tưởng là model
    print(f"-> {MODEL_PATH} ({MODEL_PATH.stat().st_size / 1e6:.1f}MB)")


if __name__ == "__main__":
    if sys.argv[1:] == ["download"]:
        download()
    else:
        print("dùng: python -m interview.voice download")
