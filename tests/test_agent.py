"""Test trợ lý trong cuộc họp: tra cứu -> câu trả lời có nội dung -> sản phẩm hiển thị (dashboard, báo cáo)."""
import json
import unittest
from unittest import mock

from tests.helpers import reset_db
from meeting import artifacts, db, live, llm, mcp

SEGMENTS = [
    {"seq": 1, "speaker_label": "Bùi Hồng Phúc", "t_start": 2.0, "t_end": 8.0, "text": "Chào mọi người, review sprint 39 nhé."},
    {"seq": 2, "speaker_label": "Hương", "t_start": 9.0, "t_end": 20.0,
     "text": "Landing page xong bản desktop, bản mobile còn banner, thứ sáu xong."},
    {"seq": 3, "speaker_label": "Lê Văn Tuấn", "t_start": 70.0, "t_end": 80.0, "text": "Migrate Postgres cuối tuần chạy staging."},
]

FINAL_DASHBOARD = {
    "thought": "Cần số liệu ticket",
    "insights": [{"kind": "risk", "text": "URBOX-102 của Tuấn đã quá hạn từ 30/09/2026."}],
    "artifact_needed": "dashboard",
    "artifact_prompt": "Dashboard tiến độ ticket sprint",
    "chat_response": "Sprint có 6 ticket, URBOX-102 đang quá hạn.",
}

DASH = {"title": "Tiến độ Sprint 39", "kpis": [{"label": "Ticket", "value": "6"}],
        "charts": [{"type": "bar", "title": "Theo trạng thái", "labels": ["Đang làm", "Chưa làm"],
                    "series": [{"name": "Ticket", "data": [3, 2]}]}]}


class ParsingTests(unittest.TestCase):
    def test_tool_call_only_output_is_not_a_final_answer(self):
        calls, final, text = llm.parse_agent_output('TOOL_CALL: {"tool": "query_meeting_history", "arguments": {"keyword": "x"}}')
        self.assertEqual(calls, [{"tool": "query_meeting_history", "arguments": {"keyword": "x"}}])
        self.assertIsNone(final)
        self.assertEqual(text, "")

    def test_final_json_inside_fence_and_truncated_json(self):
        _, final, _ = llm.parse_agent_output("Dạ.\n```json\n" + json.dumps(FINAL_DASHBOARD, ensure_ascii=False) + "\n```")
        self.assertEqual(final["artifact_needed"], "dashboard")
        _, final, _ = llm.parse_agent_output('{"chat_response": "Có 6 ticket", "report_markdown": "# Báo cáo\\n- dòng 1')
        self.assertEqual(final["chat_response"], "Có 6 ticket")
        self.assertTrue(final["report_markdown"].startswith("# Báo cáo"))

    def test_guess_kind_and_ack(self):
        self.assertEqual(llm.guess_kind("em vẽ cho anh mấy cái chạt số liệu nha"), "dashboard")
        self.assertEqual(llm.guess_kind("tạo nhanh trang web giới thiệu sản phẩm"), "web_design")
        self.assertEqual(llm.guess_kind("design một cái report tin nhanh"), "report")
        self.assertIsNone(llm.guess_kind("ai phụ trách phần này"))
        self.assertIn("dashboard", llm.ack_phrase("dựng dashboard tiến độ"))

    def test_generic_reply_is_replaced(self):
        self.assertIn("chưa tìm được", llm._final_reply("Em đã tiếp nhận yêu cầu và xử lý xong ạ!", None, None, "", ""))
        self.assertEqual(llm._final_reply("URBOX-101 đã hoàn thành.", None, None, "", ""), "URBOX-101 đã hoàn thành.")
        art = {"kind": "dashboard", "title": "Dashboard: Sprint", "content": json.dumps(DASH)}
        self.assertIn("1 chỉ số và 1 biểu đồ", llm._final_reply("", "dashboard", art, "", ""))


class FindingTests(unittest.TestCase):
    def test_jira_overview_and_spoken_finding(self):
        issues = [{"key": "A-1", "status": "In Progress", "assignee": "Tuấn", "priority": "High", "due_date": "2026-09-30"},
                  {"key": "A-2", "status": "Done", "assignee": "Hương", "due_date": "2026-09-01"},
                  {"key": "A-3", "status": "To Do", "assignee": "Tuấn", "story_points": 3}]
        ov = llm.jira_overview(issues, today="2026-10-01")
        self.assertEqual(ov["by_assignee"], {"Tuấn": 2, "Hương": 1})
        self.assertEqual(ov["overdue"], [{"key": "A-1", "due": "30/09/2026", "assignee": "Tuấn"}])   # ticket xong thì không tính
        with mock.patch.object(llm.time, "strftime", lambda fmt: "2026-10-01"):
            text = llm.summarize_tool_result("query_jira_issues", {"count": 3, "issues": issues})
        self.assertTrue(text.startswith("Em tìm thấy 3 ticket"))
        self.assertIn("đang làm", text)
        self.assertIn("A-1 của Tuấn đã quá hạn từ 30/09/2026", text)
        self.assertIn("lỗi kết nối", llm.summarize_tool_result("query_jira_issues", {"error": "lỗi kết nối"}))

    def test_meeting_facts(self):
        f = artifacts.meeting_facts(SEGMENTS, {"title": "Review"})
        self.assertEqual([s["speaker"] for s in f["speakers"]], ["Hương", "Lê Văn Tuấn", "Bùi Hồng Phúc"])
        self.assertAlmostEqual(sum(s["share_pct"] for s in f["speakers"]), 100.0, delta=0.2)
        self.assertEqual(f["segments"], 3)
        self.assertEqual(len(f["words_per_minute"]), 2)


class SkillTests(unittest.TestCase):
    def test_skills_are_loaded_into_prompts(self):
        for name in ("diagram", "slides", "report", "dashboard", "assistant"):
            self.assertGreater(len(artifacts.load_skill(name)), 300, name)
        self.assertIn("Skill: Vẽ sơ đồ", artifacts.with_skill("SYS", "diagram"))
        self.assertEqual(artifacts.with_skill("SYS", "không-có"), "SYS")

    def test_talk_stats_only_when_asked(self):
        self.assertTrue(artifacts.wants_stats("ai nói nhiều nhất trong cuộc họp"))
        self.assertFalse(artifacts.wants_stats("làm slide báo cáo tiến độ sprint"))
        facts = {"speakers": [{"speaker": "A", "talk_s": 10}]}
        self.assertNotIn("Thống kê", artifacts._data_prompt("làm slide tiến độ", "A: x", "", facts))
        self.assertIn("Thống kê phát biểu", artifacts._data_prompt("ai nói nhiều nhất", "A: x", "", facts))

    def test_mermaid_checks(self):
        ok = 'flowchart LR\n  A["Thanh toán (VNPay)"] --> B["OK"]'
        self.assertEqual(artifacts.mermaid_problem(ok), "")
        self.assertIn("ngoặc kép", artifacts.mermaid_problem("flowchart LR\n  A[Thanh toán (VNPay)] --> B"))
        self.assertIn("loại sơ đồ", artifacts.mermaid_problem("Dưới đây là sơ đồ"))
        self.assertIn("dateFormat", artifacts.mermaid_problem("gantt\n  title X\n  section A\n  Việc :a1, 2026-10-01, 3d"))
        self.assertEqual(artifacts.extract_mermaid("Sơ đồ:\n```mermaid\nflowchart TD\n A-->B\n```"), "flowchart TD\n A-->B")


class DiagramRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_mermaid_is_retried_once(self):
        reset_db()
        mid = db.create_meeting("Họp")
        calls = []

        async def fake(system, prompt, max_tokens=4000):
            calls.append(prompt)
            return "flowchart LR\n  A[Lỗi (x)] --> B" if len(calls) == 1 else '```mermaid\nflowchart LR\n  A["Lỗi (x)"] --> B\n```'

        with mock.patch.object(artifacts, "_call_llm", fake):
            art = await artifacts.generate_diagram(mid, "vẽ luồng", "A: nói gì đó")
        self.assertEqual(len(calls), 2)
        self.assertIn("bị lỗi", calls[1])
        self.assertEqual(art["content"], 'flowchart LR\n  A["Lỗi (x)"] --> B')


class DashboardTests(unittest.TestCase):
    def test_normalize_messy_dashboard(self):
        raw = {"dashboard": {"title": "Doanh thu", "kpis": [{"label": "Doanh thu", "value": "1.200.000.000đ"}, {"label": ""}],
                             "charts": [
                                 {"type": "column", "title": "Theo tháng", "labels": ["T7", "T8", "T9", "T10"],
                                  "series": [{"name": "Doanh thu", "data": ["1.000.000", "1,5", 2, "x"]}]},
                                 {"type": "doughnut", "labels": ["A", "B"], "series": [{"data": [1, 2]}, {"data": [3, 4]}]},
                                 {"type": "table", "columns": ["Việc", "Người"], "rows": [["Mobile", "Hương"], "lỗi"]},
                                 {"type": "radar", "series": [{"data": ["không có số"]}]},
                                 "không phải biểu đồ"]}}
        d = artifacts.normalize_dashboard(raw)
        self.assertEqual([k["label"] for k in d["kpis"]], ["Doanh thu"])
        bar, donut, table = d["charts"]
        self.assertEqual(bar["type"], "bar")
        self.assertEqual(bar["series"][0]["data"], [1000000.0, 1.5, 2.0, 0])
        self.assertEqual((donut["type"], len(donut["series"])), ("donut", 1))
        self.assertEqual(table["rows"], [["Mobile", "Hương"]])
        self.assertIsNone(artifacts.normalize_dashboard({"kpis": [], "charts": []}))
        self.assertIsNone(artifacts.normalize_dashboard("không phải json"))


class AgentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        mcp.seed_mock_data()
        self.mid = db.create_meeting("Review Sprint 39")
        self.s = await live.get_session(self.mid)
        self.s.segments.extend(SEGMENTS)
        self.q = await self.s.subscribe()

    async def asyncTearDown(self):
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()

    def events(self, kind=None):
        out = []
        while not self.q.empty():
            e = self.q.get_nowait()
            if kind is None or e["type"] == kind:
                out.append(e)
        return out

    async def test_tool_round_then_dashboard_shown_with_spoken_progress(self):
        prompts = []

        async def fake(system, prompt, max_tokens=4000):
            prompts.append((system, prompt))
            if system.startswith(artifacts.DASHBOARD_SYSTEM):
                self.assertIn('"tong_hop"', prompt)                       # số liệu Jira tổng hợp sẵn cho dashboard
                self.assertNotIn('"speakers"', prompt)                    # không hỏi ai nói nhiều -> không đưa thống kê phát biểu
                return json.dumps(DASH, ensure_ascii=False)
            if "Dữ liệu đã tra cứu:\n(chưa tra cứu)" in prompt:
                return 'TOOL_CALL: {"tool": "query_jira_issues", "arguments": {}}'
            return "```json\n" + json.dumps(FINAL_DASHBOARD, ensure_ascii=False) + "\n```"

        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            await self.s._handle_ai_activation("vẽ cho anh dashboard tiến độ sprint", "Tùng ơi, vẽ dashboard", "Tùng")
        ev = self.events()
        types = [e["type"] for e in ev]
        progress = [e["text"] for e in ev if e["type"] == "ai_progress"]
        self.assertEqual(progress[0], "Dạ, em dựng dashboard ngay, anh chị chờ em chút nhé.")
        self.assertTrue(any(p.startswith("Em tìm thấy 6 ticket") for p in progress))
        self.assertIn("Em đang dựng dashboard, khoảng nửa phút là có ạ.", progress)
        self.assertLess(types.index("ai_insights"), types.index("ai_response"))
        resp = next(e for e in ev if e["type"] == "ai_response")["response"]
        self.assertEqual(resp["chat_response"], "Sprint có 6 ticket, URBOX-102 đang quá hạn.")
        self.assertEqual(resp["artifact"]["kind"], "dashboard")
        self.assertEqual(self.s.stage["artifact_id"], resp["artifact"]["id"])       # đưa lên màn hình trình bày
        self.assertEqual(len(prompts), 3)                                           # tra cứu, trả lời, dựng dashboard
        self.assertIn("TOOL_CALL", prompts[0][0])

    async def test_generic_answer_with_long_report_becomes_report_artifact(self):
        report = "# Tổng hợp sprint\n- Hương: xong bản mobile trước thứ sáu\n- Tuấn: chạy staging cuối tuần\n" + "- thêm ý\n" * 30

        async def fake(system, prompt, max_tokens=4000):
            return json.dumps({"artifact_needed": None, "report_markdown": report,
                               "chat_response": "Em đã tiếp nhận yêu cầu và xử lý xong ạ!"}, ensure_ascii=False)

        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            res = await llm.think_and_act(self.mid, "tổng hợp lại cuộc họp giúp anh", SEGMENTS)
        self.assertEqual(res["artifact"]["kind"], "report")
        self.assertEqual(res["artifact"]["content"], report.strip())
        self.assertNotIn("xử lý xong", res["chat_response"])
        self.assertTrue(res["chat_response"])

    async def test_plain_text_answer_is_kept(self):
        async def fake(system, prompt, max_tokens=4000):
            return "Dạ, ticket URBOX-102 do anh Tuấn phụ trách, hạn 30/09/2026."

        with mock.patch.object(artifacts, "_call_llm", fake):
            res = await llm.think_and_act(self.mid, "ai phụ trách ticket 102", SEGMENTS)
        self.assertEqual(res["chat_response"], "Dạ, ticket URBOX-102 do anh Tuấn phụ trách, hạn 30/09/2026.")
        self.assertIsNone(res["artifact"])

    async def test_model_keeps_calling_tools_still_produces_result(self):
        async def fake(system, prompt, max_tokens=4000):
            if system.startswith(artifacts.DASHBOARD_SYSTEM):
                return json.dumps(DASH, ensure_ascii=False)
            return 'TOOL_CALL: {"tool": "query_jira_issues", "arguments": {}}'

        with mock.patch.object(artifacts, "_call_llm", fake):
            res = await llm.think_and_act(self.mid, "vẽ chart số liệu ticket", SEGMENTS)
        self.assertEqual(res["artifact"]["kind"], "dashboard")
        self.assertIn("dashboard", res["chat_response"])
        self.assertLessEqual(len(res["tool_calls"]), llm.MAX_TOOL_ROUNDS * 3)

    async def test_early_build_is_cancelled_when_plan_picks_another_kind(self):
        import asyncio
        report = "# Số liệu sprint\n- 6 ticket, 1 quá hạn\n- 3 đang làm\n" + "- chi tiết\n" * 30

        async def fake(system, prompt, max_tokens=4000):
            if system.startswith(artifacts.DASHBOARD_SYSTEM):
                await asyncio.sleep(0.3)          # dựng sớm còn đang chạy thì kế hoạch đã chọn báo cáo
                return json.dumps(DASH, ensure_ascii=False)
            if "Dữ liệu đã tra cứu:\n(chưa tra cứu)" in prompt:
                return 'TOOL_CALL: {"tool": "query_jira_issues", "arguments": {}}'
            return json.dumps({"artifact_needed": "report", "report_markdown": report,
                               "chat_response": "Sprint có 6 ticket, 1 ticket quá hạn."}, ensure_ascii=False)

        with mock.patch.object(artifacts, "_call_llm", fake):
            res = await llm.think_and_act(self.mid, "báo cáo số liệu sprint", SEGMENTS)
        await asyncio.sleep(0.4)
        self.assertEqual(res["artifact"]["kind"], "report")
        self.assertEqual([a["kind"] for a in db.get_artifacts(self.mid)], ["report"])   # không có dashboard thừa

    async def test_artifact_failure_is_reported_not_hidden(self):
        async def fake(system, prompt, max_tokens=4000):
            if system.startswith(artifacts.DASHBOARD_SYSTEM):
                return "không phải json"
            return json.dumps(FINAL_DASHBOARD, ensure_ascii=False)

        with mock.patch.object(artifacts, "_call_llm", fake):
            res = await llm.think_and_act(self.mid, "vẽ dashboard", SEGMENTS)
        self.assertIsNone(res["artifact"])
        self.assertIn("Em chưa tạo được dashboard", res["chat_response"])

    async def test_refine_dashboard_on_stage(self):
        aid = db.save_artifact(self.mid, "dashboard", "Dashboard: Tiến độ", json.dumps(DASH, ensure_ascii=False))
        await self.s.stage_action("show", artifact_id=aid)
        new = json.loads(json.dumps(DASH))
        new["charts"][0]["type"] = "line"

        async def fake(system, prompt, max_tokens=4000):
            self.assertIs(system, artifacts.DASHBOARD_REFINE_SYSTEM)
            return json.dumps({"chat_message": "Em đã đổi sang biểu đồ đường.", "dashboard": new}, ensure_ascii=False)

        with mock.patch.object(artifacts, "_call_llm", fake):
            await self.s._handle_ai_activation("đổi biểu đồ cột thành đường", "Tùng ơi, đổi biểu đồ cột thành đường", "Tùng")
        art = db.get_artifact(self.s.stage["artifact_id"])
        self.assertEqual((art["parent_id"], art["kind"]), (aid, "dashboard"))
        self.assertEqual(json.loads(art["content"])["charts"][0]["type"], "line")
        self.assertIn("Em đã đổi sang biểu đồ đường.", [e["text"] for e in self.events("ai_say")])


if __name__ == "__main__":
    unittest.main()
