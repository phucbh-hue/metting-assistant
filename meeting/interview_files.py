"""Đọc file HR tải lên (JD, CV, bảng tiêu chí) thành thứ đưa được cho model.
Port từ interviewer-assistant-AI-circle/interview/files.py (tự chứa, không thêm thư viện nào).

.docx và .xlsx là file zip chứa XML, lấy chữ bằng zipfile + ElementTree. PDF và ảnh KHÔNG tự bóc chữ mà gửi
nguyên file cho Claude (đọc được cả bản scan, giữ bố cục hai cột của CV). File gốc không bao giờ lưu: chỉ đi qua
RAM một request. Gọi read() trong thread (asyncio.to_thread): parse XML vài MB mất cả giây CPU.
"""
import datetime
import io
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Optional
from xml.etree import ElementTree as ET

MAX_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 3_700_000         # Anthropic giới hạn 5MB ảnh SAU base64 (x4/3) -> file ~3.75MB
MAX_PART_BYTES = 4 * 1024 * 1024    # trần GIẢI NÉN cho một phần docx/xlsx (chống zip bomb)
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_CHARS = 60000
TEXT_EXT = {".txt", ".md", ".csv", ".tsv"}
OLD_OFFICE = {".doc", ".xls", ".ppt", ".rtf", ".odt", ".ods"}
BROKEN = "File bị hỏng hoặc định dạng lạ - mở bằng Word/Excel rồi lưu thành file mới, hoặc xuất PDF"


class FileError(ValueError):
    """Lỗi người dùng sửa được (file hỏng, sai loại, quá lớn) - thông báo nói thẳng cách sửa."""


@dataclass
class Doc:
    name: str
    text: str = ""                                    # chữ đã bóc (docx, xlsx, txt)
    parts: list = field(default_factory=list)         # [(bytes, mime)] gửi thẳng cho model (pdf, ảnh)
    kind: str = ""                                    # pdf | image | docx | xlsx | text


def read(name: str, data: bytes) -> Doc:
    name = PurePath(name or "file").name
    if not data:
        raise FileError("File rỗng")
    if len(data) > MAX_BYTES:
        raise FileError(f"File {len(data) / 1e6:.1f}MB - tối đa {MAX_BYTES // 1024 // 1024}MB")
    ext = PurePath(name).suffix.lower()
    if data[:5] == b"%PDF-":
        return Doc(name, parts=[(data, "application/pdf")], kind="pdf")
    img = _image_mime(data)
    if img == "image/gif":
        raise FileError("Ảnh GIF chưa hỗ trợ - dùng PNG hoặc JPG")
    if img:
        if len(data) > MAX_IMAGE_BYTES:
            raise FileError(f"Ảnh {len(data) / 1e6:.1f}MB - tối đa {MAX_IMAGE_BYTES / 1e6:.1f}MB, "
                            "chụp nhỏ lại hoặc xuất PDF")
        return Doc(name, parts=[(data, img)], kind="image")
    if ext in OLD_OFFICE or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise FileError("File Word/Excel đời cũ - mở bằng Word/Excel rồi lưu lại thành .docx/.xlsx, hoặc xuất PDF")
    try:
        if data[:2] == b"PK":
            z = zipfile.ZipFile(io.BytesIO(data))
            names = set(z.namelist())
            budget = _Budget(MAX_TOTAL_BYTES)
            if "word/document.xml" in names:
                return _cap(_docx(name, z, budget))
            if "xl/workbook.xml" in names:
                return _cap(Doc(name, text=_xlsx(z, budget), kind="xlsx"))
            raise FileError("File nén không phải Word (.docx) hay Excel (.xlsx)")
        if ext in TEXT_EXT or ext == "":
            return _cap(Doc(name, text=_decode(data), kind="text"))
    except FileError:
        raise
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, ValueError, KeyError,
            EOFError, OSError, RecursionError, ET.ParseError, UnicodeError):
        raise FileError(BROKEN)
    raise FileError("Chưa đọc được loại file này - dùng PDF, Word (.docx), Excel (.xlsx), ảnh PNG/JPG hoặc file text")


def _image_mime(b: bytes) -> str:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def _cap(d: Doc) -> Doc:
    d.text = re.sub(r"\n{3,}", "\n\n", d.text).strip()
    if len(d.text) > MAX_CHARS:
        d.text = d.text[:MAX_CHARS] + "\n[... phần sau đã bị cắt vì file quá dài]"
    if not d.text and not d.parts:
        raise FileError("Không thấy chữ nào trong file. Nếu là bản scan, xuất ra PDF hoặc ảnh rồi tải lại")
    return d


class _Budget:
    """Tổng số byte còn được giải nén cho một file."""
    def __init__(self, n: int):
        self.left = n


def _zread(z: zipfile.ZipFile, name: str, budget: _Budget, limit: int = MAX_PART_BYTES,
           strict: bool = True) -> Optional[bytes]:
    """Đọc một phần trong zip với trần cứng (không tin kích thước zip tự khai)."""
    cap = min(limit, budget.left)
    info = z.getinfo(name)
    if info.file_size <= cap:
        with z.open(info) as f:
            b = f.read(cap + 1)
        if len(b) <= cap:
            budget.left -= len(b)
            return b
    if strict:
        raise FileError("File có phần nội dung quá lớn - với CV/JD thì thường là file hỏng; "
                        "thử xuất PDF rồi tải lại")
    return None


def _decode(b: bytes) -> str:
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    for enc in ("utf-8-sig", "cp1258"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1")


# --------------------------------------------------------------------- docx ---
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _docx(name: str, z: zipfile.ZipFile, budget: _Budget) -> Doc:
    """Chữ theo đúng thứ tự đọc, kể cả bảng và text box; bỏ chữ ẩn (w:vanish)."""
    root = ET.fromstring(_zread(z, "word/document.xml", budget))
    lines: list = []
    heads: list = []
    for n in sorted(x for x in z.namelist() if re.fullmatch(r"word/header\d*\.xml", x)):
        b = _zread(z, n, budget, limit=512 * 1024, strict=False)
        if b is None:
            continue
        h: list = []
        _walk(ET.fromstring(b), h)
        t = " ".join(" ".join(h).split())
        if t and t not in heads and len(t) < 300:
            heads.append(t)
    lines += heads
    body = root.find(W + "body")
    if body is not None:
        _walk(body, lines)
    d = Doc(name, text="\n".join(lines), kind="docx")
    if len(d.text) < 200:                        # Word chỉ chứa ẢNH của CV: gửi ảnh cho model đọc
        media = []
        for n in z.namelist():
            if n.startswith("word/media/"):
                b = _zread(z, n, budget, limit=MAX_IMAGE_BYTES, strict=False)
                if b and len(b) > 20_000 and _image_mime(b) in ("image/png", "image/jpeg"):
                    media.append((b, _image_mime(b)))
        d.parts = sorted(media, key=lambda x: -len(x[0]))[:3]
    return d


def _walk(el, lines: list):
    for ch in el:
        t = ch.tag
        if t == W + "p":
            _para(ch, lines)
        elif t == W + "tbl":
            _table(ch, lines)
        elif t in (W + "sectPr", MC_FALLBACK):
            continue
        else:
            _walk(ch, lines)


def _hidden(run) -> bool:
    v = run.find(f"{W}rPr/{W}vanish")
    return v is not None and (v.get(W + "val") or "true").lower() not in ("0", "false", "off")


def _para(p, lines: list):
    buf, boxes = [], []

    def rec(e):
        for ch in e:
            t = ch.tag
            if t == MC_FALLBACK:
                continue
            if t == W + "txbxContent":
                boxes.append(ch)
                continue
            if t == W + "r" and _hidden(ch):
                continue
            if t == W + "t":
                buf.append(ch.text or "")
            elif t == W + "tab":
                buf.append("\t")
            elif t in (W + "br", W + "cr"):
                buf.append("\n")
            elif t == W + "noBreakHyphen":
                buf.append("-")
            else:
                rec(ch)
    rec(p)
    s = "\n".join(" ".join(x.split()) for x in "".join(buf).split("\n")).strip()
    if s:
        bullet = "- " if p.find(f"{W}pPr/{W}numPr") is not None else ""
        lines.append(bullet + s)
    for b in boxes:
        _walk(b, lines)


def _table(tbl, lines: list):
    for tr in tbl.findall(W + "tr"):
        cells = []
        for tc in tr.findall(W + "tc"):
            sub: list = []
            _walk(tc, sub)
            if sub:
                cells.append(sub)
        if not cells:
            continue
        if all(len(c) <= 2 and sum(len(x) for x in c) <= 160 for c in cells):
            lines.append(" | ".join(" / ".join(c) for c in cells))
        else:
            for c in cells:
                lines.extend(c)
                lines.append("")


# --------------------------------------------------------------------- xlsx ---
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DATE_FMT_IDS = set(range(14, 23)) | {45, 46, 47}
MAX_ROWS = 400


def _xlsx(z: zipfile.ZipFile, budget: _Budget) -> str:
    """Mỗi sheet một khối, mỗi hàng một dòng "số hàng | CỘT: giá trị" (giữ chữ cái cột cho bảng tiêu chí HR)."""
    def xml(n, strict=False):
        if n not in names:
            return None
        b = _zread(z, n, budget, strict=strict)
        return ET.fromstring(b) if b is not None else None

    names = set(z.namelist())
    shared = []
    ss = xml("xl/sharedStrings.xml", strict=True)
    if ss is not None:
        for si in ss.findall(S + "si"):
            ts = si.findall(S + "t") + [t for r in si.findall(S + "r") for t in r.findall(S + "t")]
            shared.append("".join(t.text or "" for t in ts))
    styles = _num_styles(xml("xl/styles.xml"))
    wb = xml("xl/workbook.xml", strict=True)
    rels = xml("xl/_rels/workbook.xml.rels")
    target = {}
    if rels is not None:
        for r in rels.findall(PKG_REL + "Relationship"):
            tg = r.get("Target", "")
            target[r.get("Id")] = tg.lstrip("/") if tg.startswith("/") else "xl/" + tg
    out = []
    sheets = wb.find(S + "sheets") if wb is not None else None
    for i, sh in enumerate(sheets.findall(S + "sheet") if sheets is not None else []):
        title = sh.get("name", str(i + 1))
        path = target.get(sh.get(R_ID)) or f"xl/worksheets/sheet{i + 1}.xml"
        if path not in names:
            continue
        b = _zread(z, path, budget, strict=False)
        if b is None:
            out.append(f"## Sheet: {title} (quá lớn, đã bỏ qua)")
            continue
        root = ET.fromstring(b)
        rows = []
        for row in root.iter(S + "row"):
            vals = []
            for c in row.findall(S + "c"):
                v = _cell(c, shared, styles)
                if v:
                    col = re.sub(r"\d+", "", c.get("r", "")) or "?"
                    vals.append(f"{col}: {v}")
            if vals:
                rows.append(f"{row.get('r', '?')} | " + " | ".join(vals))
            if len(rows) >= MAX_ROWS:
                rows.append("[... còn nữa, đã cắt]")
                break
        if rows:
            hidden = " (ẩn)" if sh.get("state") in ("hidden", "veryHidden") else ""
            out.append(f"## Sheet: {title}{hidden}\n" + "\n".join(rows))
    return "\n\n".join(out)


def _num_styles(st) -> dict:
    """style index -> "date" | "pct" (Excel lưu ngày là số, % là 0.4 - chỉ định dạng ô mới nói nó là gì)."""
    if st is None:
        return {}
    custom = {}
    fmts = st.find(S + "numFmts")
    for f in (fmts.findall(S + "numFmt") if fmts is not None else []):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", f.get("formatCode", "")).lower()
        fid = _int(f.get("numFmtId"))
        if "%" in code:
            custom[fid] = "pct"
        elif re.search(r"[dy]", code) or (re.search(r"m", code) and not re.search(r"[#0]", code)):
            custom[fid] = "date"
    out = {}
    xfs = st.find(S + "cellXfs")
    for i, xf in enumerate(xfs.findall(S + "xf") if xfs is not None else []):
        nid = _int(xf.get("numFmtId"))
        kind = "date" if nid in DATE_FMT_IDS else "pct" if nid in (9, 10) else custom.get(nid)
        if kind:
            out[i] = kind
    return out


def _int(v, default: int = -1) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _cell(c, shared: list, styles: dict) -> str:
    t = c.get("t", "n")
    v = c.find(S + "v")
    raw = v.text if v is not None else None
    if t == "s":
        i = _int(raw)
        s = shared[i] if 0 <= i < len(shared) else ""
    elif t == "inlineStr":
        s = "".join(x.text or "" for x in c.iter(S + "t"))
    elif raw is None:
        s = ""
    elif t == "b":
        s = "TRUE" if raw == "1" else "FALSE"
    elif t in ("str", "e"):
        s = raw
    else:
        s = _num(raw, styles.get(_int(c.get("s"), 0)))
    return " / ".join(" ".join(x.split()) for x in s.splitlines() if x.strip())


def _num(raw: str, kind: Optional[str]) -> str:
    try:
        f = float(raw)
    except ValueError:
        return raw
    if kind == "date" and 1 <= f < 2958466:
        d = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=f)
        return d.strftime("%d/%m/%Y") + (d.strftime(" %H:%M") if f % 1 else "")
    if kind == "pct":
        return f"{f * 100:.4g}%"
    if f.is_integer():
        return str(int(f))
    return f"{f:.6g}"


# ------------------------------------------------------------ dữ liệu cá nhân ---
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w+])(?:(?:\+84|84|0)[ .\-]?(?:\(0\)[ .\-]?)?(?:\d{9,10}|\d{1,4}(?:[ .\-]\d{1,4}){1,5})"
                      r"|\(0\d{1,3}\)[ .\-]?\d{3,4}(?:[ .\-]?\d{3,4}){1,2})(?!\w)")
DATE_SHAPE = re.compile(r"\b(?:0?[1-9]|1[0-2])[./\-](?:19|20)\d{2}\b")


def _is_phone(s: str) -> bool:
    if DATE_SHAPE.search(s):                      # "06.2016": tháng.năm, không phải SĐT
        return False
    d = re.sub(r"\D", "", s)
    if d.startswith("84"):
        d = "0" + d[2:]
    d = d.replace("00", "0", 1) if d.startswith("00") else d
    return (len(d) == 10 and d[1] in "35789") or (len(d) == 11 and d.startswith("02"))


def scrub_contacts(text: str) -> str:
    """Bỏ email và SĐT khỏi CV (chỉ cần CV để đánh giá năng lực). Không nuốt mốc thời gian '06.2016 - 9.2019'."""
    text = EMAIL_RE.sub("[email đã ẩn]", text or "")
    return PHONE_RE.sub(lambda m: "[SĐT đã ẩn]" if _is_phone(m.group(0)) else m.group(0), text)
