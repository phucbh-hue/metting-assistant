"""Mẫu biên bản (14/10/2026): biên bản = bản recap của cuộc họp; mọi người tự thiết kế cấu trúc biên bản cho từng loại họp.

Phạm vi và quyền:
- Hệ thống (dựng sẵn, id "b-..."): ai cũng dùng và nhân bản được, không ai sửa / xóa.
- Công ty: quản trị viên (AUTH_ADMIN_EMAILS) tạo, sửa, xóa, đặt làm mặc định của công ty; mọi người dùng được.
- Nhóm: chủ nhóm tạo, sửa, xóa, đặt làm mặc định của nhóm; thành viên nhóm dùng và nhân bản được.
- Cá nhân: ai cũng tạo được, chỉ người tạo thấy, sửa, xóa.
- Chọn mẫu cho một cuộc họp, lập lại biên bản bằng mẫu khác: người tạo cuộc họp hoặc chủ nhóm của cuộc họp.
Bản chạy trên máy (không đăng nhập): một người dùng, được làm mọi việc.

Mẫu dùng cho một cuộc họp (mẫu bị xóa thì xuống bước sau): mẫu chọn riêng cho cuộc họp > mặc định của nhóm > mặc định
của công ty > mẫu dựng sẵn hợp với loại / tên cuộc họp (daily, phỏng vấn, họp khách...) > Biên bản tiêu chuẩn.
"""
import copy
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from meeting import auth, db, groups, retrieval

log = logging.getLogger("meeting.minutes_templates")

FORMATS = ("paragraph", "bullets", "table")
MAX_SECTIONS = 12
DEFAULT_KEY = "minutes_template_default"


def _s(title: str, instructions: str, fmt: str = "bullets", columns: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"title": title, "instructions": instructions, "format": fmt, "columns": columns or []}


BUILTIN: List[Dict[str, Any]] = [
    {"id": "b-standard", "name": "Biên bản tiêu chuẩn", "description": "Dùng được cho hầu hết cuộc họp nội bộ.",
     "sections": [_s("Tóm tắt điều hành", "2-3 đoạn ngắn: trọng tâm, vấn đề nổi cộm, kết quả đạt được.", "paragraph"),
                  _s("Các quyết định đã thống nhất", "Mỗi quyết định: nội dung, lý do, người đề xuất."),
                  _s("Bảng phân công nhiệm vụ", "Mỗi việc một dòng; ghi chú trích ngắn lời nói làm căn cứ.", "table",
                     ["STT", "Nhiệm vụ", "Người phụ trách", "Hạn chót", "Ưu tiên", "Ghi chú & căn cứ"]),
                  _s("Vấn đề tồn đọng, rủi ro cần theo dõi", "Câu hỏi chưa có câu trả lời, rủi ro, việc chưa có người nhận.")]},
    {"id": "b-daily", "name": "Daily standup", "description": "Họp nhanh hằng ngày: hôm qua, hôm nay, vướng mắc.",
     "sections": [_s("Cập nhật theo từng người", "Mỗi người một dòng.", "table",
                     ["Người", "Hôm qua đã làm", "Hôm nay sẽ làm", "Vướng mắc"]),
                  _s("Vướng mắc cần hỗ trợ", "Blocker, ai hỗ trợ, cần xử lý trước khi nào."),
                  _s("Việc cần theo dõi", "Việc phát sinh trong buổi.", "table", ["Việc", "Người phụ trách", "Hạn"])]},
    {"id": "b-client", "name": "Họp với khách hàng (BD)", "description": "Buổi làm việc với khách: nhu cầu, câu hỏi, cam kết, bước tiếp theo.",
     "sections": [_s("Thông tin buổi họp", "Khách hàng (công ty, người tham dự), đội UrBox tham dự, mục tiêu buổi họp."),
                  _s("Nhu cầu và vấn đề của khách", "Điều khách cần, đang gặp khó khăn, tiêu chí lựa chọn."),
                  _s("Câu hỏi của khách và câu trả lời của UrBox", "Mỗi câu hỏi một dòng; câu chưa trả lời được thì ghi cần gửi thêm.",
                     "table", ["Câu hỏi của khách", "UrBox đã trả lời", "Cần gửi thêm"]),
                  _s("Cam kết, báo giá, điều kiện đã đưa ra", "Ghi đúng số tiền, thời hạn, điều kiện và ai đưa ra."),
                  _s("Bước tiếp theo", "Việc của cả hai bên.", "table", ["Việc", "Bên phụ trách", "Người phụ trách", "Hạn chót"]),
                  _s("Cơ hội và rủi ro của deal", "Tín hiệu mua, lo ngại của khách, đối thủ được nhắc tới.")]},
    {"id": "b-interview", "name": "Phỏng vấn tuyển dụng", "description": "Ghi lại buổi phỏng vấn theo tiêu chí, không tự kết luận thay người phỏng vấn.",
     "sections": [_s("Thông tin buổi phỏng vấn", "Vị trí, ứng viên (ghi tên như trong cuộc họp), người phỏng vấn."),
                  _s("Tóm tắt kinh nghiệm ứng viên", "Theo lời ứng viên kể, 1-2 đoạn.", "paragraph"),
                  _s("Đánh giá theo tiêu chí", "Mỗi tiêu chí một dòng, bằng chứng là điều ứng viên nói; mức đánh giá chỉ ghi khi "
                     "người phỏng vấn nói ra, không thì để trống.", "table", ["Tiêu chí", "Bằng chứng trong buổi phỏng vấn", "Mức đánh giá"]),
                  _s("Điểm mạnh", "Theo bằng chứng trong buổi."),
                  _s("Điểm cần lưu ý", "Chỗ trả lời chưa rõ, cần hỏi thêm ở vòng sau."),
                  _s("Câu hỏi của ứng viên", "Câu ứng viên hỏi và câu trả lời của bên mình."),
                  _s("Ý kiến của người phỏng vấn", "Chỉ ghi điều người phỏng vấn nói ra; AI không tự kết luận đạt hay không đạt.",
                     "paragraph")],
     "instructions": "Không ghi thông tin cá nhân nhạy cảm (số điện thoại, CCCD, lương hiện tại) nếu có trong cuộc họp."},
    {"id": "b-sprint", "name": "Sprint review / planning", "description": "Kết quả sprint, demo, kế hoạch sprint tới.",
     "sections": [_s("Kết quả sprint", "Mục tiêu sprint, việc đã xong và chưa xong (mã ticket nếu có)."),
                  _s("Demo và phản hồi", "Tính năng đã demo, phản hồi của người xem."),
                  _s("Kế hoạch sprint tới", "Mỗi hạng mục một dòng.", "table", ["Hạng mục", "Người phụ trách", "Ước lượng", "Ghi chú"]),
                  _s("Rủi ro và phụ thuộc", "Việc phụ thuộc đội khác, rủi ro trễ hạn.")]},
    {"id": "b-retro", "name": "Retrospective", "description": "Nhìn lại: điều tốt, điều cần cải thiện, hành động.",
     "sections": [_s("Điều làm tốt", "Ghi theo ý của mọi người."), _s("Điều cần cải thiện", "Ghi theo ý của mọi người."),
                  _s("Hành động cải tiến", "Mỗi hành động một dòng.", "table", ["Hành động", "Người phụ trách", "Hạn"])]},
    {"id": "b-1on1", "name": "Họp 1:1", "description": "Trao đổi giữa quản lý và thành viên.",
     "sections": [_s("Chủ đề trao đổi", ""), _s("Phản hồi hai chiều", "Phản hồi của quản lý và của thành viên."),
                  _s("Mục tiêu và cam kết", ""), _s("Việc cần làm", "", "table", ["Việc", "Người phụ trách", "Hạn"])]},
    {"id": "b-weekly", "name": "Báo cáo tuần", "description": "Cập nhật kết quả tuần theo mảng, vấn đề, kế hoạch tuần tới.",
     "sections": [_s("Kết quả tuần theo mảng", "Mỗi mảng một dòng, ghi số liệu nếu có.", "table",
                     ["Mảng", "Kết quả", "Chỉ số / số liệu", "Trạng thái"]),
                  _s("Vấn đề cần xử lý", "Vấn đề, ảnh hưởng, đề xuất."),
                  _s("Kế hoạch tuần tới", "", "table", ["Việc", "Người phụ trách", "Hạn"])]},
]
for _b in BUILTIN:
    _b.update(scope="builtin", owner="", group_id=None, version=1)
    _b.setdefault("instructions", "")
_BUILTIN = {b["id"]: b for b in BUILTIN}

_HINTS = (("b-daily", r"\b(daily|stand ?up|hop nhanh)\b"), ("b-interview", r"\b(phong van|interview|ung vien)\b"),
          ("b-retro", r"\bretro"), ("b-sprint", r"\bsprint\b"), ("b-1on1", r"\b(1[:\-]1|1on1|one on one)\b"),
          ("b-weekly", r"\b(weekly|hang tuan|bao cao tuan|giao ban)\b"),
          ("b-client", r"\b(khach hang|client|customer|deal)\b"))


class TemplateError(Exception):
    """Lỗi viết cho người dùng đọc."""


def _col():
    return db._get_db()["minutes_templates"]


def _email(user: Optional[Dict[str, Any]]) -> str:
    return str((user or {}).get("email") or "").lower()


def _admin(user: Optional[Dict[str, Any]]) -> bool:
    return not auth.ENABLED or auth.is_admin(user)


# --------------------------------------------------------------- đọc / quyền ---
def get(ref: Any) -> Optional[Dict[str, Any]]:
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref in _BUILTIN:
        return copy.deepcopy(_BUILTIN[ref])
    if not ref.isdigit():
        return None
    doc = _col().find_one({"id": int(ref)}, {"_id": 0})
    if doc:
        doc["id"] = str(doc["id"])
    return doc


def can_view(user: Optional[Dict[str, Any]], t: Optional[Dict[str, Any]]) -> bool:
    if not t:
        return False
    if t["scope"] in ("builtin", "company") or not auth.ENABLED:
        return True
    if t["scope"] == "personal":
        return t.get("owner") == _email(user)
    g = groups.get_group(t.get("group_id"))
    return groups.role_of(g, _email(user)) is not None


def can_edit(user: Optional[Dict[str, Any]], t: Optional[Dict[str, Any]]) -> bool:
    if not t or t["scope"] == "builtin":
        return False
    if not auth.ENABLED:
        return True
    if t["scope"] == "company":
        return auth.is_admin(user)
    if t["scope"] == "personal":
        return t.get("owner") == _email(user)
    return groups.role_of(groups.get_group(t.get("group_id")), _email(user)) == "owner"


def can_create(user: Optional[Dict[str, Any]], scope: str, group_id: Optional[int]) -> bool:
    if scope == "personal":
        return True
    if scope == "company":
        return _admin(user)
    if scope == "group":
        return not auth.ENABLED or groups.role_of(groups.get_group(group_id), _email(user)) == "owner"
    return False


def company_default() -> str:
    return str(db.get_setting(DEFAULT_KEY, "") or "")


def group_default(gid: Optional[int]) -> str:
    g = groups.get_group(gid) if gid else None
    return str((g or {}).get("minutes_template") or "")


def visible(user: Optional[Dict[str, Any]], group_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Mẫu anh chị thấy: dựng sẵn, công ty, của các nhóm mình thuộc, của riêng mình. Kèm quyền sửa và cờ mặc định."""
    email = _email(user)
    q: Dict[str, Any] = {"scope": "company"}
    ors = [q]
    if auth.ENABLED:
        ors.append({"scope": "personal", "owner": email})
        gids = groups.group_ids_for(email)
        if gids:
            ors.append({"scope": "group", "group_id": {"$in": gids}})
    else:
        ors += [{"scope": "personal"}, {"scope": "group"}]
    rows = [copy.deepcopy(b) for b in BUILTIN] + [dict(d, id=str(d["id"])) for d in
                                                  _col().find({"$or": ors}, {"_id": 0}).sort("name", 1)]
    cdef, gdef = company_default(), group_default(group_id)
    for t in rows:
        t["can_edit"] = can_edit(user, t)
        t["default_company"] = t["id"] == cdef
        t["default_group"] = bool(gdef) and t["id"] == gdef
        if t["scope"] == "group":
            g = groups.get_group(t.get("group_id"))
            t["group_name"] = (g or {}).get("name") or ""
    return rows


# --------------------------------------------------------------- tạo / sửa ---
def _clean(text: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace(chr(0x2014), "-")).strip()[:n]


def validate(data: Dict[str, Any]) -> Dict[str, Any]:
    name = _clean(data.get("name"), 120)
    if not name:
        raise TemplateError("Mẫu biên bản cần có tên")
    secs = []
    for s in (data.get("sections") or [])[:MAX_SECTIONS]:
        title = _clean((s or {}).get("title"), 120)
        if not title:
            continue
        fmt = (s or {}).get("format") if (s or {}).get("format") in FORMATS else "bullets"
        cols = [_clean(c, 40) for c in ((s or {}).get("columns") or []) if _clean(c, 40)][:8] if fmt == "table" else []
        if fmt == "table" and len(cols) < 2:
            raise TemplateError(f"Mục \"{title}\" dạng bảng cần ít nhất 2 cột")
        secs.append({"title": title, "instructions": _clean((s or {}).get("instructions"), 600), "format": fmt,
                     "columns": cols})
    if not secs:
        raise TemplateError("Mẫu biên bản cần ít nhất một mục")
    return {"name": name, "description": _clean(data.get("description"), 300), "sections": secs,
            "instructions": _clean(data.get("instructions"), 1500)}


def create(user: Optional[Dict[str, Any]], data: Dict[str, Any]) -> Dict[str, Any]:
    scope = str(data.get("scope") or "personal")
    gid = int(data["group_id"]) if scope == "group" and data.get("group_id") else None
    if scope not in ("personal", "group", "company") or (scope == "group" and not gid):
        raise TemplateError("Phạm vi mẫu không hợp lệ")
    if not can_create(user, scope, gid):
        raise PermissionError({"company": "Chỉ quản trị viên tạo được mẫu của công ty",
                               "group": "Chỉ chủ nhóm tạo được mẫu của nhóm"}.get(scope, "Không có quyền"))
    body = validate(data)
    now = time.time()
    doc = {"id": db._next_id("minutes_templates"), **body, "scope": scope, "group_id": gid, "owner": _email(user),
           "version": 1, "created_at": now, "updated_at": now, "updated_by": _email(user)}
    _col().insert_one(dict(doc))
    doc.pop("_id", None)
    return dict(doc, id=str(doc["id"]))


def update(user: Optional[Dict[str, Any]], ref: str, data: Dict[str, Any]) -> Dict[str, Any]:
    t = get(ref)
    if not t or not can_view(user, t):
        raise KeyError(ref)
    if not can_edit(user, t):
        raise PermissionError("Anh chị không sửa được mẫu này (mẫu dựng sẵn thì nhân bản rồi sửa bản sao)")
    body = validate({**t, **data})
    _col().update_one({"id": int(t["id"])}, {"$set": {**body, "updated_at": time.time(), "updated_by": _email(user)},
                                              "$inc": {"version": 1}})
    return get(t["id"])


def delete(user: Optional[Dict[str, Any]], ref: str) -> bool:
    t = get(ref)
    if not t or not can_view(user, t):
        raise KeyError(ref)
    if not can_edit(user, t):
        raise PermissionError("Anh chị không xóa được mẫu này")
    _col().delete_one({"id": int(t["id"])})
    if company_default() == t["id"]:
        db.set_setting(DEFAULT_KEY, "")
    db._get_db()["meeting_groups"].update_many({"minutes_template": t["id"]}, {"$set": {"minutes_template": ""}})
    groups._CACHE.clear()
    return True


def duplicate(user: Optional[Dict[str, Any]], ref: str, scope: str = "personal", group_id: Optional[int] = None,
              name: str = "") -> Dict[str, Any]:
    t = get(ref)
    if not t or not can_view(user, t):
        raise KeyError(ref)
    return create(user, {**t, "name": name or f"{t['name']} (bản sao)", "scope": scope, "group_id": group_id})


def set_company_default(user: Optional[Dict[str, Any]], ref: Optional[str]) -> str:
    if not _admin(user):
        raise PermissionError("Chỉ quản trị viên đặt được mẫu mặc định của công ty")
    ref = str(ref or "")
    t = get(ref) if ref else None
    if ref and (not t or t["scope"] not in ("builtin", "company")):
        raise TemplateError("Mẫu mặc định của công ty phải là mẫu dựng sẵn hoặc mẫu của công ty")
    db.set_setting(DEFAULT_KEY, ref)
    return ref


def set_group_default(user: Optional[Dict[str, Any]], gid: int, ref: Optional[str]) -> str:
    g = groups.get_group(gid)
    if g is None:
        raise KeyError(gid)
    if auth.ENABLED and groups.role_of(g, _email(user)) != "owner":
        raise PermissionError("Chỉ chủ nhóm đặt được mẫu mặc định của nhóm")
    ref = str(ref or "")
    t = get(ref) if ref else None
    if ref and (not t or t["scope"] == "personal" or (t["scope"] == "group" and t.get("group_id") != g["id"])):
        raise TemplateError("Mẫu mặc định của nhóm phải là mẫu dựng sẵn, của công ty hoặc của chính nhóm này")
    groups.set_minutes_template(g["id"], ref)
    return ref


# --------------------------------------------------------------- chọn mẫu cho cuộc họp ---
def hint(meeting: Optional[Dict[str, Any]]) -> str:
    text = retrieval.fold(f"{(meeting or {}).get('meeting_type') or ''} {(meeting or {}).get('title') or ''}")
    return next((ref for ref, pat in _HINTS if re.search(pat, text)), "b-standard")


def resolve(meeting: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Mẫu dùng để lập biên bản cho cuộc họp này (mẫu bị xóa thì xuống bước sau)."""
    meeting = meeting or {}
    for ref in (meeting.get("minutes_template"), group_default(meeting.get("group_id")), company_default(), hint(meeting)):
        t = get(ref)
        if t:
            return t
    return copy.deepcopy(_BUILTIN["b-standard"])


def usable_for(user: Optional[Dict[str, Any]], ref: str, meeting: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Mẫu người này được chọn cho cuộc họp: thấy được mẫu; mẫu nhóm thì phải đúng nhóm của cuộc họp."""
    t = get(ref)
    if not t or not can_view(user, t):
        raise TemplateError("Không tìm thấy mẫu biên bản này")
    if t["scope"] == "group" and t.get("group_id") != (meeting or {}).get("group_id"):
        raise TemplateError("Mẫu của nhóm chỉ dùng cho cuộc họp trong nhóm đó")
    return t


# --------------------------------------------------------------- prompt ---
def _section_lines(t: Dict[str, Any]) -> str:
    out = []
    for i, s in enumerate(t.get("sections") or [], 1):
        how = s.get("instructions") or ""
        if s.get("format") == "table":
            cols = s.get("columns") or []
            body = f"| {' | '.join(cols)} |\n|{'---|' * len(cols)}"
            out.append(f"## {i}. {s['title']}\n{('(' + how + ')' + chr(10)) if how else ''}{body}")
        elif s.get("format") == "paragraph":
            out.append(f"## {i}. {s['title']}\n(đoạn văn{': ' + how if how else ''})")
        else:
            out.append(f"## {i}. {s['title']}\n(gạch đầu dòng{': ' + how if how else ''})")
    return "\n\n".join(out)


def render_system(t: Dict[str, Any]) -> str:
    extra = f"\n- Yêu cầu riêng của mẫu: {t['instructions']}" if t.get("instructions") else ""
    return (f"""Bạn là Thư ký Cuộc họp Thông minh (Executive Meeting Scribe) của UrBox.
Nhiệm vụ: đọc toàn bộ transcript và soạn biên bản (bản recap) của cuộc họp theo MẪU "{t['name']}".

Cấu trúc bắt buộc (định dạng Markdown), đúng thứ tự và đúng tên các mục:
# BIÊN BẢN CUỘC HỌP: [Tiêu đề cuộc họp]
**Thời gian:** [Thời gian] | **Chủ trì:** [Host] | **Thành viên tham dự:** [Danh sách]

{_section_lines(t)}

Yêu cầu:
- Viết tiếng Việt chuẩn xác; chỉ dùng thông tin có trong cuộc họp, không bịa số liệu, tên người, cam kết.
- Mục nào cuộc họp không nói tới thì ghi "Không có trong cuộc họp", không bỏ mục.
- Bảng giữ đúng các cột của mẫu. Tên người nói và nhiệm vụ phải đối chiếu đúng với nhân sự công ty; nhãn "Người nói N" là
  người chưa xác định được tên.
- Ngày tháng dd/mm/yyyy, tiền theo dạng 1.000.000đ, không dùng gạch dài.{extra}""")


def public(t: Dict[str, Any]) -> Dict[str, Any]:
    return {k: t.get(k) for k in ("id", "name", "description", "scope", "group_id", "group_name", "owner", "sections",
                                  "instructions", "version", "updated_at", "updated_by", "can_edit", "default_company",
                                  "default_group") if k in t}


# --------------------------------------------------------------- AI soạn mẫu ---
DRAFT_SYSTEM = """Bạn thiết kế MẪU BIÊN BẢN CUỘC HỌP cho UrBox (nền tảng quà tặng, loyalty tại Việt Nam) từ mô tả của người dùng.
Trả về DUY NHẤT một JSON, không kèm chữ nào khác:
{"name": "tên mẫu ngắn", "description": "1 câu: dùng cho loại họp nào",
 "sections": [{"title": "tên mục", "instructions": "AI cần ghi gì ở mục này, 1 câu", "format": "paragraph|bullets|table",
               "columns": ["cột 1", "cột 2"]}],
 "instructions": "yêu cầu chung của mẫu (có thể rỗng)"}
Quy tắc: 3-8 mục, mục quan trọng nhất trước; việc cần làm / phân công luôn là bảng có cột Người phụ trách và Hạn;
columns chỉ có khi format là table (2-6 cột); viết tiếng Việt, không dùng gạch dài."""


async def draft(description: str) -> Dict[str, Any]:
    """AI soạn một mẫu từ mô tả (chưa lưu: người dùng xem, sửa rồi mới lưu)."""
    from meeting import artifacts
    description = _clean(description, 1500)
    if len(description) < 8:
        raise TemplateError("Mô tả thêm một chút: loại họp nào, biên bản cần những phần gì")
    with artifacts.purpose("mẫu biên bản"):
        data = artifacts._json_from_text(await artifacts._call_llm(DRAFT_SYSTEM, description, max_tokens=2000))
    if not isinstance(data, dict):
        raise TemplateError("AI chưa soạn được mẫu, anh chị thử mô tả lại")
    return validate(data)
