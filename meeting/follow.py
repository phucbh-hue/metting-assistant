"""Tự chuyển slide theo lời trình bày: so lời vừa nói với nội dung từng slide.

Mỗi slide thành một "túi từ" (tiêu đề nặng gấp đôi, ghi chú nhẹ hơn), gồm từ đơn và cặp từ liền nhau (tiếng Việt
nhiều từ ghép hai âm tiết: "ngân sách", "tiến độ"). Từ xuất hiện ở nhiều slide được tính nhẹ (IDF) để chỉ những
từ đặc trưng của từng slide mới quyết định. Chống nhảy lung tung bằng ngưỡng, chênh lệch tối thiểu, thời gian chờ
và yêu cầu xác nhận hai lần khi nhảy xa hoặc lùi lại.
"""
import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional

STOPWORDS = set("""
và của cho các những là có được trong với này đã sẽ một để từ theo khi thì mà ở về ra vào lên như cũng rất
nhiều không chưa đang bị hay hoặc nên phải thêm đến tại do vì nếu anh chị em mình chúng ta tôi bạn ạ ờ à ừ
dạ vâng nhé nha thế đó đây kia ấy cái con người việc rồi lại nữa thôi vậy nào gì sao nhưng mới đi làm nói
còn hơn bây giờ hiện tại lúc nay hôm thấy biết muốn cần cứ luôn chỉ đều tất cả mọi khá hết qua sang
""".split())


def tokens(text: str) -> List[str]:
    return re.findall(r"\w+", (text or "").lower())


def features(text: str, weight: float = 1.0) -> Dict[str, float]:
    """Từ đơn (bỏ từ chức năng) và cặp từ liền nhau, kèm trọng số."""
    toks = tokens(text)
    out: Dict[str, float] = {}
    for i, t in enumerate(toks):
        if t in STOPWORDS or (len(t) < 2 and not t.isdigit()):
            continue
        out[t] = max(out.get(t, 0.0), weight)
        if i + 1 < len(toks) and toks[i + 1] not in STOPWORDS:
            out[f"{t} {toks[i + 1]}"] = max(out.get(f"{t} {toks[i + 1]}", 0.0), 1.5 * weight)
    return out


class SlideMatcher:
    """Chấm điểm mức khớp giữa một đoạn lời nói và từng slide của bộ slide."""

    def __init__(self, slides: List[Dict[str, Any]]):
        self.docs: List[Dict[str, float]] = []
        for sl in slides:
            doc: Dict[str, float] = {}
            parts = [(sl.get("title", ""), 2.0)] + [(b, 1.0) for b in sl.get("bullets") or []] + [(sl.get("notes", ""), 0.6)]
            for text, w in parts:
                for k, v in features(text, w).items():
                    doc[k] = max(doc.get(k, 0.0), v)
            self.docs.append(doc)
        n = max(1, len(self.docs))
        df = Counter(k for d in self.docs for k in d)
        self.idf = {k: math.log(1.0 + n / c) for k, c in df.items()}

    def scores(self, text: str) -> List[float]:
        spoken = features(text)
        return [sum(self.idf[k] * w for k, w in doc.items() if k in spoken) for doc in self.docs]

    def best_bullet(self, slide: Dict[str, Any], text: str) -> Optional[int]:
        """Ý (bullet) của slide đang được nói tới, để tô sáng trên màn hình."""
        spoken = features(text)
        best, best_s = None, 0.0
        for i, b in enumerate(slide.get("bullets") or []):
            s = sum(self.idf.get(k, 0.5) * w for k, w in features(b).items() if k in spoken)
            if s > best_s:
                best, best_s = i, s
        return best if best_s >= 2.0 else None


class SlideFollower:
    """Quyết định có tự chuyển slide hay không (có trễ, chống nhảy qua lại)."""
    MIN_SCORE = 3.0          # khớp tối thiểu (khoảng 2 từ đặc trưng hoặc 1 cụm từ của slide đích)
    RATIO = 1.6              # slide đích phải khớp hơn slide hiện tại ít nhất ngần này lần...
    MARGIN = 1.0             # ... và hơn ít nhất ngần này điểm
    COOLDOWN_S = 6.0         # vừa đổi slide (kể cả tự động): chờ ngần này giây
    MANUAL_HOLD_S = 12.0     # người dùng vừa tự chuyển slide: tôn trọng, không tự chuyển trong ngần này giây

    def __init__(self):
        self.pending: Optional[int] = None

    def decide(self, scores: List[float], cur: int, now: float, changed_at: float, manual_at: float) -> Optional[int]:
        if not scores or now - changed_at < self.COOLDOWN_S or now - manual_at < self.MANUAL_HOLD_S:
            self.pending = None
            return None
        cur = min(max(cur, 0), len(scores) - 1)
        best = max(range(len(scores)), key=lambda i: scores[i])
        s_best, s_cur = scores[best], scores[cur]
        if best == cur or s_best < self.MIN_SCORE or s_best < s_cur * self.RATIO + self.MARGIN:
            self.pending = None
            return None
        if best == cur + 1:              # sang ý tiếp theo: chuyển ngay
            self.pending = None
            return best
        if self.pending == best:         # nhảy xa / lùi lại: phải khớp hai lần liên tiếp
            self.pending = None
            return best
        self.pending = best
        return None
