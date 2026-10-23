"""Bộ slide "UrBox Meeting Copilot" trình bày với DH/LM (19/10/2026), kèm kịch bản.

Nội dung: điểm khác biệt; tài liệu của nhóm và ứng dụng WikiBox (WikiBox là ứng dụng hỗ trợ, tách rời Meeting Copilot);
truy cập WikiBox lấy dữ liệu; DH/LM quản lý cuộc họp của thành viên; bot Recall.ai tính phí; lưu trữ realtime và sau họp.

Đầu ra (docs/pitch/):
- "UrBox Meeting Copilot - Trinh bay 19.10.2026.pptx": chữ, hình, bảng là đối tượng thật nên sửa được; tải lên Google
  Drive rồi Mở bằng Google Trang trình bày. Ghi chú người trình bày của từng slide là lời nói (script).
- kich-ban-trinh-bay.md và .docx: kịch bản đầy đủ (thời lượng, thông điệp, lời nói, câu hỏi dự kiến). Tải .docx lên
  Drive và mở bằng Google Tài liệu nếu cần.
- Ảnh xem trước (data/pitch_build/preview, không đưa lên git): Chromium dựng cùng tọa độ, cùng font Be Vietnam Pro
  (Google Slides có sẵn font này) để soát chữ tràn khung.

Chạy: python docs/pitch/build_pitch.py   (cần python-pptx, python-docx, Pillow, playwright + chromium, mạng để tải font)
Sửa nội dung: sửa các hàm s01..s13 (bố cục, chữ trên slide) và NOTES / QA / TIME (lời nói, câu hỏi dự kiến).
"""
import base64
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BUILD = REPO / "data" / "pitch_build"
ASSETS = BUILD / "assets"
PREVIEW = BUILD / "preview"
NAME = "UrBox Meeting Copilot - Trinh bay 19.10.2026"
DATE = "19/10/2026"
STATUS_DATE = "23/10/2026"                 # trạng thái kết nối WikiBox
OWNER = "phuc.bh@urbox.vn"

W, H = 13.333, 7.5                      # 16:9, đơn vị inch
FONT = "Be Vietnam Pro"                 # font của UrBox; Google Slides có sẵn
PURPLE, PURPLE2, DEEP = "6821C2", "8235E4", "4B1393"
GOLD, INK, MUTED = "FFB700", "1F1B2E", "5F5A73"
BG2, TINT, LINE, WHITE = "F5F3FA", "EFE7FB", "E4DDF2", "FFFFFF"
GOLDTINT, SOFT = "FFF4D6", "E9DDFB"
MARGIN = 0.6
CW = W - 2 * MARGIN                     # bề rộng vùng nội dung

ICONS = {  # biểu tượng nét 24x24 (cùng bộ với bộ slide demo, kiểu urbox.vn)
    "mic": '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
    "ask": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6"/><path d="M12 16h.01"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "cloud": '<path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>',
    "spark": '<path d="M12 3l1.9 5.8L20 10l-6.1 1.2L12 17l-1.9-5.8L4 10l6.1-1.2z"/>',
    "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "video": '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="M16 10l6-4v12l-6-4z"/>',
    "wave": '<path d="M2 12h2M6 8v8M10 5v14M14 8v8M18 10v4M22 12h0"/>',
    "db": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "plug": '<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v5"/>',
    "gauge": '<path d="M12 14l4-4"/><path d="M3.3 19a10 10 0 1 1 17.4 0"/>',
    "coin": '<circle cx="12" cy="12" r="9"/><path d="M14.5 9a2.5 2 0 0 0-2.5-1.5c-1.5 0-2.5.8-2.5 2s1 1.7 2.5 2 2.5.8 2.5 2-1 2-2.5 2A2.5 2 0 0 1 9.5 15"/><path d="M12 6v1.5M12 16.5V18"/>',
    "layers": '<path d="M12 2l10 5-10 5L2 7z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    "flag": '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/>',
}


# ================================================================ mô hình slide ===
@dataclass
class Para:
    runs: List[Tuple[str, Dict[str, Any]]]
    size: float = 14
    color: str = INK
    bold: bool = False
    align: str = "left"
    after: float = 0            # khoảng sau đoạn (pt)
    bullet: bool = False
    line: float = 1.1           # giãn dòng (bội số)


@dataclass
class Text:
    x: float
    y: float
    w: float
    h: float
    paras: List[Para]
    valign: str = "top"
    inset: float = 0.0
    name: str = ""


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float
    fill: Optional[str] = None
    line: Optional[str] = None
    line_w: float = 1.0
    radius: float = 0.0
    shape: str = "rect"         # rect | oval | arrow_r | arrow_lr
    text: Optional[Text] = None  # chữ nằm trong hình (huy hiệu, nhãn)
    name: str = ""


@dataclass
class Pic:
    path: Path
    x: float
    y: float
    w: float
    h: float
    name: str = ""


@dataclass
class Table:
    x: float
    y: float
    cols: List[float]
    rows: List[List[str]]
    row_h: float = 0.5
    size: float = 13
    name: str = ""


@dataclass
class Slide:
    items: List[Any] = field(default_factory=list)
    notes: str = ""
    title: str = ""
    message: str = ""
    dark: bool = False

    def add(self, *items):
        self.items.extend(items)
        return items[-1] if items else None


def runs_of(text: str, color: Optional[str] = None, bold: Optional[bool] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """'**đậm**' trong chuỗi thành đoạn chữ đậm."""
    out, b = [], False
    for part in text.split("**"):
        if part:
            st: Dict[str, Any] = {}
            if b or bold:
                st["bold"] = True
            if color:
                st["color"] = color
            out.append((part, st))
        b = not b
    return out


def T(x, y, w, h, text, size=14, color=INK, bold=False, align="left", valign="top", after=0, line=1.1,
      bullet=False, inset=0.0, name="") -> Text:
    """Ô chữ; text là chuỗi (xuống dòng = đoạn mới) hoặc danh sách Para."""
    if isinstance(text, str):
        paras = [Para(runs_of(t), size=size, color=color, bold=bold, align=align, after=after, bullet=bullet, line=line)
                 for t in text.split("\n")]
    else:
        paras = text
    return Text(x, y, w, h, paras, valign=valign, inset=inset, name=name)


# ================================================================== tài nguyên ===
def build_assets() -> None:
    """Biểu tượng PNG (Chromium vẽ SVG), linh vật, nền chuyển màu, ảnh chụp màn hình bo góc."""
    from playwright.sync_api import sync_playwright
    ASSETS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 600, "height": 600})
        for name, path in ICONS.items():
            for tone, color in (("p", "#" + PURPLE), ("w", "#FFFFFF"), ("g", "#" + GOLD)):
                pg.set_content(f'<html><body style="margin:0;background:transparent"><svg id="i" width="256" height="256" '
                               f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" '
                               f'stroke-linejoin="round">{path}</svg></body></html>')
                pg.locator("#i").screenshot(path=str(ASSETS / f"ic-{name}-{tone}.png"), omit_background=True)
        svg = (REPO / "static" / "mascot" / "urbox-mascot.svg").read_text(encoding="utf-8")
        svg = re.sub(r'<svg width="\d+" height="\d+"', '<svg id="m" width="672" height="656"', svg, count=1)
        pg.set_viewport_size({"width": 800, "height": 800})
        pg.set_content(f'<html><body style="margin:0;background:transparent">{svg}</body></html>')
        pg.locator("#m").screenshot(path=str(ASSETS / "mascot.png"), omit_background=True)
        b.close()
    # nền bìa: tím UrBox chuyển màu chéo, thêm quầng sáng nhẹ
    w, h = 2000, 1125
    img = Image.new("RGB", (w, h))
    top, bot = (0x4B, 0x13, 0x93), (0x86, 0x3B, 0xE6)
    px = img.load()
    for yy in range(h):
        for xx in range(0, w, 4):
            t = min(1.0, max(0.0, (xx / w) * 0.55 + (yy / h) * 0.45))
            c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
            for k in range(4):
                if xx + k < w:
                    px[xx + k, yy] = c
    glow = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glow).ellipse((1180, 80, 1980, 880), fill=120)
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img = Image.composite(Image.new("RGB", (w, h), (0xB0, 0x7A, 0xF5)), img, glow)
    img.save(ASSETS / "cover-bg.jpg", quality=92)
    # ảnh chụp màn hình: cắt đúng tỉ lệ khung, bo góc
    for src, out, ratio in ((REPO / "docs/demo/img/bd-panel.png", "shot-bd.png", 7.2 / 4.25),
                            (REPO / "docs/huong-dan/img/14-bien-ban-thay-doi.jpg", "shot-minutes.png", 5.85 / 3.66)):
        im = Image.open(src).convert("RGB")
        iw, ih = im.size
        if iw / ih > ratio:
            nw = int(ih * ratio)
            im = im.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
        else:
            im = im.crop((0, 0, iw, int(iw / ratio)))
        im = im.resize((1600, int(1600 / ratio)), Image.LANCZOS)
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1), radius=34, fill=255)
        im.putalpha(mask)
        im.save(ASSETS / out)
    for tone in ("purple", "white"):
        Image.open(REPO / f"docs/demo/img/urbox-logo-{tone}.png").save(ASSETS / f"logo-{tone}.png")


def ic(name: str, tone: str = "p") -> Path:
    return ASSETS / f"ic-{name}-{tone}.png"


# ================================================================ khung dùng chung ===
TOTAL = 13


def header(s: Slide, eyebrow: str, title: str, sub: str = "") -> None:
    s.title = title
    ew = 0.42 + 0.098 * len(eyebrow)
    s.add(Box(MARGIN, 0.42, ew, 0.36, fill=TINT, radius=0.18, name="eyebrow",
              text=T(0, 0, 0, 0, eyebrow, size=12, color=PURPLE, bold=True, align="center", valign="middle")))
    s.add(T(MARGIN, 0.86, CW, 0.7, title, size=32, bold=True, color=INK, line=1.0, name="title"))
    if sub:
        s.add(T(MARGIN, 1.56, CW, 0.5, sub, size=16, color=MUTED, line=1.05, name="subtitle"))


def footer(s: Slide, n: int) -> None:
    s.add(Pic(ASSETS / "logo-purple.png", MARGIN, 6.98, 0.93, 0.26, name="logo"))
    s.add(T(1.7, 6.97, 6.0, 0.3, f"Meeting Copilot · {DATE}", size=10, color=MUTED, valign="middle", name="footer"))
    s.add(T(W - MARGIN - 1.2, 6.97, 1.2, 0.3, f"{n} / {TOTAL}", size=10, color=MUTED, align="right", valign="middle",
            name="page"))


def badge(x: float, y: float, d: float, text: str, fill: str = GOLD, color: str = INK, size: float = 14) -> Box:
    return Box(x, y, d, d, fill=fill, shape="oval", name="badge",
               text=T(0, 0, 0, 0, text, size=size, color=color, bold=True, align="center", valign="middle"))


def tile(x: float, y: float, d: float, icon: str, fill: str = TINT, tone: str = "p") -> List[Any]:
    pad = d * 0.22
    return [Box(x, y, d, d, fill=fill, radius=d * 0.28, name="tile"),
            Pic(ic(icon, tone), x + pad, y + pad, d - 2 * pad, d - 2 * pad, name="icon")]


def pill(x: float, y: float, w: float, text: str, fill: str, color: str, size: float = 11, h: float = 0.32) -> Box:
    return Box(x, y, w, h, fill=fill, radius=h / 2, name="pill",
               text=T(0, 0, 0, 0, text, size=size, color=color, bold=True, align="center", valign="middle"))


# ==================================================================== các slide ===
def s01() -> Slide:
    s = Slide(dark=True, title="UrBox Meeting Copilot")
    s.add(Pic(ASSETS / "cover-bg.jpg", 0, 0, W, H, name="background"))
    s.add(Pic(ASSETS / "logo-white.png", 0.8, 0.6, 1.3, 0.364, name="logo"))
    s.add(pill(0.8, 1.45, 3.55, f"Trình bày nội bộ · {DATE}", GOLD, INK, size=13, h=0.42))
    s.add(T(0.8, 2.08, 8.2, 0.95, "UrBox Meeting Copilot", size=44, bold=True, color=WHITE, line=1.0, name="title"))
    s.add(T(0.8, 3.2, 7.2, 1.45, "Trợ lý AI trong cuộc họp: hỗ trợ ngay lúc họp, lưu trữ cả realtime lẫn sau buổi họp, "
            "dùng ứng dụng WikiBox làm kho tri thức", size=20, color=SOFT, line=1.15, name="subtitle"))
    s.add(T(0.8, 4.95, 7.2, 0.4, "Dành cho DH/LM và các phòng ban của UrBox", size=15, color=WHITE, bold=True,
            name="audience"))
    s.add(T(0.8, 6.45, 7.2, 0.4, f"Phụ trách: {OWNER} · Bản 3.21", size=13, color=SOFT, name="owner"))
    s.add(Box(8.75, 1.35, 3.9, 3.9, fill="7A3BD8", shape="oval", name="halo"))
    s.add(Pic(ASSETS / "mascot.png", 9.25, 1.85, 2.9, 2.83, name="mascot"))
    return s


def s02() -> Slide:
    s = Slide(message="Sáu điểm chính, đi theo đúng thứ tự trình bày.")
    header(s, "Tóm tắt", "Sáu điểm chính của Meeting Copilot",
           "Mỗi điểm có slide riêng phía sau, số trên thẻ là thứ tự trình bày")
    items = [
        ("spark", "Điểm khác biệt", "Hỗ trợ ngay lúc họp, bằng dữ liệu của UrBox"),
        ("book", "Tài liệu nhóm và WikiBox", "Hai nguồn tách rời, mỗi nguồn một dấu riêng"),
        ("search", "Truy cập WikiBox", "Tra tài liệu chính thức ngay trong buổi họp"),
        ("users", "DH/LM quản lý cuộc họp", "Nhóm theo phòng ban, biên bản cùng một chuẩn"),
        ("coin", "Bot Recall.ai", "Bot tính phí 0,5 USD mỗi giờ, một bot mỗi cuộc họp"),
        ("db", "Lưu trữ", "Lưu từng câu khi nói, biên bản lên Drive sau họp"),
    ]
    cw, ch, gap = (CW - 0.6) / 3, 2.08, 0.3
    for i, (icon, head, body) in enumerate(items):
        x = MARGIN + (i % 3) * (cw + gap)
        y = 2.3 + (i // 3) * (ch + 0.22)
        s.add(Box(x, y, cw, ch, fill=BG2, radius=0.2, name="card"))
        s.add(badge(x + 0.3, y + 0.28, 0.46, str(i + 1)))
        s.add(*tile(x + cw - 0.9, y + 0.22, 0.6, icon))
        s.add(T(x + 0.3, y + 0.9, cw - 0.6, 0.42, head, size=18, bold=True, name="card-head"))
        s.add(T(x + 0.3, y + 1.32, cw - 0.6, 0.7, body, size=14, color=MUTED, line=1.05, name="card-body"))
    footer(s, 2)
    return s


def s03() -> Slide:
    s = Slide(message="Khác biệt nằm ở hỗ trợ ngay lúc họp bằng dữ liệu của UrBox; ghi chép là phần cơ bản.")
    header(s, "1 · Khác biệt", "Hỗ trợ ngay lúc họp, bằng dữ liệu của UrBox",
           "So với Gemini trong Google Meet, Otter, Fireflies, tl;dv: mạnh ở chép lời, tóm tắt sau buổi họp")
    items = [
        ("ask", "Gợi ý trả lời khách sau 4-8 giây",
         "Bảng BD lấy từ tài liệu nhóm, cuộc họp trước và WikiBox, có dấu nguồn; chỉ đội mình thấy"),
        ("mic", "Trợ lý giọng nói tiếng Việt",
         "Gọi \"UrBox ơi\" để tra cứu, tóm tắt, soạn slide, sơ đồ, dashboard ngay trên màn hình họp"),
        ("users", "Biết chính xác ai đang nói",
         "Mẫu giọng dùng chung cả công ty, cả họp trực tiếp; đúng 97-98% số câu ở 2 cuộc họp thật"),
        ("history", "Nhớ xuyên các cuộc họp",
         "Biên bản tự nhắc khi buổi này nói khác điều nhóm đã chốt ở 12 buổi gần nhất"),
        ("video", "Họp kết hợp, một bot cho tất cả",
         "Mic trong phòng cùng người online qua Meet; nhiều người dùng Copilot chỉ cần một bot"),
        ("shield", "Quản lý theo nhóm, dữ liệu UrBox",
         "Nhóm theo phòng ban, mẫu biên bản chuẩn; dữ liệu ở Atlas, Drive của UrBox, đăng nhập @urbox.vn"),
    ]
    cw, ch, gap = (CW - 0.6) / 3, 1.98, 0.3
    for i, (icon, head, body) in enumerate(items):
        x = MARGIN + (i % 3) * (cw + gap)
        y = 2.25 + (i // 3) * (ch + 0.18)
        s.add(Box(x, y, cw, ch, fill=BG2, radius=0.2, name="card"))
        s.add(*tile(x + 0.28, y + 0.28, 0.6, icon))
        s.add(T(x + 1.02, y + 0.26, cw - 1.24, 0.66, head, size=16, bold=True, valign="middle", line=1.0,
                name="card-head"))
        s.add(T(x + 0.28, y + 0.98, cw - 0.5, 0.95, body, size=14, color=MUTED, line=1.05, name="card-body"))
    s.add(T(MARGIN, 6.5, CW, 0.32, "Chép lời, tóm tắt, bot Google Meet, lưu Drive là phần cơ bản, công cụ khác cũng có. "
            "Điểm mạnh là gộp tất cả trên dữ liệu và quy trình của UrBox.", size=11.5, color=MUTED, name="note"))
    footer(s, 3)
    return s


def s04() -> Slide:
    s = Slide(message="BD trả lời được ngay cả câu chưa nắm, và biết nguồn để tin.")
    header(s, "1 · Ví dụ trong buổi họp", "Khách hỏi điều BD chưa nắm: vài giây là có gợi ý",
           "Bảng BD mở trên máy của đội mình, không chiếu lên màn hình chung, trợ lý không đọc thành tiếng")
    x, w = MARGIN, 4.75
    s.add(T(x, 2.3, w, 0.3, "KHÁCH HỎI", size=11, bold=True, color=PURPLE, name="step"))
    s.add(Box(x, 2.62, w, 0.92, fill=BG2, radius=0.18, name="bubble",
              text=T(0, 0, 0, 0, "\"Phí tích hợp API bao nhiêu, có khác lần trước không?\"", size=15, valign="middle",
                     inset=0.18)))
    s.add(T(x, 3.75, w, 0.3, "SAU 4-8 GIÂY (ĐO THẬT)", size=11, bold=True, color=PURPLE, name="step"))
    s.add(Box(x, 4.07, w, 1.62, fill=WHITE, line=PURPLE, line_w=1.25, radius=0.18, name="answer"))
    s.add(T(x + 0.2, 4.17, w - 0.4, 0.62, "**5.000.000đ, thu một lần**, gồm hỗ trợ 24/7 [K1]", size=15, line=1.05,
            name="answer-text"))
    s.add(T(x + 0.2, 4.82, w - 0.4, 0.82, "Ghi chú cho đội: buổi 02/10 đội mình đã báo 4.000.000đ [M1], nên giải thích "
            "phần chênh lệch", size=13, color=MUTED, line=1.05, name="answer-note"))
    s.add(T(x, 5.9, w, 0.3, "BD TRẢ LỜI NGAY", size=11, bold=True, color=PURPLE, name="step"))
    s.add(T(x, 6.2, w, 0.62, "Không phải hẹn \"em kiểm tra rồi gửi lại\", và biết thông tin lấy từ đâu", size=14,
            line=1.05, name="result"))
    s.add(Pic(ASSETS / "shot-bd.png", 5.6, 2.3, 7.13, 4.21, name="screenshot"))
    s.add(T(5.6, 6.55, 7.13, 0.32, "K: tài liệu nhóm · M: cuộc họp trước · U: WikiBox · W: trên mạng · dữ liệu mẫu",
            size=11.5, color=MUTED, name="legend"))
    footer(s, 4)
    return s


def s05() -> Slide:
    s = Slide(message="Tài liệu riêng của nhóm tải lên Copilot; tài liệu chung của công ty đọc từ ứng dụng WikiBox.")
    header(s, "2 · Tài liệu nhóm và WikiBox", "Hai nguồn tri thức, hai nơi quản lý riêng",
           "Tài liệu riêng của nhóm tải lên Meeting Copilot; tài liệu chung của công ty đọc từ ứng dụng WikiBox")
    cw = (CW - 0.3) / 2
    cards = [
        (MARGIN, "folder", "Tài liệu của nhóm", ("Trong Meeting Copilot", TINT, PURPLE), [
            "Chủ nhóm tải lên: bảng giá, hồ sơ năng lực, FAQ, JD",
            "PDF, Word, PowerPoint, .txt; tối đa 25 MB, 100 tài liệu",
            "Chỉ giữ phần chữ, không giữ tệp gốc",
            "Dùng cho Bảng BD, hỏi trên mọi cuộc họp (dấu K)"]),
        (MARGIN + cw + 0.3, "plug", "Ứng dụng WikiBox (wiki.urbox.dev)", ("Ứng dụng riêng", GOLD, INK), [
            "Kho chính thức: sản phẩm, quy trình, chính sách",
            "Mỗi người tự kết nối tài khoản WikiBox của mình",
            "Chỉ đọc, đúng quyền xem của người đó trên WikiBox",
            "Dùng cho trợ lý và Bảng BD (dấu U)"]),
    ]
    for x, icon, head, (tag, tfill, tcolor), lines in cards:
        s.add(Box(x, 2.3, cw, 3.05, fill=BG2, radius=0.2, name="card"))
        s.add(*tile(x + 0.3, 2.55, 0.62, icon))
        s.add(T(x + 1.1, 2.5, cw - 1.4, 0.4, head, size=18, bold=True, name="card-head"))
        s.add(pill(x + 1.1, 2.93, 0.4 + 0.088 * len(tag), tag, tfill, tcolor))
        s.add(T(x + 0.3, 3.5, cw - 0.6, 1.8, "\n".join(lines), size=14, bullet=True, after=9, line=1.05,
                name="card-list"))
    s.add(Box(MARGIN, 5.6, CW, 1.05, fill=WHITE, line=LINE, line_w=1.0, radius=0.18, name="where"))
    s.add(T(MARGIN + 0.3, 5.7, 2.0, 0.85, "Để ở đâu?", size=16, bold=True, color=PURPLE, valign="middle", name="where-h"))
    s.add(T(2.55, 5.7, 4.9, 0.85, "Riêng của nhóm hoặc của một khách (giá riêng, điều khoản, JD) → **tài liệu của nhóm**",
            size=14, valign="middle", line=1.05, name="where-a"))
    s.add(T(7.65, 5.7, 4.95, 0.85, "Chung cả công ty, cần một bản chuẩn → **WikiBox**, không tải bản sao vào Copilot",
            size=14, valign="middle", line=1.05, name="where-b"))
    footer(s, 5)
    return s


def s06() -> Slide:
    s = Slide(message="WikiBox là ứng dụng hỗ trợ, nối qua cổng chỉ đọc; hai bên không ghi đè nội dung của nhau.")
    header(s, "2 · Nguyên tắc thiết kế", "WikiBox là ứng dụng hỗ trợ, không gộp vào Copilot",
           "Hai ứng dụng độc lập, nối với nhau qua một cổng chỉ đọc, để nội dung không chồng chéo")
    bw, bh, y = 4.55, 1.78, 2.3
    s.add(Box(MARGIN, y, bw, bh, fill=PURPLE, radius=0.2, name="copilot"))
    s.add(T(MARGIN + 0.3, y + 0.18, bw - 0.6, 0.42, "UrBox Meeting Copilot", size=18, bold=True, color=WHITE,
            name="copilot-h"))
    s.add(T(MARGIN + 0.3, y + 0.66, bw - 0.6, 1.05, "Cuộc họp, lời thoại, biên bản\nTài liệu riêng của nhóm\n"
            "Nhóm, phân quyền, mẫu giọng", size=13.5, color=SOFT, after=2, line=1.05, name="copilot-list"))
    rx = W - MARGIN - bw
    s.add(Box(rx, y, bw, bh, fill=WHITE, line=PURPLE, line_w=1.5, radius=0.2, name="wikibox"))
    s.add(T(rx + 0.3, y + 0.18, bw - 0.6, 0.42, "Ứng dụng WikiBox", size=18, bold=True, color=PURPLE, name="wikibox-h"))
    s.add(T(rx + 0.3, y + 0.66, bw - 0.6, 1.05, "wiki.urbox.dev\nSản phẩm, quy trình, chính sách\n"
            "Do các phòng ban tự cập nhật", size=13.5, color=MUTED, after=2, line=1.05, name="wikibox-list"))
    mx, mw = MARGIN + bw + 0.2, rx - (MARGIN + bw + 0.2) - 0.2
    s.add(T(mx, y + 0.1, mw, 0.34, "Cổng MCP", size=14, bold=True, color=PURPLE, align="center", name="gate"))
    s.add(Box(mx + 0.1, y + 0.55, mw - 0.2, 0.5, fill=GOLD, shape="arrow_lr", name="arrow"))
    s.add(T(mx, y + 1.15, mw, 0.6, "chỉ đọc · theo quyền từng người", size=12, color=MUTED, align="center", line=1.0,
            name="gate-sub"))
    rules = [
        ("Một chiều, chỉ đọc", "Copilot không thêm, sửa, xóa gì trên WikiBox và không ghi dữ liệu họp sang đó"),
        ("Không sao chép", "Hỏi lúc nào đọc lúc đó nên luôn là bản mới nhất, không có bản cũ nằm trong Copilot"),
        ("Mỗi nguồn một dấu", "K tài liệu nhóm, M cuộc họp trước, U WikiBox: người nghe biết thông tin từ đâu"),
        ("Khi hai nguồn khác nhau", "Giá, điều khoản riêng cho khách theo K; kiến thức chung theo U; chỗ khác ghi cho đội"),
        ("Tách được bất cứ lúc nào", "WikiBox chậm quá 8 giây hoặc ngắt kết nối thì Copilot vẫn chạy bằng nguồn còn lại"),
    ]
    for i, (head, body) in enumerate(rules):
        yy = 4.32 + i * 0.5
        s.add(badge(MARGIN, yy + 0.03, 0.36, str(i + 1), fill=TINT, color=PURPLE, size=12))
        s.add(T(MARGIN + 0.55, yy, 3.25, 0.42, head, size=14.5, bold=True, valign="middle", name="rule-h"))
        s.add(T(MARGIN + 3.85, yy, CW - 3.85, 0.42, body, size=14, color=MUTED, valign="middle", name="rule-t"))
    footer(s, 6)
    return s


def s07() -> Slide:
    s = Slide(message="Trợ lý và Bảng BD lấy dữ liệu từ WikiBox ngay trong buổi họp, mỗi ý có nguồn U.")
    header(s, "3 · Truy cập WikiBox", "Lấy dữ liệu từ WikiBox ngay trong buổi họp",
           "Trợ lý và Bảng BD tìm trên WikiBox bằng tài khoản của người tạo cuộc họp")
    steps = [
        ("ask", "Hỏi", "\"UrBox ơi, chính sách đổi voucher là gì?\" hoặc khách hỏi"),
        ("search", "Tìm trên WikiBox", "Máy chủ Copilot tìm, cùng lúc với tài liệu nhóm và các buổi trước"),
        ("spark", "Trả lời có nguồn", "AI chỉ dùng đoạn tìm được, gắn U1, U2 sau từng ý"),
        ("file", "Mở tài liệu gốc", "Bấm U1 để mở đúng trang trên WikiBox"),
    ]
    gap = 0.34
    cw = (CW - 3 * gap) / 4
    for i, (icon, head, body) in enumerate(steps):
        x = MARGIN + i * (cw + gap)
        s.add(Box(x, 2.3, cw, 2.2, fill=BG2, radius=0.2, name="step"))
        s.add(badge(x + 0.25, 2.5, 0.4, str(i + 1)))
        s.add(*tile(x + cw - 0.8, 2.45, 0.55, icon))
        s.add(T(x + 0.25, 3.08, cw - 0.5, 0.4, head, size=16, bold=True, name="step-h"))
        s.add(T(x + 0.25, 3.48, cw - 0.45, 0.98, body, size=13.5, color=MUTED, line=1.05, name="step-t"))
        if i < 3:
            s.add(Box(x + cw + 0.07, 3.22, 0.2, 0.36, fill=PURPLE2, shape="arrow_r", name="flow"))
    bw = (CW - 0.3) / 2
    s.add(Box(MARGIN, 4.75, bw, 2.02, fill=WHITE, line=LINE, radius=0.2, name="connect"))
    s.add(*tile(MARGIN + 0.28, 4.98, 0.55, "plug"))
    s.add(T(MARGIN + 1.0, 4.98, bw - 1.3, 0.5, "Kết nối một lần", size=16, bold=True, valign="middle", name="connect-h"))
    s.add(T(MARGIN + 0.28, 5.62, bw - 0.56, 1.05, "Cài đặt > Kho tri thức: đăng nhập bằng Google hoặc API key\n"
            "Chỉ đọc, đúng quyền của mình; ngắt lúc nào cũng được",
            size=13, color=MUTED, after=3, bullet=True, line=1.05, name="connect-t"))
    x2 = MARGIN + bw + 0.3
    s.add(Box(x2, 4.75, bw, 2.02, fill=GOLDTINT, radius=0.2, name="status"))
    s.add(*tile(x2 + 0.28, 4.98, 0.55, "gauge", fill=WHITE))
    s.add(T(x2 + 1.0, 4.98, bw - 1.3, 0.5, f"Trạng thái {STATUS_DATE}", size=16, bold=True, valign="middle", name="status-h"))
    s.add(T(x2 + 0.28, 5.62, bw - 0.56, 1.12, "WikiBox đã cho Copilot đăng nhập trực tiếp bằng tài khoản Google\n"
            "Đã tích hợp qua MCP; cần chạy thử với tài liệu thật",
            size=13, color=INK, after=3, bullet=True, line=1.05, name="status-t"))
    footer(s, 7)
    return s


def s08() -> Slide:
    s = Slide(message="DH/LM nắm được cuộc họp của cả nhóm mà không phải dự từng buổi.")
    header(s, "4 · DH/LM quản lý cuộc họp", "Nắm cuộc họp của cả nhóm, không phải dự từng buổi",
           "Dành cho trưởng phòng (DH) và quản lý trực tiếp (LM) làm chủ nhóm")
    rows = [
        ("users", "Nhóm theo phòng ban, dự án", "DH/LM là chủ nhóm; họp định kỳ trên lịch tự vào nhóm"),
        ("file", "Biên bản cùng một chuẩn", "Quyết định, việc cần làm, người phụ trách, hạn chót"),
        ("history", "Không sót điều đã chốt", "Biên bản tự nhắc khi nói khác điều đã chốt trước đó"),
        ("search", "Hỏi trên mọi cuộc họp", "\"Nhóm đã hứa gì với khách?\" (nhóm bật chế độ BD)"),
        ("folder", "Thư mục Drive của nhóm", "Biên bản tự lưu, tự chia sẻ cho thành viên"),
    ]
    for i, (icon, head, body) in enumerate(rows):
        y = 2.3 + i * 0.88
        s.add(*tile(MARGIN, y + 0.05, 0.56, icon))
        s.add(T(MARGIN + 0.75, y, 5.25, 0.36, head, size=15.5, bold=True, name="row-h"))
        s.add(T(MARGIN + 0.75, y + 0.36, 5.25, 0.5, body, size=13.5, color=MUTED, line=1.0, name="row-t"))
    s.add(Pic(ASSETS / "shot-minutes.png", 6.88, 2.3, 5.85, 3.66, name="screenshot"))
    s.add(T(6.88, 6.08, 5.85, 0.62, "Biên bản trong ứng dụng (dữ liệu mẫu): bảng phân công và mục Thay đổi so với các "
            "buổi trước", size=11.5, color=MUTED, line=1.05, name="caption"))
    footer(s, 8)
    return s


def s09() -> Slide:
    s = Slide(message="Nhóm thì chia sẻ, cá nhân thì riêng; DH/LM chỉ thấy cuộc họp được đưa vào nhóm.")
    header(s, "4 · Phân quyền", "Nhóm thì chia sẻ, cá nhân thì riêng", "Ai xem được gì trong Meeting Copilot")
    rows = [
        ["Nội dung", "DH/LM (chủ nhóm)", "Thành viên nhóm", "Người ngoài nhóm"],
        ["Cuộc họp và biên bản của nhóm", "Xem, quản lý", "Xem, tạo cuộc họp", "Không xem được"],
        ["Thư mục Drive của nhóm", "Chủ thư mục", "Được chia sẻ quyền sửa", "Không"],
        ["Tài liệu của nhóm", "Tải lên, xóa", "Xem danh sách", "Không"],
        ["Mẫu biên bản của nhóm", "Tạo, sửa, đặt mặc định", "Dùng khi lập biên bản", "Không"],
        ["Hỏi trên mọi cuộc họp của nhóm", "Có (bật chế độ BD)", "Không", "Không"],
        ["Cuộc họp cá nhân của mỗi người", "Không, trừ khi đưa vào nhóm", "Chỉ người tạo", "Không"],
    ]
    s.add(Table(MARGIN, 2.25, [3.7, 2.9, 2.9, 2.633], rows, row_h=0.5, size=13.5, name="permissions"))
    s.add(Box(MARGIN, 5.95, CW, 0.82, fill=BG2, radius=0.18, name="note-box"))
    s.add(*tile(MARGIN + 0.22, 6.08, 0.56, "video"))
    s.add(T(MARGIN + 0.98, 5.97, CW - 1.2, 0.78, "Nhiều người dùng Copilot trong cùng một cuộc họp Google Meet: mỗi người một "
            "cuộc họp riêng, biên bản vào Drive của mình. Cuộc họp thuộc nhóm thì dùng chung một biên bản trong thư mục "
            "Drive của chủ nhóm.", size=13, valign="middle", line=1.05, name="note"))
    footer(s, 9)
    return s


def s10() -> Slide:
    s = Slide(message="Recall.ai là bot tính phí khoảng 0,5 USD mỗi giờ, một bot cho cả cuộc họp; cần nạp credit.")
    header(s, "5 · Recall.ai", "Recall.ai: bot tính phí ghi lại Google Meet",
           "Dịch vụ bên ngoài, chỉ dùng khi họp online; họp trực tiếp dùng mic trình duyệt, không cần bot")
    steps = [
        ("calendar", "Hẹn bot", "Theo lịch (bật \"Bot tự vào cuộc họp\") hoặc dán link Google Meet"),
        ("users", "Xin vào như khách", "Người trong cuộc họp bấm Cho vào; bot báo trong chat là cuộc họp được chép lời"),
        ("wave", "Chép lời từng người", "Mỗi người một luồng, đúng tên trong Meet, về Copilot theo thời gian thực"),
        ("check", "Tự rời, tự dọn", "Mọi người rời 60 giây thì bot rời, biên bản lập ngay; bản ghi trên Recall xóa ngay"),
    ]
    for i, (icon, head, body) in enumerate(steps):
        y = 2.3 + i * 0.93
        s.add(badge(MARGIN, y + 0.08, 0.4, str(i + 1)))
        s.add(*tile(MARGIN + 0.55, y + 0.02, 0.52, icon))
        s.add(T(MARGIN + 1.25, y, 4.95, 0.36, head, size=15.5, bold=True, name="step-h"))
        s.add(T(MARGIN + 1.25, y + 0.36, 4.95, 0.52, body, size=13.5, color=MUTED, line=1.0, name="step-t"))
    x, w = 7.0, W - MARGIN - 7.0
    s.add(Box(x, 2.3, w, 3.05, fill=PURPLE, radius=0.22, name="cost"))
    s.add(T(x + 0.35, 2.45, w - 0.7, 0.35, "CHI PHÍ THAM KHẢO", size=11.5, bold=True, color=SOFT, name="cost-label"))
    s.add(T(x + 0.35, 2.78, w - 0.7, 0.75, "0,5 USD / giờ", size=36, bold=True, color=GOLD, line=1.0, name="cost-big"))
    s.add(T(x + 0.35, 3.58, w - 0.7, 1.7, "Khoảng 12.500đ mỗi giờ bot ở trong cuộc họp\n"
            "80 giờ có bot mỗi tháng: khoảng 40 USD, tức 1.000.000đ\n"
            "Một bot cho cả cuộc họp, dù nhiều người dùng Copilot", size=13.5, color=WHITE, after=5, bullet=True,
            line=1.05, name="cost-list"))
    s.add(T(x, 5.43, w, 0.5, f"Theo trang giá Recall.ai {DATE}, tỷ giá 25.000đ/USD; chép lời Soniox tính riêng. "
            "Ngân sách cần Tài chính xác nhận.", size=11, color=MUTED, line=1.05, name="cost-note"))
    s.add(Box(MARGIN, 6.12, CW, 0.66, fill=GOLDTINT, radius=0.18, name="status"))
    s.add(T(MARGIN + 0.3, 6.14, CW - 0.6, 0.62, "**Hiện trạng:** tài khoản Recall.ai của công ty hết credit từ 13/10/2026, "
            "bot chưa vào được Google Meet cho tới khi nạp thêm.", size=13.5, valign="middle", name="status-t"))
    footer(s, 10)
    return s


def s11() -> Slide:
    s = Slide(message="Dữ liệu được lưu ngay khi nói, và có biên bản, ghi âm, Drive sau buổi họp.")
    header(s, "6 · Lưu trữ", "Lưu trữ cả trong lúc họp và sau buổi họp",
           "Realtime: từng câu lưu ngay khi nói xong. Sau buổi: biên bản, ghi âm, Google Drive, máy tính")
    cw = (CW - 0.3) / 2
    cols = [
        (MARGIN, "clock", "Trong lúc họp (realtime)", [
            "Mỗi câu lưu ngay vào MongoDB Atlas của UrBox khi vừa nói xong",
            "Tên người nói cập nhật liền trên lời thoại đã lưu",
            "Rớt mạng: tự nối lại, phát lại phần âm thanh vừa lỡ (tối đa 15 giây)",
            "Nhiều người cùng xem trực tiếp; mỗi người dùng Copilot có cuộc họp riêng",
            "Ghi âm lưu song song, chỉ khi mọi người đồng ý"]),
        (MARGIN + cw + 0.3, "folder", "Sau buổi họp", [
            "Biên bản tự lập theo mẫu ngay khi kết thúc",
            "Google Drive: nhóm vào thư mục của chủ nhóm, cá nhân vào Drive của mình",
            "Tải Word, MP3; tự lưu về thư mục trên máy",
            "Ghi âm tự xóa sau 30 ngày; biên bản giữ lâu dài",
            "Tìm lại, hỏi lại cuộc họp cũ; xuất Markdown, JSON"]),
    ]
    for x, icon, head, lines in cols:
        s.add(Box(x, 2.3, cw, 3.7, fill=BG2, radius=0.2, name="card"))
        s.add(*tile(x + 0.3, 2.52, 0.6, icon))
        s.add(T(x + 1.08, 2.52, cw - 1.4, 0.6, head, size=18, bold=True, valign="middle", name="card-head"))
        s.add(T(x + 0.3, 3.32, cw - 0.6, 2.6, "\n".join(lines), size=14, bullet=True, after=5, line=1.05,
                name="card-list"))
    chips = ["Lưu ở Atlas, Drive của UrBox", "Giọng nói chỉ lưu vector số", "Ghi âm cần mọi người đồng ý",
             "Recall xóa bản ghi ngay"]
    gw = (CW - 3 * 0.2) / 4
    for i, c in enumerate(chips):
        s.add(Box(MARGIN + i * (gw + 0.2), 6.22, gw, 0.55, fill=WHITE, line=LINE, radius=0.27, name="chip",
                  text=T(0, 0, 0, 0, c, size=12, color=PURPLE, bold=True, align="center", valign="middle")))
    footer(s, 11)
    return s


def s12() -> Slide:
    s = Slide(message="Năm việc để triển khai: credit Recall, kết nối WikiBox, nhóm theo phòng ban, thí điểm, phát triển tiếp.")
    header(s, "Bước tiếp theo", "Đề xuất triển khai và việc cần quyết định", "Thí điểm 2 tuần trước khi mở rộng")
    rows = [
        ("Nạp credit Recall.ai", "Ngân sách tham khảo khoảng 1.000.000đ cho 80 giờ họp có bot mỗi tháng",
         "Quản trị viên, Tài chính"),
        ("Kết nối WikiBox", "Mỗi người đăng nhập WikiBox bằng Google trong Cài đặt; chạy thử với tài liệu thật",
         "Từng người dùng"),
        ("Mỗi phòng ban một nhóm", "DH/LM làm chủ nhóm, thêm thành viên, chọn mẫu biên bản, tải tài liệu riêng",
         "DH/LM"),
        ("Thí điểm 2 tuần", "2-3 phòng ban (ví dụ BD, Engineering); đo số cuộc họp, thời gian có biên bản, "
         "số gợi ý dùng được", "Phụ trách dự án"),
        ("Phát triển tiếp", "Mở \"Hỏi trên mọi cuộc họp\" cho mọi nhóm; xử lý các trường hợp hiếm có 2 bot",
         "Đội phát triển"),
    ]
    for i, (head, body, who) in enumerate(rows):
        y = 2.3 + i * 0.88
        s.add(Box(MARGIN, y, CW, 0.76, fill=BG2 if i % 2 == 0 else WHITE, line=None if i % 2 == 0 else LINE,
                  radius=0.18, name="row"))
        s.add(badge(MARGIN + 0.22, y + 0.17, 0.42, str(i + 1)))
        s.add(T(MARGIN + 0.85, y, 3.0, 0.76, head, size=15.5, bold=True, valign="middle", name="row-h"))
        s.add(T(MARGIN + 3.9, y, 5.85, 0.76, body, size=13.5, color=MUTED, valign="middle", line=1.0, name="row-t"))
        s.add(pill(W - MARGIN - 2.3, y + 0.21, 2.1, who, TINT, PURPLE, size=11, h=0.34))
    footer(s, 12)
    return s


def s13() -> Slide:
    s = Slide(dark=True, title="Cảm ơn anh chị", message="Hỏi đáp.")
    s.add(Pic(ASSETS / "cover-bg.jpg", 0, 0, W, H, name="background"))
    s.add(Pic(ASSETS / "logo-white.png", 0.8, 0.6, 1.3, 0.364, name="logo"))
    s.add(T(0.8, 2.0, 7.5, 1.0, "Cảm ơn anh chị", size=48, bold=True, color=WHITE, line=1.0, name="title"))
    s.add(T(0.8, 3.05, 7.5, 0.6, "Hỏi đáp", size=26, bold=True, color=GOLD, name="subtitle"))
    s.add(T(0.8, 4.05, 7.6, 1.9, "Ứng dụng: urbox-meeting-api.onrender.com (đăng nhập @urbox.vn)\n"
            "Hướng dẫn: mục 10.6 (bot Meet dùng chung), mục 13.4 (WikiBox)\n"
            f"Phụ trách: {OWNER}", size=15, color=SOFT, after=6, line=1.1, name="info"))
    s.add(Box(8.75, 1.35, 3.9, 3.9, fill="7A3BD8", shape="oval", name="halo"))
    s.add(Pic(ASSETS / "mascot.png", 9.25, 1.85, 2.9, 2.83, name="mascot"))
    return s


SLIDES = [s01, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11, s12, s13]

# ========================================================== lời nói (script) ===
TIME = ["0:45", "1:00", "2:00", "1:30", "1:30", "1:45", "1:30", "1:45", "1:15", "1:45", "1:30", "1:30", "Hỏi đáp"]

NOTES = [
    # 1
    "Dạ em chào anh chị. Hôm nay em trình bày về UrBox Meeting Copilot, trợ lý AI dùng trong các cuộc họp của UrBox. "
    "Trong khoảng mười tám phút, em đi qua sáu nội dung: điểm khác biệt so với các công cụ ghi chú họp đang có; cách dùng "
    "tài liệu của nhóm và ứng dụng WikiBox; cách trợ lý lấy dữ liệu từ WikiBox; cách giúp DH và LM quản lý cuộc họp của các "
    "thành viên; chi phí bot Recall.ai; và cách lưu trữ dữ liệu cả trong lúc họp lẫn sau buổi họp. Cuối buổi em có đề "
    "xuất triển khai và các việc cần anh chị quyết định.",
    # 2
    "Đây là sáu điểm chính. Một, Meeting Copilot hỗ trợ ngay trong lúc họp bằng dữ liệu của UrBox, không chỉ ghi chép "
    "sau buổi. Hai, tri thức đến từ hai nguồn tách rời: tài liệu riêng của nhóm tải lên Copilot, và ứng dụng WikiBox của "
    "công ty. Ba, trợ lý tra được WikiBox ngay trong buổi, có nguồn. Bốn, DH và LM nắm được cuộc họp của cả nhóm. Năm, "
    "bot Recall.ai là dịch vụ tính phí, khoảng nửa đô la mỗi giờ, một bot cho cả cuộc họp. Sáu, dữ liệu được lưu ngay khi "
    "nói và có biên bản trên Google Drive sau buổi họp.",
    # 3
    "Các công cụ như Gemini trong Google Meet, Otter hay Fireflies đã làm tốt phần chép lời và tóm tắt sau buổi họp. "
    "Meeting Copilot khác ở sáu điểm. Thứ nhất, khi khách hỏi, đội BD có gợi ý trả lời sau bốn đến tám giây, lấy từ tài "
    "liệu nhóm, các buổi họp trước và WikiBox, có dấu nguồn. Thứ hai, trợ lý nghe tiếng Việt: chỉ cần gọi \"UrBox ơi\" là "
    "tra cứu, tóm tắt, soạn slide, sơ đồ ngay trên màn hình họp. Thứ ba, hệ thống biết ai đang nói nhờ mẫu giọng dùng "
    "chung của công ty, kể cả khi họp trực tiếp; đo trên hai cuộc họp thật nội bộ, đúng chín mươi bảy và chín mươi tám "
    "phần trăm số câu. Thứ tư, biên bản tự nhắc khi buổi này nói khác điều đã chốt. Thứ năm, họp kết hợp trong phòng và "
    "online, nhiều người cùng dùng Copilot chỉ cần một bot. Thứ sáu, quản lý theo nhóm, dữ liệu nằm trên hạ tầng của "
    "UrBox. Em không khẳng định công cụ khác không có từng phần; điểm mạnh là gộp tất cả trên dữ liệu và quy trình của "
    "UrBox.",
    # 4
    "Đây là một ví dụ cụ thể. Khách hỏi: phí tích hợp API bao nhiêu, có khác lần trước không. Chỉ bốn đến tám giây sau, "
    "trên Bảng BD mở ở máy của đội mình đã có gợi ý: năm triệu đồng, thu một lần, lấy từ bảng giá của nhóm, dấu K1. Trợ "
    "lý còn ghi chú: buổi ngày hai tháng mười đội mình đã báo bốn triệu, nên giải thích phần chênh lệch, dấu M1. Mỗi ý đều "
    "có dấu nguồn: K là tài liệu nhóm, M là cuộc họp trước, U là WikiBox, W là thông tin trên mạng khi đội bấm tra thêm. "
    "Bảng này không chiếu lên màn hình chung và trợ lý không đọc thành tiếng, nên khách không thấy.",
    # 5
    "Về tài liệu, Copilot dùng hai nguồn và hai nơi quản lý riêng. Bên trái là tài liệu của nhóm: chủ nhóm tải lên bảng "
    "giá, hồ sơ năng lực, FAQ, JD; hệ thống chỉ giữ phần chữ, không giữ tệp gốc. Bên phải là WikiBox, một ứng dụng riêng của "
    "công ty tại wiki.urbox.dev. Copilot không chứa WikiBox mà kết nối vào: mỗi người "
    "tự kết nối tài khoản WikiBox của mình, chỉ đọc, đúng quyền xem. Nguyên tắc chọn nơi để tài liệu: cái gì riêng của nhóm "
    "hay của một khách thì để ở tài liệu nhóm; cái gì chung cả công ty, cần một bản chuẩn, thì để trên WikiBox và không "
    "tải bản sao vào Copilot.",
    # 6
    "Đây là nguyên tắc thiết kế quan trọng nhất: WikiBox là ứng dụng hỗ trợ, không gộp vào Meeting Copilot. Hai bên độc "
    "lập, nối qua một cổng MCP chỉ đọc. Một, Copilot chỉ đọc, không thêm sửa xóa gì trên WikiBox và không ghi dữ liệu họp "
    "sang đó. Hai, không sao chép: hỏi lúc nào đọc lúc đó, nên luôn là bản mới nhất. Ba, mỗi nguồn một dấu riêng để người "
    "nghe biết thông tin từ đâu. Bốn, khi hai nguồn nói khác nhau: giá và điều khoản riêng cho khách theo tài liệu nhóm, "
    "kiến thức chung theo WikiBox, chỗ khác nhau được ghi cho đội. Năm, tách được: WikiBox chậm quá tám giây hay ngắt kết "
    "nối thì Copilot vẫn chạy bằng các nguồn còn lại. Nhờ vậy nội dung hai bên không chồng chéo.",
    # 7
    "Lấy dữ liệu từ WikiBox trong buổi họp gồm bốn bước. Người họp hỏi bằng giọng nói, ví dụ \"UrBox ơi, quy trình đổi "
    "voucher hết hạn thế nào?\", hoặc khách hỏi và Bảng BD tự nhận ra. Máy chủ Copilot tìm trên WikiBox bằng tài khoản của "
    "người tạo cuộc họp, song song với tài liệu nhóm và các buổi trước. AI chỉ dùng các đoạn tìm được và gắn dấu U1, U2. "
    "Bấm U1 là mở đúng tài liệu gốc trên WikiBox. Kết nối chỉ làm một lần trong Cài đặt: đăng nhập "
    "WikiBox bằng tài khoản Google công ty, hoặc dán API key. Về trạng thái: WikiBox đã cho Copilot đăng nhập trực tiếp; "
    "cần chạy thử với tài liệu thật.",
    # 8
    "Với DH và LM, mục tiêu là nắm được cuộc họp của cả nhóm mà không phải dự từng buổi. DH hoặc LM làm chủ nhóm theo "
    "phòng ban hay dự án; các buổi họp định kỳ trên lịch tự vào đúng nhóm. Biên bản theo một mẫu chuẩn của nhóm, luôn có "
    "quyết định, việc cần làm, người phụ trách và hạn chót. Biên bản tự nhắc khi buổi này nói khác điều đã chốt trước đó. "
    "Chủ nhóm hỏi trên mọi cuộc họp, ví dụ \"tuần này nhóm đã hứa gì với khách\", và nhận báo cáo có nguồn từng buổi; hiện "
    "tính năng này cần bật chế độ BD của nhóm. Mọi biên bản tự lưu vào thư mục Drive của nhóm và tự chia sẻ cho thành viên.",
    # 9
    "Về phân quyền, nguyên tắc là nhóm thì chia sẻ, cá nhân thì riêng. Chủ nhóm, tức DH hoặc LM, xem và quản lý cuộc họp, "
    "tài liệu, mẫu biên bản của nhóm. Thành viên xem được mọi cuộc họp và biên bản của nhóm, dùng mẫu biên bản chung. "
    "Người ngoài nhóm không xem được gì. Cuộc họp cá nhân của mỗi người chỉ người đó xem; DH, LM chỉ thấy khi cuộc họp "
    "được đưa vào nhóm. Khi nhiều người cùng dùng Copilot trong một cuộc họp Google Meet, mỗi người có cuộc họp riêng và "
    "biên bản vào Drive của mình; cuộc họp thuộc nhóm thì dùng chung một biên bản trong thư mục của chủ nhóm.",
    # 10
    "Bot Google Meet dùng Recall.ai, một dịch vụ bên ngoài có tính phí. Bot được hẹn theo lịch hoặc bằng link, xin vào "
    "như khách, chép lời từng người đúng tên trong Meet, và tự rời sau sáu mươi giây khi mọi người rời; bản ghi trên Recall "
    "bị xóa ngay. Chi phí theo trang giá của Recall là nửa đô la mỗi giờ bot ở trong cuộc họp, tính theo giây, khoảng "
    "mười hai nghìn năm trăm đồng. Ví dụ tám mươi giờ họp có bot mỗi tháng là khoảng bốn mươi đô la, tức khoảng một triệu "
    "đồng. Nhiều người dùng Copilot trong cùng cuộc họp vẫn chỉ một bot. Họp trực tiếp thì dùng mic trình duyệt, không cần "
    "bot. Lưu ý hiện trạng: tài khoản Recall của công ty hết credit từ mười ba tháng mười, cần nạp để bot hoạt động lại.",
    # 11
    "Lưu trữ có hai lớp. Trong lúc họp, mỗi câu được lưu ngay vào MongoDB Atlas của UrBox khi vừa nói xong, không chờ hết "
    "buổi; tên người nói cập nhật liền; rớt mạng thì tự nối lại và phát lại phần âm thanh vừa lỡ. Sau buổi họp, biên bản "
    "tự lập theo mẫu và lưu lên Google Drive: cuộc họp nhóm vào thư mục của chủ nhóm, cuộc họp cá nhân vào Drive của mình; "
    "tải được bản Word, ghi âm, và tự lưu về thư mục trên máy. Ghi âm chỉ bật khi mọi người đồng ý và tự xóa sau ba mươi "
    "ngày; giọng nói chỉ lưu dạng vector số, không lưu âm thanh mẫu.",
    # 12
    "Em đề xuất năm việc. Một, nạp credit Recall.ai, ngân sách tham khảo khoảng một triệu đồng cho tám mươi giờ họp có bot "
    "mỗi tháng, cần phòng Tài chính xác nhận. Hai, mỗi người kết nối WikiBox một lần trong Cài đặt, đăng nhập "
    "bằng tài khoản Google công ty, và chạy thử với tài liệu thật. Ba, mỗi phòng ban tạo một nhóm, DH hoặc LM làm chủ nhóm, chọn mẫu biên bản và tải "
    "tài liệu riêng. Bốn, thí điểm hai tuần với hai, ba phòng ban, đo số cuộc họp, thời gian có biên bản và số gợi ý dùng "
    "được. Năm, đội phát triển mở tính năng hỏi trên mọi cuộc họp cho mọi nhóm.",
    # 13
    "Em cảm ơn anh chị đã lắng nghe, em xin nhận câu hỏi. Ứng dụng chạy tại địa chỉ trên màn hình, đăng nhập bằng tài "
    "khoản @urbox.vn. Hướng dẫn chi tiết ở mục 10.6 cho bot Google Meet dùng chung và mục 13.4 cho WikiBox.",
]

QA = [
    [],
    [],
    [("Khác gì Gemini ghi chú trong Google Meet?",
      "Gemini mạnh ở ghi chú, tóm tắt cuộc họp online. Copilot tập trung vào lúc đang họp, dùng tài liệu và lịch sử họp "
      "của UrBox, chạy cả họp trực tiếp, có nhóm và phân quyền theo UrBox."),
     ("Điểm nào công cụ khác thật sự không làm được?",
      "Không khẳng định tuyệt đối. Gợi ý trả lời, nhớ xuyên cuộc họp, tra WikiBox cần dữ liệu nội bộ của UrBox mà công cụ "
      "bên ngoài không có sẵn; nhận ra người nói dùng hồ sơ giọng chung của công ty, chạy cả khi họp trực tiếp.")],
    [("AI có bịa giá hay chính sách không?",
      "Chỉ dùng nguồn trong tài liệu nhóm, các buổi trước và WikiBox; mỗi ý có dấu nguồn bấm xem được. Thiếu thông tin thì "
      "gợi ý \"bên em kiểm tra và gửi lại\", độ chắc thấp."),
     ("Mỗi câu gợi ý tốn bao nhiêu?",
      "Đo ngày 10-11/10/2026 với Claude Sonnet 5.5: 0,007-0,013 USD mỗi câu (khoảng 250đ), 4-8 giây.")],
    [("Sao không đưa hết tài liệu vào Copilot cho tiện?",
      "Hai bản sao sẽ lệch nhau theo thời gian. WikiBox là nơi chuẩn của công ty nên Copilot chỉ đọc lúc cần; tài liệu "
      "nhóm dành cho thứ riêng của nhóm hoặc của khách mà WikiBox không có."),
     ("Đăng nhập WikiBox thế nào?",
      "Cài đặt > Kho tri thức WikiBox > Kết nối WikiBox, đăng nhập bằng tài khoản Google công ty, chọn không gian làm việc, "
      "bấm Cho phép. Quyền chỉ đọc, thu hồi được bất cứ lúc nào.")],
    [("Nếu WikiBox và tài liệu nhóm nói khác nhau thì sao?",
      "Giá, điều khoản riêng cho khách theo tài liệu nhóm (K); kiến thức chung theo WikiBox (U); chỗ khác nhau ghi trong "
      "phần ghi chú cho đội để người họp quyết định."),
     ("Copilot có làm thay đổi nội dung WikiBox không?",
      "Không. Kết nối chỉ đọc (quyền tìm và đọc), không có thao tác thêm, sửa, xóa.")],
    [("Ai thấy được tài liệu WikiBox qua Copilot?",
      "Copilot tìm bằng quyền của người tạo cuộc họp. Câu trả lời hiện trong phòng họp đó, nên người khác trong phòng cũng "
      "thấy; với tài liệu nhạy cảm, người tạo cuộc họp cân nhắc trước khi hỏi."),
     ("Có tốn thêm phí AI không?",
      "Chỉ phần chữ tìm được được gửi kèm câu hỏi cho AI; chi phí mỗi câu tăng không đáng kể. Dùng được với mọi nguồn AI "
      "của ứng dụng.")],
    [("DH có xem được cuộc họp riêng của nhân viên không?",
      "Không. Chỉ cuộc họp thuộc nhóm; người tạo cuộc họp hoặc chủ nhóm mới chuyển cuộc họp vào nhóm."),
     ("Nhóm không phải BD có dùng \"Hỏi trên mọi cuộc họp\" được không?",
      "Hiện cần bật chế độ BD trong Cài đặt nhóm (bật được cho mọi nhóm). Đề xuất tách riêng tính năng này ở slide 12.")],
    [("Thành viên rời nhóm thì sao?",
      "Chủ nhóm bớt thành viên: người đó không xem được cuộc họp của nhóm nữa, quyền trên thư mục Drive của nhóm cũng "
      "được gỡ.")],
    [("Có cách nào giảm chi phí bot?",
      "Bot chỉ vào buổi có bật, bấm \"Bỏ qua buổi này\" cho buổi không cần; họp trực tiếp không cần bot; một bot cho cả "
      "cuộc họp; bot tự rời sau 60 giây khi mọi người rời."),
     ("Recall có giữ nội dung cuộc họp không?",
      "Bản ghi trên Recall giữ tối đa 1 giờ và bị xóa ngay khi bot xong. Copilot không dùng dịch vụ chép lời của Recall "
      "(chép lời bằng Soniox).")],
    [("Dữ liệu có rời khỏi UrBox không?",
      "Âm thanh đi qua Soniox để chép lời và qua Recall khi dùng bot; lời thoại gửi tới dịch vụ AI để lập biên bản và trả "
      "lời. Lưu trữ lâu dài ở MongoDB Atlas và Google Drive của UrBox. Nội dung pháp lý (Nghị định 13/2023) chỉ để tham "
      "khảo, cần bộ phận pháp chế xác nhận khi áp dụng rộng."),
     ("Mất điện, mất mạng giữa buổi thì sao?",
      "Câu đã nói xong đã nằm trong cơ sở dữ liệu. Kết nối tự nối lại; bản chạy trên máy còn lưu tạm xuống máy khi mất "
      "kết nối cơ sở dữ liệu.")],
    [("Thí điểm đo thế nào?",
      "Số cuộc họp dùng Copilot, thời gian từ lúc kết thúc tới khi có biên bản, số gợi ý BD được dùng, phản hồi của DH/LM "
      "sau 2 tuần.")],
    [],
]


# ============================================================== xuất PPTX ===
def to_pptx(slides: List[Slide], path: Path) -> None:
    from lxml import etree
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Emu, Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(W), Inches(H)
    prs.core_properties.title = "UrBox Meeting Copilot"
    prs.core_properties.author = OWNER
    prs.core_properties.subject = f"Trình bày nội bộ {DATE}: khác biệt, WikiBox, DH/LM, Recall.ai, lưu trữ"
    blank = prs.slide_layouts[6]
    align = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
    anchor = {"top": MSO_ANCHOR.TOP, "middle": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}
    shapes = {"rect": MSO_SHAPE.RECTANGLE, "oval": MSO_SHAPE.OVAL, "arrow_r": MSO_SHAPE.RIGHT_ARROW,
              "arrow_lr": MSO_SHAPE.LEFT_RIGHT_ARROW}

    def bullet(p, color: str):
        pPr = p._p.get_or_add_pPr()
        pPr.set("marL", str(int(Inches(0.24))))
        pPr.set("indent", str(-int(Inches(0.24))))
        clr = etree.SubElement(pPr, qn("a:buClr"))
        etree.SubElement(clr, qn("a:srgbClr")).set("val", color)
        etree.SubElement(pPr, qn("a:buSzPct")).set("val", "100000")
        etree.SubElement(pPr, qn("a:buFont")).set("typeface", "Arial")
        etree.SubElement(pPr, qn("a:buChar")).set("char", "•")

    def fill_text(tf, t: Text, default_inset: float = 0.0):
        tf.word_wrap = True
        tf.auto_size = MSO_AUTO_SIZE.NONE
        ins = t.inset or default_inset
        tf.margin_left = tf.margin_right = Inches(ins)
        tf.margin_top = tf.margin_bottom = Inches(min(ins, 0.05))
        tf.vertical_anchor = anchor[t.valign]
        for i, para in enumerate(t.paras):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align[para.align]
            p.line_spacing = para.line
            p.space_after = Pt(para.after)
            if para.bullet:
                bullet(p, PURPLE if para.color != WHITE else GOLD)
            for txt, st in para.runs:
                r = p.add_run()
                r.text = txt
                f = r.font
                f.name = FONT
                f.size = Pt(st.get("size", para.size))
                f.bold = bool(st.get("bold", para.bold))
                f.color.rgb = RGBColor.from_string(st.get("color", para.color))

    for sl in slides:
        slide = prs.slides.add_slide(blank)
        for it in sl.items:
            if isinstance(it, Pic):
                pic = slide.shapes.add_picture(str(it.path), Inches(it.x), Inches(it.y), Inches(it.w), Inches(it.h))
                pic.name = it.name or pic.name
            elif isinstance(it, Box):
                kind = MSO_SHAPE.ROUNDED_RECTANGLE if (it.shape == "rect" and it.radius) else shapes[it.shape]
                shp = slide.shapes.add_shape(kind, Inches(it.x), Inches(it.y), Inches(it.w), Inches(it.h))
                shp.name = it.name or shp.name
                if kind == MSO_SHAPE.ROUNDED_RECTANGLE:
                    shp.adjustments[0] = min(0.5, it.radius / min(it.w, it.h))
                if it.fill:
                    shp.fill.solid()
                    shp.fill.fore_color.rgb = RGBColor.from_string(it.fill)
                else:
                    shp.fill.background()
                if it.line:
                    shp.line.color.rgb = RGBColor.from_string(it.line)
                    shp.line.width = Pt(it.line_w)
                else:
                    shp.line.fill.background()
                shp.shadow.inherit = False
                if it.text is not None:
                    fill_text(shp.text_frame, it.text, default_inset=0.06)
                else:
                    shp.text_frame.text = ""
            elif isinstance(it, Text):
                tb = slide.shapes.add_textbox(Inches(it.x), Inches(it.y), Inches(it.w), Inches(it.h))
                tb.name = it.name or tb.name
                fill_text(tb.text_frame, it)
            elif isinstance(it, Table):
                n, m = len(it.rows), len(it.cols)
                gf = slide.shapes.add_table(n, m, Inches(it.x), Inches(it.y), Inches(sum(it.cols)),
                                            Inches(it.row_h * n))
                gf.name = it.name or gf.name
                tbl = gf.table
                tbl.first_row = False
                tbl.horz_banding = False
                for j, cw in enumerate(it.cols):
                    tbl.columns[j].width = Inches(cw)
                for i in range(n):
                    tbl.rows[i].height = Inches(it.row_h)
                    for j in range(m):
                        cell = tbl.cell(i, j)
                        head = i == 0
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = RGBColor.from_string(PURPLE if head else (BG2 if i % 2 == 0 else WHITE))
                        cell.margin_left = cell.margin_right = Inches(0.14)
                        cell.margin_top = cell.margin_bottom = Inches(0.04)
                        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                        tf = cell.text_frame
                        tf.word_wrap = True
                        p = tf.paragraphs[0]
                        p.line_spacing = 1.0
                        r = p.add_run()
                        r.text = it.rows[i][j]
                        r.font.name = FONT
                        r.font.size = Pt(it.size)
                        r.font.bold = head or j == 0
                        r.font.color.rgb = RGBColor.from_string(WHITE if head else INK)
                        tcPr = cell._tc.get_or_add_tcPr()
                        for side in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):        # viền mảnh, đặt trước phần tô màu
                            ln = etree.Element(qn(side), w=str(int(Pt(0.75))), cap="flat", cmpd="sng", algn="ctr")
                            sf = etree.SubElement(ln, qn("a:solidFill"))
                            etree.SubElement(sf, qn("a:srgbClr")).set("val", LINE)
                            etree.SubElement(ln, qn("a:prstDash")).set("val", "solid")
                            tcPr.insert(["a:lnL", "a:lnR", "a:lnT", "a:lnB"].index(side), ln)
        slide.notes_slide.notes_text_frame.text = sl.notes
    prs.save(str(path))


# ============================================================ xem trước (HTML) ===
PX = 96.0          # 1 inch = 96 px, khung 1280x720
LH = 1.32          # chiều cao dòng tự nhiên của Be Vietnam Pro (ước lượng, dư một chút để soát tràn chữ)


def _css_text(t: Text) -> str:
    out = []
    for para in t.paras:
        size = para.size * PX / 72
        spans = "".join(
            f'<span style="font-weight:{700 if st.get("bold", para.bold) else 400};color:#{st.get("color", para.color)}">'
            f'{_esc(txt)}</span>' for txt, st in para.runs)
        lh = LH * para.line
        style = (f"margin:0 0 {para.after * PX / 72:.1f}px 0;font-size:{size:.2f}px;line-height:{lh:.3f};"
                 f"text-align:{para.align};color:#{para.color};font-weight:{700 if para.bold else 400}")
        if para.bullet:
            bc = GOLD if para.color == WHITE else PURPLE
            out.append(f'<p style="{style};display:flex;padding-left:0"><span style="flex:none;width:{0.24 * PX:.0f}px;'
                       f'color:#{bc}">•</span><span style="flex:1">{spans}</span></p>')
        else:
            out.append(f'<p style="{style}">{spans}</p>')
    return "".join(out)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_html(sl: Slide, idx: int) -> str:
    parts = []
    for k, it in enumerate(sl.items):
        if isinstance(it, Pic):
            parts.append(f'<img src="{it.path.as_uri()}" style="position:absolute;left:{it.x * PX:.1f}px;'
                         f'top:{it.y * PX:.1f}px;width:{it.w * PX:.1f}px;height:{it.h * PX:.1f}px">')
        elif isinstance(it, Box):
            radius = "50%" if it.shape == "oval" else f"{it.radius * PX:.1f}px"
            border = f"border:{it.line_w * PX / 72:.2f}px solid #{it.line};" if it.line else ""
            bg = f"background:#{it.fill};" if it.fill else ""
            inner = ""
            if it.shape in ("arrow_r", "arrow_lr"):
                pts = ("0,30 70,30 70,0 100,50 70,100 70,70 0,70" if it.shape == "arrow_r"
                       else "0,50 22,0 22,28 78,28 78,0 100,50 78,100 78,72 22,72 22,100")
                parts.append(f'<svg style="position:absolute;left:{it.x * PX:.1f}px;top:{it.y * PX:.1f}px" '
                             f'width="{it.w * PX:.1f}" height="{it.h * PX:.1f}" viewBox="0 0 100 100" '
                             f'preserveAspectRatio="none"><polygon points="{pts}" fill="#{it.fill}"/></svg>')
                continue
            if it.text is not None:
                inner = _text_div(it.text, 0, 0, it.w, it.h, f"b{k}", inset=it.text.inset or 0.06)
            parts.append(f'<div style="position:absolute;left:{it.x * PX:.1f}px;top:{it.y * PX:.1f}px;'
                         f'width:{it.w * PX:.1f}px;height:{it.h * PX:.1f}px;{bg}{border}border-radius:{radius};'
                         f'box-sizing:border-box">{inner}</div>')
        elif isinstance(it, Text):
            parts.append(_text_div(it, it.x, it.y, it.w, it.h, f"t{k}"))
        elif isinstance(it, Table):
            cols = "".join(f'<col style="width:{c * PX:.1f}px">' for c in it.cols)
            trs = []
            for i, row in enumerate(it.rows):
                bg = PURPLE if i == 0 else (BG2 if i % 2 == 0 else WHITE)
                tds = "".join(
                    f'<td style="background:#{bg};color:#{WHITE if i == 0 else INK};font-weight:'
                    f'{700 if i == 0 or j == 0 else 400};border:1px solid #{LINE};padding:3px {0.14 * PX:.0f}px;'
                    f'height:{it.row_h * PX - 8:.0f}px;font-size:{it.size * PX / 72:.2f}px;line-height:{LH:.2f}">'
                    f'{_esc(c)}</td>' for j, c in enumerate(row))
                trs.append(f"<tr>{tds}</tr>")
            parts.append(f'<table data-id="tbl{k}" style="position:absolute;left:{it.x * PX:.1f}px;top:{it.y * PX:.1f}px;'
                         f'border-collapse:collapse;table-layout:fixed;width:{sum(it.cols) * PX:.1f}px">'
                         f'<colgroup>{cols}</colgroup>{"".join(trs)}</table>')
    bg = "#FFFFFF"
    return (f'<div class="slide" id="s{idx}" style="position:relative;width:{W * PX:.0f}px;height:{H * PX:.0f}px;'
            f'background:{bg};overflow:hidden;font-family:\'{FONT}\'">{"".join(parts)}</div>')


def _text_div(t: Text, x: float, y: float, w: float, h: float, ident: str, inset: Optional[float] = None) -> str:
    ins = (t.inset if inset is None else inset) * PX
    just = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}[t.valign]
    return (f'<div data-id="{ident}" data-name="{t.name}" style="position:absolute;left:{x * PX:.1f}px;top:{y * PX:.1f}px;'
            f'width:{w * PX:.1f}px;height:{h * PX:.1f}px;box-sizing:border-box;padding:{min(ins, 0.05 * PX):.1f}px '
            f'{ins:.1f}px;display:flex;flex-direction:column;justify-content:{just}"><div class="inner">'
            f'{_css_text(t)}</div></div>')


def preview(slides: List[Slide]) -> List[Dict[str, Any]]:
    """Chụp từng slide bằng Chromium, trả về danh sách ô chữ bị tràn (chữ cao hơn khung)."""
    from playwright.sync_api import sync_playwright
    PREVIEW.mkdir(parents=True, exist_ok=True)
    for old in PREVIEW.glob("slide-*.png"):
        old.unlink()
    html = ("<!doctype html><html><head><meta charset='utf-8'><link rel='stylesheet' "
            "href='https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;700&display=swap'>"
            "<style>body{margin:0;background:#888}.slide{margin:0 0 20px 0}p{margin:0}</style></head><body>"
            + "".join(to_html(s, i + 1) for i, s in enumerate(slides)) + "</body></html>")
    page_path = BUILD / "preview.html"
    page_path.write_text(html, encoding="utf-8")
    problems = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": int(W * PX), "height": int(H * PX)}, device_scale_factor=1.5)
        pg.goto(page_path.as_uri())
        pg.evaluate("document.fonts.ready")
        pg.wait_for_timeout(800)
        for i in range(len(slides)):
            el = pg.locator(f"#s{i + 1}")
            el.screenshot(path=str(PREVIEW / f"slide-{i + 1:02d}.png"))
            over = pg.evaluate(f"""() => [...document.querySelectorAll('#s{i + 1} [data-id]')].map(d => {{
                const inner = d.querySelector('.inner');
                const box = d.getBoundingClientRect();
                const r = inner ? inner.getBoundingClientRect() : box;
                return {{name: d.dataset.name || d.dataset.id, over: Math.round(r.height - box.height),
                         wide: Math.round((inner ? inner.scrollWidth : 0) - box.width)}};
            }}).filter(x => x.over > 2 || x.wide > 2)""")
            for o in over:
                problems.append({"slide": i + 1, **o})
        b.close()
    return problems


# ============================================================ kịch bản (md, docx) ===
INTRO = [
    f"Ngày: {DATE}. Phụ trách: {OWNER}.",
    f"Bộ slide: docs/pitch/{NAME}.pptx (13 slide, 16:9). Tải lên Google Drive, chuột phải > Mở bằng > Google Trang trình "
    "bày. Ghi chú người trình bày của từng slide chính là lời nói dưới đây.",
    "Người nghe: DH (trưởng phòng), LM (quản lý trực tiếp) và các phòng ban của UrBox.",
    "Thời lượng: khoảng 18 phút trình bày, cộng hỏi đáp.",
    "Số liệu: chi phí Recall.ai theo trang giá Recall.ai ngày 19/10/2026 (0,5 USD mỗi giờ, tính theo giây), tỷ giá tham "
    "khảo 25.000đ/USD; ngân sách cần phòng Tài chính xác nhận. Độ chính xác nhận người nói đo trên 2 cuộc họp thật nội "
    "bộ. Gợi ý BD đo ngày 10-11/10/2026 với dữ liệu mẫu.",
    "WikiBox là ứng dụng kho tri thức của công ty tại wiki.urbox.dev; trong Cài đặt của Copilot là mục Kho tri thức "
    "WikiBox.",
    "Ảnh chụp màn hình trong slide dùng dữ liệu mẫu (Khách hàng ABC, tài khoản demo).",
]
PREP = [
    "Mở sẵn bộ slide ở chế độ Trình bày có ghi chú (Presenter view).",
    "Nếu muốn minh họa trực tiếp: mở ứng dụng, một cuộc họp mẫu có Bảng BD, và Cài đặt > Kho tri thức.",
    "Kiểm tra trạng thái Recall.ai (hết credit từ 13/10/2026) để trả lời câu hỏi về bot.",
]


def _wrap(text: str, first: str = "- ", rest: str = "  ", width: int = 110) -> List[str]:
    """Xuống dòng ở ranh giới từ cho Markdown (tối đa 110 ký tự)."""
    out, cur = [], first
    for word in text.split():
        start = cur in (first, rest)
        if not start and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = rest + word
        else:
            cur += ("" if start else " ") + word
    out.append(cur)
    return out


def write_script(slides: List[Slide]) -> None:
    lines = ["---", "title: Kịch bản trình bày UrBox Meeting Copilot", "version: 3.21.0", f"updated: {DATE}",
             f"owner: {OWNER}", "---", "", "# Kịch bản trình bày UrBox Meeting Copilot", ""]
    for t in INTRO:
        lines += _wrap(t)
    lines += ["", "## Chuẩn bị trước buổi trình bày", ""]
    for t in PREP:
        lines += _wrap(t)
    lines += ["", "## Lời trình bày từng slide", ""]
    for i, s in enumerate(slides):
        lines += [f"### Slide {i + 1}. {s.title} ({TIME[i]})", ""]
        if s.message:
            lines += _wrap(f"**Thông điệp:** {s.message}") + [""]
        lines += _wrap(NOTES[i], first="", rest="") + [""]
        if QA[i]:
            lines += ["**Câu hỏi dự kiến:**", ""]
            for q, a in QA[i]:
                lines += _wrap(f"*{q}* {a}")
            lines += [""]
    md = "\n".join(lines).rstrip() + "\n"
    (HERE / "kich-ban-trinh-bay.md").write_text(md, encoding="utf-8")

    from docx import Document
    from docx.shared import Pt, RGBColor
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = FONT
    st.font.size = Pt(11)
    doc.add_heading("Kịch bản trình bày UrBox Meeting Copilot", level=0)
    for t in INTRO:
        doc.add_paragraph(t, style="List Bullet")
    doc.add_heading("Chuẩn bị trước buổi trình bày", level=1)
    for t in PREP:
        doc.add_paragraph(t, style="List Bullet")
    doc.add_heading("Lời trình bày từng slide", level=1)
    for i, s in enumerate(slides):
        doc.add_heading(f"Slide {i + 1}. {s.title} ({TIME[i]})", level=2)
        if s.message:
            p = doc.add_paragraph()
            r = p.add_run("Thông điệp: ")
            r.bold = True
            p.add_run(s.message)
        doc.add_paragraph(NOTES[i])
        if QA[i]:
            p = doc.add_paragraph()
            p.add_run("Câu hỏi dự kiến:").bold = True
            for q, a in QA[i]:
                p = doc.add_paragraph(style="List Bullet")
                rq = p.add_run(q + " ")
                rq.italic = True
                rq.font.color.rgb = RGBColor(0x68, 0x21, 0xC2)
                p.add_run(a)
    doc.core_properties.author = OWNER
    doc.core_properties.title = "Kịch bản trình bày UrBox Meeting Copilot"
    doc.save(str(HERE / "kich-ban-trinh-bay.docx"))


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    build_assets()
    slides = [f() for f in SLIDES]
    assert len(slides) == TOTAL == len(NOTES) == len(QA) == len(TIME)
    for s, n in zip(slides, NOTES):
        s.notes = n
    to_pptx(slides, HERE / f"{NAME}.pptx")
    write_script(slides)
    problems = preview(slides)
    print(json.dumps({"pptx": str(HERE / f"{NAME}.pptx"), "preview": str(PREVIEW), "overflow": problems},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
