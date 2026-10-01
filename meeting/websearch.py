"""Tra cứu web bằng trình duyệt thật (Playwright + Chromium chạy ngầm).

Tìm trên Bing (Google và DuckDuckGo chặn trình duyệt tự động), mở các trang kết quả đầu, lấy phần nội dung chính
(kể cả bảng giá do JavaScript vẽ hoặc nằm trong iframe), lọc lấy các dòng liên quan đến câu hỏi, rồi đưa cho AI tóm
tắt kèm nguồn đánh số. Không tốn phí tìm kiếm; chỉ tốn token khi AI đọc phần nội dung đã lọc.

Cài đặt (một lần): pip install playwright && python -m playwright install chromium
"""
import asyncio
import base64
import logging
import os
import re
import time
import unicodedata
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("meeting.websearch")

SEARCH_URL = "https://www.bing.com/search?q={q}&setlang=vi&cc=VN&count=10"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
MAX_PAGES = int(os.getenv("WEB_SEARCH_PAGES", "4") or 4)
PAGE_CHARS = 3500               # tối đa số ký tự lấy từ mỗi trang (sau khi lọc theo câu hỏi)
TOTAL_TIMEOUT_S = 60
NAV_TIMEOUT_MS = 15000
# Trang khó đọc bằng trình duyệt không đăng nhập (video, mạng xã hội) hoặc là trang của chính bộ máy tìm kiếm
SKIP_DOMAINS = ("youtube.com", "youtu.be", "facebook.com", "fb.com", "fb.watch", "tiktok.com", "instagram.com",
                "x.com", "twitter.com", "zalo.me", "linkedin.com", "bing.com", "go.microsoft.com", "pinterest.com")
_STOP = {"la", "cua", "va", "cho", "anh", "chi", "em", "toi", "minh", "coi", "xem", "giup", "nhe", "nha", "the", "nao",
         "gi", "bao", "nhieu", "dang", "hien", "tai", "co", "khong", "ve", "o", "trong", "mot", "nhung", "cac", "duoc"}

# Từ quá chung để xét một kết quả có liên quan hay không ("hôm nay", "mới nhất"...)
_GENERIC = {"hom", "nay", "moi", "nhat", "cap", "nhap", "today", "latest", "now", "news", "tin", "tuc", "thong"}
# Từ đệm trong câu nói, bỏ đi khi tự đổi câu hỏi thành từ khóa (dự phòng khi không có AI)
_FILLERS = re.compile(r"\b(hiện tại|hiện giờ|bây giờ|đang là|đang|là bao nhiêu|bao nhiêu|thế nào|như thế nào|ra sao|"
                      r"giúp anh|giúp chị|giúp em|giúp mình|giùm|cho anh|cho chị|coi thử|coi|xem thử|xem|thử|nhé|nha|ạ|"
                      r"vậy|không|có|cái|là gì|gì)\b", re.I)
_TIMELY = re.compile(r"hiện tại|hiện giờ|bây giờ|hôm nay|mới nhất|bao nhiêu|đang", re.I)

Progress = Optional[Callable[[str, str], Awaitable[Any]]]

RESULTS_JS = r"""() => [...document.querySelectorAll('li.b_algo')].map(li => {
  const a = li.querySelector('h2 a') || li.querySelector('a[href]');
  const sn = li.querySelector('.b_caption p, p.b_lineclamp2, p.b_lineclamp3, p.b_lineclamp4, .b_algoSlug, .b_snippet')
          || li.querySelector('.b_caption');
  return { title: a ? (a.textContent || '').trim() : '', href: a ? a.href : '',
           snippet: sn ? (sn.textContent || '').trim() : '' };
})"""

EXTRACT_JS = r"""() => {
  const meta = n => (document.querySelector(`meta[property="${n}"],meta[name="${n}"],meta[itemprop="${n}"]`) || {}).content || '';
  const info = { title: document.title || '', site: meta('og:site_name'),
                 published: meta('article:published_time') || meta('datePublished') || meta('pubdate') || meta('date')
                            || meta('article:modified_time') || meta('og:updated_time') };
  const full = (document.body && document.body.innerText) || '';
  const junk = 'script,style,noscript,svg,canvas,nav,footer,header,aside,form,button,[role=navigation],[role=banner],' +
               '[role=contentinfo],[aria-hidden=true]';
  document.querySelectorAll(junk).forEach(e => e.remove());
  document.querySelectorAll('[class*="menu" i],[class*="footer" i],[class*="sidebar" i],[class*="comment" i],' +
                            '[class*="advert" i],[class*="cookie" i],[class*="related" i],[id*="comment" i]')
    .forEach(e => { if (e !== document.body && (e.innerText || '').length < 4000) e.remove(); });
  const root = document.querySelector('article') || document.querySelector('main') || document.querySelector('[role=main]')
               || document.body;
  let text = (root && root.innerText) || '';
  if (text.length < 400) text = (document.body && document.body.innerText) || '';
  if (text.length < 400) text = full;
  info.text = text;
  return info;
}"""


def available() -> bool:
    """Đã cài gói playwright chưa (trình duyệt Chromium kiểm tra khi chạy thật)."""
    try:
        import playwright.async_api  # noqa: F401
        return True
    except Exception:
        return False


def fold(text: str) -> str:
    t = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "d")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def _words(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", fold(text)))


def rewrite_heuristic(question: str) -> str:
    """Câu nói -> từ khóa tìm kiếm khi không có AI: "hiện tại giá vàng đang bao nhiêu" -> "giá vàng hôm nay"."""
    q = re.sub(r"[?!.,;:\"']", " ", question or "")
    kw = re.sub(r"\s+", " ", _FILLERS.sub(" ", q)).strip()
    if not kw:
        kw = q.strip()
    if _TIMELY.search(question or "") and "hôm nay" not in kw.lower():
        kw += " hôm nay"
    return kw


def key_terms(text: str) -> set:
    return {w for w in _words(text) if len(w) > 1 and w not in _STOP and w not in _GENERIC}


def relevance(item: Dict[str, Any], terms: set) -> int:
    return len(terms & _words(f"{item.get('title', '')} {item.get('snippet', '')} {item.get('url', '')}"))


def decode_bing(href: str) -> str:
    """Link kết quả của Bing dạng bing.com/ck/a?...&u=a1<base64url> -> link gốc của trang."""
    try:
        parsed = urllib.parse.urlparse(href)
        if "bing.com" not in parsed.netloc or not parsed.path.startswith("/ck/"):
            return href
        u = (urllib.parse.parse_qs(parsed.query).get("u") or [""])[0]
        if u.startswith("a1"):
            s = u[2:]
            s += "=" * (-len(s) % 4)
            return base64.urlsafe_b64decode(s).decode("utf-8", "replace")
    except Exception:
        pass
    return href


def domain_of(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def clean_results(raw: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    """Giải link Bing, bỏ trùng, bỏ trang video / mạng xã hội."""
    out, seen = [], set()
    for r in raw or []:
        url = decode_bing(str(r.get("href") or ""))
        if not url.startswith(("http://", "https://")):
            continue
        dom = domain_of(url)
        if any(dom == d or dom.endswith("." + d) for d in SKIP_DOMAINS):
            continue
        key = url.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": str(r.get("title") or "").strip() or dom, "url": url, "domain": dom,
                    "snippet": re.sub(r"\s+", " ", str(r.get("snippet") or "")).strip()[:300]})
        if len(out) >= limit:
            break
    return out


def pick_diverse(results: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """Chọn n trang để đọc, ưu tiên mỗi trang một nguồn khác nhau (giữ thứ tự của Bing)."""
    picks, doms = [], set()
    for r in results:
        if len(picks) >= n:
            break
        if r["domain"] not in doms:
            picks.append(r)
            doms.add(r["domain"])
    for r in results:
        if len(picks) >= n:
            break
        if r not in picks:
            picks.append(r)
    return picks


def focus_text(text: str, query: str, limit: int = PAGE_CHARS) -> str:
    """Giữ phần đầu trang (tiêu đề, ngày cập nhật) và các dòng liên quan đến câu hỏi kèm vài dòng kế tiếp (bảng số liệu)."""
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if sum(len(ln) + 1 for ln in lines) <= limit:
        return "\n".join(lines)
    terms = {w for w in _words(query) if len(w) > 1 and w not in _STOP}
    scores = []
    for ln in lines:
        ws = _words(ln)
        scores.append(sum(1 for t in terms if t in ws) + (0.3 if re.search(r"\d", ln) else 0.0))
    keep = set(range(min(len(lines), 6)))
    total = sum(len(lines[i]) + 1 for i in keep)
    for i in sorted(range(len(lines)), key=lambda k: (-scores[k], k)):
        if scores[i] < 1 or total >= limit:
            break
        for j in range(max(0, i - 1), min(len(lines), i + 5)):
            if j not in keep:
                keep.add(j)
                total += len(lines[j]) + 1
    out, prev = [], -2
    for i in sorted(keep):
        if out and i != prev + 1:
            out.append("...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)[:limit]


async def _block_heavy(route):
    """Không tải ảnh, video, font: trang mở nhanh hơn, chữ vẫn đầy đủ."""
    try:
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


async def _read_page(ctx, item: Dict[str, Any], query: str) -> Optional[Dict[str, Any]]:
    page = await ctx.new_page()
    try:
        await page.goto(item["url"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass                        # trang có kết nối chạy mãi (quảng cáo, giá live): đọc luôn phần đã có
        info = await page.evaluate(EXTRACT_JS)
        text = info.get("text") or ""
        if len(text) < 300:             # nội dung nằm trong iframe (bảng giá nhúng)
            for fr in page.frames[1:5]:
                try:
                    text += "\n" + (await fr.evaluate("() => document.body ? document.body.innerText : ''") or "")
                except Exception:
                    pass
        text = focus_text(text, query)
        if len(text) < 120:
            return None
        return {"title": (info.get("title") or item["title"]).strip()[:160], "url": item["url"], "domain": item["domain"],
                "published": str(info.get("published") or "")[:40], "snippet": item.get("snippet", ""), "text": text}
    except Exception as e:
        log.info("meeting.websearch: không đọc được %s (%s)", item["url"], str(e)[:120])
        return None
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _search(ctx, query: str) -> List[Dict[str, Any]]:
    page = await ctx.new_page()
    try:
        await page.goto(SEARCH_URL.format(q=urllib.parse.quote_plus(query)), wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS)
        try:
            await page.wait_for_selector("li.b_algo", timeout=6000)
        except Exception:
            pass
        return clean_results(await page.evaluate(RESULTS_JS))
    finally:
        await page.close()


async def _research(question: str, on_progress: Progress, max_pages: int, queries: List[str]) -> Dict[str, Any]:
    from playwright.async_api import async_playwright
    t0 = time.time()
    focus = " ".join(queries)               # lọc nội dung trang theo từ khóa, không theo câu nói nguyên văn

    async def say(text: str, kind: str = "status"):
        if on_progress:
            try:
                await on_progress(text, kind)
            except Exception:
                pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=UA, locale="vi-VN", viewport={"width": 1280, "height": 900})
            await ctx.route("**/*", _block_heavy)
            results: List[Dict[str, Any]] = []
            used: List[str] = []
            for q in queries[:3]:
                await say(f"Em đang tìm trên mạng với từ khóa: {q}.")
                used.append(q)
                terms = key_terms(q)
                found = await _search(ctx, q)
                seen = {r["url"] for r in results}
                results += [r for r in found if r["url"] not in seen and (not terms or relevance(r, terms) > 0)]
                if len(results) >= 3:
                    break
            if not results:
                raise RuntimeError("Bing không trả về kết quả phù hợp (có thể bị chặn hoặc mất mạng)")
            picks = pick_diverse(results, max_pages)
            await say(f"Em tìm thấy {len(results)} kết quả, đang đọc {len(picks)} trang: "
                      f"{', '.join(r['domain'] for r in picks)}.", "progress")
            pages = [pg for pg in await asyncio.gather(*(_read_page(ctx, r, focus) for r in picks)) if pg]
            if len(pages) < 2 and len(results) > len(picks):
                more = [r for r in results if r not in picks][:3]
                pages += [pg for pg in await asyncio.gather(*(_read_page(ctx, r, focus) for r in more)) if pg]
        finally:
            await browser.close()
    return {"query": question, "queries": used, "engine": "Bing", "results": results, "pages": pages,
            "seconds": round(time.time() - t0, 1)}


async def research(question: str, on_progress: Progress = None, max_pages: int = MAX_PAGES,
                   queries: Optional[List[str]] = None) -> Dict[str, Any]:
    """Tìm và đọc các trang đầu.

    queries: từ khóa tìm kiếm (AI đã đổi từ câu nói); thiếu thì tự đổi bằng luật đơn giản.
    Trả về {"query", "queries", "engine", "results", "pages": [{title, url, domain, published, text}]}."""
    qs = [q.strip() for q in (queries or []) if q and q.strip()] or [rewrite_heuristic(question)]
    return await asyncio.wait_for(_research(question, on_progress, max_pages, qs), timeout=TOTAL_TIMEOUT_S)


if __name__ == "__main__":      # chạy thử: python -m meeting.websearch "giá vàng SJC hôm nay"
    import sys

    async def _main():
        async def show(text, kind):
            print(f"[{kind}] {text}")
        data = await research(" ".join(sys.argv[1:]) or "giá vàng SJC hôm nay", show)
        print(f"\n{data['engine']} (từ khóa: {data['queries']}): {len(data['results'])} kết quả, "
              f"đọc {len(data['pages'])} trang trong {data['seconds']}s")
        for i, pg in enumerate(data["pages"], 1):
            print(f"\n[{i}] {pg['title']} - {pg['url']} ({pg['published'] or 'không rõ ngày'})\n{pg['text'][:600]}")
    asyncio.run(_main())
