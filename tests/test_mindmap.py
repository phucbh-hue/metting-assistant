"""Test sơ đồ tư duy kiểu NotebookLM: chuẩn hóa, sinh sơ đồ, bấm ý để giải thích, thuyết trình sơ đồ và dashboard."""
import asyncio
import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import artifacts, db, live, llm

MM = {"type": "mindmap", "title": "Kế hoạch Mega Sale 10.10",
      "root": {"label": "Mega Sale 10.10", "detail": "Mục tiêu 2.500.000.000đ, ra mắt 10/10/2026.",
               "children": [
                   {"label": "1. Chốt đối tác", "tone": "xong", "detail": "Lan đã chốt 120 thương hiệu.",
                    "children": ["120 thương hiệu", {"label": "Mục tiêu 2.500.000.000đ"}]},
                   {"label": "2. Nạp 5.000 voucher", "tone": "in_progress", "detail": "Minh làm với vận hành, hạn 05/10.",
                    "children": [{"label": "Phụ trách: Minh"}, {"label": "Hạn 05/10/2026"}]},
                   {"label": "3. Banner mobile", "tone": "rủi ro", "detail": "Đang trễ 3 ngày.",
                    "children": [{"label": "Trễ 3 ngày", "tone": "risk"}, {"label": "Chưa rõ người duyệt", "tone": "todo"}]}]}}

DASH = {"title": "Doanh số", "kpis": [{"label": "Doanh số tháng 9", "value": "1.800.000.000đ"}],
        "charts": [{"type": "bar", "title": "Theo tháng", "labels": ["T8", "T9"], "series": [{"name": "Doanh số", "data": [1525, 1800]}]}],
        "highlights": ["Tháng 9 tăng 18% so với tháng 8."]}

SEGMENTS_TEXT = "Trần Văn Minh: Anh làm việc với bên vận hành để nạp voucher trước ngày 5/10."


class NormalizeTests(unittest.TestCase):
    def test_mindmap_ids_tones_and_string_children(self):
        mm = artifacts.normalize_mindmap(MM)
        self.assertEqual(mm["root"]["id"], "n0")
        idx = artifacts.mindmap_index(mm)
        self.assertEqual(list(idx), [f"n{i}" for i in range(len(idx))])          # mã nút theo thứ tự duyệt
        b1, b2, b3 = mm["root"]["children"]
        self.assertEqual((b1["tone"], b2["tone"], b3["tone"]), ("done", "doing", "risk"))
        self.assertEqual(b1["children"][0], {"id": "n2", "label": "120 thương hiệu"})
        self.assertEqual(idx["n5"]["path"], ["Mega Sale 10.10", "2. Nạp 5.000 voucher", "Phụ trách: Minh"])
        self.assertIn("[n4] 2. Nạp 5.000 voucher (doing): Minh làm", artifacts.mindmap_outline(mm))

    def test_mindmap_limits_and_rejects(self):
        self.assertIsNone(artifacts.normalize_mindmap({"root": {"label": "Chỉ có gốc"}}))
        self.assertIsNone(artifacts.normalize_mindmap({"root": {"label": "", "children": ["a"]}}))
        self.assertIsNone(artifacts.normalize_mindmap("không phải JSON"))
        deep = {"label": "L5"}
        for i in range(4, -1, -1):
            deep = {"label": f"L{i}", "children": [deep]}
        mm = artifacts.normalize_mindmap({"root": deep})
        self.assertEqual(max(e["depth"] for e in artifacts.mindmap_index(mm).values()), artifacts.MINDMAP_MAX_DEPTH)
        many = {"root": {"label": "Gốc", "children": [{"label": f"Ý {i}", "children": [f"Con {i}.{j}" for j in range(12)]}
                                                       for i in range(12)]}}
        self.assertLessEqual(len(artifacts.mindmap_index(artifacts.normalize_mindmap(many))), artifacts.MINDMAP_MAX_NODES)
        long = artifacts.normalize_mindmap({"root": {"label": "x" * 300 + " \u2014 y", "children": ["a"]}})
        self.assertLessEqual(len(long["root"]["label"]), 90)
        self.assertEqual(long["title"], long["root"]["label"])

    def test_load_diagram_formats(self):
        self.assertEqual(artifacts.load_diagram("flowchart LR\n A-->B"), {"type": "mermaid", "code": "flowchart LR\n A-->B"})
        wrapped = json.dumps({"type": "mermaid", "code": 'flowchart LR\n A["Nạp voucher"] --> B["Ra mắt"]',
                              "walkthrough": [{"target": "nap voucher", "text": "Bước nạp voucher."}, {"target": "x", "text": "Hết."}]})
        dg = artifacts.load_diagram(wrapped)
        self.assertEqual(dg["walkthrough"], [{"target": "Nạp voucher", "text": "Bước nạp voucher."}, {"target": "", "text": "Hết."}])
        self.assertEqual(artifacts.load_diagram(json.dumps(MM, ensure_ascii=False))["type"], "mindmap")

    def test_mermaid_labels(self):
        code = ('flowchart LR\n  A["Khách đổi voucher"] --> B{"Còn hạn?"}\n  B -->|"Còn"| C(Phát mã)\n'
                '  classDef risk fill:#fde2e2;\n  class C risk;')
        self.assertEqual(artifacts.mermaid_labels(code), ["Khách đổi voucher", "Còn hạn?", "Phát mã"])
        seq = 'sequenceDiagram\n  participant A as "App UrBox"\n  participant P\n  A->>P: gọi API'
        self.assertEqual(artifacts.mermaid_labels(seq), ["App UrBox", "P"])

    def test_walkthrough_targets_are_checked(self):
        mm = artifacts.normalize_mindmap(MM)
        steps = artifacts.normalize_walkthrough(mm, [{"target": "n0", "text": "Mở đầu."}, {"node": "n9", "text": "Banner."},
                                                     {"target": "n99", "text": "Không có nút này."}, {"text": ""}, "x"])
        self.assertEqual(steps, [{"target": "n0", "text": "Mở đầu."}, {"target": "n9", "text": "Banner."},
                                 {"target": "", "text": "Không có nút này."}])
        d = artifacts.normalize_dashboard({**DASH, "walkthrough": [{"target": "kpis", "text": "Chỉ số."},
                                                                    {"target": "Chart: 0", "text": "Biểu đồ."},
                                                                    {"target": "chart:5", "text": "Không có."},
                                                                    {"target": "highlights", "text": "Kết luận."}]})
        self.assertEqual([s["target"] for s in d["walkthrough"]], ["kpis", "chart:0", "", "highlights"])
        self.assertTrue(artifacts.has_walkthrough({"kind": "dashboard", "content": json.dumps(d)}))
        self.assertFalse(artifacts.has_walkthrough({"kind": "dashboard", "content": json.dumps(DASH)}))

    def test_find_node_by_spoken_words(self):
        mm = artifacts.normalize_mindmap(MM)
        picked, score = live.MeetingSession._find_node(mm, "nạp voucher")
        self.assertEqual((picked["key"], picked["label"]), ("n4", "2. Nạp 5.000 voucher"))
        self.assertGreaterEqual(score, 0.99)
        self.assertIsNone(live.MeetingSession._find_node(mm, "giá vàng hôm nay"))


class GenerateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        self.mid = db.create_meeting("Họp")

    async def test_mindmap_is_default_and_bad_reply_is_retried(self):
        calls = []

        async def fake(system, prompt, max_tokens=4000):
            calls.append((system, prompt))
            if len(calls) == 1:
                return '{"type": "mindmap", "root": {"label": "Chỉ có gốc"}}'
            return "Dạ đây:\n```json\n" + json.dumps(MM, ensure_ascii=False) + "\n```"
        with mock.patch.object(artifacts, "_call_llm", fake):
            art = await artifacts.generate_diagram(self.mid, "vẽ sơ đồ kế hoạch", "A: nói gì đó")
        self.assertIn("NotebookLM", calls[0][0])
        self.assertIn("Skill: Vẽ sơ đồ", calls[0][0])
        self.assertIn("bị lỗi", calls[1][1])
        content = json.loads(art["content"])
        self.assertEqual((content["type"], art["title"]), ("mindmap", "Sơ đồ: Kế hoạch Mega Sale 10.10"))
        self.assertEqual(db.get_artifact(art["id"])["kind"], "diagram")

    async def test_mermaid_reply_is_kept_as_code(self):
        async def fake(system, prompt, max_tokens=4000):
            return '```mermaid\nsequenceDiagram\n  participant A as "App"\n  A->>A: gọi\n```'
        with mock.patch.object(artifacts, "_call_llm", fake):
            art = await artifacts.generate_diagram(self.mid, "vẽ sequence thanh toán", "")
        self.assertTrue(art["content"].startswith("sequenceDiagram"))

    async def test_walkthrough_for_mindmap_and_dashboard(self):
        async def fake(system, prompt, max_tokens=4000):
            if "sơ đồ tư duy" in system:
                self.assertIn("[n4] 2. Nạp 5.000 voucher", prompt)
                return json.dumps({"steps": [{"target": "n0", "text": "Sơ đồ có ba nhánh."},
                                             {"target": "n4", "text": "Minh nạp voucher trước 05/10."}]}, ensure_ascii=False)
            self.assertIn('"target": "chart:0"', prompt)
            return json.dumps({"steps": [{"target": "kpis", "text": "Doanh số 1.800.000.000đ."},
                                         {"target": "chart:0", "text": "Tháng 9 cao nhất."}]}, ensure_ascii=False)
        mm_art = {"kind": "diagram", "title": "Sơ đồ", "content": json.dumps(MM, ensure_ascii=False)}
        dash_art = {"kind": "dashboard", "title": "Dashboard", "content": json.dumps(DASH, ensure_ascii=False)}
        with mock.patch.object(artifacts, "_call_llm", fake):
            mm = await artifacts.generate_walkthrough(mm_art, SEGMENTS_TEXT)
            d = await artifacts.generate_walkthrough(dash_art, SEGMENTS_TEXT)
        self.assertEqual([s["target"] for s in mm["walkthrough"]], ["n0", "n4"])
        self.assertEqual([s["target"] for s in d["walkthrough"]], ["kpis", "chart:0"])
        self.assertTrue(artifacts.has_walkthrough({"kind": "diagram", "content": artifacts.dump_content("diagram", mm)}))

        async def too_short(system, prompt, max_tokens=4000):
            return '{"steps": [{"target": "n0", "text": "Một câu."}]}'
        with mock.patch.object(artifacts, "_call_llm", too_short):
            with self.assertRaises(RuntimeError):
                await artifacts.generate_walkthrough(mm_art, "")

    async def test_mermaid_walkthrough_is_saved_as_json_wrapper(self):
        code = 'flowchart LR\n  A["Nạp voucher"] --> B["Ra mắt"]'

        async def fake(system, prompt, max_tokens=4000):
            return json.dumps({"steps": [{"target": "Nạp voucher", "text": "Đầu tiên nạp voucher."},
                                         {"target": "Ra mắt", "text": "Sau đó ra mắt."}]}, ensure_ascii=False)
        with mock.patch.object(artifacts, "_call_llm", fake):
            out = await artifacts.generate_walkthrough({"kind": "diagram", "title": "S", "content": code})
        saved = artifacts.dump_content("diagram", out)
        self.assertEqual(json.loads(saved)["code"], code)
        self.assertEqual(artifacts.load_diagram(saved)["walkthrough"][1]["target"], "Ra mắt")
        self.assertEqual(artifacts.dump_content("diagram", {"type": "mermaid", "code": code}), code)

    async def test_refine_mindmap_keeps_format(self):
        aid = db.save_artifact(self.mid, "diagram", "Sơ đồ: Mega Sale", json.dumps(MM, ensure_ascii=False))

        async def fake(system, prompt, max_tokens=4000):
            self.assertIn("sơ đồ tư duy", system)
            data = json.loads(json.dumps(MM))
            data["root"]["children"].append({"label": "4. Ra mắt 10/10"})
            return json.dumps({"chat_message": "Em đã thêm nhánh ra mắt.", "mindmap": data}, ensure_ascii=False)
        with mock.patch.object(artifacts, "_call_llm", fake):
            res = await artifacts.co_design_refine(self.mid, aid, "thêm nhánh ra mắt")
        mm = json.loads(res["content"])
        self.assertEqual((res["parent_id"], res["chat_message"]), (aid, "Em đã thêm nhánh ra mắt."))
        self.assertEqual(mm["root"]["children"][-1]["label"], "4. Ra mắt 10/10")
        self.assertNotIn("walkthrough", mm)


class StageIntentTests(unittest.TestCase):
    def test_present_and_explain_commands(self):
        cases = {
            "thuyết trình sơ đồ này": {"action": "present", "kind": "diagram"},
            "giải thích giúp anh cái dashboard": {"action": "present", "kind": "dashboard"},
            "em trình bày sơ đồ tư duy đi": {"action": "present", "kind": "diagram"},
            "thuyết trình giúp anh": {"action": "present"},
            "giải thích nhánh nạp voucher": {"action": "explain", "query": "nạp voucher", "loose": False},
            "nói rõ hơn ý banner mobile nhé": {"action": "explain", "query": "banner mobile", "loose": False},
            "giải thích giúp anh vì sao doanh số giảm": {"action": "explain", "query": "vì sao doanh số giảm", "loose": True},
            "phân tích doanh số tháng 9": None,
            "vẽ sơ đồ quy trình rồi thuyết trình luôn": None,
            "làm dashboard doanh số rồi trình bày luôn": None,
            "mở lại mind map lúc nãy": {"action": "back", "kind": "diagram", "past": True},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(llm.stage_intent(text), expected)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_db()
        llm.set_assistant_config("Bông", [])
        self.mid = db.create_meeting("Họp")
        self.s = await live.get_session(self.mid)
        self.q = await self.s.subscribe()
        self.ev = []
        self.mm_id = db.save_artifact(self.mid, "diagram", "Sơ đồ: Mega Sale", json.dumps(MM, ensure_ascii=False))
        await self.s.stage_action("show", artifact_id=self.mm_id)

    async def asyncTearDown(self):
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()

    def events(self, kind=None):
        while not self.q.empty():
            self.ev.append(self.q.get_nowait())
        return [e for e in self.ev if kind is None or e["type"] == kind]

    async def test_click_explains_with_meeting_context_then_uses_cache(self):
        calls = []

        async def fake(system, prompt, max_tokens=4000):
            calls.append(prompt)
            self.assertIn("Mega Sale 10.10 > 2. Nạp 5.000 voucher", prompt)
            self.assertIn("Ghi chú trong sơ đồ: Minh làm với vận hành", prompt)
            return "**Dạ**, anh Minh nhận nạp 5.000 voucher với bên vận hành, hạn 05/10/2026."
        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            res = await self.s.explain_node(self.mm_id, node="n4", req="r1")
            again = await self.s.explain_node(self.mm_id, node="n4", req="r2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["text"], "Dạ, anh Minh nhận nạp 5.000 voucher với bên vận hành, hạn 05/10/2026.")
        self.assertEqual((res["cached"], again["cached"], again["text"]), (False, True, res["text"]))
        self.assertEqual([e["node"] for e in self.events("node_explaining")], ["n4", "n4"])
        done = self.events("node_explained")
        self.assertEqual((done[0]["req"], done[0]["label"], done[0]["path"][-1]), ("r1", "2. Nạp 5.000 voucher", "2. Nạp 5.000 voucher"))

    async def test_without_ai_reads_the_note_in_the_diagram(self):
        with mock.patch.object(artifacts, "llm_available", lambda: False):
            res = await self.s.explain_node(self.mm_id, label="3. banner mobile")
            leaf = await self.s.explain_node(self.mm_id, node="n2")
        self.assertEqual(res["text"], "Đang trễ 3 ngày.")
        self.assertEqual(leaf["text"], "120 thương hiệu.")
        with self.assertRaises(KeyError):
            await self.s.explain_node(self.mm_id, node="n99")

    async def test_voice_explain_picks_the_branch(self):
        async def fake(system, prompt, max_tokens=4000):
            return "Banner mobile đang trễ 3 ngày."
        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            ok = await self.s._handle_stage_intent(llm.stage_intent("giải thích nhánh banner mobile"))
            loose = await self.s._handle_stage_intent(llm.stage_intent("giải thích giúp anh vì sao doanh số giảm"))
        self.assertTrue(ok)
        self.assertFalse(loose)                 # không khớp ý nào: để trợ lý trả lời như câu hỏi thường
        self.assertEqual(self.events("node_explained")[0]["node"], "n7")

    async def test_present_diagram_writes_walkthrough_once(self):
        calls = []

        async def fake(system, prompt, max_tokens=4000):
            calls.append(system)
            return json.dumps({"steps": [{"target": "n0", "text": "Sơ đồ có ba nhánh chính."},
                                         {"target": "n1", "text": "Lan đã chốt 120 thương hiệu."},
                                         {"target": "n7", "text": "Banner mobile đang trễ 3 ngày."}]}, ensure_ascii=False)
        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            await self.s._handle_stage_intent({"action": "present", "kind": "diagram", "text": "thuyết trình sơ đồ này"})
            new_id = self.s.stage["artifact_id"]
            await self.s._handle_stage_intent({"action": "present", "text": "thuyết trình"})
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(new_id, self.mm_id)
        new = db.get_artifact(new_id)
        self.assertEqual((new["parent_id"], new["kind"]), (self.mm_id, "diagram"))
        self.assertEqual([s["target"] for s in json.loads(new["content"])["walkthrough"]], ["n0", "n1", "n7"])
        starts = [e for e in self.events("stage_present") if e["action"] == "start"]
        self.assertEqual([e["artifact_id"] for e in starts], [new_id, new_id])

    async def test_present_by_kind_switches_from_slides(self):
        deck = {"title": "Bộ slide", "slides": [{"title": "Mở đầu", "layout": "title", "bullets": [], "notes": ""}]}
        deck_id = db.save_artifact(self.mid, "slides", "Slide: Bộ slide", json.dumps(deck))
        dash_id = db.save_artifact(self.mid, "dashboard", "Dashboard: Doanh số",
                                   json.dumps({**DASH, "walkthrough": [{"target": "kpis", "text": "Doanh số."},
                                                                       {"target": "chart:0", "text": "Biểu đồ."}]}))
        await self.s.stage_action("show", artifact_id=deck_id)
        with mock.patch.object(artifacts, "llm_available", lambda: True), \
                mock.patch.object(artifacts, "_call_llm", side_effect=AssertionError("đã có lời, không gọi AI")):
            await self.s._handle_stage_intent(llm.stage_intent("trình bày dashboard giúp anh"))
        self.assertEqual(self.s.stage["artifact_id"], dash_id)
        self.assertTrue([e for e in self.events("stage_present") if e["action"] == "start"])

        reset_db()
        mid = db.create_meeting("Họp khác")
        s2 = await live.get_session(mid)
        q = await s2.subscribe()
        await s2._handle_stage_intent({"action": "present", "kind": "diagram", "text": "thuyết trình sơ đồ"})
        says = []
        while not q.empty():
            e = q.get_nowait()
            if e["type"] == "ai_say":
                says.append(e["text"])
        self.assertTrue(any("Chưa có sơ đồ nào" in t for t in says), says)

    async def test_agent_present_flag_presents_new_diagram(self):
        with mock.patch.object(artifacts, "llm_available", lambda: False):
            await self.s.apply_ai_result({"artifact": {"id": self.mm_id, "kind": "diagram"}, "present": True})
        self.assertTrue([e for e in self.events("stage_present") if e["action"] == "start"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.client = TestClient(appmod.app)
        self.mid = db.create_meeting("Họp")
        self.mm_id = db.save_artifact(self.mid, "diagram", "Sơ đồ", json.dumps(MM, ensure_ascii=False))
        deck = {"title": "Bộ slide", "slides": [{"title": "Mở đầu", "layout": "title", "bullets": [], "notes": ""}]}
        self.deck_id = db.save_artifact(self.mid, "slides", "Slide", json.dumps(deck))

    def tearDown(self):
        for s in list(live.SESSIONS.values()):
            s.dispose()
        live.SESSIONS.clear()

    def test_explain_and_present_endpoints(self):
        with mock.patch.object(artifacts, "llm_available", lambda: False):
            r = self.client.post(f"/api/meetings/{self.mid}/explain", json={"artifact_id": self.mm_id, "node": "n1", "req": "a"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["text"], r.json()["req"]), ("Lan đã chốt 120 thương hiệu.", "a"))
            self.assertEqual(self.client.post(f"/api/meetings/{self.mid}/explain",
                                              json={"artifact_id": self.deck_id, "node": "n1"}).status_code, 404)
            self.assertEqual(self.client.post(f"/api/meetings/{self.mid}/present", json={"artifact_id": self.deck_id}).status_code, 400)
            r = self.client.post(f"/api/meetings/{self.mid}/present", json={"artifact_id": self.mm_id})
            self.assertEqual(r.json(), {"ok": True, "artifact_id": self.mm_id})


if __name__ == "__main__":
    unittest.main()
