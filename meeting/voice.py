"""Voice Biometrics & Meeting Speaker Identification.

Module nhận diện và định danh giọng nói cho cuộc họp:
1. Trích xuất vector đặc trưng (192-dim speaker embedding) qua mô hình CAM++ (3D-Speaker)
   chạy trên CPU bằng sherpa-onnx.
2. Kiểm tra chất lượng âm thanh RMS (voiced_s) để loại bỏ khoảng lặng/tiếng xì trước khi tính vector.
3. MeetingSpeakers: Gom cụm phân cấp các câu nói trong buổi họp và đối chiếu với Voice Registry.
   - Nhận diện người quen khi khớp với vector lưu trong DB.
   - Đánh dấu người lạ (Unknown_1, Unknown_2...) khi chưa có mẫu giọng.
"""
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("meeting.voice")

HERE = Path(__file__).resolve().parent
MODEL_PATH = Path(os.getenv("VOICE_MODEL_PATH", str(HERE.parent / "models" / "speaker.onnx")))
RATE = 16000
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
        "dimension": 192,
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
    global _warm
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
        return np.zeros(192, dtype=np.float32)
    if weights is None:
        weights = [1.0] * len(vectors)
    acc = np.sum([v * max(w, 0.1) for v, w in zip(vectors, weights)], axis=0)
    return unit(acc)


# ==============================================================================
# QUẢN LÝ NGƯỜI NÓI TRONG CUỘC HỌP (MeetingSpeakers)
# ==============================================================================
class MeetingSpeakers:
    """Quản lý và định danh người nói trong một cuộc họp đa bên ($N$ người).

    1. GOM CỤM (Clustering): gom các câu trong buổi theo độ tương đồng âm học.
    2. ĐỐI CHIẾU ANCHOR: khớp cụm với danh sách giọng người quen đã lưu trong DB.
    3. ĐỊNH DANH NGƯỜI LẠ: các cụm không khớp người quen được gán mã Người lạ #1...
    4. MANUAL OVERRIDE: Người dùng sửa tay thì luôn ghi đè và ghi nhớ vĩnh viễn cho buổi họp.
    """
    MERGE_THRESHOLD = 0.50   # Ngưỡng cosine gộp cụm chuẩn
    LABEL_PENALTY = 0.04     # Phạt nhãn Soniox 0.04 trong 45s để tránh gộp 2 người nói kề nhau
    LABEL_WINDOW = 45.0
    SOLID_SECONDS = 2.5      # Cụm cần ít nhất ngần này giây để chốt gọi tên
    FLOOR_HOST = 0.35        # Người chắc chắn cầm máy/mic: nhận ở ngưỡng âm học thực tế
    MATCH_FLOOR = 0.55       # Ngưỡng cosine cho người quen khác
    MATCH_MARGIN = 0.03      # Khoảng chênh lệch tối thiểu giữa người cao nhất và nhì
    MAX_HISTORY = 200        # Số câu gần nhất đưa vào gom cụm

    def __init__(self, anchors: Dict[int, Dict[str, Any]], expected_host_id: Optional[int] = None):
        """anchors: {voice_id: {"name": str, "vector": np.ndarray, "role": str, "department": str}}"""
        self.anchors = {vid: dict(info) for vid, info in anchors.items()}
        # Nếu chưa chỉ định host_id, tự động lấy anchor đầu tiên làm Host mặc định
        if expected_host_id is not None and expected_host_id in self.anchors:
            self.host_id = expected_host_id
        elif self.anchors:
            self.host_id = list(self.anchors.keys())[0]
        else:
            self.host_id = None

        self.segs: List[Dict[str, Any]] = []
        self.decided: Dict[Any, str] = {}
        self.speaker_ids: Dict[Any, Optional[int]] = {}
        self.unknown_map: Dict[int, str] = {}
        self.next_unknown_idx = 1
        self.cluster_centroids: Dict[str, np.ndarray] = {}
        self.last_speaker_name = self.anchors[self.host_id]["name"] if (self.host_id and self.host_id in self.anchors) else "Người nói"
        self.last_speaker_id = self.host_id
        self.manual_overrides: Dict[str, Tuple[str, Optional[int]]] = {}

    def override_speaker(self, old_label: str, new_name: str, new_vid: Optional[int] = None):
        """Sửa tay người nói: luôn ghi đè và ghi nhớ."""
        self.manual_overrides[old_label] = (new_name, new_vid)
        for s in self.segs:
            k = s["key"]
            if self.decided.get(k) == old_label:
                self.decided[k] = new_name
                self.speaker_ids[k] = new_vid
        log.info("meeting.voice: Manual override %s -> %s (vid=%s)", old_label, new_name, new_vid)

    def update_anchors(self, new_anchors: Dict[int, Dict[str, Any]]):
        self.anchors.update(new_anchors)

    def add(self, key: Any, v: Optional[np.ndarray], raw_label: Any, t: float = 0.0,
            voiced: float = 1.0, text: str = "") -> Dict[Any, Tuple[str, Optional[int]]]:
        """Thêm câu mới và tính toán lại phân vai.

        Trả về {changed_key: (speaker_label, voice_id)} cho các câu có thay đổi nhãn.
        """
        w = 0.0 if v is None else min(max(float(voiced), 0.5), 10.0)
        self.segs.append({
            "key": key, "v": v, "label": str(raw_label), "t": float(t),
            "w": w, "text": text
        })
        if len(self.segs) > self.MAX_HISTORY:
            self.segs = self.segs[-self.MAX_HISTORY:]

        new_labels, new_ids = self._decide()
        changes = {}
        for k, lbl in new_labels.items():
            old_lbl = self.decided.get(k)
            old_id = self.speaker_ids.get(k)
            nid = new_ids.get(k)
            if old_lbl != lbl or old_id != nid:
                changes[k] = (lbl, nid)

        self.decided.update(new_labels)
        self.speaker_ids.update(new_ids)
        return changes

    def get_cluster_vectors(self, label: str) -> List[np.ndarray]:
        """Lấy tất cả các vector của một speaker label (để tính centroid lưu DB)."""
        res = [s["v"] for s in self.segs if s["v"] is not None and self.decided.get(s["key"]) == label]
        if not res:
            res = [s["v"] for s in self.segs if s["v"] is not None and (
                label in str(self.decided.get(s["key"], "")) or str(self.decided.get(s["key"], "")) in label
            )]
        return res

    def _clusters(self, idx: List[int]) -> List[List[int]]:
        """Gom cụm liên kết trung bình trên ma trận Cosine similarity."""
        n = len(idx)
        if n == 1:
            return [[idx[0]]]
        V = np.stack([self.segs[i]["v"] for i in idx])
        M = (V @ V.T).astype(np.float64)

        if self.LABEL_PENALTY:
            lab = np.array([self.segs[i]["label"] for i in idx])
            t = np.array([self.segs[i]["t"] for i in idx])
            diff_lab = (lab[:, None] != lab[None, :])
            near_t = (np.abs(t[:, None] - t[None, :]) <= self.LABEL_WINDOW)
            M -= self.LABEL_PENALTY * (diff_lab & near_t)

        np.fill_diagonal(M, -9.0)
        members = [[i] for i in range(n)]
        size = np.ones(n)
        active = np.ones(n, bool)

        while active.sum() > 1:
            Mm = np.where(active[:, None] & active[None, :], M, -9.0)
            a, b = np.unravel_index(np.argmax(Mm), Mm.shape)
            if Mm[a, b] < self.MERGE_THRESHOLD:
                break
            M[a, :] = (M[a, :] * size[a] + M[b, :] * size[b]) / (size[a] + size[b])
            M[:, a] = M[a, :]
            M[a, a] = -9.0
            size[a] += size[b]
            active[b] = False
            members[a] += members[b]
            members[b] = []

        return [[idx[i] for i in m] for m in members if m]

    def _decide(self) -> Tuple[Dict[Any, str], Dict[Any, Optional[int]]]:
        out_labels = {}
        out_ids = {}

        vec_indices = [i for i, s in enumerate(self.segs) if s["v"] is not None]
        default_fallback_name = self.anchors[self.host_id]["name"] if (self.host_id and self.host_id in self.anchors) else "Người nói"
        default_fallback_id = self.host_id

        if not vec_indices:
            # Chưa có câu nào có vector: kế thừa Host hoặc người nói gần nhất
            for s in self.segs:
                out_labels[s["key"]] = self.last_speaker_name or default_fallback_name
                out_ids[s["key"]] = self.last_speaker_id or default_fallback_id
            return out_labels, out_ids

        clusters = self._clusters(vec_indices)
        named_clusters = {}
        unknown_centroids: Dict[str, np.ndarray] = {}

        for m in clusters:
            cid = min(self.segs[i]["key"] for i in m)
            cluster_dur = sum(self.segs[i]["w"] for i in m)
            cv = unit(np.sum([self.segs[i]["v"] * max(self.segs[i]["w"], 0.5) for i in m], axis=0))

            # So khớp với anchors trong DB (nếu có anchors)
            best_vid = None
            best_sim = -1.0
            second_sim = -1.0

            if self.anchors:
                for vid, info in self.anchors.items():
                    sim = cosine_sim(cv, info["vector"])
                    if sim > best_sim:
                        second_sim = best_sim
                        best_sim = sim
                        best_vid = vid
                    elif sim > second_sim:
                        second_sim = sim

            # Kiểm tra ngưỡng chấp nhận
            floor = self.FLOOR_HOST if (best_vid is not None and best_vid == self.host_id) else self.MATCH_FLOOR
            margin_ok = (best_sim - second_sim) >= self.MATCH_MARGIN or len(self.anchors) == 1

            if self.anchors and best_vid is not None and best_sim >= floor and margin_ok:
                person = self.anchors[best_vid]
                lbl = person["name"]
                named_clusters[cid] = (lbl, best_vid, cv)
            else:
                # Không khớp ai hoặc chưa có anchors: kiểm tra xem có giống một Người nói đã xuất hiện trước đó không
                reused_label = None
                for u_lbl, u_vec in unknown_centroids.items():
                    if cosine_sim(cv, u_vec) >= 0.46:
                        reused_label = u_lbl
                        break

                if reused_label:
                    named_clusters[cid] = (reused_label, None, cv)
                else:
                    if cid not in self.unknown_map:
                        prefix = "Người nói" if not self.anchors else "Người lạ"
                        self.unknown_map[cid] = f"{prefix} #{self.next_unknown_idx}"
                        self.next_unknown_idx += 1
                    lbl = self.unknown_map[cid]
                    named_clusters[cid] = (lbl, None, cv)
                    unknown_centroids[lbl] = cv

            # Lưu nhãn cho từng câu trong cụm
            for idx in m:
                key = self.segs[idx]["key"]
                out_labels[key] = named_clusters[cid][0]
                out_ids[key] = named_clusters[cid][1]
                self.cluster_centroids[named_clusters[cid][0]] = cv

        # Gán nhãn cho các câu không có vector (quá ngắn như "Alo", "ừ", "vâng")
        for s in self.segs:
            if s["key"] not in out_labels:
                near_lbl = None
                near_id = None
                min_dt = 999999.0
                # Ưu tiên 1: Tìm câu cùng raw_label của Soniox gần nhất trong 45 giây
                same_raw = [other for other in self.segs if other["key"] in out_labels and other["label"] == s["label"] and abs(other["t"] - s["t"]) <= 45.0]
                if same_raw:
                    closest = min(same_raw, key=lambda other: abs(other["t"] - s["t"]))
                    near_lbl = out_labels[closest["key"]]
                    near_id = out_ids[closest["key"]]
                else:
                    # Ưu tiên 2: Tìm câu gần nhất bất kỳ
                    for other in self.segs:
                        if other["key"] in out_labels:
                            dt = abs(other["t"] - s["t"])
                            if dt < min_dt:
                                min_dt = dt
                                near_lbl = out_labels[other["key"]]
                                near_id = out_ids[other["key"]]

                out_labels[s["key"]] = near_lbl or default_fallback_name
                out_ids[s["key"]] = near_id or default_fallback_id

        # Áp dụng Manual Overrides (người dùng sửa tay luôn thắng tuyệt đối)
        for k, lbl in list(out_labels.items()):
            if lbl in self.manual_overrides:
                out_labels[k], out_ids[k] = self.manual_overrides[lbl]

        # Cập nhật người nói gần nhất
        if self.segs:
            last_key = self.segs[-1]["key"]
            if last_key in out_labels:
                self.last_speaker_name = out_labels[last_key]
                self.last_speaker_id = out_ids.get(last_key)

        return out_labels, out_ids
