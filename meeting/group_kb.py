"""Tài liệu của nhóm (kho tri thức cho chế độ BD, bản 3.19): bảng giá, hồ sơ năng lực, FAQ, chính sách...

- Chủ nhóm tải lên PDF, Word (.docx), PowerPoint (.pptx), Markdown, .txt. Thành viên xem danh sách.
- Chỉ lưu chữ đã trích (cắt thành đoạn khoảng 900 ký tự, chồng 150 ký tự) trong Atlas (`group_docs`); tệp gốc xóa ngay
  sau khi đọc, không giữ trên server.
- Giới hạn: 25 MB mỗi tệp, 400.000 ký tự mỗi tài liệu, 100 tài liệu mỗi nhóm.
"""
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from meeting import db, decks

EXTS = (".pdf", ".docx", ".pptx", ".md", ".txt")
MAX_BYTES = 25 * 1024 * 1024
MAX_CHARS = 400_000
MAX_DOCS = 100
CHUNK, OVERLAP = 900, 150


def _col():
    return db._get_db()["group_docs"]


def chunks(text: str, size: int = CHUNK, overlap: int = OVERLAP) -> List[str]:
    """Cắt theo đoạn văn; đoạn quá dài thì cắt theo câu. Mỗi phần nối thêm phần cuối của phần trước (chồng `overlap`)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    pieces: List[str] = []
    for p in paras:
        if len(p) <= size:
            pieces.append(p)
            continue
        cur = ""
        for sent in re.split(r"(?<=[.!?;:])\s+|\n", p):
            if len(cur) + len(sent) + 1 > size and cur:
                pieces.append(cur)
                cur = ""
            while len(sent) > size:                  # câu rất dài (bảng, danh sách liền): cắt cứng
                pieces.append(sent[:size])
                sent = sent[size - overlap:]
            cur = f"{cur} {sent}".strip()
        if cur:
            pieces.append(cur)
    out: List[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 2 > size:
            out.append(cur)
            cur = cur[-overlap:].split(" ", 1)[-1] if overlap else ""
        cur = f"{cur}\n\n{piece}".strip() if cur else piece
    if cur:
        out.append(cur)
    return out


def _meta(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: d.get(k) for k in ("id", "group_id", "name", "ext", "size", "chars", "n_chunks", "uploaded_by", "uploaded_at")}


def add(gid: int, name: str, path: str, uploaded_by: Optional[str], size: int = 0) -> Dict[str, Any]:
    """Đọc chữ của tệp, cắt đoạn, lưu. Lỗi đọc / sai định dạng / quá giới hạn: ValueError (viết cho người dùng đọc)."""
    ext = Path(name or "").suffix.lower()
    if ext not in EXTS:
        raise ValueError(f"Chỉ nhận {', '.join(EXTS)}")
    if size > MAX_BYTES:
        raise ValueError("Tệp lớn hơn 25 MB")
    if _col().count_documents({"group_id": int(gid)}) >= MAX_DOCS:
        raise ValueError(f"Mỗi nhóm tối đa {MAX_DOCS} tài liệu: xóa bớt tài liệu cũ trước")
    try:
        text = decks.read_text(path)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Không đọc được tài liệu: {e}")
    text = re.sub(r"[ \t]+", " ", (text or "").replace("\x00", "")).strip()
    if len(text) < 20:
        raise ValueError("Không đọc được chữ trong tài liệu (PDF dạng ảnh chụp thì cần bản có chữ)")
    truncated = len(text) > MAX_CHARS
    text = text[:MAX_CHARS]
    parts = chunks(text)
    doc = {"id": db._next_id("group_docs"), "group_id": int(gid), "name": Path(name).name[:200], "ext": ext,
           "size": int(size), "chars": len(text), "n_chunks": len(parts), "truncated": truncated, "chunks": parts,
           "uploaded_by": (uploaded_by or "").lower() or None, "uploaded_at": time.time()}
    _col().insert_one(dict(doc))
    from meeting import retrieval
    retrieval.forget(gid)
    return _meta(doc)


def list_docs(gid: int) -> List[Dict[str, Any]]:
    return [_meta(d) for d in _col().find({"group_id": int(gid)}, {"_id": 0, "chunks": 0}).sort("uploaded_at", -1)]


def delete(gid: int, doc_id: int) -> bool:
    ok = _col().delete_one({"group_id": int(gid), "id": int(doc_id)}).deleted_count > 0
    if ok:
        from meeting import retrieval
        retrieval.forget(gid)
    return ok


def passages(gid: int) -> List[Dict[str, Any]]:
    out = []
    for d in _col().find({"group_id": int(gid)}, {"_id": 0, "id": 1, "name": 1, "chunks": 1}):
        for i, text in enumerate(d.get("chunks") or []):
            out.append({"kind": "kb", "doc_id": d["id"], "title": d["name"], "ref": f"đoạn {i + 1}", "text": text})
    return out


def signature(gid: int) -> Tuple[int, float]:
    docs = list(_col().find({"group_id": int(gid)}, {"_id": 0, "id": 1, "uploaded_at": 1}))
    return len(docs), max([float(d.get("uploaded_at") or 0) for d in docs] + [0.0])


def delete_group(gid: int) -> None:
    _col().delete_many({"group_id": int(gid)})
