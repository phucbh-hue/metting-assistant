"""Mẫu biên bản (14/10/2026): biên bản = recap; mọi người tự thiết kế cấu trúc biên bản, phân quyền công ty / nhóm / cá nhân,
thứ tự chọn mẫu cho một cuộc họp, lập lại biên bản bằng mẫu khác. AI là bản giả."""
import asyncio
import json
import os
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import artifacts, auth, db, groups, live
from meeting import minutes_templates as mt

WEB = dict(ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS={"admin@urbox.vn"},
           CLIENT_ID="123-test.apps.googleusercontent.com", TTL_S=3600)
ADMIN, OWNER, MEMBER, OTHER = ({"email": e} for e in ("admin@urbox.vn", "an@urbox.vn", "lan@urbox.vn", "binh@urbox.vn"))
SECTIONS = [{"title": "Kết quả chiến dịch", "instructions": "Số voucher đã phát, tỉ lệ dùng", "format": "paragraph"},
            {"title": "Việc tiếp theo", "format": "table", "columns": ["Việc", "Người phụ trách", "Hạn"]}]


def hdr(email):
    return {"Authorization": f"Bearer {auth.issue({'email': email, 'name': email})['token']}"}


class WebBase(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.p = mock.patch.multiple(auth, **WEB)
        self.p.start()
        self.g = groups.create_group("Marketing", "an@urbox.vn")
        groups.update_group(self.g["id"], members=["lan@urbox.vn"])

    def tearDown(self):
        self.p.stop()


class PermissionTests(WebBase):
    def test_who_can_create_edit_and_see_what(self):
        with self.assertRaises(PermissionError):
            mt.create(OWNER, {"name": "Công ty", "scope": "company", "sections": SECTIONS})
        company = mt.create(ADMIN, {"name": "Mẫu công ty", "scope": "company", "sections": SECTIONS})
        with self.assertRaises(PermissionError):
            mt.create(MEMBER, {"name": "Nhóm", "scope": "group", "group_id": self.g["id"], "sections": SECTIONS})
        grp = mt.create(OWNER, {"name": "Mẫu chiến dịch", "scope": "group", "group_id": self.g["id"], "sections": SECTIONS})
        mine = mt.create(OTHER, {"name": "Của Bình", "scope": "personal", "sections": SECTIONS})
        ids = lambda u: {t["id"] for t in mt.visible(u)}
        self.assertTrue({company["id"], grp["id"]} <= ids(MEMBER))              # thành viên thấy mẫu công ty + nhóm
        self.assertNotIn(mine["id"], ids(MEMBER))                               # mẫu cá nhân chỉ người tạo thấy
        self.assertNotIn(grp["id"], ids(OTHER))                                 # người ngoài nhóm không thấy mẫu nhóm
        self.assertIn("b-standard", ids(OTHER))
        with self.assertRaises(PermissionError):
            mt.update(MEMBER, grp["id"], {"name": "Sửa"})                       # thành viên chỉ dùng, không sửa
        with self.assertRaises(PermissionError):
            mt.update(ADMIN, "b-standard", {"name": "Sửa"})                     # dựng sẵn: không ai sửa
        with self.assertRaises(KeyError):
            mt.update(MEMBER, mine["id"], {"name": "x"})                        # không thấy thì như không có
        upd = mt.update(OWNER, grp["id"], {"name": "Mẫu chiến dịch v2"})
        self.assertEqual((upd["name"], upd["version"]), ("Mẫu chiến dịch v2", 2))
        copy = mt.duplicate(MEMBER, "b-interview")                              # nhân bản mẫu dựng sẵn để sửa
        self.assertEqual((copy["scope"], copy["owner"], copy["name"]), ("personal", "lan@urbox.vn", "Phỏng vấn tuyển dụng (bản sao)"))
        self.assertTrue(next(t for t in mt.visible(MEMBER) if t["id"] == copy["id"])["can_edit"])

    def test_defaults_and_resolution_order(self):
        grp = mt.create(OWNER, {"name": "Mẫu nhóm", "scope": "group", "group_id": self.g["id"], "sections": SECTIONS})
        with self.assertRaises(PermissionError):
            mt.set_group_default(MEMBER, self.g["id"], grp["id"])
        with self.assertRaises(PermissionError):
            mt.set_company_default(OWNER, "b-weekly")
        mt.set_company_default(ADMIN, "b-weekly")
        meeting = {"title": "Họp bất kỳ", "meeting_type": "Technical Review", "group_id": self.g["id"]}
        self.assertEqual(mt.resolve(meeting)["id"], "b-weekly")                 # chưa có mặc định nhóm: theo công ty
        mt.set_group_default(OWNER, self.g["id"], grp["id"])
        self.assertEqual(mt.resolve(meeting)["id"], grp["id"])                  # nhóm đè công ty
        self.assertEqual(mt.resolve({**meeting, "minutes_template": "b-retro"})["id"], "b-retro")   # cuộc họp đè nhóm
        mt.delete(OWNER, grp["id"])                                             # xóa mẫu đang là mặc định của nhóm
        self.assertEqual(groups.get_group(self.g["id"])["minutes_template"], "")
        self.assertEqual(mt.resolve(meeting)["id"], "b-weekly")
        mt.set_company_default(ADMIN, "")
        self.assertEqual(mt.resolve({"title": "Daily standup team Payment"})["id"], "b-daily")      # theo tên cuộc họp
        self.assertEqual(mt.resolve({"title": "Gặp khách", "meeting_type": "Phỏng vấn"})["id"], "b-interview")
        self.assertEqual(mt.resolve({"title": "BD Operation | Thảo luận workflow"})["id"], "b-standard")
        with self.assertRaises(mt.TemplateError):
            mt.set_group_default(OWNER, self.g["id"], mt.create(OWNER, {"name": "Riêng", "sections": SECTIONS})["id"])

    def test_validation(self):
        with self.assertRaises(mt.TemplateError):
            mt.validate({"name": "", "sections": SECTIONS})
        with self.assertRaises(mt.TemplateError):
            mt.validate({"name": "x", "sections": [{"title": "Bảng", "format": "table", "columns": ["một"]}]})
        v = mt.validate({"name": "  Mẫu — mới ", "sections": [{"title": "A", "format": "lạ"}] * 20})
        self.assertEqual((v["name"], len(v["sections"]), v["sections"][0]["format"]), ("Mẫu - mới", mt.MAX_SECTIONS, "bullets"))


class PromptTests(unittest.TestCase):
    def setUp(self):
        reset_db()

    def test_standard_template_keeps_the_old_structure_and_tables_have_columns(self):
        sys_ = mt.render_system(mt.get("b-standard"))
        for s in ("## 1. Tóm tắt điều hành", "## 2. Các quyết định đã thống nhất", "## 3. Bảng phân công nhiệm vụ",
                  "| STT | Nhiệm vụ | Người phụ trách | Hạn chót | Ưu tiên | Ghi chú & căn cứ |", "Không có trong cuộc họp"):
            self.assertIn(s, sys_)
        self.assertIn("Thư ký Cuộc họp Thông minh", sys_)
        self.assertNotIn(chr(0x2014), sys_)

    def test_minutes_follow_the_meeting_template_and_record_it(self):
        t = mt.create(None, {"name": "Recap chiến dịch", "scope": "personal", "sections": SECTIONS,
                             "instructions": "Ghi số liệu bằng bảng nếu có"})
        mid = db.create_meeting("Họp chiến dịch Tết")
        db.update_meeting(mid, {"minutes_template": t["id"]}, True)
        seen = []

        async def fake(system, prompt, max_tokens=4000):
            seen.append(str(system))
            return "# BIÊN BẢN CUỘC HỌP: Họp chiến dịch Tết\n## 1. Kết quả chiến dịch\nĐã phát 10.000 voucher."
        with mock.patch.object(artifacts, "_call_llm", fake):
            art = asyncio.run(artifacts.generate_meeting_minutes(mid, [{"speaker_label": "An", "text": "Đã phát 10.000 voucher"}],
                                                                 "Họp chiến dịch Tết", meeting=db.get_meeting(mid)))
        self.assertIn('MẪU "Recap chiến dịch"', seen[0])
        self.assertIn("## 2. Việc tiếp theo\n| Việc | Người phụ trách | Hạn |", seen[0])
        self.assertIn("Yêu cầu riêng của mẫu: Ghi số liệu bằng bảng nếu có", seen[0])
        self.assertEqual(db.get_artifact(art["id"])["meta"]["template_name"], "Recap chiến dịch")


class ApiTests(WebBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_manage_choose_and_regenerate(self):
        c = self.client
        r = c.get("/api/minutes-templates", headers=hdr("lan@urbox.vn")).json()
        self.assertFalse(r["can_company"])
        self.assertEqual(r["owned_groups"], [])
        self.assertEqual(c.post("/api/minutes-templates", json={"name": "N", "scope": "group", "group_id": self.g["id"],
                                                                "sections": SECTIONS}, headers=hdr("lan@urbox.vn")).status_code, 403)
        t = c.post("/api/minutes-templates", json={"name": "Mẫu nhóm", "scope": "group", "group_id": self.g["id"],
                                                   "sections": SECTIONS}, headers=hdr("an@urbox.vn")).json()
        self.assertEqual(c.put(f"/api/groups/{self.g['id']}/minutes-template", json={"template": t["id"]},
                               headers=hdr("lan@urbox.vn")).status_code, 403)
        c.put(f"/api/groups/{self.g['id']}/minutes-template", json={"template": t["id"]}, headers=hdr("an@urbox.vn"))
        mid = c.post("/api/meetings", json={"title": "Họp nhóm", "group_id": self.g["id"]}, headers=hdr("lan@urbox.vn")).json()["meeting_id"]
        self.assertEqual(c.put(f"/api/meetings/{mid}/minutes-template", json={"template": "b-retro"},
                               headers=hdr("binh@urbox.vn")).status_code, 404)  # người ngoài: không thấy cuộc họp
        r = c.put(f"/api/meetings/{mid}/minutes-template", json={"template": "b-retro"}, headers=hdr("lan@urbox.vn")).json()
        self.assertEqual(r["resolved"], "Retrospective")                        # người tạo cuộc họp chọn được
        db.add_segment(mid, 0, 3, "Lan", "Điều làm tốt là phát hành đúng hạn", seq=1)
        db.update_meeting(mid, {"status": "ended", "ended_at": time.time()}, True)

        async def fake(system, prompt, max_tokens=4000):
            return "# BIÊN BẢN CUỘC HỌP: Họp nhóm\n## 1. Kết quả chiến dịch\nOk"
        with mock.patch.object(artifacts, "_call_llm", fake), mock.patch.object(artifacts, "llm_available", lambda: True):
            self.assertEqual(c.post(f"/api/meetings/{mid}/minutes", json={"template": t["id"]},
                                    headers=hdr("lan@urbox.vn")).json()["template"], "Mẫu nhóm")
            for _ in range(100):
                if db.get_meeting(mid).get("minutes_status") == "done":
                    break
                time.sleep(0.03)
        arts = [a for a in db.get_artifacts(mid) if a["kind"] == "minutes"]
        self.assertEqual(arts[0]["meta"]["template_name"], "Mẫu nhóm")
        for s in list(live.SESSIONS.values()):
            s.dispose()

    def test_ai_drafts_a_template_for_review(self):
        reply = json.dumps({"name": "Họp đối tác merchant", "description": "Làm việc với merchant",
                            "sections": [{"title": "Đề xuất của merchant", "instructions": "", "format": "bullets"},
                                         {"title": "Việc cần làm", "format": "table", "columns": ["Việc", "Người phụ trách", "Hạn"]}]},
                           ensure_ascii=False)

        async def fake(system, prompt, max_tokens=4000):
            self.assertIn("MẪU BIÊN BẢN CUỘC HỌP", str(system))
            return reply
        with mock.patch.object(artifacts, "_call_llm", fake):
            r = self.client.post("/api/minutes-templates-draft", json={"description": "Biên bản họp với merchant: đề xuất, việc cần làm"},
                                 headers=hdr("lan@urbox.vn")).json()
        self.assertEqual([s["title"] for s in r["sections"]], ["Đề xuất của merchant", "Việc cần làm"])
        self.assertEqual(db._get_db()["minutes_templates"].count_documents({}), 0)   # chưa lưu


if __name__ == "__main__":
    unittest.main()
