"""Thinking Engine & AI Assistant Orchestration.

Xử lý khi trợ lý được gọi tên (Wake-Word / Mention):
1. Nhận diện lời gọi trợ lý theo tên do người dùng đặt (mặc định "Jarvis") + "trợ lý ơi", "hey assistant".
2. Thinking Loop (ReAct):
   - Phân tích yêu cầu và ngữ cảnh cuộc họp.
   - Gọi Mock MCP Server để tra cứu dữ liệu (Nhân sự, Jira tickets, System specs).
   - Truyền stream thinking log xuống client để người dùng theo dõi quá trình suy nghĩ.
3. Sinh sản phẩm trực quan: dashboard số liệu, báo cáo nhanh, slide, sơ đồ Mermaid, trang web, biên bản.
4. Phản hồi bằng lời có nội dung thật; trong lúc chờ báo tiến độ ("em tìm thấy...").
"""
import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple

from meeting import artifacts, db, mcp

log = logging.getLogger("meeting.llm")

AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.6-flash")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
PROVIDER = (os.getenv("LLM_PROVIDER") or "claude").strip().lower()

# Cụm gọi chung luôn dùng được, bất kể trợ lý đặt tên gì
GENERIC_WAKE = [r"trợ\s*lý\s*ơi", r"hey\s*assistant", r"@ai(?!\w)", r"bot\s*ơi"]
DEFAULT_ASSISTANT = {"name": "Jarvis", "aliases": []}
ASSISTANT_KEY = "assistant"
_assistant_cache: Dict[str, Any] = {"value": None, "at": 0.0}
_CALL_AFTER = re.compile(r"^\s*(ơi|à|ạ|ê|,|:|!|\?)", re.IGNORECASE)
_LEAD_IN = re.compile(r"^(này|ê|hey|hi|hello|alo|ok|okay|ờ|ừ|à|thì|vậy|nhờ|cho\s+(anh|chị|em|mình|tôi)\s+hỏi)$",
                      re.IGNORECASE)
# Tên đứng đầu câu mà không có "ơi"/dấu phẩy: chỉ tính là lời gọi khi phần sau là một yêu cầu
_REQUEST_CUE = re.compile(
    r"(tra\s*cứu|tóm\s*tắt|lập|vẽ|liệt\s*kê|thiết\s*kế|giúp|hãy|kiểm\s*tra|tìm|nhắc|ghi|báo\s*cáo|cập\s*nhật|soạn|"
    r"viết|tạo|xem|phân\s*tích|so\s*sánh|đọc|dịch|cho\s+(anh|chị|em|mình|tôi|bọn)|\?)", re.IGNORECASE)


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip(" ,.;:!?\"'")


def assistant_config(refresh: bool = False) -> Dict[str, Any]:
    """Tên gọi trợ lý (lưu ở collection settings). Có cache 60 giây."""
    import time
    now = time.monotonic()
    if not refresh and _assistant_cache["value"] is not None and now - _assistant_cache["at"] < 60:
        return _assistant_cache["value"]
    cfg = dict(DEFAULT_ASSISTANT)
    try:
        raw = db.get_setting(ASSISTANT_KEY, "")
        if raw:
            data = json.loads(raw)
            name = _clean_name(data.get("name", ""))
            if name:
                cfg = {"name": name, "aliases": [_clean_name(a) for a in data.get("aliases", []) if _clean_name(a)]}
    except Exception as e:
        log.warning("meeting.llm: đọc cấu hình trợ lý lỗi: %s", e)
    _assistant_cache.update({"value": cfg, "at": now})
    return cfg


def validate_assistant_config(name: str, aliases: List[str]) -> Dict[str, Any]:
    name = _clean_name(name)
    if not (2 <= len(name) <= 30) or not re.search(r"[^\W\d_]", name):
        raise ValueError("Tên gọi cần 2-30 ký tự và có chữ cái")
    seen, clean = {name.casefold()}, []
    for a in aliases or []:
        a = _clean_name(a)
        if not a or a.casefold() in seen:
            continue
        if not (2 <= len(a) <= 30) or not re.search(r"[^\W\d_]", a):
            raise ValueError(f"Tên gọi khác '{a}' cần 2-30 ký tự và có chữ cái")
        seen.add(a.casefold())
        clean.append(a)
    if len(clean) > 6:
        raise ValueError("Tối đa 6 tên gọi khác")
    return {"name": name, "aliases": clean}


def set_assistant_config(name: str, aliases: List[str]) -> Dict[str, Any]:
    cfg = validate_assistant_config(name, aliases)
    db.set_setting(ASSISTANT_KEY, json.dumps(cfg, ensure_ascii=False))
    _assistant_cache.update({"value": cfg, "at": 0.0})
    return assistant_config(refresh=True)


def assistant_terms() -> List[str]:
    cfg = assistant_config()
    return [cfg["name"]] + list(cfg.get("aliases", []))


def _name_pattern(name: str) -> "re.Pattern":
    core = r"\s+".join(re.escape(w) for w in name.split())
    return re.compile(rf"(?<!\w){core}(?!\w)", re.IGNORECASE)


def _clean_command(rest: str) -> str:
    # Bỏ dấu câu và tiếng gọi ("ơi", "à"...) ở đầu câu lệnh: "Jarvis ơi, liệt kê..." -> "liệt kê..."
    return re.sub(r"^[,:;.!?\-\s]*((ơi|à|ạ|ê|này|nhé)(?!\w)[,:;.!?\-\s]*)?", "", rest, flags=re.IGNORECASE).strip()


def detect_wake_word(text: str, cfg: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, str]]:
    """Phát hiện câu GỌI trợ lý và trích xuất yêu cầu theo sau.

    Tên trợ lý được coi là lời gọi khi đứng đầu câu ("Bông, tóm tắt giúp anh") hoặc đi kèm tiếng gọi
    ("..., Bông ơi, ..."). Nhắc tên giữa câu ("tôi nghĩ Bông làm được") không kích hoạt.
    Trả về (tên đã gọi, yêu cầu) hoặc None. Yêu cầu có thể rỗng khi người nói ngừng sau khi gọi tên.
    """
    if not text:
        return None
    cfg = cfg or assistant_config()
    names = sorted({cfg["name"], *cfg.get("aliases", [])}, key=len, reverse=True)
    for name in names:
        for m in _name_pattern(name).finditer(text):
            before = re.sub(r"[\W_]+", " ", text[:m.start()]).strip()
            rest = text[m.end():]
            called = _CALL_AFTER.match(rest) is not None
            at_start = not before or _LEAD_IN.match(before) is not None
            only_punct = not re.sub(r"[\W_]+", "", rest)
            if called or (at_start and (only_punct or _REQUEST_CUE.search(rest))):
                return cfg["name"], _clean_command(rest)
    for pattern in GENERIC_WAKE:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return cfg["name"], _clean_command(text[m.end():])
    return None


TOOL_LABELS = {
    "query_employee_directory": "danh bạ nhân sự",
    "query_jira_issues": "ticket Jira",
    "query_system_architecture": "tài liệu kiến trúc hệ thống",
    "query_meeting_history": "biên bản các cuộc họp trước",
    "update_jira_issue_status": "cập nhật ticket Jira",
    "web_search": "thông tin trên mạng",
}

# ---------------------------------------------------------------- lệnh trình chiếu ---
_SLIDE = r"(?:slide|slides|slai|xlai|trang)"
_NUM_WORDS = {"một": 1, "hai": 2, "ba": 3, "bốn": 4, "tư": 4, "năm": 5, "sáu": 6, "bảy": 7, "tám": 8,
              "chín": 9, "mười": 10}
KIND_WORDS = {"slide": "slides", "bộ slide": "slides", "sơ đồ": "diagram", "dashboard": "dashboard", "báo cáo": "report",
              "biên bản": "minutes", "trang web": "web_design", "giao diện": "web_design"}
_BACK_KIND = re.compile(r"(quay|trở|mở|chiếu|bật|chuyển|xem)\s*(lại|về)\s*(cái\s*)?(bộ\s*)?(slide|sơ đồ|dashboard|báo cáo|biên bản|trang web|giao diện)"
                        r"\s*(cũ|khi nãy|hồi nãy|lúc nãy|ban nãy|lúc trước|(mà\s*)?(em\s*)?(đã\s*|vừa\s*|mới\s*)?"
                        r"(present|trình bày|chiếu|làm|soạn|tạo|vẽ|dựng)\s*(hồi nãy|lúc nãy|khi nãy|ban nãy|lúc trước|cho anh|cho chị|$))", re.I)
_KIND_ALT = r"(slide|bộ slide|sơ đồ|dashboard|báo cáo|biên bản|trang web|giao diện)"
# "chuyển lại cái slide gần nhất mà em đã tạo" (#40): mở sản phẩm mới tạo gần nhất của loại đó
_LATEST = re.compile(rf"(chuyển|quay|trở|mở|chiếu|bật|xem|đưa)\s*(lại|về|sang|lên)?\s*(cái\s*|bộ\s*)?{_KIND_ALT}\s*"
                     r"(gần nhất|mới nhất|cuối cùng|vừa (tạo|làm|soạn|vẽ|dựng)|(mà\s*)?em\s*(vừa|mới)\s*(tạo|làm|soạn|vẽ|dựng))", re.I)
# "quay lại cái slide v1" (#40): mở phiên bản cũ của bộ đang xem
_VERSION = re.compile(r"(?:\bbản|phiên bản|version|\bver|\bv)\s*\.?\s*(\d{1,2})\b", re.I)
_NAV_VERB = re.compile(r"(quay|trở|mở|chuyển|xem|chiếu|sang|qua|đưa)", re.I)
# "thôi em đừng có soạn slide nữa" (#40): dừng việc đang làm
_CANCEL = re.compile(r"(đừng|không cần|khỏi)\s*(có\s*)?(soạn|tạo|làm|vẽ|dựng|viết|tìm|tra cứu|search)|"
                     r"^thôi\s*(em\s*)?(dừng|ngừng|khỏi|bỏ|không cần|đừng)", re.I)
# "em quay lại cái slide điểm cộng" (#40): tìm slide theo chủ đề trong mọi bộ slide
_TOPIC_ANY = re.compile(r"(?:quay|trở|chuyển|mở|xem|sang|qua|tới|đến)\s*(?:lại\s*|về\s*|sang\s*|tới\s*|đến\s*)?(?:cái\s*)?"
                        r"(?:slide|slides|slai|trang)\s+(?!(?:trước|sau|tiếp|kế|cũ|gần|mới|cuối|số|này|đó|nữa|đầu|sang|qua|tới|đến)\b)"
                        r"(?!v?\d)(?:về\s+|nói về\s+|có\s+)?(.+)", re.I)
_TOPIC_TAIL = re.compile(r"(\s+(coi|nha|nhé|nhá|giúp anh|giúp chị|giúp em|giùm|đi|ạ|với|hồi nãy|lúc nãy|khi nãy|cho anh|cho chị))+$", re.I)
_CREATE_DECK = re.compile(r"\b(tạo|làm|soạn|viết|dựng|chuẩn bị|thiết kế)\s+(giúp\s+|cho\s+)?(anh\s+|chị\s+|em\s+)?(một\s+|1\s+)?"
                          r"(cái\s+|bộ\s+|bài\s+)?(slide|bài thuyết trình|bài trình bày)", re.I)
# "vẽ sơ đồ quy trình rồi thuyết trình luôn": tạo mới rồi mới trình bày, không trình bày cái đang chiếu
_CREATE_VISUAL = re.compile(r"\b(vẽ|tạo|làm|soạn|dựng|lập|thiết kế)\s+(giúp\s+|cho\s+|hộ\s+)?(anh\s+|chị\s+|em\s+|mình\s+)?"
                            r"(một\s+|1\s+)?(cái\s+|bộ\s+|bản\s+)?(lại\s+)?(sơ đồ|mind\s*map|dashboard|biểu đồ|bảng số liệu)", re.I)
# "thuyết trình sơ đồ này", "giải thích cái dashboard", "đi qua biểu đồ giúp anh": trình bày nội dung theo loại
_PRESENT_KIND = re.compile(r"(trình bày|giải thích|thuyết minh|đi qua|điểm qua|nói qua|đọc)\s*(giúp|cho|hộ|giùm|lại|qua|hết|luôn)?\s*"
                           r"(anh|chị|em|mình|tôi|cả nhà)?\s*(về\s*)?(cái\s*|bộ\s*)?(nội dung\s*)?((của|trong|trên)\s*)?(cái\s*)?"
                           r"(sơ đồ|mind\s*map|dashboard|biểu đồ|bảng số liệu)", re.I)
_KIND_IN_TEXT = re.compile(r"(sơ đồ|mind\s*map|dashboard|biểu đồ|bảng số liệu|slide)", re.I)
_PRESENT_KINDS = {"sơ đồ": "diagram", "mind map": "diagram", "mindmap": "diagram", "dashboard": "dashboard",
                  "biểu đồ": "dashboard", "bảng số liệu": "dashboard", "slide": "slides"}
# "giải thích nhánh nạp voucher", "nói rõ hơn ý banner mobile": giải thích một ý của sơ đồ đang chiếu
_EXPLAIN = re.compile(r"^(?:em\s+)?(?:hãy\s+)?(giải thích|nói rõ|nói thêm|làm rõ|diễn giải|nói kỹ|phân tích)\s*"
                      r"(?:giúp|cho|hộ|giùm)?\s*(?:anh|chị|em|mình|tôi)?\s*(?:thêm|rõ|kỹ|chi tiết)?\s*(?:hơn)?\s*"
                      r"(?:về\s*)?(?:cái\s*)?(?:(nhánh|nút|ý|mục|phần|node|ô|bước)\s+)?(.+)$", re.I)
_WEB_SEARCH = re.compile(r"\b(search|sớt|xớt|research|google)\b|(tra cứu|tìm kiếm|tìm|kiểm tra|xem)\s*(giúp|giùm|hộ|cho)?\s*(anh|chị|em|mình|tôi)?\s*"
                         r"(coi|xem|thử)?\s*(trên\s*)?(mạng|internet|google|web|online)", re.I)
_WEB_FILLER = re.compile(r"^(em\s+)?(hãy\s+)?(search|sớt|xớt|research|tra cứu|tìm kiếm|tìm|kiểm tra|xem|google)\s*(trên\s*)?(mạng|internet|google|web|online)?"
                         r"\s*(giúp|giùm|hộ|cho)?\s*(anh|chị|em|mình|tôi)?\s*(coi|xem|thử|là|về)?\s*", re.I)
_OPEN_FILE = re.compile(r"(mở|lấy|tìm|chiếu|trình chiếu|load)\b.*\b(slide|bộ slide|bài|file|tệp|tài liệu)\b.*\b(folder|thư mục|tệp|file|trên máy|ổ|desktop|downloads?|documents)\b|"
                        r"(mở|lấy|tìm|chiếu)\s*(lại\s*)?(cái\s*)?(file|tệp|tài liệu)\s*(slide|pdf|powerpoint|ppt|pptx|word|docx|báo cáo|thuyết trình|trình bày)?|"
                        r"(mở|chiếu)\s*(cái\s*)?(file\s*)?(pdf|powerpoint|pptx|ppt|word|docx)\b|[a-z]:\\[^\n]+\.(pdf|pptx|ppt|docx|md|txt)", re.I)
_ANS_NO_SCRIPT = re.compile(r"không\s*(cần|có|dùng)\s*kịch\s*bản", re.I)
# "em tự trình bày", "tự soạn lời luôn": chọn rõ ràng
_ANS_SELF = re.compile(r"(em\s*)?tự\s*(trình bày|thuyết trình|nói|làm|soạn|viết|trình)", re.I)
# chung chung ("em thuyết trình giúp anh"): chỉ coi là câu trả lời khi không phải câu lệnh kèm cách trình bày
_ANS_LOOSE = re.compile(r"cứ\s*(trình bày|nói|thuyết trình)|em\s*(trình bày|nói|thuyết trình)\s*(luôn|đi|giúp)|em\s*lo\b", re.I)
_ANS_SCRIPT = re.compile(r"kịch\s*bản|\bscript\b|theo\s*(bài|ghi chú|note|file|tệp)|đọc\s*(theo|đúng)", re.I)
_ANS_NOTES = re.compile(r"ghi\s*chú|\bnotes?\b|speaker|có\s*sẵn|phần\s*dưới|trong\s*(slide|file|tệp)\s*(luôn|đó|này)", re.I)
_ANS_PASTE = re.compile(r"\b(dán|paste|gõ|nhắn|copy|gửi)\b", re.I)
_ANS_FILE = re.compile(r"(?:file|tệp|folder|thư mục|tài liệu)\s+(.+)", re.I)
# "để anh trình bày", "anh tự nói": người trong phòng tự trình bày, AI chỉ chuyển slide theo lời
_ANS_HUMAN = re.compile(r"\bđể\s*(anh|chị|tôi|mình|tụi anh|bọn anh)\s*(tự\s*)?(trình bày|thuyết trình|nói)\b|"
                        r"\b(anh|chị|tôi|mình|tụi anh|bọn anh)\s*(sẽ\s*)?tự\s*(trình bày|thuyết trình|nói)\b", re.I)
# đang chờ trả lời: "để anh", "để chị lo", "anh trình bày nhé" (không nhận "để anh xem", "file anh nói lúc nãy")
_ANS_HUMAN_ASKED = re.compile(r"^(thôi\s*|vậy\s*|ừ\s*)?để\s*(anh|chị|tôi|mình)(\s*(tự|lo|làm|nói|trình bày|thuyết trình))*"
                              r"(\s*(nhé|nha|đi|được rồi))*$|"
                              r"^(thôi\s*|vậy\s*|ừ\s*)?(anh|chị|tôi|mình)\s*(sẽ\s*)?(tự\s*)?(trình bày|thuyết trình|nói)"
                              r"(\s*(nhé|nha|đi|được rồi|luôn))*$", re.I)
_WIN_PATH = re.compile(r"[a-z]:[\\/]", re.I)
# chỉ khi đang chờ trả lời: "trình bày đi", "nói luôn đi em", "bắt đầu đi"
_ANS_GO = re.compile(r"^(ừ\s*|ok\s*|được\s*|vâng\s*)?(em\s*)?(cứ\s*)?(trình bày|thuyết trình|nói|bắt đầu|làm)"
                     r"(\s*(luôn|đi|nhé|nha|thôi|em|giúp anh|giúp chị))*$", re.I)


def present_answer(text: str, asked: bool = False, explicit: bool = False) -> Optional[Dict[str, Any]]:
    """Câu trả lời cho "anh muốn em tự trình bày hay trình bày theo kịch bản ạ?".

    {"mode": "auto"} | {"mode": "human"} | {"mode": "script", "source": "notes" | "file" | "text" | None, "query": ...}
    | None.
    - asked=True: trợ lý vừa hỏi, nhận cả câu ngắn ("trình bày đi", "để anh", "file kich ban ở Downloads").
    - explicit=True: câu lệnh nói kèm cách trình bày ("mở file X rồi trình bày theo ghi chú"); chỉ nhận lựa chọn nói rõ,
      câu chung chung như "em thuyết trình giúp anh" thì trợ lý vẫn hỏi."""
    c = re.sub(r"\s+", " ", (text or "").lower()).strip(" .!?,")
    if not c:
        return None
    if _ANS_NO_SCRIPT.search(c):
        return {"mode": "auto"}
    if _CREATE_DECK.search(c) or _WEB_SEARCH.search(c):
        return None                                 # yêu cầu mới (soạn slide, tra cứu), không phải câu trả lời
    if _ANS_SCRIPT.search(c) and _WIN_PATH.search(c):
        return {"mode": "script", "source": "file", "query": (text or "").strip()}   # đọc thẳng đường dẫn đầy đủ
    if _ANS_SCRIPT.search(c):
        if _ANS_NOTES.search(c):
            return {"mode": "script", "source": "notes"}
        m = _ANS_FILE.search(c)
        if m and not re.fullmatch(r"(này|đó|luôn)", m.group(1).strip()):
            return {"mode": "script", "source": "file", "query": c}
        if _ANS_PASTE.search(c):
            return {"mode": "script", "source": "text"}
        return {"mode": "script", "source": None}
    if _ANS_HUMAN.search(c) or (asked and _ANS_HUMAN_ASKED.search(c)):
        return {"mode": "human"}
    if _ANS_SELF.search(c) or (not explicit and _ANS_LOOSE.search(c)) or (asked and _ANS_GO.search(c)):
        return {"mode": "auto"}
    m = _ANS_FILE.search(c)                         # trả lời câu "kịch bản ở đâu ạ?" chỉ bằng tên file
    if asked and m and re.search(r"\b(ở|trong|tên|là)\b", c) and not re.match(r"(mở|chiếu|trình chiếu)\b", c):
        return {"mode": "script", "source": "file", "query": c}
    if asked and _ANS_NOTES.search(c):              # "có sẵn trong ghi chú đó em"
        return {"mode": "script", "source": "notes"}
    return None


_STAGE_PATTERNS = [
    ("follow_off", re.compile(r"(tắt|ngừng|dừng|đừng|không|thôi)\s*(chế độ\s*)?(tự\s*(động\s*)?|tự\s*ý\s*)"
                              r"(chuyển|lật|theo|đổi)", re.I)),
    ("follow_on", re.compile(r"(bật|mở|cho)\s*(chế độ\s*)?tự\s*(động\s*)?(chuyển|lật|theo|đổi)", re.I)),
    ("present_stop", re.compile(r"(dừng|ngừng|thôi|stop|tạm dừng)\s*(việc\s*)?(thuyết trình|trình bày|present|đọc|nói)|"
                                r"(im|dừng|ngừng)\s*lại\s*(đi|nhé|đã)?$", re.I)),
    ("present", re.compile(r"\bpresent\b|\bresend\b|thuyết\s*trình|"
                           r"((tự\s*(động\s*)?)?(chuyển|kéo|lật)\s*slide\s*(và|rồi|xong)?\s*(em\s*)?(tự\s*)?(nói|đọc|trình bày))|"
                           r"(trình bày|đọc|nói)\s*(giúp|cho|hộ|lại|qua|hết|luôn)?\s*(anh|chị|em|mình)?\s*(về\s*)?(cái\s*)?"
                           r"(nội dung\s*)?((của|trong|ở trong|trên|bên trong)\s*)?(cái\s*)?(bộ\s*|các\s*|mấy\s*)?"
                           r"(slide|bài này)", re.I)),
    ("open", re.compile(r"(mở|bật|vào|chuyển sang)\s*(chế độ\s*|màn hình\s*|màn\s*)?(toàn màn hình|sân khấu|trình chiếu|trình bày)"
                        r"|phóng to", re.I)),
    ("close", re.compile(r"(thoát|tắt|đóng)\s*(chế độ\s*|màn hình\s*|màn\s*)?(toàn màn hình|sân khấu|trình chiếu|trình bày)"
                         r"|thu nhỏ", re.I)),
    ("prompt", re.compile(r"nhắc\s*(bài|ý|lời|mình|anh|chị|em|giúp)|(nói|trình bày)\s*(gì|cái gì)\s*tiếp|"
                          r"ý\s*tiếp\s*theo|quên\s*(bài|ý|mất)", re.I)),
    ("analyze", re.compile(r"(nhận xét|phân tích|đánh giá|góp ý)\s*(nhanh\s*|giúp\s*|giùm\s*)*(về\s*)?"
                           r"(cuộc họp|buổi họp|nội dung|từ đầu|cho mình|cho anh|cho chị|cho em)", re.I)),
    ("back", re.compile(r"(quay|trở)\s*(lại|về)\s*(phần|nội dung|bài|bản|màn|cái)\s*(trình bày|trình chiếu|lúc nãy)?\s*"
                        r"(trước|cũ|lúc nãy|ban nãy)", re.I)),
]


# ---------------------------------------------------------------- quay lại nội dung đã có ---
# "quay lại cái bộ slide em vừa trình bày trước đó" (#42), "cho anh xem lại cái dashboard vừa rồi", "quay về sơ đồ kiến trúc"
_RECALL_VERB = re.compile(r"(?:quay|trở)\s*(?:lại|về)|(?:mở|xem|chiếu|bật|đưa|hiện|kéo|lấy|coi)\s*lại|"
                          r"cho\s*(?:anh|chị|em|mình|tôi|tụi anh|bọn anh|cả nhà)?\s*(?:xem|coi)\s*lại", re.I)
_PAST_REF = re.compile(r"\bcũ\b|lúc nãy|hồi nãy|khi nãy|ban nãy|vừa nãy|nãy giờ|vừa rồi|trước đó|lúc trước|hồi trước|"
                       r"lúc đầu|ban đầu|đầu tiên|(?:đã|vừa|mới|từng)\s*(?:trình bày|thuyết trình|chiếu|present|làm|tạo|soạn|vẽ|"
                       r"dựng|mở|xem|nói|đưa)", re.I)
_FIRST_REF = re.compile(r"đầu tiên|lúc đầu|ban đầu", re.I)
_TIME_REF = re.compile(r"\bcũ\b|lúc nãy|hồi nãy|khi nãy|ban nãy|vừa nãy|vừa rồi|trước đó|lúc trước|hồi trước", re.I)
_RECALL_KIND = re.compile(r"(bộ slide|slides|slide|slai|xlai|sơ đồ|bản vẽ|mind map|mindmap|biểu đồ|dashboard|báo cáo|biên bản|"
                          r"trang web|giao diện|bản thiết kế)", re.I)
RECALL_KINDS = {"bộ slide": "slides", "slides": "slides", "slide": "slides", "slai": "slides", "xlai": "slides",
                "sơ đồ": "diagram", "bản vẽ": "diagram", "mind map": "diagram", "mindmap": "diagram", "biểu đồ": "dashboard", "dashboard": "dashboard",
                "báo cáo": "report", "biên bản": "minutes", "trang web": "web_design", "giao diện": "web_design",
                "bản thiết kế": "web_design"}
# từ đệm của câu nói (đã bỏ dấu): không dùng để tìm theo chủ đề
_TOPIC_STOP = set("""em anh chi minh toi tui hay giup gium ho cho xem coi nha nhe di a ay do nay kia cai bo phan ma da vua
moi tung co the duoc khong voi luon lai ve trinh bay thuyet chieu present lam tao soan dung mo noi dua cua muon quay tro
bat hien keo lay muc doan va thi la nhung cac mot so nao gi dum nhu vay oi roi nua voi ca nha""".split())


def fold(text: str) -> str:
    """Bỏ dấu tiếng Việt, chữ thường ("Khai Phóng" -> "khai phong")."""
    t = unicodedata.normalize("NFD", str(text or "")).replace("\u0111", "d").replace("\u0110", "d")
    return "".join(ch for ch in t if unicodedata.category(ch) != "Mn").lower()


def topic_words(text: str) -> List[str]:
    """Các từ mang nghĩa trong một đoạn (giữ nguyên dấu để nói lại cho người dùng)."""
    return [w for w in re.findall(r"\w+", (text or "").lower()) if len(fold(w)) >= 2 and fold(w) not in _TOPIC_STOP]


def topic_score(query: str, text: str) -> Tuple[float, int]:
    """(tỷ lệ từ của câu hỏi có trong đoạn, số từ trùng), so khớp không dấu."""
    q = {fold(w) for w in topic_words(query)}
    if not q:
        return 0.0, 0
    have = {fold(w) for w in topic_words(text)}
    hit = len(q & have)
    return hit / len(q), hit


def recall_intent(c: str) -> Optional[Dict[str, Any]]:
    """Mở lại nội dung đã có thay vì tạo mới: {"action": "back", "kind", "query", "past", "first"}."""
    m = _RECALL_VERB.search(c)
    if not m or _VERSION.search(c) or _LATEST.search(c) or _OPEN_FILE.search(c) or _WEB_SEARCH.search(c) \
            or _CREATE_DECK.search(c):
        return None
    rest = c[m.end():]
    if re.search(rf"{_SLIDE}\s*(?:số\s*)?\d", rest):
        return None                                   # "quay lại slide 4": chuyển đúng slide số
    if re.match(rf"\s*(?:cái\s*)?(?:một\s*)?(?:{_SLIDE}|phần|mục|bước)\s*(?:trước|sau|tiếp|kế)\b(?!\s*đó)", rest) \
            or re.match(rf"\s*(?:một\s*)?{_SLIDE}\s*$", rest):
        return None                                   # "quay lại slide trước": lùi một slide
    nm = re.search(rf"{_SLIDE}\s*(?:số\s*)?({'|'.join(_NUM_WORDS)})(?!\w)", rest)
    if nm and not topic_words(rest[nm.end():]):
        return {"action": "goto", "slide": _NUM_WORDS[nm.group(1)] - 1}    # "quay lại slide số bảy"
    km = _RECALL_KIND.search(rest)
    kind = RECALL_KINDS.get(km.group(1).lower()) if km else None
    past = bool(_PAST_REF.search(c))
    words = topic_words(_RECALL_KIND.sub(" ", _PAST_REF.sub(" ", rest)))
    if not kind and not past and not words:
        return None
    out: Dict[str, Any] = {"action": "back"}
    if kind:
        out["kind"] = kind
    if words:
        out["query"] = " ".join(words)
    if past:
        out["past"] = True
    if _FIRST_REF.search(c):
        out["first"] = True
    return out


def recall_like(text: str) -> bool:
    """Câu nhờ mở lại nội dung đã có (để trợ lý không dựng sản phẩm mới khi chưa chắc)."""
    return bool(_RECALL_VERB.search((text or "").lower()))


def stage_intent(command: str) -> Optional[Dict[str, Any]]:
    """Nhận lệnh điều khiển màn hình trình bày từ câu nói (không cần gọi LLM)."""
    c = re.sub(r"\s+", " ", (command or "").lower()).strip(" .!?,")
    if not c:
        return None
    r = recall_intent(c)
    if r:
        return r
    m = _BACK_KIND.search(c)
    if m:
        return {"action": "back", "kind": KIND_WORDS.get(m.group(5), "slides")}
    m = _LATEST.search(c)
    if m:
        return {"action": "latest", "kind": KIND_WORDS.get(m.group(4), "slides")}
    if _CANCEL.search(c) and not _STAGE_PATTERNS[2][1].search(c):          # "dừng thuyết trình" là present_stop
        return {"action": "cancel"}
    m = _VERSION.search(c)
    if m and _NAV_VERB.search(c):
        k = re.search(_KIND_ALT, c)
        return {"action": "version", "n": int(m.group(1)), "kind": KIND_WORDS.get(k.group(1), "slides") if k else None}
    # "làm slide thuyết trình về Q4", "vẽ sơ đồ rồi trình bày luôn": tạo mới, không phải trình bày cái đang chiếu
    creating = bool(_CREATE_DECK.search(c) or _CREATE_VISUAL.search(c))
    if _OPEN_FILE.search(c):
        return {"action": "open_file", "query": c}
    if _WEB_SEARCH.search(c):
        q = _WEB_FILLER.sub("", c).strip(" ?.!,") or c
        q = re.sub(r"\s*(nhé|nha|nhá|đi|ạ|với|giúp anh|giúp em|cho anh)\s*$", "", q).strip(" ?.!,")
        return {"action": "web_search", "query": q or c}
    for action, pat in _STAGE_PATTERNS:
        if action == "present" and creating:
            continue
        if pat.search(c) or (action == "present" and _PRESENT_KIND.search(c)):
            if action == "present" and _TIME_REF.search(c):
                km = _RECALL_KIND.search(c)
                if km:      # "thuyết trình lại cái slide hồi nãy": mở lại bộ đó rồi mới trình bày
                    return {"action": "present", "recall": {"action": "back", "kind": RECALL_KINDS[km.group(1).lower()],
                                                            "past": True}}
            if action == "present":
                km = _KIND_IN_TEXT.search(c)
                kind = _PRESENT_KINDS.get(re.sub(r"\s+", " ", km.group(1).lower())) if km else None
                return {"action": "present", "kind": kind} if kind in ("diagram", "dashboard") else {"action": "present"}
            return {"action": action}
    m = _EXPLAIN.match(c)
    if m and (m.group(2) or m.group(1).lower() != "phân tích"):     # "phân tích X" chung chung để trợ lý xử lý
        q = _TOPIC_TAIL.sub("", m.group(3)).strip(" ?.!,")
        if q:
            return {"action": "explain", "query": q, "loose": not m.group(2)}
    m = re.search(rf"{_SLIDE}\s*(?:số\s*)?(\d{{1,2}}|{'|'.join(_NUM_WORDS)})(?!\w)", c)
    if m and not m.group(1).isdigit() and topic_words(c[m.end():]):
        m = None          # "slide hai cách hiểu về khai phóng" là tên slide, không phải slide số 2
    if m and re.search(r"(đến|tới|sang|qua|mở|về|xem|chuyển|quay|cho)", c):
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS[m.group(1)]
        return {"action": "goto", "slide": max(0, n - 1)}
    m = re.search(rf"(?:mở|xem|chuyển|quay|tới|đến|sang)\s*(?:lại\s*)?(?:sang\s*|đến\s*|tới\s*)?(?:{_SLIDE}|phần)\s*"
                  r"(?:về|nói về|có|liên quan)\s+(.+)", c)
    if m:
        return {"action": "topic", "query": m.group(1).strip()}
    m = re.search(r"^(?:chuyển|sang|qua|tới|đến|mở)\s*(?:sang\s*|đến\s*|tới\s*)?phần\s+(?!trước|sau|tiếp)(.+)", c)
    if m:
        return {"action": "topic", "query": m.group(1).strip()}
    m = _TOPIC_ANY.search(c)
    if m:
        q = _TOPIC_TAIL.sub("", m.group(1)).strip(" ?.!,")
        if q:
            return {"action": "topic", "query": q}
    if re.search(rf"{_SLIDE}\s*(trước|lúc nãy|ban nãy)|(quay|trở|lùi)\s*(lại|về)?\s*(một\s*)?{_SLIDE}", c):
        return {"action": "prev"}
    if re.search(rf"(chuyển|sang|qua|tới|tiếp)\s*(sang\s*)?(một\s*)?{_SLIDE}|{_SLIDE}\s*(tiếp|kế|sau)|"
                 r"^(tiếp|tiếp đi|tiếp tục|next|chuyển tiếp)$|(chuyển|sang|qua|tới)\s*(sang\s*)?(phần|mục)\s*(tiếp theo|tiếp|sau|kế)", c):
        return {"action": "next"}
    return None


_EDIT_CMD = re.compile(r"^(sửa|chỉnh|đổi|thêm|bớt|xóa|xoá|bỏ|cập nhật|viết lại|rút gọn|làm ngắn|làm rõ)(?!\w)", re.I)
_EDIT_TARGET = re.compile(r"(slide|trang|này|đó|bản|biểu đồ|sơ đồ|giao diện|biên bản|nội dung|ý|tiêu đề|dòng)", re.I)


def is_edit_command(command: str) -> bool:
    """Câu lệnh sửa nội dung đang trình chiếu, ví dụ "sửa slide này thêm số liệu doanh thu"."""
    c = (command or "").strip()
    return bool(_EDIT_CMD.search(c) and _EDIT_TARGET.search(c))


def _extract_json(text: str) -> Dict[str, Any]:
    """Trích xuất và parse JSON an toàn từ phản hồi của LLM."""
    if not text:
        return {}
    clean = re.sub(r"```(?:json)?\s*", "", text)
    clean = re.sub(r"```\s*", "", clean).strip()

    # 1. Thử parse trực tiếp toàn bộ
    try:
        return json.loads(clean)
    except Exception:
        pass

    # 2. Tìm khối JSON từ dấu { đầu tiên đến } cuối cùng
    s = clean.find("{")
    e = clean.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(clean[s:e+1])
        except Exception:
            pass

    # 3. Quét các khối nhỏ hơn
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", clean):
        try:
            d = json.loads(m.group(0))
            if any(k in d for k in ("artifact_needed", "chat_response", "thought", "needs_tool", "tool")):
                return d
        except Exception:
            pass

    return {}


# ==============================================================================
# TRỢ LÝ TRONG CUỘC HỌP: TRA CỨU DỮ LIỆU -> TRẢ LỜI CỤ THỂ -> TẠO SẢN PHẨM TRỰC QUAN
# ==============================================================================
AGENT_TOOLS = {
    "query_jira_issues": "query_jira_issues(issue_key?, assignee?, status?): ticket Jira, tiến độ, người phụ trách, hạn chót",
    "query_employee_directory": "query_employee_directory(query, department?): nhân sự, kỹ năng, phòng ban, email",
    "query_system_architecture": "query_system_architecture(system_name): hệ thống, API, cơ sở dữ liệu",
    "query_meeting_history": "query_meeting_history(keyword): quyết định ở các cuộc họp trước",
    "search_knowledge": "search_knowledge(query, top_k?): tìm trong kho tri thức nội bộ (quy trình, chính sách, tài liệu kỹ thuật)",
    "read_document": "read_document(path): đọc toàn văn một tài liệu trong kho tri thức (path lấy từ search_knowledge)",
    "web_search": "web_search(query): tìm trên Internet thông tin công khai mới nhất (giá cả, tỷ giá, tin tức, đối thủ, "
                  "quy định); trả về nội dung các trang đã đọc kèm nguồn",
}
ARTIFACT_KINDS = ("dashboard", "report", "slides", "web_design", "diagram", "minutes")
KIND_NAMES = {"dashboard": "dashboard", "report": "báo cáo nhanh", "slides": "bộ slide", "web_design": "trang web",
              "diagram": "sơ đồ", "minutes": "biên bản"}
MAX_TOOL_ROUNDS = 2

AGENT_SYSTEM = """Bạn là {{NAME}}, trợ lý AI ngồi cùng cuộc họp của UrBox (mọi người gọi bạn là "{{NAME}}").
Bạn xưng "em", gọi người hỏi là "anh" hoặc "chị" (không rõ thì "anh chị").
Mục tiêu: giúp cuộc họp hiệu quả hơn - trả lời nhanh, báo cáo đúng số liệu, dựng ngay sản phẩm trực quan để mọi người cùng xem.

Công cụ tra cứu dữ liệu nội bộ:
{{TOOLS}}

Cách làm việc:
1. Cần dữ liệu thì CHỈ trả về các dòng gọi công cụ (tối đa 3 dòng), hệ thống sẽ gửi lại kết quả:
   TOOL_CALL: {"tool": "query_jira_issues", "arguments": {}}
2. Đủ dữ liệu (hoặc không cần tra cứu) thì trả về MỘT JSON duy nhất:
{"thought": "suy luận ngắn",
 "insights": [{"kind": "risk|todo|improve|good|info", "text": "điều em phát hiện, 1 câu cụ thể"}],
 "artifact_needed": "dashboard" | "report" | "slides" | "web_design" | "diagram" | "minutes" | null,
 "artifact_prompt": "mô tả chi tiết cho bộ tạo sản phẩm: dùng dữ liệu nào, biểu đồ/bố cục nào",
 "report_markdown": "nội dung chi tiết để HIỂN THỊ trên màn hình (markdown: gạch đầu dòng, bảng)",
 "show": {"artifact_id": id trong "Nội dung đã có", "slide": số slide tính từ 1 hoặc null} hoặc null,
 "present": true khi người dùng muốn em thuyết trình luôn sản phẩm vừa tạo hoặc vừa mở lại, ngược lại false,
 "chat_response": "1-3 câu nói thành tiếng"}

Chọn sản phẩm (artifact_needed):
- "dashboard": vẽ chart, biểu đồ, báo cáo số liệu, thống kê, KPI, dashboard kiểu Power BI.
- "report": báo cáo nhanh, tổng hợp, review, liệt kê bằng chữ.
- "slides": slide, bài trình bày. "web_design": trang web, giao diện, landing page, prototype.
- "diagram": sơ đồ (mặc định là sơ đồ tư duy kiểu NotebookLM, bấm vào ý nào cũng nghe giải thích được), quy trình,
  luồng xử lý, kiến trúc. "minutes": biên bản cuộc họp.
- null: câu hỏi ngắn trả lời được ngay trong 1-3 câu.

Màn hình trình chiếu (em điều khiển được):
- Người dùng muốn quay lại / xem lại / mở lại / chiếu lại nội dung ĐÃ CÓ (kể cả nói tắt "cái lúc nãy", "cái em vừa trình
  bày", "bộ slide trước đó", gọi tên gần đúng, hoặc chữ bị nhận dạng giọng nói chép sai): chọn đúng mục trong "Nội dung đã
  có" và điền show; artifact_needed = null; chat_response nói ngắn em mở lại gì. Không tạo sản phẩm mới.
- Không chắc mục nào: chọn mục đã chiếu gần nhất hợp với câu nói và nói rõ là mục nào. Không bao giờ nói là em không điều
  khiển được màn hình.

Quy tắc:
- chat_response phải nói ra thông tin thật (con số, tên người, hạn chót, kết luận). Cấm câu chung chung như "em đã xử lý xong".
- Có sản phẩm thì chat_response nêu 1-2 điểm chính trong đó. Hỏi thông tin/liệt kê thì điền report_markdown đầy đủ.
- insights: tối đa 3 điều đáng chú ý em thấy khi phân tích (rủi ro, việc chưa có người nhận, điểm cần cải thiện, điểm tốt).
- Chỉ dùng số liệu trong transcript hoặc dữ liệu tra cứu. Thiếu dữ liệu thì nói rõ.
- Mọi sản phẩm nói về NỘI DUNG đã bàn (chủ đề, số liệu, quyết định, việc cần làm); không thống kê ai nói nhiều hay ít
  trừ khi được hỏi đúng điều đó.
- Ngày dd/mm/yyyy, tiền dạng 1.000.000đ, không dùng gạch dài. Không lặp lại nguyên văn câu hỏi."""

_GENERIC_REPLY = re.compile(r"(tiếp nhận|xử lý xong|hoàn thành)\s+(yêu cầu|xong)|đã xử lý xong", re.I)
_TOOL_LINE = re.compile(r"^\s*TOOL_CALL\s*:.*$", re.M)
_FINAL_KEYS = ("chat_response", "artifact_needed", "report_markdown", "insights")
_KIND_HINTS = [
    ("dashboard", r"dashboard|power\s*bi|chart|chạt|biểu\s*đồ|đồ\s*thị|kpi|thống\s*kê|số\s*liệu"),
    ("slides", r"slide|trình\s*chiếu|thuyết\s*trình|bài\s*trình\s*bày|deck"),
    ("web_design", r"trang\s*web|website|landing|giao\s*diện|\bweb\b|html|prototype"),
    ("diagram", r"sơ\s*đồ|diagram|flowchart|luồng|mind\s*map|tư\s*duy"),
    ("minutes", r"biên\s*bản|minutes"),
    ("report", r"báo\s*cáo|report|tổng\s*hợp|review|liệt\s*kê"),
]
PROGRESS_START = {
    "dashboard": "Em đang dựng dashboard, khoảng nửa phút là có ạ.",
    "report": "Em đang viết báo cáo nhanh.",
    "slides": "Em đang soạn bộ slide.",
    "web_design": "Em đang dựng trang web, mất khoảng một phút ạ.",
    "diagram": "Em đang vẽ sơ đồ.",
    "minutes": "Em đang lập biên bản cuộc họp.",
}


def guess_kind(text: str) -> Optional[str]:
    """Đoán loại sản phẩm người dùng muốn từ câu lệnh (dự phòng khi LLM không chỉ rõ)."""
    low = (text or "").lower()
    for kind, pat in _KIND_HINTS:
        if re.search(pat, low):
            return kind
    return None


def ack_phrase(command: str) -> str:
    """Câu đáp ngay khi được gọi, trước khi bắt đầu xử lý (không cần LLM)."""
    kind = guess_kind(command)
    if kind in ("dashboard", "slides", "web_design", "diagram"):
        return f"Dạ, em dựng {KIND_NAMES[kind]} ngay, anh chị chờ em chút nhé."
    if kind in ("report", "minutes"):
        return "Dạ, để em tổng hợp ngay."
    return "Dạ, để em xem."


def iter_json_objects(text: str) -> List[Any]:
    """Mọi object JSON hợp lệ ở mức ngoài cùng trong văn bản (bỏ qua chữ thường, khối ``` và JSON hỏng)."""
    clean = re.sub(r"```(?:json)?", "", text or "")
    dec, out, i = json.JSONDecoder(), [], 0
    while True:
        i = clean.find("{", i)
        if i < 0:
            return out
        try:
            obj, end = dec.raw_decode(clean, i)
            out.append(obj)
            i = end
        except ValueError:
            i += 1


def _salvage_field(raw: str, key: str) -> str:
    """Lấy một trường chuỗi từ JSON bị cắt dở (vượt giới hạn token)."""
    m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)' % key, raw or "", re.S)
    if not m:
        return ""
    try:
        return json.loads('"' + m.group(1).rstrip("\\") + '"')
    except ValueError:
        return m.group(1)


def parse_agent_output(raw: str) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    """-> (lệnh gọi công cụ, JSON kết quả cuối, phần chữ thường còn lại)."""
    objs = [o for o in iter_json_objects(raw) if isinstance(o, dict)]
    finals = [o for o in objs if any(k in o for k in _FINAL_KEYS)]
    calls = [o for o in objs if "tool" in o and not any(k in o for k in _FINAL_KEYS)]
    if not finals and re.search(r'"(chat_response|report_markdown)"', raw or ""):
        salvaged = {k: _salvage_field(raw, k) for k in ("chat_response", "report_markdown", "artifact_needed")}
        if salvaged["chat_response"] or salvaged["report_markdown"]:
            finals = [{k: v for k, v in salvaged.items() if v}]
    text = raw or ""
    if objs or calls:
        text = _TOOL_LINE.sub("", re.sub(r"```(?:json)?[\s\S]*?```", "", text))
        text = re.sub(r"\{[\s\S]*\}", "", text)
    text = re.sub(r"</?thinking>", "", text).strip()
    return calls, (finals[-1] if finals else None), text


def _fmt_day(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(iso or ""))
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else str(iso or "")


_DONE_STATUS = ("done", "closed", "resolved", "hoàn thành", "xong")
_STATUS_VI = {"to do": "chưa làm", "todo": "chưa làm", "open": "đang mở", "in progress": "đang làm",
              "in review": "chờ review", "review": "chờ review", "blocked": "bị chặn", "done": "đã xong",
              "closed": "đã đóng", "resolved": "đã xử lý", "testing": "đang kiểm thử", "qa": "đang kiểm thử"}


def status_vi(status: str) -> str:
    return _STATUS_VI.get(str(status or "").strip().lower(), str(status or "không rõ"))


def jira_overview(issues: List[Dict[str, Any]], today: Optional[str] = None) -> Dict[str, Any]:
    """Số liệu tổng hợp từ danh sách ticket (dùng cho lời nói và dashboard, không qua LLM)."""
    today = today or time.strftime("%Y-%m-%d")
    by_status: Dict[str, int] = {}
    by_assignee: Dict[str, int] = {}
    points: Dict[str, float] = {}
    overdue, high_open = [], []
    for it in issues:
        st = str(it.get("status") or "Không rõ")
        by_status[st] = by_status.get(st, 0) + 1
        who = str(it.get("assignee") or "Chưa giao")
        by_assignee[who] = by_assignee.get(who, 0) + 1
        try:
            points[who] = points.get(who, 0.0) + float(it.get("story_points") or 0)
        except (TypeError, ValueError):
            pass
        done = st.lower() in _DONE_STATUS
        due = str(it.get("due_date") or "")
        if not done and due and due[:10] < today:
            overdue.append({"key": it.get("key"), "due": _fmt_day(due), "assignee": who})
        if not done and str(it.get("priority") or "").lower() in ("high", "highest", "critical", "cao"):
            high_open.append(it.get("key"))
    return {"total": len(issues), "by_status": by_status, "by_assignee": by_assignee,
            "story_points_by_assignee": points, "overdue": overdue, "high_priority_open": high_open}


def summarize_tool_result(tool: str, result: Any) -> str:
    """Câu nói ngắn về điều vừa tìm thấy ("em tìm thấy..."), tính trực tiếp từ dữ liệu."""
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        return f"Em chưa tra được {TOOL_LABELS.get(tool, tool)}: {result['error']}."
    if tool == "web_search":
        srcs = result.get("sources") or []
        if not srcs:
            return "Em chưa tìm được trang nào phù hợp trên mạng."
        doms = ", ".join(dict.fromkeys(str(s.get("domain") or s.get("title") or "")[:40] for s in srcs[:4]))
        return f"Em đã đọc {len(srcs)} nguồn trên mạng: {doms}."
    if tool == "query_jira_issues":
        issues = result.get("issues") or []
        if not issues:
            return "Em chưa thấy ticket Jira nào khớp."
        ov = jira_overview(issues)
        parts = ", ".join(f"{n} {status_vi(st)}" for st, n in sorted(ov["by_status"].items(), key=lambda kv: -kv[1])[:3])
        text = f"Em tìm thấy {ov['total']} ticket: {parts}."
        if ov["overdue"]:
            o = ov["overdue"][0]
            more = f" và {len(ov['overdue']) - 1} ticket khác" if len(ov["overdue"]) > 1 else ""
            text += f" {o['key']} của {o['assignee']} đã quá hạn từ {o['due']}{more}."
        elif ov["high_priority_open"]:
            text += f" Có {len(ov['high_priority_open'])} ticket ưu tiên cao chưa xong."
        return text
    if tool == "query_employee_directory":
        emps = result.get("employees") or []
        if not emps:
            return "Em chưa thấy nhân sự nào khớp."
        names = ", ".join(str(e.get("name")) for e in emps[:3])
        return f"Em thấy {len(emps)} nhân sự liên quan: {names}{' ...' if len(emps) > 3 else ''}."
    if tool == "query_system_architecture":
        systems = result.get("matched_systems") or []
        return f"Em đã lấy tài liệu {len(systems)} hệ thống: " + ", ".join(str(s.get("name")) for s in systems[:3]) + "." \
            if systems else "Em chưa thấy tài liệu hệ thống phù hợp."
    if tool == "query_meeting_history":
        ms = result.get("meetings") or []
        if not ms:
            return "Em chưa thấy cuộc họp trước nào nhắc tới nội dung này."
        m = ms[0]
        return f"Em tìm thấy {len(ms)} cuộc họp trước liên quan, gần nhất là \"{m.get('title')}\" ngày {_fmt_day(m.get('date'))}."
    return ""


def _context_lines(segments: List[Dict[str, Any]], n: int = 400, max_chars: int = 24000) -> str:
    """Toàn bộ transcript (cắt bớt phần đầu nếu quá dài) để sản phẩm bám sát cả cuộc họp, không chỉ vài câu cuối."""
    lines = [f"{s.get('speaker_label', 'Không rõ')}: {s.get('text', '')}" for s in segments[-n:] if (s.get('text') or '').strip()]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "(... phần đầu đã lược bớt ...)\n" + text[-max_chars:]
    return text if text else "(Chưa có nội dung)"


def _data_text(tool_results: List[Dict[str, Any]], limit: int = 9000) -> str:
    if not tool_results:
        return ""
    data = []
    for r in tool_results:
        item = {"tool": r["tool"], "arguments": r["args"], "result": r["result"]}
        issues = (r["result"] or {}).get("issues") if isinstance(r["result"], dict) else None
        if issues:
            item["tong_hop"] = jira_overview(issues)
        data.append(item)
    return json.dumps(data, ensure_ascii=False)[:limit]


TRANSCRIPT_CHUNK = 30           # số câu mỗi khối transcript: khối đã đủ câu giữ nguyên giữa các lần gọi -> đọc từ cache


def transcript_blocks(segments: List[Dict[str, Any]], max_chars: int = 24000) -> List[Dict[str, Any]]:
    """Transcript chia khối cố định 30 câu tính từ đầu cuộc họp, đánh dấu cache ở khối đủ câu cuối cùng.

    Câu mới chỉ làm đổi khối cuối, nên lần gọi sau (vòng tra cứu tiếp theo, câu hỏi tiếp theo trong vài phút) đọc lại
    phần đầu từ cache. Quá dài thì bỏ bớt nguyên khối ở đầu để phần còn lại vẫn giữ nguyên."""
    lines = [f"{s.get('speaker_label', 'Không rõ')}: {s.get('text', '')}" for s in segments
             if (s.get('text') or '').strip()]
    if not lines:
        return [artifacts.text_block("## Nội dung cuộc họp:\n(Chưa có nội dung)")]
    chunks = [lines[i:i + TRANSCRIPT_CHUNK] for i in range(0, len(lines), TRANSCRIPT_CHUNK)]
    total, start = sum(len(ln) + 1 for ln in lines), 0
    while total > max_chars and start < len(chunks) - 1:
        total -= sum(len(ln) + 1 for ln in chunks[start])
        start += 1
    last_full = len(chunks) - 1 if len(chunks[-1]) == TRANSCRIPT_CHUNK else len(chunks) - 2
    blocks = []
    for i in range(start, len(chunks)):
        text = "\n".join(chunks[i])
        if i == start:
            head = ("## Nội dung cuộc họp (toàn bộ, theo thứ tự thời gian):" if start == 0
                    else "## Nội dung cuộc họp (phần đầu đã lược bớt, theo thứ tự thời gian):")
            text = f"{head}\n{text}"
        blocks.append(artifacts.text_block(text, cache=(i == last_full)))
    return blocks


def library_text(library: Optional[Dict[str, Any]]) -> str:
    """Danh sách nội dung đã tạo / đã chiếu trong cuộc họp, để trợ lý mở lại đúng thứ người dùng nhắc tới."""
    items = (library or {}).get("items") or []
    if not items:
        return ""
    lines = []
    for it in items:
        flags = []
        if it.get("on_stage"):
            flags.append("đang chiếu" + (f", slide {it['slide']}" if it.get("slide") else ""))
        elif it.get("shown"):
            flags.append("đã chiếu")
        s = f"- [{it['id']}] {KIND_NAMES.get(it.get('kind'), it.get('kind'))} \"{it.get('title', '')}\" v{it.get('version', 1)}"
        s += f" ({', '.join(flags)})" if flags else ""
        if it.get("slides"):
            s += " - các slide: " + "; ".join(f"{i}. {str(t)[:40]}" for i, t in enumerate(it["slides"], 1))
        lines.append(s)
    order = " -> ".join(str(x) for x in (library or {}).get("order") or [])
    return ("## Nội dung đã có trong cuộc họp (mới nhất trước):\n" + "\n".join(lines)
            + (f"\nThứ tự đã chiếu (cũ -> mới): {order}" if order else ""))


def _agent_request(prompt: str, transcript: List[Dict[str, Any]], facts: Dict[str, Any],
                   tool_results: List[Dict[str, Any]], stage_art: Optional[Dict[str, Any]], final_only: bool,
                   library: Optional[Dict[str, Any]] = None) -> str:
    """Lượt gọi agent: transcript (cache) -> màn hình -> từng kết quả tra cứu (cache ở kết quả cuối) -> yêu cầu.

    Các vòng tra cứu sau gửi lại y nguyên phần trước, chỉ thêm kết quả mới ở cuối, nên phần lặp lại đọc từ cache."""
    blocks = [dict(b) for b in transcript]
    if artifacts.wants_stats(prompt):
        blocks.append(artifacts.text_block(f"## Thống kê phát biểu (người dùng có hỏi; số liệu thật):\n"
                                           f"{json.dumps(facts, ensure_ascii=False)}"))
    if stage_art:
        blocks.append(artifacts.text_block(f"## Đang chiếu trên màn hình: {KIND_NAMES.get(stage_art.get('kind'), stage_art.get('kind'))} "
                                           f"\"{stage_art.get('title', '')}\""))
    lib = library_text(library)
    if lib:
        blocks.append(artifacts.text_block(lib))
    if tool_results:
        for i, r in enumerate(tool_results, 1):
            blocks.append(artifacts.text_block(f"## Dữ liệu đã tra cứu ({i}):\n{_data_text([r], limit=6000)}",
                                               cache=(i == len(tool_results))))
    else:
        blocks.append(artifacts.text_block("## Dữ liệu đã tra cứu:\n(chưa tra cứu)"))
    req = f"## Yêu cầu:\n\"{prompt}\""
    if final_only:
        req += "\n\nKhông gọi thêm công cụ. Trả về JSON cuối cùng ngay."
    elif tool_results:
        req += "\n\nĐã có kết quả tra cứu ở trên. Cần thêm dữ liệu thì gọi công cụ, đủ rồi thì trả về JSON cuối cùng."
    blocks.append(artifacts.text_block(req))
    return artifacts.Blocks(blocks)


def _agent_prompt(prompt: str, context_text: str, facts: Dict[str, Any], tool_results: List[Dict[str, Any]],
                  stage_art: Optional[Dict[str, Any]], final_only: bool) -> str:
    parts = [f"## Nội dung cuộc họp (toàn bộ, theo thứ tự thời gian):\n{context_text}"]
    if artifacts.wants_stats(prompt):
        parts.insert(0, f"## Thống kê phát biểu (người dùng có hỏi; số liệu thật):\n{json.dumps(facts, ensure_ascii=False)}")
    if stage_art:
        parts.append(f"## Đang chiếu trên màn hình: {KIND_NAMES.get(stage_art.get('kind'), stage_art.get('kind'))} "
                     f"\"{stage_art.get('title', '')}\"")
    parts.append("## Dữ liệu đã tra cứu:\n" + (_data_text(tool_results) or "(chưa tra cứu)"))
    parts.append(f"## Yêu cầu:\n\"{prompt}\"")
    if final_only:
        parts.append("Không gọi thêm công cụ. Trả về JSON cuối cùng ngay.")
    elif tool_results:
        parts.append("Đã có kết quả tra cứu ở trên. Cần thêm dữ liệu thì gọi công cụ, đủ rồi thì trả về JSON cuối cùng.")
    return "\n\n".join(parts)


def _first_sentences(md: str, n: int = 2) -> str:
    plain = re.sub(r"[#*_`|>]+", " ", md or "")
    plain = re.sub(r"\s+", " ", plain).strip()
    sents = re.split(r"(?<=[.!?])\s+", plain)
    return " ".join(sents[:n])[:300]


def _final_reply(chat: str, kind: Optional[str], art: Optional[Dict[str, Any]], err: str, report_md: str) -> str:
    chat = (chat or "").strip()
    generic = not chat or (len(chat) < 90 and bool(_GENERIC_REPLY.search(chat)))
    if err:
        return (f"{'' if generic else chat + ' '}Em chưa tạo được {KIND_NAMES.get(kind, 'sản phẩm')}: {err}").strip()
    if art and generic:
        extra = ""
        if art.get("kind") == "dashboard":
            try:
                d = json.loads(art.get("content") or "{}")
                extra = f" gồm {len(d.get('kpis') or [])} chỉ số và {len(d.get('charts') or [])} biểu đồ"
            except ValueError:
                pass
        return f"Em đã làm xong {KIND_NAMES.get(art.get('kind'), 'sản phẩm')} \"{art.get('title', '')}\"{extra}, đang hiện trên màn hình."
    if generic and report_md:
        return _first_sentences(report_md) or "Em đã tổng hợp xong, nội dung đang hiện trên màn hình."
    if generic:
        return "Em chưa tìm được thông tin phù hợp cho yêu cầu này. Anh chị nói rõ hơn cần số liệu hay sản phẩm gì giúp em nhé."
    return chat


EARLY_KINDS = ("dashboard", "slides", "web_design", "diagram")   # sản phẩm lâu: dựng sớm, song song với lời đáp


def _artifact_job(kind: str, meeting_id: int, art_prompt: str, context_text: str, data_text: str,
                  facts: Dict[str, Any], segments: List[Dict[str, Any]], meeting: Optional[Dict[str, Any]]):
    if kind == "dashboard":
        return artifacts.generate_dashboard(meeting_id, art_prompt, context_text, data_text, facts)
    if kind == "report":
        return artifacts.generate_report(meeting_id, art_prompt, context_text, data_text, facts)
    if kind == "slides":
        return artifacts.generate_slides(meeting_id, art_prompt, context_text, data_text)
    if kind == "web_design":
        return artifacts.generate_web_sandbox(meeting_id, art_prompt, context_text, data_text)
    if kind == "diagram":
        return artifacts.generate_diagram(meeting_id, art_prompt, context_text, data_text)
    return artifacts.generate_meeting_minutes(meeting_id, segments, (meeting or {}).get("title") or "Biên bản cuộc họp",
                                              meeting=meeting)


async def _with_reminder(coro, progress: Callable[..., Any], after: float = 20.0):
    """Chạy coro; quá `after` giây chưa xong thì báo một câu cho người dùng biết vẫn đang làm."""
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=after)
    except asyncio.TimeoutError:
        await progress("Sắp xong rồi ạ, em đang hoàn thiện nốt.", "status")
        return await task


async def think_and_act(meeting_id: int, prompt: str, segments: List[Dict[str, Any]],
                        trigger: str = "voice_wake_word",
                        on_thinking: Optional[Callable[[str], Any]] = None,
                        on_tool: Optional[Callable[[Dict[str, Any]], Any]] = None,
                        on_insights: Optional[Callable[[List[Dict[str, str]]], Any]] = None,
                        on_progress: Optional[Callable[[str, str], Any]] = None,
                        meeting: Optional[Dict[str, Any]] = None,
                        stage_art: Optional[Dict[str, Any]] = None,
                        library: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Trợ lý xử lý một yêu cầu: tra cứu (nếu cần) -> câu trả lời có nội dung thật -> sản phẩm trực quan.

    Trong lúc chờ, báo tiến độ bằng lời qua on_progress ("em tìm thấy 6 ticket, 1 cái quá hạn...")."""
    from meeting.artifacts import _call_llm
    artifacts.set_meeting(meeting_id, "trợ lý")
    context_text = _context_lines(segments)
    facts = artifacts.meeting_facts(segments, meeting)
    thinking_trace: List[str] = []
    tool_results: List[Dict[str, Any]] = []

    async def _thought(text: str):
        thinking_trace.append(text)
        if on_thinking:
            await on_thinking(text)

    async def _progress(text: str, kind: str = "progress"):
        """kind: progress = điều tìm thấy (đọc to, giữ lại); status = đang làm gì (bỏ khi đã có kết quả)."""
        thinking_trace.append(text)
        if on_progress:
            await on_progress(text, kind)

    system = artifacts.Blocks([artifacts.text_block(artifacts.with_skill(
        AGENT_SYSTEM.replace("{{NAME}}", assistant_config()["name"])
        .replace("{{TOOLS}}", "\n".join(f"- {d}" for d in AGENT_TOOLS.values())), "assistant"), cache=True)])
    transcript = transcript_blocks(segments)
    plan: Dict[str, Any] = {}
    plain = ""
    recall = recall_like(prompt)            # "quay lại cái slide lúc nãy": không dựng sớm sản phẩm mới
    early_kind = guess_kind(prompt) if guess_kind(prompt) in EARLY_KINDS and not recall else None
    early_job = None
    for rnd in range(MAX_TOOL_ROUNDS + 1):
        last = rnd == MAX_TOOL_ROUNDS
        raw = await _call_llm(system, _agent_request(prompt, transcript, facts, tool_results, stage_art, last, library),
                              max_tokens=3000)
        calls, final, text = parse_agent_output(raw)
        if final is not None:
            plan = final
            break
        calls = [c for c in calls if c.get("tool") in AGENT_TOOLS][:3]
        if not calls or last:
            plain = text
            break
        for c in calls:
            tool = c["tool"]
            args = c.get("arguments") if isinstance(c.get("arguments"), dict) else (c.get("args") or {})
            await _thought(f"Em đang tra cứu {TOOL_LABELS.get(tool, tool)}...")
            if on_tool:
                await on_tool({"tool": tool, "args": args})
            if tool == "web_search":                                # trình duyệt thật / Claude, không qua MCP
                async def _web_status(text, kind="status"):
                    await _progress(text, "status")
                res = await artifacts.web_search_tool(str(args.get("query") or prompt), _web_status,
                                                      rewrite=not args.get("query"))
            else:
                call_async = getattr(mcp, "call_tool_async", None)  # qua MCP thật nếu đã cấu hình, không thì tại chỗ
                res = await call_async(tool, args) if call_async else await asyncio.to_thread(mcp.call_tool, tool, args)
            tool_results.append({"tool": tool, "args": args, "result": res})
            finding = summarize_tool_result(tool, res)
            if finding:
                await _progress(finding)
        if early_kind and early_job is None:
            await _progress(PROGRESS_START[early_kind], "status")
            early_job = asyncio.ensure_future(_artifact_job(early_kind, meeting_id, prompt, context_text,
                                                            _data_text(tool_results), facts, segments, meeting))

    insights = artifacts.normalize_insights(plan.get("insights"))[:3]
    if insights and on_insights:
        await on_insights(insights)

    show = _pick_show(plan.get("show"), library)
    kind = plan.get("artifact_needed")
    kind = kind if kind in ARTIFACT_KINDS and show is None else None
    if kind is None and not plan and not recall:
        kind = guess_kind(prompt)       # LLM không trả về JSON: đoán theo câu lệnh để vẫn có kết quả hiển thị
    report_md = str(plan.get("report_markdown") or "").strip()
    chat = str(plan.get("chat_response") or plain or "").strip()
    if kind is None and report_md and (len(report_md) > 280 or re.search(r"^\s*([-*]|\d+\.|\|)", report_md, re.M)):
        kind = "report"                 # nội dung dài/có danh sách: chiếu lên màn hình thay vì chỉ đọc
    art_prompt = str(plan.get("artifact_prompt") or prompt)
    data_text = _data_text(tool_results)
    art, err = None, ""
    if early_job is not None and kind != early_kind:
        early_job.cancel()              # kế hoạch chọn loại khác: bỏ bản dựng sớm
        early_job = None
    if kind:
        try:
            if kind == "report" and report_md:
                art = artifacts.save_report(meeting_id, report_md, prompt)
            elif early_job is not None:
                art = await _with_reminder(early_job, _progress, after=12.0)
            else:
                await _progress(PROGRESS_START[kind], "status")
                art = await _with_reminder(_artifact_job(kind, meeting_id, art_prompt, context_text, data_text,
                                                         facts, segments, meeting), _progress)
        except Exception as e:
            log.warning("meeting.llm: tạo %s lỗi: %s", kind, e)
            err = str(e)
    if show is not None and (not chat or (len(chat) < 90 and _GENERIC_REPLY.search(chat))):
        chat = f"Dạ, em mở lại {KIND_NAMES.get(show['kind'], show['kind'])} \"{show['title']}\"."
    chat = _final_reply(chat, kind, art, err, report_md) if show is None else chat
    if art is not None and art.get("kind") == "report":
        report_md = ""                  # đã chiếu dưới dạng báo cáo, không lặp lại trong khung chat
    show_out = {"artifact_id": show["artifact_id"], "slide": show["slide"]} if show else None

    db.record_interaction(meeting_id=meeting_id, prompt=prompt, trigger=trigger, thinking="\n".join(thinking_trace),
                          tool_calls=tool_results, response={"chat_response": chat, "artifact": art, "show": show_out})
    return {"chat_response": chat, "report": report_md, "thinking": "\n".join(thinking_trace),
            "tool_calls": tool_results, "insights": insights, "artifact": art, "show": show_out,
            "present": plan.get("present") is True and (art is not None or show_out is not None)}


def _pick_show(value: Any, library: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Kiểm tra lựa chọn "mở lại" của trợ lý: chỉ nhận id có trong danh sách nội dung của cuộc họp."""
    if not isinstance(value, dict) or not library:
        return None
    items = {it["id"]: it for it in library.get("items") or [] if isinstance(it.get("id"), int)}
    try:
        aid = int(value.get("artifact_id"))
    except (TypeError, ValueError):
        return None
    it = items.get(aid)
    if it is None:
        return None
    slide = value.get("slide")
    try:
        slide = max(0, int(slide) - 1) if slide not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        slide = None
    n = len(it.get("slides") or [])
    if slide is not None and n and slide >= n:
        slide = n - 1
    return {"artifact_id": aid, "slide": slide, "kind": it.get("kind"), "title": it.get("title", "")}
