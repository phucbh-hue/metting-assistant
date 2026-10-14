"""Kho tri thức WikiBox qua MCP thật (14/10/2026): kết nối OAuth 2.1 + PKCE hoặc API key cá nhân, token mã hóa, công cụ
đọc từ server, trợ lý dùng công cụ thật thay kho tri thức giả lập, Bảng BD thêm nguồn U.

WikiBox là bản giả (httpx.MockTransport cho OAuth, phiên MCP giả), AI là bản giả: không gọi dịch vụ thật."""
import asyncio
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import artifacts, bd, db, groups, live, llm, wikibox

ENV = {"WIKIBOX_URL": "https://wiki.test", "AUTH_SECRET": "a-stable-secret-for-tests-0123456789"}
HITS = {"results": [{"title": "Chính sách đổi trả voucher", "content": "Voucher điện tử được đổi trả trong 7 ngày nếu chưa dùng.",
                     "url": "https://wiki.test/kb/1/doc/9", "score": 0.82, "knowledge_base_name": "CSKH"},
                    {"document": {"title": "Bảng giá UrBox 2026", "url": "https://wiki.test/kb/2/doc/3"},
                     "chunk": "Phí tích hợp API: 5.000.000đ một lần."}]}
TOOLS = [
    SimpleNamespace(name="ask_librarian", description="Hỏi Thủ thư của WikiBox, trả lời có trích dẫn",
                    input_schema={"type": "object", "properties": {"question": {"type": "string"}}}),
    SimpleNamespace(name="search_documents", description="Tìm tài liệu trong không gian làm việc",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
                                  "required": ["query"]}),
    SimpleNamespace(name="read_document", description="Đọc nội dung tài liệu",
                    input_schema={"type": "object", "properties": {"document_id": {"type": "string"}},
                                  "required": ["document_id"]}),
]


class FakeWiki:
    """Máy chủ OAuth giả của WikiBox: metadata (ghi http:// như máy chủ thật sau proxy), đăng ký client, token, thu hồi."""

    def __init__(self):
        self.registered, self.tokens, self.revoked, self.schemes = [], [], [], set()
        self.n, self.refresh_fail, self.allowed = 0, False, None      # allowed: origin được phép (None = mọi origin)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.schemes.add(req.url.scheme)
        path = req.url.path
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["http://wiki.test"], "resource": "http://wiki.test/mcp",
                                             "scopes_supported": ["wikibox.read"], "bearer_methods_supported": ["header"]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"authorization_endpoint": "http://wiki.test/api/v1/oauth/authorize",
                                             "token_endpoint": "http://wiki.test/api/v1/oauth/token",
                                             "registration_endpoint": "http://wiki.test/api/v1/oauth/register",
                                             "revocation_endpoint": "http://wiki.test/api/v1/oauth/revoke",
                                             "code_challenge_methods_supported": ["S256"]})
        if path == "/api/v1/oauth/register":
            body = json.loads(req.content)
            uri = body["redirect_uris"][0]
            origin = "{0.scheme}://{0.netloc}".format(urlparse(uri))
            if self.allowed is not None and origin not in self.allowed:     # câu lỗi thật của WikiBox 14/10/2026
                return httpx.Response(400, json={"error": "invalid_redirect_uri", "error_description":
                                                 f'redirect_uri "{uri}" origin "{origin}" is not in the allowed redirect origins'})
            self.registered.append(body)
            return httpx.Response(201, json={"client_id": f"cid-{len(self.registered)}"})
        if path == "/api/v1/oauth/token":
            form = dict(parse_qsl(req.content.decode()))
            self.tokens.append(form)
            if form.get("grant_type") == "refresh_token" and self.refresh_fail:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.n += 1
            return httpx.Response(200, json={"access_token": f"at-{self.n}", "refresh_token": f"rt-{self.n}",
                                             "expires_in": 3600, "scope": "wikibox.read"})
        if path == "/api/v1/oauth/revoke":
            self.revoked.append(dict(parse_qsl(req.content.decode())))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": "not_found"})


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    async def list_tools(self):
        return SimpleNamespace(tools=TOOLS)

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(HITS, ensure_ascii=False))],
                               structured_content=None, is_error=False)


class WikiBase(unittest.TestCase):
    def setUp(self):
        reset_db()
        wikibox._META.clear()
        wikibox._ACCESS.clear()
        self.wiki = FakeWiki()
        self.mcp_calls, self.tokens_used = [], []

        async def fake_mcp(token, fn):
            self.tokens_used.append(token)
            if token.startswith("bad"):
                raise RuntimeError("Client error '401 Unauthorized' for url 'https://wiki.test/mcp'")
            return await fn(FakeSession(self.mcp_calls))
        self.patches = [mock.patch.dict(os.environ, ENV), mock.patch.object(wikibox, "_transport", httpx.MockTransport(self.wiki)),
                        mock.patch.object(wikibox, "_mcp", fake_mcp)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def connect_key(self):
        return asyncio.run(wikibox.save_key(None, "wbk_live_0123456789abcdef"))


class OAuthTests(WikiBase):
    def test_pkce_flow_tokens_encrypted_refresh_and_revoke(self):
        url = asyncio.run(wikibox.auth_url(None, "http://localhost:8080/", "http://localhost:8080"))
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.assertTrue(url.startswith("https://wiki.test/api/v1/oauth/authorize?"))    # http:// trong metadata -> https
        self.assertEqual((q["response_type"], q["scope"], q["code_challenge_method"], q["client_id"]),
                         ("code", "wikibox.read", "S256", "cid-1"))
        self.assertEqual(q["redirect_uri"], "http://localhost:8080/api/wikibox/callback")
        self.assertEqual(q["resource"], "http://wiki.test/mcp")                          # đúng resource máy chủ khai báo
        self.assertEqual(self.wiki.registered[0]["redirect_uris"], ["http://localhost:8080/api/wikibox/callback"])
        self.assertEqual(self.wiki.registered[0]["token_endpoint_auth_method"], "none")
        asyncio.run(wikibox.auth_url(None, "http://localhost:8080/", "http://localhost:8080"))
        self.assertEqual(len(self.wiki.registered), 1)                                  # client_id nhớ lại, không đăng ký lại
        res = asyncio.run(wikibox.exchange("code-1", q["state"]))
        self.assertEqual(res["return_to"], "http://localhost:8080/")
        form = self.wiki.tokens[0]
        self.assertEqual((form["grant_type"], form["code"], form["client_id"]), ("authorization_code", "code-1", "cid-1"))
        self.assertEqual(len(form["code_verifier"]), 64)
        self.assertNotIn(form["code_verifier"], url)                                     # verifier không lộ trên URL
        self.assertEqual(self.wiki.schemes, {"https"})
        doc = db._get_db()["wikibox_tokens"].find_one({"owner": wikibox.LOCAL})
        self.assertNotIn("at-1", json.dumps(doc, default=str))                           # token chỉ lưu dạng mã hóa
        self.assertNotIn("rt-1", json.dumps(doc, default=str))
        st = wikibox.status(None)
        self.assertEqual((st["connected"], st["kind"], st["search_tool"]), (True, "oauth", "search_documents"))
        with self.assertRaises(wikibox.WikiboxError):
            asyncio.run(wikibox.exchange("code-1", q["state"]))                          # state chỉ dùng một lần
        wikibox._ACCESS.clear()
        db._get_db()["wikibox_tokens"].update_one({"owner": wikibox.LOCAL}, {"$set": {"expires_at": time.time() - 5}})
        self.assertEqual(asyncio.run(wikibox.access_token(wikibox.LOCAL)), "at-2")       # hết hạn: tự làm mới
        self.assertEqual(self.wiki.tokens[-1]["refresh_token"], "rt-1")
        asyncio.run(wikibox.disconnect(None))
        self.assertEqual(self.wiki.revoked[0]["token"], "rt-2")
        self.assertFalse(wikibox.status(None)["connected"])

    def test_revoked_refresh_token_asks_to_reconnect(self):
        url = asyncio.run(wikibox.auth_url(None, "", "http://localhost:8080"))
        asyncio.run(wikibox.exchange("c", parse_qs(urlparse(url).query)["state"][0]))
        wikibox._ACCESS.clear()
        db._get_db()["wikibox_tokens"].update_one({"owner": wikibox.LOCAL}, {"$set": {"expires_at": 0}})
        self.wiki.refresh_fail = True
        with self.assertRaises(wikibox.NeedConnect):
            asyncio.run(wikibox.access_token(wikibox.LOCAL))
        self.assertFalse(wikibox.connected(wikibox.LOCAL))

    def test_origin_not_allowed_by_wikibox_explains_what_to_do(self):
        self.wiki.allowed = {"http://localhost:8080"}
        with self.assertRaises(wikibox.OAuthBlocked) as cm:
            asyncio.run(wikibox.auth_url(None, "", "https://urbox-meeting-api.onrender.com"))
        msg = str(cm.exception)
        self.assertIn("https://urbox-meeting-api.onrender.com", msg)
        self.assertIn("Origin redirect được phép", msg)
        self.assertIn("API key", msg)
        self.assertEqual(self.wiki.registered, [])
        url = asyncio.run(wikibox.auth_url(None, "", "http://localhost:8080"))      # origin được phép thì vẫn chạy
        self.assertIn("client_id=cid-1", url)

    def test_needs_a_server_secret_to_store_tokens(self):
        with mock.patch.dict(os.environ, {"AUTH_SECRET": "", "GOOGLE_TOKEN_KEY": "", "WIKIBOX_TOKEN_KEY": ""}):
            with self.assertRaises(wikibox.WikiboxError):
                asyncio.run(wikibox.auth_url(None, "", "http://localhost:8080"))


class KeyAndSearchTests(WikiBase):
    def test_api_key_is_checked_then_saved_encrypted_and_search_works(self):
        with self.assertRaises(wikibox.WikiboxError):
            asyncio.run(wikibox.save_key(None, "bad-key-0123456789abcdef"))              # key sai: không lưu
        self.assertFalse(wikibox.connected(wikibox.LOCAL))
        st = self.connect_key()
        self.assertEqual((st["kind"], st["tools"], st["search_tool"]),
                         ("key", ["ask_librarian", "search_documents", "read_document"], "search_documents"))
        self.assertNotIn("wbk_live", json.dumps(db._get_db()["wikibox_tokens"].find_one({}), default=str))
        hits = asyncio.run(wikibox.search(wikibox.LOCAL, "đổi trả voucher", 3))
        self.assertEqual(self.mcp_calls[-1], ("search_documents", {"query": "đổi trả voucher", "top_k": 3}))
        self.assertEqual([h["title"] for h in hits], ["Chính sách đổi trả voucher", "Bảng giá UrBox 2026"])
        self.assertEqual((hits[0]["url"], hits[0]["kb"]), ("https://wiki.test/kb/1/doc/9", "CSKH"))
        self.assertEqual((hits[1]["url"], hits[1]["text"]), ("https://wiki.test/kb/2/doc/3", "Phí tích hợp API: 5.000.000đ một lần."))
        self.assertEqual(self.tokens_used[-1], "wbk_live_0123456789abcdef")

    def test_search_tool_guess_and_result_shapes(self):
        tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in TOOLS]
        self.assertEqual(wikibox.search_tool(tools), ("search_documents", "query", "top_k"))
        self.assertEqual(wikibox.search_tool([tools[0]]), None)                          # chỉ có hỏi Thủ thư: không coi là tìm kiếm
        self.assertEqual(wikibox.normalize_hits({"text": "Đoạn trả lời"}), [{"title": "WikiBox", "text": "Đoạn trả lời",
                                                                             "url": "", "score": None, "kb": ""}])
        self.assertEqual(wikibox.normalize_hits({"items": ["một", "hai"]}, 1)[0]["text"], "một")
        self.assertEqual(wikibox.normalize_hits({"results": [{"title": "x", "url": "javascript:alert(1)"}]})[0]["url"], "")
        lines = wikibox.agent_tool_lines(tools)
        self.assertEqual(lines["wikibox.search_documents"].split(":")[0], "wikibox.search_documents(query, top_k?)")


class AgentTests(WikiBase):
    def test_assistant_uses_real_wikibox_tools_instead_of_the_mock_kb(self):
        self.connect_key()
        mid = db.create_meeting("Họp CSKH")
        calls = []

        async def fake_llm(system, prompt, max_tokens=4000):
            calls.append(str(system))
            if len(calls) == 1:
                return 'TOOL_CALL: {"tool": "wikibox.search_documents", "arguments": {"query": "đổi trả voucher"}}'
            self.assertIn("Chính sách đổi trả voucher", str(prompt))
            return json.dumps({"chat_response": "Theo WikiBox, voucher đổi trả trong 7 ngày nếu chưa dùng.",
                               "artifact_needed": None}, ensure_ascii=False)
        with mock.patch.object(artifacts, "_call_llm", fake_llm):
            res = asyncio.run(llm.think_and_act(mid, "chính sách đổi trả voucher thế nào", [], meeting=db.get_meeting(mid)))
        self.assertIn("wikibox.search_documents(query, top_k?)", calls[0])
        self.assertNotIn("- search_knowledge(", calls[0])                               # kho giả lập không còn trong danh sách
        self.assertEqual(res["tool_calls"][0]["tool"], "wikibox.search_documents")
        self.assertEqual(self.mcp_calls[0], ("search_documents", {"query": "đổi trả voucher"}))
        self.assertIn("7 ngày", res["chat_response"])
        self.assertIn("Em tìm thấy 2 tài liệu trên WikiBox", res["thinking"])

    def test_without_connection_the_mock_kb_stays(self):
        mid = db.create_meeting("Họp")
        tools_map, owner = asyncio.run(llm.agent_tools(mid, db.get_meeting(mid)))
        self.assertIn("search_knowledge", tools_map)
        self.assertFalse(any(k.startswith("wikibox.") for k in tools_map))


class BDWikiTests(WikiBase):
    def test_bd_answer_cites_the_company_wiki(self):
        self.connect_key()
        g = groups.create_group("BD ABC", "an@urbox.vn")
        groups.update_group(g["id"], bd_mode=True)
        mid = db.create_meeting("Họp khách ABC", owner="an@urbox.vn")
        groups.set_meeting_group(mid, g["id"])
        prompts = []

        async def fake_llm(system, prompt, max_tokens=4000):
            prompts.append(str(prompt))
            return json.dumps({"question": "Chính sách đổi trả voucher?",
                               "answer": "Dạ voucher bên em đổi trả trong 7 ngày nếu chưa dùng ạ [U1].",
                               "notes": [], "confidence": "cao"}, ensure_ascii=False)

        async def go():
            s = await live.get_session(mid)
            a = bd.assistant(s)
            with mock.patch.object(artifacts, "_call_llm", fake_llm):
                await a.ask("Khách hỏi chính sách đổi trả voucher thế nào?", by="an@urbox.vn")
                await a.drain()
            return bd.load_turns(mid)
        [turn] = asyncio.run(go())
        self.assertIn("[U1] Chính sách đổi trả voucher - CSKH", prompts[0])
        self.assertEqual(turn["sources"], [{"code": "U1", "kind": "wiki", "title": "Chính sách đổi trả voucher", "ref": "CSKH",
                                            "url": "https://wiki.test/kb/1/doc/9"}])


class WikiApiTests(WikiBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_settings_api(self):
        self.assertFalse(self.client.get("/api/wikibox/status").json()["connected"])
        self.assertEqual(self.client.post("/api/wikibox/key", json={"api_key": "bad-key-0123456789abcdef"}).status_code, 400)
        st = self.client.post("/api/wikibox/key", json={"api_key": "Bearer wbk_live_0123456789abcdef"}).json()
        self.assertTrue(st["connected"])
        r = self.client.post("/api/wikibox/test", json={"query": "đổi trả"}).json()
        self.assertEqual((r["search_tool"], r["hits"][0]["title"]), ("search_documents", "Chính sách đổi trả voucher"))
        self.wiki.allowed = {"https://khac.example.com"}
        r = self.client.post("/api/wikibox/connect", json={"return_to": ""})
        self.assertEqual(r.status_code, 409)                                    # giao diện mở ô dán API key
        self.assertIn("Origin redirect được phép", r.json()["detail"])
        self.wiki.allowed = None
        url = self.client.post("/api/wikibox/connect", json={"return_to": ""}).json()["url"]
        state = parse_qs(urlparse(url).query)["state"][0]
        r = self.client.get(f"/api/wikibox/callback?error=access_denied&state={state}", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].endswith("#/?wikibox=denied"))
        self.assertFalse(self.client.post("/api/wikibox/disconnect").json()["connected"])


if __name__ == "__main__":
    unittest.main()
