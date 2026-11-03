"""Thư viện JD: kéo danh sách vị trí tuyển dụng từ trang tuyển dụng công khai talent.urbox.vn (nền tảng Base
E-Hiring) để tạo nhanh vị trí phỏng vấn mà KHÔNG phải dán JD bằng tay.

Trang /alljobs nhúng sẵn toàn bộ dữ liệu (kể cả JD đầy đủ) trong biến JS `Client.pageData` ->
`debug_normal_openings` / `debug_starred_openings`. Ta tải 1 lần, bóc JSON, đổi JD từ HTML sang text, rồi lưu
vào collection dùng chung của Meeting (`interview_talent_jobs`). Không cần đăng nhập, không cần crawl từng trang.
"""
import html as _html
import json
import logging
import re
import time
import unicodedata
import urllib.request
from typing import Any, Dict, List, Optional

from meeting import db as mdb

log = logging.getLogger("interview.talent")

SOURCE_URL = "https://talent.urbox.vn/alljobs"
JOB_BASE = "https://talent.urbox.vn/job/"
COL = "interview_talent_jobs"
STALE_S = 6 * 3600          # danh mục cũ hơn ngần này thì tự đồng bộ lại khi mở
FETCH_TIMEOUT_S = 25
_UA = "Mozilla/5.0 (compatible; UrBoxMeetingCopilot/1.0; +https://urbox.vn)"


def _col():
    return mdb._get_db()[COL]


# ------------------------------------------------------------------ parse ---
def _html_to_text(h: str) -> str:
    """JD dạng HTML (h2/h3/p/ul/li/strong/span) -> text dễ đọc cho AI tạo tiêu chí và cho HR xem."""
    h = h or ""
    h = re.sub(r"(?is)<\s*br\s*/?\s*>", "\n", h)
    h = re.sub(r"(?is)<\s*li[^>]*>", "\n- ", h)
    h = re.sub(r"(?is)<\s*/\s*(p|li|div|h[1-6]|ul|ol|tr)\s*>", "\n", h)
    h = re.sub(r"(?is)<\s*(h[1-6])[^>]*>", "\n\n", h)
    h = re.sub(r"(?is)<[^>]+>", "", h)                 # bỏ các thẻ còn lại
    h = _html.unescape(h)
    h = h.replace(" ", " ")
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r" *\n *", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def _int(x) -> int:
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return 0


def _close_ts(j: Dict[str, Any]) -> int:
    """Hạn nộp (unix). Trang Base dùng etime (thời điểm đóng tin); deadline đôi khi = '0'."""
    for k in ("etime", "deadline", "stime"):
        v = _int(j.get(k))
        if v > 946684800:        # > năm 2000 mới coi là mốc thời gian thật
            return v
    return 0


def _extract_pagedata(page: str) -> Optional[dict]:
    dec = json.JSONDecoder()
    for m in re.finditer(r"Client\.pageData\s*=\s*\{", page):
        try:
            obj, _ = dec.raw_decode(page, m.end() - 1)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("debug_normal_openings" in obj or "custom" in obj):
            return obj
    return None


def _norm_job(j: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sid = str(j.get("id") or "").strip()
    title = " ".join(str(j.get("name") or "").split())
    if not sid or not title:
        return None
    dept = j.get("department")
    dept_name = (dept.get("name") if isinstance(dept, dict) else dept) or ""
    if str(dept_name).strip().lower() in ("untitled", ""):
        dept_name = ""
    url = str(j.get("url") or "").strip() or (JOB_BASE + sid)
    jd_text = _html_to_text(j.get("content") or "")
    return {
        "source_id": sid,
        "codename": str(j.get("codename") or "").strip(),
        "title": title,
        "department": str(dept_name).strip(),
        "location": " ".join(str(j.get("location") or j.get("office") or "").split()),
        "url": url,
        "job_type": str(j.get("job_type") or "").strip(),
        "num_positions": _int(j.get("num_positions")),
        "close_ts": _close_ts(j),
        "status": str(j.get("status") or "").strip(),
        "jd_text": jd_text,
        "jd_len": len(jd_text),
    }


def fetch_remote() -> List[Dict[str, Any]]:
    """Tải trang /alljobs và trả về danh sách job đã chuẩn hoá. Ném lỗi nếu mạng / parse hỏng."""
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": _UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
        page = r.read().decode("utf-8", "replace")
    pd = _extract_pagedata(page)
    if pd is None:
        raise RuntimeError("Không đọc được dữ liệu job từ talent.urbox.vn (trang đổi cấu trúc?)")
    raw = (pd.get("debug_starred_openings") or []) + (pd.get("debug_normal_openings") or [])
    jobs = [nj for nj in (_norm_job(j) for j in raw) if nj]
    if not jobs:
        raise RuntimeError("Trang talent.urbox.vn hiện không có vị trí nào")
    return jobs


# ------------------------------------------------------------------ store ---
def sync() -> Dict[str, Any]:
    """Tải và cập nhật danh mục. Giữ JD cũ của job không còn trên trang (đánh dấu inactive) để
    vị trí đã nhập vẫn tra ngược được nguồn. Trả về số lượng thêm/cập nhật."""
    jobs = fetch_remote()
    col = _col()
    now = time.time()
    live_ids = {j["source_id"] for j in jobs}
    added = updated = 0
    for j in jobs:
        j["active"] = True
        j["fetched_at"] = now
        res = col.update_one({"source_id": j["source_id"]},
                             {"$set": j, "$setOnInsert": {"first_seen": now, "imported_pids": []}},
                             upsert=True)
        if res.upserted_id is not None:
            added += 1
        elif res.modified_count:
            updated += 1
    # job biến mất khỏi trang -> inactive (không xoá)
    gone = col.update_many({"source_id": {"$nin": list(live_ids)}, "active": {"$ne": False}},
                           {"$set": {"active": False, "fetched_at": now}}).modified_count
    _set_meta("last_sync", now)
    log.info("interview.talent: đồng bộ JD - thêm %d, cập nhật %d, ẩn %d, tổng %d", added, updated, gone, len(jobs))
    return {"total": len(jobs), "added": added, "updated": updated, "deactivated": gone, "at": now}


def _set_meta(key: str, value):
    _col().update_one({"source_id": f"__meta__{key}"}, {"$set": {"value": value}}, upsert=True)


def _get_meta(key: str):
    d = _col().find_one({"source_id": f"__meta__{key}"})
    return d.get("value") if d else None


def last_sync() -> float:
    return float(_get_meta("last_sync") or 0.0)


def ensure_fresh(force: bool = False) -> Optional[Dict[str, Any]]:
    """Đồng bộ nếu danh mục trống hoặc đã cũ. Lỗi mạng -> trả None, vẫn dùng dữ liệu đã lưu."""
    try:
        has = _col().count_documents({"source_id": {"$not": re.compile("^__meta__")}}) > 0
        if force or not has or (time.time() - last_sync()) > STALE_S:
            return sync()
    except Exception as e:
        log.warning("interview.talent: tự đồng bộ lỗi (dùng dữ liệu cũ): %s", e)
    return None


# ----------------------------------------------------------------- search ---
def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().replace("đ", "d")


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def _job_card(d: Dict[str, Any]) -> Dict[str, Any]:
    """Thông tin gọn cho danh sách (không kèm JD đầy đủ)."""
    jd = d.get("jd_text") or ""
    return {
        "source_id": d.get("source_id"),
        "codename": d.get("codename"),
        "title": d.get("title"),
        "department": d.get("department"),
        "location": d.get("location"),
        "url": d.get("url"),
        "num_positions": d.get("num_positions"),
        "close_ts": d.get("close_ts") or 0,
        "active": d.get("active", True),
        "jd_len": d.get("jd_len") or len(jd),
        "jd_preview": jd[:240],
        "imported_pids": d.get("imported_pids") or [],
    }


def list_jobs(q: str = "", include_inactive: bool = False) -> List[Dict[str, Any]]:
    """Danh sách job (đã lưu). q -> tìm theo tên gần đúng (bỏ dấu, khớp chuỗi con + độ tương tự)."""
    docs = [d for d in _col().find({"source_id": {"$not": re.compile("^__meta__")}})
            if include_inactive or d.get("active", True)]
    cards = [_job_card(d) for d in docs]
    q = (q or "").strip()
    if q:
        nq = _strip_accents(q)
        tokens = [t for t in nq.split() if t]
        scored, matched = [], []
        for c in cards:
            ntitle = _strip_accents(c["title"])
            hay = _strip_accents(f"{c['title']} {c.get('department') or ''} {c.get('codename') or ''}")
            if nq in hay:                                   # khớp nguyên cụm
                score = 2.0 + (0.5 if ntitle.startswith(nq) else 0.0)
            elif tokens and all(t in hay for t in tokens):  # khớp đủ các từ (tên gần đúng)
                score = 1.0 + sum(t in ntitle for t in tokens) / len(tokens)
            else:
                continue
            matched.append((score, c))
        if matched:
            matched.sort(key=lambda x: (-x[0], x[1]["title"].lower()))
            return [c for _, c in matched]
        # không khớp từ nào -> gợi ý theo độ tương tự (bắt lỗi gõ sai), ngưỡng cao để khỏi nhiễu
        for c in cards:
            s = _similar(nq, _strip_accents(c["title"]))
            if s >= 0.5:
                scored.append((s, c))
        scored.sort(key=lambda x: (-x[0], x[1]["title"].lower()))
        return [c for _, c in scored]
    cards.sort(key=lambda c: c["title"].lower())
    return cards


def get_job(source_id: str) -> Optional[Dict[str, Any]]:
    d = _col().find_one({"source_id": str(source_id)})
    return _strip(d)


def mark_imported(source_id: str, pid: int):
    _col().update_one({"source_id": str(source_id)}, {"$addToSet": {"imported_pids": int(pid)}})


def _strip(d):
    if d:
        d.pop("_id", None)
    return d
