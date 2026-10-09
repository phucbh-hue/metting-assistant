"""Xuất biên bản và ghi âm để lưu vào thư mục của cuộc họp (Google Drive của nhóm, thư mục trên máy người dùng).

- Biên bản: Markdown do AI viết (cấu trúc MINUTES_SYSTEM: tiêu đề, đoạn văn, gạch đầu dòng, danh sách số, bảng, chữ
  đậm) -> HTML để Drive chuyển thành Google Docs, hoặc -> Word (.docx) để lưu trên máy.
- Ghi âm: nối các lượt ghi PCM 16 kHz của cuộc họp thành một tệp MP3 (libsndfile qua soundfile, khoảng 5 MB/giờ nói).
- Tên thư mục cuộc họp: "dd-mm-yyyy HHhMM - Tiêu đề" theo giờ Việt Nam, bỏ ký tự Windows / Drive không cho dùng.
"""
import html as _html
import io
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TZ = timezone(timedelta(hours=float(os.getenv("APP_TZ_OFFSET", "7") or 7)))    # giờ Việt Nam (không đổi giờ)
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SEP_CELL = re.compile(r":?-{2,}:?")


# ------------------------------------------------------------------ Markdown -> khối ---
def md_blocks(md: str) -> List[Tuple[str, Any]]:
    """[(loại, nội dung)]: h1-h3 (chữ), p (chữ), ul / ol (danh sách chữ), table (danh sách hàng), hr."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    blocks: List[Tuple[str, Any]] = []
    para: List[str] = []

    def flush():
        if para:
            blocks.append(("p", " ".join(para)))
            para.clear()

    i = 0
    while i < len(lines):
        st = lines[i].strip()
        if not st:
            flush()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", st)
        if m:
            flush()
            blocks.append((f"h{min(len(m.group(1)), 3)}", m.group(2).strip().rstrip("#").strip()))
            i += 1
            continue
        if st.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(_SEP_CELL.fullmatch(c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        for kind, pat in (("ul", r"^[-*+]\s+(.*)$"), ("ol", r"^\d+[.)]\s+(.*)$")):
            if re.match(pat, st):
                flush()
                items = []
                while i < len(lines) and re.match(pat, lines[i].strip()):
                    items.append(re.match(pat, lines[i].strip()).group(1).strip())
                    i += 1
                blocks.append((kind, items))
                break
        else:
            if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", st):
                flush()
                blocks.append(("hr", ""))
            else:
                para.append(st)
            i += 1
    flush()
    return blocks


def _inline_html(text: str) -> str:
    t = _html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", t)


def md_to_html(md: str, title: str = "Biên bản cuộc họp") -> str:
    """HTML đầy đủ: tải lên Drive với mimeType Google Docs thì Drive chuyển thành tài liệu sửa được."""
    out = []
    for kind, val in md_blocks(md):
        if kind in ("h1", "h2", "h3"):
            out.append(f"<{kind}>{_inline_html(val)}</{kind}>")
        elif kind == "p":
            out.append(f"<p>{_inline_html(val)}</p>")
        elif kind in ("ul", "ol"):
            out.append(f"<{kind}>" + "".join(f"<li>{_inline_html(x)}</li>" for x in val) + f"</{kind}>")
        elif kind == "table":
            head, body = val[0], val[1:]
            out.append('<table border="1" cellpadding="5"><tr>' + "".join(f"<th>{_inline_html(c)}</th>" for c in head) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</table>")
        elif kind == "hr":
            out.append("<hr>")
    css = ("body{font-family:Arial,sans-serif;font-size:11pt;line-height:1.4}table{border-collapse:collapse}"
           "th,td{border:1px solid #999;padding:4px 6px;vertical-align:top}th{background:#eee}")
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{_html.escape(title)}</title><style>{css}</style>'
            f"</head><body>{''.join(out)}</body></html>")


def _runs(paragraph, text: str, bold_all: bool = False):
    """Chữ trong dòng có **đậm** thành các đoạn chạy (run) của Word."""
    for part in re.split(r"(\*\*.+?\*\*)", text):
        if not part:
            continue
        is_bold = part.startswith("**") and part.endswith("**") and len(part) > 4
        run = paragraph.add_run((part[2:-2] if is_bold else part).replace("`", ""))
        run.bold = bool(is_bold or bold_all) or None


def md_to_docx_bytes(md: str) -> bytes:
    import docx
    d = docx.Document()
    for kind, val in md_blocks(md):
        if kind in ("h1", "h2", "h3"):
            h = d.add_heading(level=int(kind[1]))
            _runs(h, val)
        elif kind == "p":
            _runs(d.add_paragraph(), val)
        elif kind in ("ul", "ol"):
            for x in val:
                _runs(d.add_paragraph(style="List Bullet" if kind == "ul" else "List Number"), x)
        elif kind == "table":
            cols = max(len(r) for r in val)
            t = d.add_table(rows=len(val), cols=cols)
            t.style = "Table Grid"
            for ri, row in enumerate(val):
                for ci in range(cols):
                    cell = t.cell(ri, ci)
                    cell.text = ""
                    _runs(cell.paragraphs[0], row[ci] if ci < len(row) else "", bold_all=ri == 0)
        elif kind == "hr":
            d.add_paragraph("")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- ghi âm ---
def recording_mp3(meeting_id: int) -> Optional[Path]:
    """Nối các lượt ghi của cuộc họp thành một MP3 trong thư mục tạm (xóa bằng discard). Không có ghi âm: None."""
    import numpy as np
    import soundfile as sf
    from meeting import recording
    runs = recording.runs(meeting_id)
    if not runs:
        return None
    out = Path(tempfile.mkdtemp(prefix="mc-rec-")) / "recording.mp3"
    with sf.SoundFile(str(out), "w", samplerate=recording.RATE, channels=1, format="MP3", subtype="MPEG_LAYER_III") as f:
        for r in runs:
            with open(r["path"], "rb") as fh:
                while True:
                    chunk = fh.read(recording.RATE * 2 * 60)          # 1 phút một lần: không nạp cả giờ vào RAM
                    if not chunk:
                        break
                    f.write(np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype="<i2"))
    return out


def discard(path: Optional[Path]) -> None:
    if path is not None and Path(path).parent.name.startswith("mc-rec-"):
        shutil.rmtree(Path(path).parent, ignore_errors=True)


# ------------------------------------------------------------------- tên thư mục ---
def clean_title(text: Any, fallback: str = "Cuộc họp", limit: int = 100) -> str:
    t = _FORBIDDEN.sub("-", str(text or ""))
    t = re.sub(r"\s+", " ", t).strip().rstrip(". ")
    return (t or fallback)[:limit].rstrip(". ") or fallback


def folder_name(meeting: Dict[str, Any]) -> str:
    """"dd-mm-yyyy HHhMM - Tiêu đề" theo giờ Việt Nam, tối đa 120 ký tự."""
    try:
        dt = datetime.fromtimestamp(float(meeting.get("started_at") or 0), TZ)
        when = dt.strftime("%d-%m-%Y %Hh%M")
    except (TypeError, ValueError, OSError):
        when = "Không rõ ngày"
    return f"{when} - {clean_title(meeting.get('title'))}"[:120].rstrip(". ")
