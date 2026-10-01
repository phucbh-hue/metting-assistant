"""Thư viện slide trên máy: tìm và nhập bộ slide từ thư mục (SLIDES_DIR, mặc định <dự án>/slides).

"Jarvis ơi, mở slide ở folder Mega Sale" -> tìm thư mục / tệp có tên gần nhất, đọc thành bộ slide của hệ thống.
Định dạng đọc được: .json (đúng cấu trúc deck của hệ thống), .md / .txt (tiêu đề "#", mỗi "##" một slide,
gạch đầu dòng là ý, đoạn văn thường là ghi chú), .pptx (lấy chữ trong từng slide, cần python-pptx).
"""
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
SLIDES_DIR = Path(os.getenv("SLIDES_DIR") or (ROOT / "slides"))
EXTS = (".json", ".md", ".txt", ".pptx")
_STOP = {"slide", "slides", "bo", "bai", "file", "tep", "folder", "thu", "muc", "o", "trong", "tu", "cua", "cai",
         "giup", "anh", "chi", "em", "minh", "nhe", "nha", "di", "cho", "xem", "mo", "ra", "len", "ve", "ten", "la"}


def fold(text: str) -> str:
    """Bỏ dấu tiếng Việt, chữ thường: "Mega Sale 10.10" -> "mega sale 10.10"."""
    t = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "d")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def _tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", fold(text)) if w not in _STOP]


def list_files(base: Optional[Path] = None) -> List[Path]:
    base = base or SLIDES_DIR
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith((".", "~$")) and p.name.lower() != "readme.md":
            out.append(p)
    return out[:500]


def find_files(query: str, base: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Xếp hạng tệp theo mức khớp giữa câu nói và tên thư mục / tên tệp."""
    base = base or SLIDES_DIR
    q = set(_tokens(query))
    results = []
    for p in list_files(base):
        rel = p.relative_to(base)
        folder_toks = set(_tokens(" ".join(rel.parts[:-1])))
        file_toks = set(_tokens(p.stem))
        score = 2.0 * len(q & folder_toks) + 1.5 * len(q & file_toks)
        fq = fold(query)
        if rel.parts[:-1] and fold(rel.parts[-2]) in fq:
            score += 3.0              # tên thư mục xuất hiện nguyên vẹn trong câu nói
        if fold(p.stem) in fq:
            score += 3.0
        if score > 0:
            results.append({"path": str(p), "rel": str(rel).replace(os.sep, "/"), "folder": "/".join(rel.parts[:-1]),
                            "name": p.stem, "score": score})
    results.sort(key=lambda r: (-r["score"], r["rel"]))
    return results


def folders(base: Optional[Path] = None) -> List[str]:
    base = base or SLIDES_DIR
    seen = []
    for p in list_files(base):
        f = "/".join(p.relative_to(base).parts[:-1]) or "(gốc)"
        if f not in seen:
            seen.append(f)
    return seen


# ------------------------------------------------------------------ đọc tệp ---
def _deck_from_markdown(text: str, fallback_title: str) -> Dict[str, Any]:
    title, slides, cur = fallback_title, [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level, heading = len(m.group(1)), m.group(2).strip()
            if level == 1 and not slides and cur is None:
                title = heading
                continue
            cur = {"title": heading, "bullets": [], "notes": ""}
            slides.append(cur)
            continue
        if cur is None:
            cur = {"title": title, "bullets": [], "notes": ""}
            slides.append(cur)
        b = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if b:
            cur["bullets"].append(b.group(1).strip())
        elif line.lstrip().startswith(">"):
            cur["notes"] = (cur["notes"] + " " + line.lstrip("> ").strip()).strip()
        else:
            cur["notes"] = (cur["notes"] + " " + line.strip()).strip()
    return {"title": title, "slides": slides}


def _deck_from_pptx(path: Path) -> Dict[str, Any]:
    from pptx import Presentation  # type: ignore
    prs = Presentation(str(path))
    slides = []
    for sl in prs.slides:
        title, bullets, notes = "", [], ""
        for shape in sl.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for para in shape.text_frame.paragraphs:
                t = "".join(r.text for r in para.runs).strip()
                if not t:
                    continue
                if not title and (shape == getattr(sl.shapes, "title", None) or shape.shape_type is None or
                                  (sl.shapes.title is not None and shape.shape_id == sl.shapes.title.shape_id)):
                    title = t
                else:
                    bullets.append(t)
        if sl.has_notes_slide and sl.notes_slide.notes_text_frame is not None:
            notes = sl.notes_slide.notes_text_frame.text.strip()
        if title or bullets:
            slides.append({"title": title or (bullets.pop(0) if bullets else f"Slide {len(slides) + 1}"),
                           "bullets": bullets, "notes": notes})
    return {"title": path.stem, "slides": slides}


def read_deck(path: str) -> Dict[str, Any]:
    """Đọc một tệp thành dict deck thô {"title", "slides":[{"title","bullets","notes"}]} (chưa chuẩn hóa)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            data = {"title": p.stem, "slides": data}
        if isinstance(data, dict) and not data.get("title"):
            data["title"] = p.stem
        return data
    if ext in (".md", ".txt"):
        return _deck_from_markdown(p.read_text(encoding="utf-8"), p.stem)
    if ext == ".pptx":
        return _deck_from_pptx(p)
    raise ValueError(f"Chưa hỗ trợ định dạng {ext}")
