"""Tìm đoạn liên quan trong kho của một nhóm: tài liệu của nhóm, biên bản và lời nói các cuộc họp trước (bản 3.19).

BM25 viết tay, không thêm thư viện, không gọi API nhúng: kho của một nhóm nhỏ (vài nghìn đoạn). Từ khóa là âm tiết bỏ
dấu, chữ thường, cộng cặp âm tiết liền nhau ("phat_hanh") để bắt từ ghép; nhờ bỏ dấu, lời nói nhận dạng sai dấu vẫn
tìm ra tài liệu gõ đúng dấu. Kho dựng lại khi nhóm có tài liệu mới hoặc cuộc họp mới; nhớ trong RAM theo nhóm.
"""
import math
import re
import threading
import time
import unicodedata
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from meeting import db

STOP = set("""a ah ai anh ba ban bi bo cac cai can cho chi chu chung co con cua cung da dang day de den deu di do du duoc
em gi gio ha hay he hoac hon ho khi la lai len luc ma minh mot moi nao nay neu nhe nha nhu nhung nua o oi ong ra rang
roi sao se sau tai the thi thoi tren trong tu tung va vang ve vi voi vay vo xong ya u uh um ok oke""".split())
K1, B = 1.4, 0.75
WINDOW_CHARS = 700
_CACHE: Dict[int, Tuple[Any, "Index"]] = {}
_LOCK = threading.Lock()


def fold(text: str) -> str:
    t = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "d")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def terms(text: str) -> List[str]:
    ws = words(text)
    out = [w for w in ws if w not in STOP]
    out += [f"{a}_{b}" for a, b in zip(ws, ws[1:]) if not (a in STOP and b in STOP)]
    return out


class Index:
    """BM25 trên danh sách đoạn {"kind", "title", "text", ...}."""

    def __init__(self, passages: Sequence[Dict[str, Any]]):
        self.docs = list(passages)
        self.tf: List[Counter] = []
        df: Counter = Counter()
        for p in self.docs:
            c = Counter(terms(f"{p.get('title', '')} {p.get('text', '')}"))
            self.tf.append(c)
            df.update(c.keys())
        n = max(1, len(self.docs))
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.lens = [sum(c.values()) for c in self.tf]
        self.avg = (sum(self.lens) / n) if self.docs else 1.0

    def __len__(self) -> int:
        return len(self.docs)

    def score(self, i: int, q: Iterable[str]) -> float:
        tf, ln, s = self.tf[i], self.lens[i], 0.0
        for t in q:
            f = tf.get(t)
            if f:
                s += self.idf.get(t, 0.0) * f * (K1 + 1) / (f + K1 * (1 - B + B * ln / max(self.avg, 1.0)))
        return s

    def search(self, query: str, k: int = 6, kinds: Optional[Iterable[str]] = None,
               exclude_meeting: Optional[int] = None) -> List[Dict[str, Any]]:
        q = set(terms(query))
        if not q or not self.docs:
            return []
        kinds = set(kinds) if kinds else None
        ranked = []
        for i, p in enumerate(self.docs):
            if kinds and p.get("kind") not in kinds:
                continue
            if exclude_meeting is not None and p.get("meeting_id") == exclude_meeting:
                continue
            s = self.score(i, q)
            if s > 0:
                ranked.append((s, i))
        ranked.sort(reverse=True)
        return [{**self.docs[i], "score": round(s, 3)} for s, i in ranked[:k]]


# ------------------------------------------------------------ đoạn của một cuộc họp ---
def fmt_date(ts: Any) -> str:
    try:
        return time.strftime("%d/%m/%Y", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return ""


def clock(t: Any) -> str:
    t = int(float(t or 0))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def meeting_title(m: Dict[str, Any]) -> str:
    d = fmt_date(m.get("started_at"))
    return f"{m.get('title') or 'Cuộc họp'}{f' ({d})' if d else ''}"


def minutes_sections(md: str) -> List[Tuple[str, str]]:
    """Biên bản Markdown -> [(tên mục, nội dung)] theo các tiêu đề "##"."""
    out, head, buf = [], "Biên bản", []
    for ln in (md or "").splitlines():
        m = re.match(r"^#{1,3}\s+(.*)$", ln)
        if m:
            if "".join(buf).strip():
                out.append((head, "\n".join(buf).strip()))
            head, buf = m.group(1).strip(), []
        else:
            buf.append(ln)
    if "".join(buf).strip():
        out.append((head, "\n".join(buf).strip()))
    return out


def transcript_windows(segments: Sequence[Dict[str, Any]], size: int = WINDOW_CHARS) -> List[Tuple[float, str]]:
    """Lời nói cắt thành khung khoảng `size` ký tự: [(giờ bắt đầu khung, "[mm:ss] Tên: câu ...")]."""
    out, buf, start, n = [], [], None, 0
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        line = f"[{clock(s.get('t_start'))}] {s.get('speaker_label') or 'Không rõ'}: {text}"
        if start is None:
            start = float(s.get("t_start") or 0)
        buf.append(line)
        n += len(line)
        if n >= size:
            out.append((start, "\n".join(buf)))
            buf, start, n = [], None, 0
    if buf:
        out.append((start or 0.0, "\n".join(buf)))
    return out


def latest_minutes(mid: int) -> Optional[Dict[str, Any]]:
    arts = [a for a in db.get_artifacts(mid) if a.get("kind") == "minutes" and (a.get("content") or "").strip()]
    return max(arts, key=lambda a: (a.get("created_at") or 0, a.get("id") or 0)) if arts else None


def meeting_passages(m: Dict[str, Any], segments: Optional[Sequence[Dict[str, Any]]] = None,
                     minutes: Optional[str] = None) -> List[Dict[str, Any]]:
    title = meeting_title(m)
    if minutes is None:
        art = latest_minutes(m["id"])
        minutes = (art or {}).get("content") or ""
    if segments is None:
        segments = db.get_segments(m["id"])
    out = [{"kind": "minutes", "meeting_id": m["id"], "title": title, "ref": head, "text": body[:1800]}
           for head, body in minutes_sections(minutes)]
    out += [{"kind": "talk", "meeting_id": m["id"], "title": title, "ref": clock(t), "t": t, "text": text}
            for t, text in transcript_windows(segments)]
    return out


# ------------------------------------------------------------ kho của nhóm ---
def _signature(gid: int) -> Tuple[Any, ...]:
    from meeting import group_kb
    ms = db.find_meetings({"group_id": gid}, limit=500, sort="started_at")
    return (tuple((m["id"], m.get("status"), m.get("minutes_status"), m.get("ended_at")) for m in ms),
            group_kb.signature(gid))


def group_index(gid: int) -> Index:
    """Kho tìm kiếm của nhóm: tài liệu + biên bản + lời nói của mọi cuộc họp trong nhóm (cuộc họp đang diễn ra thì lời nói
    tới lúc dựng kho). Dựng lại khi chữ ký (danh sách cuộc họp, tài liệu) đổi."""
    from meeting import group_kb
    gid = int(gid)
    sig = _signature(gid)
    with _LOCK:
        hit = _CACHE.get(gid)
        if hit and hit[0] == sig:
            return hit[1]
    passages = list(group_kb.passages(gid))
    for m in db.find_meetings({"group_id": gid}, limit=200, sort="started_at"):
        passages += meeting_passages(m)
    idx = Index(passages)
    with _LOCK:
        _CACHE[gid] = (sig, idx)
    return idx


def forget(gid: Optional[int] = None) -> None:
    with _LOCK:
        if gid is None:
            _CACHE.clear()
        else:
            _CACHE.pop(int(gid), None)
