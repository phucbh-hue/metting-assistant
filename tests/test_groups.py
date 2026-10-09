"""Nhóm cuộc họp: chủ nhóm, thành viên, quyền xem cuộc họp trong nhóm, API quản lý nhóm."""
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import auth, db, groups

WEB = dict(ENABLED=True, SECRET=b"k" * 32, DOMAINS=["urbox.vn"], ADMINS={"admin@urbox.vn"},
           CLIENT_ID="123-test.apps.googleusercontent.com", TTL_S=3600)


def hdr(email):
    return {"Authorization": f"Bearer {auth.issue({'email': email, 'name': email})['token']}"}


class GroupDataTests(unittest.TestCase):
    """Task 1: dữ liệu nhóm và vai trò."""

    def setUp(self):
        reset_db()
        self.p = mock.patch.multiple(auth, **WEB)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_owner_and_members_see_the_group_outsiders_do_not(self):
        g = groups.create_group("  Sprint   Payment ", "An@urbox.vn")
        self.assertEqual((g["name"], g["owner"], g["members"]), ("Sprint Payment", "an@urbox.vn", []))
        groups.update_group(g["id"], members=["Binh@urbox.vn", "an@urbox.vn", "binh@urbox.vn"])
        g = groups.get_group(g["id"])
        self.assertEqual(g["members"], ["binh@urbox.vn"])          # chữ thường, bỏ trùng, bỏ chủ nhóm
        self.assertEqual(groups.role_of(g, "an@urbox.vn"), "owner")
        self.assertEqual(groups.role_of(g, "BINH@urbox.vn"), "member")
        self.assertIsNone(groups.role_of(g, "chi@urbox.vn"))
        names = lambda e: [x["name"] for x in groups.list_groups_for(e)]
        self.assertEqual(names("an@urbox.vn"), ["Sprint Payment"])
        self.assertEqual(names("binh@urbox.vn"), ["Sprint Payment"])
        self.assertEqual(names("chi@urbox.vn"), [])
        self.assertEqual(groups.list_groups_for("binh@urbox.vn")[0]["role"], "member")

    def test_members_must_be_company_emails(self):
        g = groups.create_group("BD", "an@urbox.vn")
        with self.assertRaises(ValueError) as cm:
            groups.update_group(g["id"], members=["binh@urbox.vn", "khach@gmail.com", "khong-phai-email"])
        self.assertIn("khach@gmail.com", str(cm.exception))
        self.assertEqual(groups.get_group(g["id"])["members"], [])
        with self.assertRaises(ValueError):
            groups.create_group("   ", "an@urbox.vn")

    def test_deleting_a_group_keeps_its_meetings(self):
        g = groups.create_group("BD", "an@urbox.vn")
        mid = db.create_meeting("Họp khách A", owner="an@urbox.vn")
        self.assertTrue(groups.set_meeting_group(mid, g["id"]))
        self.assertEqual(db.get_meeting(mid)["group_id"], g["id"])
        self.assertEqual(groups.list_groups_for("an@urbox.vn")[0]["meeting_count"], 1)
        self.assertTrue(groups.delete_group(g["id"]))
        self.assertIsNone(groups.get_group(g["id"]))
        m = db.get_meeting(mid)
        self.assertIsNotNone(m)
        self.assertNotIn("group_id", m)

    def test_local_mode_has_no_sharing(self):
        with mock.patch.object(auth, "ENABLED", False):
            g = groups.create_group("Nội bộ", None)
            self.assertEqual(groups.role_of(g, None), "owner")
            self.assertEqual([x["name"] for x in groups.list_groups_for(None)], ["Nội bộ"])


class GroupAccessTests(unittest.TestCase):
    """Task 2 + 3: quyền xem cuộc họp theo nhóm và API nhóm."""

    def setUp(self):
        reset_db()
        self.p = mock.patch.multiple(auth, **WEB)
        self.p.start()
        self.client = TestClient(appmod.app)
        self.client.__enter__()
        self.an, self.binh, self.chi = hdr("an@urbox.vn"), hdr("binh@urbox.vn"), hdr("chi@urbox.vn")
        r = self.client.post("/api/groups", json={"name": "Sprint Payment"}, headers=self.an)
        self.assertEqual(r.status_code, 200, r.text)
        self.gid = r.json()["id"]
        r = self.client.patch(f"/api/groups/{self.gid}", json={"members": ["binh@urbox.vn"]}, headers=self.an)
        self.assertEqual(r.status_code, 200, r.text)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.p.stop()

    def create(self, h, title, gid=None):
        body = {"title": title} if gid is None else {"title": title, "group_id": gid}
        r = self.client.post("/api/meetings", json=body, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["meeting_id"]

    def titles(self, h, **params):
        return sorted(m["title"] for m in self.client.get("/api/meetings", headers=h, params=params).json()["meetings"])

    def test_members_see_group_meetings_outsiders_get_404(self):
        mid = self.create(self.an, "Họp nhóm", self.gid)
        private = self.create(self.an, "Họp riêng của An")
        self.assertEqual(self.titles(self.binh), ["Họp nhóm"])
        self.assertEqual(self.titles(self.an), ["Họp nhóm", "Họp riêng của An"])
        self.assertEqual(self.titles(self.an, group_id=self.gid), ["Họp nhóm"])
        self.assertEqual(self.client.get(f"/api/meetings/{mid}", headers=self.binh).status_code, 200)
        self.assertEqual(self.client.get(f"/api/meetings/{private}", headers=self.binh).status_code, 404)
        self.assertEqual(self.client.get(f"/api/meetings/{mid}", headers=self.chi).status_code, 404)
        tok = self.binh["Authorization"][7:]
        with self.client.websocket_connect(f"/ws/meeting/{mid}/events?token={tok}") as ws:
            self.assertIn('"meeting"', ws.receive_text())
        self.assertEqual(self.client.get("/api/stats", headers=self.binh).json()["meetings_total"], 1)

    def test_member_creates_meeting_in_group_outsider_cannot(self):
        mid = self.create(self.binh, "Binh họp trong nhóm", self.gid)
        self.assertIn("Binh họp trong nhóm", self.titles(self.an))       # chủ nhóm thấy cuộc họp thành viên tạo
        r = self.client.post("/api/meetings", json={"title": "x", "group_id": self.gid}, headers=self.chi)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(db.get_meeting(mid)["owner"], "binh@urbox.vn")

    def test_only_owner_manages_group(self):
        self.assertEqual(self.client.patch(f"/api/groups/{self.gid}", json={"name": "Đổi tên"}, headers=self.binh).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/groups/{self.gid}", headers=self.binh).status_code, 403)
        self.assertEqual(self.client.get(f"/api/groups/{self.gid}", headers=self.chi).status_code, 404)
        g = self.client.get(f"/api/groups/{self.gid}", headers=self.binh).json()
        self.assertEqual((g["role"], g["members"]), ("member", ["binh@urbox.vn"]))
        self.assertEqual(self.client.patch(f"/api/groups/{self.gid}", json={"members": ["x@gmail.com"]}, headers=self.an).status_code, 400)

    def test_moving_meetings_in_and_out_of_groups(self):
        mid = self.create(self.an, "Họp chuyển nhóm")
        self.assertEqual(self.client.get(f"/api/meetings/{mid}", headers=self.binh).status_code, 404)
        r = self.client.put(f"/api/meetings/{mid}/group", json={"group_id": self.gid}, headers=self.an)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get(f"/api/meetings/{mid}", headers=self.binh).status_code, 200)
        # thành viên không gỡ được cuộc họp của chủ nhóm khỏi nhóm
        self.assertEqual(self.client.put(f"/api/meetings/{mid}/group", json={"group_id": None}, headers=self.binh).status_code, 403)
        # người ngoài không chuyển cuộc họp của mình vào nhóm không thuộc về họ
        own = self.create(self.chi, "Họp của Chi")
        self.assertEqual(self.client.put(f"/api/meetings/{own}/group", json={"group_id": self.gid}, headers=self.chi).status_code, 403)
        # gỡ khỏi nhóm: thành viên mất quyền ngay
        self.assertEqual(self.client.put(f"/api/meetings/{mid}/group", json={"group_id": None}, headers=self.an).status_code, 200)
        self.assertEqual(self.client.get(f"/api/meetings/{mid}", headers=self.binh).status_code, 404)

    def test_member_cannot_delete_other_peoples_group_meetings(self):
        mid = self.create(self.an, "Họp của chủ nhóm", self.gid)
        own = self.create(self.binh, "Họp của Bình", self.gid)
        self.assertEqual(self.client.delete(f"/api/meetings/{mid}", headers=self.binh).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/meetings/{own}", headers=self.binh).status_code, 200)   # của mình
        other = self.create(self.binh, "Họp khác của Bình", self.gid)
        self.assertEqual(self.client.delete(f"/api/meetings/{other}", headers=self.an).status_code, 200)   # chủ nhóm
        self.assertIsNotNone(db.get_meeting(mid))

    def test_removed_member_loses_access_and_deleted_group_keeps_meetings(self):
        mid = self.create(self.an, "Họp nhóm", self.gid)
        self.client.patch(f"/api/groups/{self.gid}", json={"members": []}, headers=self.an)
        self.assertEqual(self.client.get(f"/api/meetings/{mid}", headers=self.binh).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/groups/{self.gid}", headers=self.an).status_code, 200)
        self.assertEqual(self.client.get(f"/api/meetings/{mid}", headers=self.an).status_code, 200)
        self.assertEqual(self.client.get("/api/groups", headers=self.an).json()["groups"], [])


if __name__ == "__main__":
    unittest.main()
