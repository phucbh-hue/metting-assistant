"""Test tra cứu web bằng trình duyệt: giải link Bing, lọc nội dung, đổi câu hỏi thành từ khóa, báo cáo có nguồn đánh số,
dự phòng sang Claude, và công cụ web_search của agent.

Không gọi mạng. Riêng BrowserParsingTests chạy Chromium thật trên trang mẫu (set_content), bỏ qua nếu chưa cài trình duyệt.
"""
import base64
import json
import unittest
from unittest import mock

from tests.helpers import reset_db
from meeting import artifacts, db, live, llm, websearch

PAGES = [
    {"title": "Giá vàng SJC", "url": "https://sjc.com.vn/gia", "domain": "sjc.com.vn", "published": "",
     "text": "Cập nhật lúc 13:31 01/10/2026\nVÀNG SJC 1L 141,300,000 144,300,000"},
    {"title": "Giá vàng hôm nay", "url": "https://24h.com.vn/gv", "domain": "24h.com.vn",
     "published": "2026-10-01T21:42:00+07:00", "text": "SJC Mua 141,300 Bán 144,300"},
]


def bing_href(url: str) -> str:
    enc = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc&u=a1{enc}&ntb=1"


class HelperTests(unittest.TestCase):
    def test_decode_bing_links(self):
        url = "https://www.sjc.com.vn/gia-vang-online"
        self.assertEqual(websearch.decode_bing(bing_href(url)), url)
        self.assertEqual(websearch.decode_bing("https://example.com/x"), "https://example.com/x")

    def test_clean_results_skips_social_and_duplicates(self):
        raw = [{"title": "Giá vàng", "href": bing_href("https://webgia.com/gia-vang/sjc/"), "snippet": "Bảng giá"},
               {"title": "Trùng", "href": "https://webgia.com/gia-vang/sjc", "snippet": ""},
               {"title": "Video", "href": "https://www.youtube.com/watch?v=1", "snippet": ""},
               {"title": "", "href": "https://m.facebook.com/abc", "snippet": ""},
               {"title": "", "href": "javascript:void(0)", "snippet": ""},
               {"title": "", "href": "https://www.pnj.com.vn/blog/gia-vang/", "snippet": ""}]
        res = websearch.clean_results(raw)
        self.assertEqual([r["domain"] for r in res], ["webgia.com", "pnj.com.vn"])
        self.assertEqual(res[1]["title"], "pnj.com.vn")                 # thiếu tiêu đề thì dùng tên miền

    def test_query_rewrite_and_relevance(self):
        self.assertEqual(websearch.rewrite_heuristic("hiện tại giá vàng đang bao nhiêu?"), "giá vàng hôm nay")
        self.assertEqual(websearch.rewrite_heuristic("xu hướng gifting 2026"), "xu hướng gifting 2026")
        terms = websearch.key_terms("giá vàng hôm nay")
        self.assertEqual(terms, {"gia", "vang"})
        clock = {"title": "Thời gian hiện tại | Đồng Hồ Online", "url": "https://donghoonline.com/"}
        gold = {"title": "Bảng giá", "url": "https://sjc.com.vn/gia-vang-online"}
        self.assertEqual(websearch.relevance(clock, terms), 0)          # trang đồng hồ không liên quan: bị loại
        self.assertEqual(websearch.relevance(gold, terms), 2)

    def test_pick_diverse_prefers_different_sites(self):
        rs = [{"domain": "24h.com.vn", "url": "a"}, {"domain": "24h.com.vn", "url": "b"},
              {"domain": "sjc.com.vn", "url": "c"}, {"domain": "pnj.com.vn", "url": "d"}]
        self.assertEqual([r["url"] for r in websearch.pick_diverse(rs, 3)], ["a", "c", "d"])

    def test_focus_text_keeps_header_and_relevant_rows(self):
        noise = "\n".join(f"Tin bóng đá số {i} hôm qua" for i in range(400))
        text = ("Giá vàng SJC hôm nay\nCập nhật lúc 13:31 01/10/2026\n" + noise
                + "\nLOẠI VÀNG MUA BÁN\nVÀNG SJC 1L 141,300,000 144,300,000\nVÀNG NHẪN 140,800,000 143,800,000\n" + noise)
        out = websearch.focus_text(text, "giá vàng SJC hôm nay", limit=1500)
        self.assertLessEqual(len(out), 1500)
        for must in ("Cập nhật lúc 13:31", "141,300,000", "VÀNG NHẪN 140,800,000"):
            self.assertIn(must, out)
        self.assertEqual(websearch.focus_text("ngắn gọn", "x"), "ngắn gọn")


class BrowserParsingTests(unittest.IsolatedAsyncioTestCase):
    """Chạy đoạn JS lọc kết quả và lọc nội dung trên trang mẫu trong Chromium thật (không cần mạng)."""

    async def asyncSetUp(self):
        try:
            from playwright.async_api import async_playwright
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(headless=True)
        except Exception as e:
            self.skipTest(f"Chưa có Playwright / Chromium: {e}")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_bing_results_and_main_text(self):
        page = await self.browser.new_page()
        await page.set_content(
            f'<ol><li class="b_algo"><h2><a href="{bing_href("https://webgia.com/gia-vang/sjc/")}">Giá vàng SJC</a></h2>'
            '<div class="b_caption"><p>Bảng giá cập nhật liên tục</p></div></li>'
            '<li class="b_algo"><h2><a href="https://www.youtube.com/watch?v=1">Video giá vàng</a></h2></li></ol>')
        res = websearch.clean_results(await page.evaluate(websearch.RESULTS_JS))
        self.assertEqual([(r["url"], r["title"], r["snippet"]) for r in res],
                         [("https://webgia.com/gia-vang/sjc/", "Giá vàng SJC", "Bảng giá cập nhật liên tục")])
        body = ('<nav>Trang chủ Tin tức Liên hệ</nav><div class="main-menu">Đăng nhập</div><article><h1>Giá vàng hôm nay</h1>'
                + "<p>Vàng SJC mua 141.300.000đ, bán 144.300.000đ.</p>" * 20
                + '</article><footer>Bản quyền 2026</footer>')
        await page.set_content('<html><head><meta property="article:published_time" content="2026-10-01T21:42:00+07:00">'
                               f'<title>Giá vàng</title></head><body>{body}</body></html>')
        info = await page.evaluate(websearch.EXTRACT_JS)
        self.assertIn("141.300.000đ", info["text"])
        for junk in ("Bản quyền", "Liên hệ", "Đăng nhập"):
            self.assertNotIn(junk, info["text"])
        self.assertTrue(info["published"].startswith("2026-10-01"))
        self.assertEqual(info["title"], "Giá vàng")


class WebResearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Họp")

    async def test_browser_path_builds_report_with_numbered_sources(self):
        calls, prog = [], []

        async def fake_research(q, on_progress=None, max_pages=4, queries=None):
            calls.append(queries)
            await on_progress("Em tìm thấy 2 kết quả, đang đọc 2 trang: sjc.com.vn, 24h.com.vn.", "progress")
            return {"query": q, "queries": queries, "engine": "Bing", "results": [], "pages": PAGES, "seconds": 1.0}

        async def fake_llm(system, prompt, max_tokens=4000):
            if system.startswith("Đổi câu hỏi"):
                return '{"queries": ["giá vàng hôm nay", "giá vàng SJC 01/10/2026"]}'
            self.assertIn("[1] Giá vàng SJC - https://sjc.com.vn/gia", prompt)
            self.assertIn("(đăng/cập nhật: 2026-10-01T21:42:00+07:00)", prompt)
            self.assertIn("Thời điểm hiện tại:", prompt)
            return "Em đã đọc xong các trang.\n# Tra cứu: giá vàng\n\nVàng SJC bán 144.300.000đ [1][2].\n\n## Chi tiết\n- Mua 141.300.000đ [1]"

        async def on_progress(text, kind="progress"):
            prog.append((kind, text))

        with mock.patch.dict("os.environ", {"WEB_SEARCH_PROVIDER": "auto"}), \
                mock.patch.object(websearch, "available", lambda: True), \
                mock.patch.object(websearch, "research", fake_research), \
                mock.patch.object(artifacts, "_call_llm", fake_llm), mock.patch.object(artifacts, "llm_available", lambda: True):
            art = await artifacts.web_research(self.mid, "hiện tại giá vàng đang bao nhiêu", on_progress=on_progress)
        self.assertEqual(calls, [["giá vàng hôm nay", "giá vàng SJC 01/10/2026"]])
        c = art["content"]
        self.assertTrue(c.startswith("# Tra cứu: giá vàng"))                      # bỏ câu dạo đầu của AI
        self.assertIn("## Nguồn\n1. [Giá vàng SJC](https://sjc.com.vn/gia) - sjc.com.vn\n2. [Giá vàng hôm nay]", c)
        self.assertIn('Yêu cầu: "hiện tại giá vàng đang bao nhiêu"', c)
        self.assertIn("bằng trình duyệt (Bing, từ khóa: giá vàng hôm nay; giá vàng SJC 01/10/2026), đọc 2 trang", c)
        self.assertEqual([k for k, _ in prog], ["progress", "status"])
        self.assertEqual(db.get_artifact(art["id"])["kind"], "report")

    async def test_browser_failure_falls_back_to_claude_in_auto_mode(self):
        async def broken(*a, **k):
            raise RuntimeError("Bing chặn")
        claude = []

        async def fake_claude(query, context_text=""):
            claude.append(query)
            return "# Tra cứu: tỷ giá\n\n25.624 VND/USD", [{"url": "https://sbv.gov.vn", "title": "NHNN"}], 2
        prog = []

        async def on_progress(text, kind="progress"):
            prog.append(text)
        with mock.patch.dict("os.environ", {"WEB_SEARCH_PROVIDER": "auto", "ANTHROPIC_API_KEY": "x"}), \
                mock.patch.object(websearch, "available", lambda: True), mock.patch.object(websearch, "research", broken), \
                mock.patch.object(artifacts, "llm_available", lambda: False), \
                mock.patch.object(artifacts, "_claude_search", fake_claude):
            art = await artifacts.web_research(self.mid, "tỷ giá đô hôm nay", on_progress=on_progress)
        self.assertEqual(claude, ["tỷ giá đô hôm nay"])
        self.assertIn("- [NHNN](https://sbv.gov.vn)", art["content"])
        self.assertIn("công cụ tìm kiếm của Claude, 2 lượt tìm", art["content"])
        self.assertTrue(any("chuyển sang công cụ tìm kiếm của Claude" in p for p in prog))

    async def test_playwright_only_mode_reports_error(self):
        async def broken(*a, **k):
            raise RuntimeError("không có mạng")
        with mock.patch.dict("os.environ", {"WEB_SEARCH_PROVIDER": "playwright", "ANTHROPIC_API_KEY": "x"}), \
                mock.patch.object(websearch, "available", lambda: True), mock.patch.object(websearch, "research", broken), \
                mock.patch.object(artifacts, "llm_available", lambda: False):
            with self.assertRaises(RuntimeError) as cm:
                await artifacts.web_research(self.mid, "giá vàng")
        self.assertIn("không có mạng", str(cm.exception))


class AgentWebSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Họp")

    async def asyncTearDown(self):
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()

    async def test_agent_can_call_web_search_tool(self):
        self.assertIn("web_search", llm.AGENT_TOOLS)
        seen = []

        async def fake_tool(query, on_progress=None):
            seen.append(query)
            return {"query": query, "engine": "Bing",
                    "sources": [dict(n=i, excerpt=p["text"], **{k: p[k] for k in ("title", "url", "domain", "published")})
                                for i, p in enumerate(PAGES, 1)]}

        async def fake_llm(system, prompt, max_tokens=4000):
            if "Dữ liệu đã tra cứu:\n(chưa tra cứu)" in prompt:
                return 'TOOL_CALL: {"tool": "web_search", "arguments": {"query": "giá vàng SJC hôm nay"}}'
            self.assertIn("141,300,000", prompt)                                  # nội dung trang đã vào ngữ cảnh
            return json.dumps({"chat_response": "Vàng SJC đang bán 144.300.000đ một lượng.", "artifact_needed": None},
                              ensure_ascii=False)
        progress = []

        async def on_progress(text, kind="progress"):
            progress.append(text)
        with mock.patch.object(artifacts, "web_search_tool", fake_tool), mock.patch.object(artifacts, "_call_llm", fake_llm), \
                mock.patch.object(artifacts, "llm_available", lambda: True):
            res = await llm.think_and_act(self.mid, "so sánh giá vàng hôm nay giúp anh", [], on_progress=on_progress)
        self.assertEqual(seen, ["giá vàng SJC hôm nay"])
        self.assertIn("Em đã đọc 2 nguồn trên mạng: sjc.com.vn, 24h.com.vn.", progress)
        self.assertEqual(res["chat_response"], "Vàng SJC đang bán 144.300.000đ một lượng.")

    def test_summarize_web_result(self):
        self.assertEqual(llm.summarize_tool_result("web_search", {"sources": []}),
                         "Em chưa tìm được trang nào phù hợp trên mạng.")
        self.assertIn("chưa tra được", llm.summarize_tool_result("web_search", {"error": "bị chặn"}))


if __name__ == "__main__":
    unittest.main()
