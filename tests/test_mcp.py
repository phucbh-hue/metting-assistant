"""Test MCP: tìm kiếm KB, chặn path traversal, định tuyến call_tool_async và round-trip stdio qua MCP server thật.

Chạy: python -m unittest tests.test_mcp -v   (không cần mạng; server con chạy MEETING_DB=mock)
"""
import asyncio
import importlib.util
import os
import sys
import unittest
from unittest import mock

from tests.helpers import reset_db
from meeting import mcp, mcp_client
from mcp_server import kb_search

HAS_MCP = importlib.util.find_spec("mcp.client.stdio") is not None if importlib.util.find_spec("mcp") else False
_NO_SERVER_ENV = {"MCP_SERVER_URL": "", "MCP_SERVER_CMD": ""}


class KbSearchTest(unittest.TestCase):
    def test_voucher_return_doc_ranks_first(self):
        for q in ("đổi trả voucher", "doi tra voucher"):
            res = kb_search.search(q, top_k=3)
            self.assertGreater(res["count"], 0)
            top = res["results"][0]
            self.assertEqual(top["path"], "chinh-sach-doi-tra-voucher.md", q)
            self.assertIn("đổi trả", top["snippet"].lower())
            self.assertTrue(all(set(r) >= {"title", "path", "snippet", "score"} for r in res["results"]))

    def test_other_topics_and_empty_query(self):
        self.assertEqual(kb_search.search("webhook memory leak", 1)["results"][0]["path"],
                         "kien-truc-he-thong-thanh-toan.md")
        self.assertEqual(kb_search.search("chiết khấu đối tác", 1)["results"][0]["path"], "faq-doi-tac.md")
        self.assertEqual(kb_search.search("", 5)["count"], 0)
        self.assertLessEqual(kb_search.search("voucher", 2)["count"], 2)

    def test_read_document_ok(self):
        for p in ("chinh-sach-doi-tra-voucher.md", "chinh-sach-doi-tra-voucher", "kb://chinh-sach-doi-tra-voucher"):
            doc = kb_search.read_document(p)
            self.assertTrue(doc["content"].startswith("# Chính sách đổi trả"))

    def test_read_document_rejects_traversal(self):
        for p in ("../server.py", "..\\kb_search.py", "../../meeting/db.py", "kb/../../README",
                  "/etc/passwd", "C:/Windows/win.ini", os.path.abspath(__file__), ""):
            with self.assertRaises(ValueError, msg=p):
                kb_search.read_document(p)
        with self.assertRaises(FileNotFoundError):
            kb_search.read_document("khong-ton-tai.md")
        # Qua call_tool: lỗi trả về dạng dict, không raise
        self.assertIn("error", mcp.call_tool("read_document", {"path": "../server.py"}))


class CallToolAsyncFallbackTest(unittest.TestCase):
    def setUp(self):
        reset_db()
        mcp.seed_mock_data()

    def test_kb_tools_listed(self):
        names = {t["name"] for t in mcp.list_tools()}
        self.assertTrue({"search_knowledge", "read_document", "query_jira_issues"} <= names)

    def test_falls_back_in_process_when_unconfigured(self):
        with mock.patch.dict(os.environ, _NO_SERVER_ENV):
            self.assertFalse(mcp_client.is_configured())
            self.assertFalse(asyncio.run(mcp_client.connected()))
            with self.assertRaises(RuntimeError):
                asyncio.run(mcp_client.call_tool("query_jira_issues", {}))
            for name, args in (("query_jira_issues", {"assignee": "Quân"}),
                               ("query_employee_directory", {"query": "Tuấn"}),
                               ("search_knowledge", {"query": "đổi trả voucher", "top_k": 2})):
                self.assertEqual(asyncio.run(mcp.call_tool_async(name, args)), mcp.call_tool(name, args), name)


@unittest.skipUnless(HAS_MCP, 'Chưa cài SDK MCP (pip install "mcp[cli]") - bỏ qua test round-trip stdio')
class StdioRoundTripTest(unittest.TestCase):
    def test_stdio_round_trip(self):
        env = {"MCP_SERVER_URL": "", "MCP_SERVER_CMD": f'"{sys.executable}" -m mcp_server', "MCP_SERVER_DB": "mock"}

        async def scenario():
            try:
                self.assertTrue(await mcp_client.connected())
                names = {t["name"] for t in await mcp_client.list_tools()}
                self.assertTrue({"search_knowledge", "read_document", "query_jira_issues",
                                 "update_jira_issue_status"} <= names)
                jira = await mcp_client.call_tool("query_jira_issues", {"issue_key": "URBOX-103"})
                kb = await mcp_client.call_tool("search_knowledge", {"query": "đổi trả voucher", "top_k": 3})
                bad = await mcp_client.call_tool("read_document", {"path": "../server.py"})
                upd = await mcp_client.call_tool("update_jira_issue_status",
                                                 {"issue_key": "URBOX-104", "new_status": "Done"})
                after = await mcp_client.call_tool("query_jira_issues", {"issue_key": "URBOX-104"})
                via_router = await mcp.call_tool_async("query_jira_issues", {"issue_key": "URBOX-103"})
                return jira, kb, bad, upd, after, via_router
            finally:
                await mcp_client.close()

        with mock.patch.dict(os.environ, env):
            jira, kb, bad, upd, after, via_router = asyncio.run(asyncio.wait_for(scenario(), 40))
        self.assertEqual(jira["count"], 1)
        self.assertEqual(jira["issues"][0]["key"], "URBOX-103")
        self.assertEqual(via_router, jira)
        self.assertEqual(kb["results"][0]["path"], "chinh-sach-doi-tra-voucher.md")
        self.assertIn("error", bad)
        self.assertTrue(upd.get("success"))
        self.assertEqual(after["issues"][0]["status"], "Done")  # trạng thái giữ trong phiên server


if __name__ == "__main__":
    unittest.main()
