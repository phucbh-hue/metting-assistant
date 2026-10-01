"""Tìm kiếm tri thức (Knowledge Base) trên các tài liệu markdown trong mcp_server/kb/.

Module thuần Python, KHÔNG phụ thuộc SDK MCP, để dùng chung cho:
- MCP server thật (mcp_server/server.py) - tool search_knowledge / read_document và resource kb://{name}
- Fallback in-process trong meeting/mcp.py khi chưa cấu hình MCP server

Thuật toán: BM25 (k1=1.5, b=0.75) trên token đã bỏ dấu tiếng Việt + bigram, tính ở mức đoạn văn
(passage = một khối nội dung dưới một heading). Điểm tài liệu = điểm đoạn tốt nhất + thưởng khi từ khóa
xuất hiện trong tiêu đề. Khi chuyển sang KB thật (vector store), chỉ cần thay search()/read_document()
và giữ nguyên định dạng kết quả.

Thư mục KB mặc định: mcp_server/kb (đổi bằng biến môi trường MCP_KB_DIR).
"""
import math
import os
import re
import threading
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_KB_DIR = Path(__file__).resolve().parent / "kb"

K1 = 1.5
B = 0.75
TITLE_BOOST = 0.6
SNIPPET_CHARS = 320

# Từ dừng phổ biến (đã bỏ dấu) - không mang nghĩa khi xếp hạng
STOPWORDS = {
    "va", "la", "cua", "cho", "cac", "nhung", "mot", "duoc", "khi", "trong", "voi", "co", "khong",
    "de", "theo", "tu", "den", "ve", "nay", "do", "thi", "hoac", "neu", "da", "se", "o", "tai",
    "the", "nao", "gi", "bang", "tren", "sau", "truoc", "can", "phai", "moi", "cung", "hay",
    "a", "an", "of", "to", "in", "and", "or", "is",
}

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def kb_dir() -> Path:
    env = os.getenv("MCP_KB_DIR", "").strip()
    return Path(env).resolve() if env else DEFAULT_KB_DIR


def fold(text: str) -> str:
    """Chữ thường + bỏ dấu tiếng Việt (đ -> d) để 'đổi trả' khớp cả 'doi tra'."""
    text = unicodedata.normalize("NFD", text.lower()).replace("đ", "d")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", text).replace("đ", "d")


def tokenize(text: str) -> List[str]:
    words = [w for w in _TOKEN_RE.findall(fold(text)) if w not in STOPWORDS and w != "_"]
    return words + [f"{a}_{b}" for a, b in zip(words, words[1:])]


# ------------------------------------------------------------------ INDEX ---
class _Passage:
    __slots__ = ("heading", "text", "tf", "length")

    def __init__(self, heading: str, text: str):
        self.heading = heading
        self.text = text
        toks = tokenize(f"{heading}\n{text}")
        self.tf = Counter(toks)
        self.length = len(toks)


class _Doc:
    def __init__(self, path: Path, root: Path):
        self.path = path
        self.name = path.stem
        self.rel = path.relative_to(root).as_posix()
        self.content = path.read_text(encoding="utf-8")
        self.title = self.name
        self.passages: List[_Passage] = []
        heading, buf = "", []
        for line in self.content.splitlines():
            m = re.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                if m.group(1) == "#" and self.title == self.name:
                    self.title = m.group(2).strip()
                self._flush(heading, buf)
                heading, buf = m.group(2).strip(), []
            else:
                buf.append(line)
        self._flush(heading, buf)
        self.title_tokens = set(tokenize(self.title))

    def _flush(self, heading: str, buf: List[str]):
        text = "\n".join(buf).strip()
        if text:
            self.passages.append(_Passage(heading, text))


class _Index:
    def __init__(self, root: Path):
        self.root = root
        self.docs = [_Doc(p, root) for p in sorted(root.glob("*.md")) if p.name.lower() != "readme.md"]
        passages = [p for d in self.docs for p in d.passages]
        self.n = len(passages) or 1
        self.avgdl = (sum(p.length for p in passages) / self.n) or 1.0
        self.df: Counter = Counter()
        for p in passages:
            self.df.update(p.tf.keys())

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, p: _Passage, terms: List[str]) -> float:
        s = 0.0
        for t in terms:
            f = p.tf.get(t, 0)
            if f:
                s += self.idf(t) * f * (K1 + 1) / (f + K1 * (1 - B + B * p.length / self.avgdl))
        return s


_lock = threading.Lock()
_cache: Dict[str, Any] = {"sig": None, "index": None}


def _signature(root: Path) -> Tuple:
    if not root.is_dir():
        return (str(root),)
    return (str(root),) + tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in root.glob("*.md")))


def _index() -> _Index:
    root = kb_dir()
    sig = _signature(root)
    with _lock:
        if _cache["sig"] != sig:
            _cache["index"] = _Index(root) if root.is_dir() else None
            _cache["sig"] = sig
        idx = _cache["index"]
    if idx is None:
        raise FileNotFoundError(f"Không tìm thấy thư mục KB: {root}")
    return idx


def _snippet(p: _Passage, terms: List[str]) -> str:
    text = re.sub(r"\s+", " ", p.text).strip()
    if len(text) > SNIPPET_CHARS:
        folded = fold(text)
        pos = min((folded.find(t) for t in terms if "_" not in t and folded.find(t) >= 0), default=0)
        start = max(0, pos - 80)
        text = ("..." if start else "") + text[start:start + SNIPPET_CHARS].strip() + "..."
    return f"[{p.heading}] {text}" if p.heading else text


# ------------------------------------------------------------- PUBLIC API ---
def list_documents() -> List[Dict[str, Any]]:
    """Danh sách tài liệu KB: name, title, path, số ký tự."""
    return [{"name": d.name, "title": d.title, "path": d.rel, "chars": len(d.content)} for d in _index().docs]


def search(query: str, top_k: int = 5) -> Dict[str, Any]:
    """Tìm tài liệu liên quan nhất với câu hỏi. Trả về {query, count, results: [{title, path, uri, snippet, score}]}."""
    terms = tokenize(query or "")
    try:
        top_k = max(1, min(int(top_k or 5), 20))
    except (TypeError, ValueError):
        top_k = 5
    if not terms:
        return {"query": query, "count": 0, "results": []}
    idx = _index()
    results = []
    for d in idx.docs:
        best: Optional[_Passage] = None
        best_s = 0.0
        for p in d.passages:
            s = idx.score(p, terms)
            if s > best_s:
                best, best_s = p, s
        if best is None:
            continue
        title_hits = sum(idx.idf(t) for t in set(terms) if t in d.title_tokens)
        score = best_s + TITLE_BOOST * title_hits
        results.append({"title": d.title, "path": d.rel, "uri": f"kb://{d.name}",
                        "snippet": _snippet(best, terms), "score": round(score, 3)})
    results.sort(key=lambda r: -r["score"])
    results = results[:top_k]
    return {"query": query, "count": len(results), "results": results}


def resolve_path(path: str) -> Path:
    """Chuẩn hóa đường dẫn tài liệu ('ten.md', 'ten', 'kb/ten.md', 'kb://ten') và chặn mọi đường dẫn ra ngoài thư mục KB."""
    raw = (path or "").strip().replace("\\", "/")
    if raw.startswith("kb://"):
        raw = raw[len("kb://"):]
    if raw.startswith("kb/"):
        raw = raw[len("kb/"):]
    if not raw or "\x00" in raw:
        raise ValueError("Đường dẫn tài liệu không hợp lệ")
    if not raw.lower().endswith(".md"):
        raw += ".md"
    root = kb_dir().resolve()
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("Chỉ được đọc tài liệu nằm trong thư mục KB")
    full = (root / candidate).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise ValueError("Chỉ được đọc tài liệu nằm trong thư mục KB")
    if not full.is_file():
        raise FileNotFoundError(f"Không tìm thấy tài liệu: {path}")
    return full


def read_document(path: str) -> Dict[str, Any]:
    """Trả về toàn văn markdown của một tài liệu KB: {title, path, uri, content}."""
    full = resolve_path(path)
    root = kb_dir().resolve()
    content = full.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", content, re.M)
    return {"title": m.group(1).strip() if m else full.stem, "path": full.relative_to(root).as_posix(),
            "uri": f"kb://{full.stem}", "content": content}
