"""Test kho trên máy (lưu khi mất kết nối Atlas) và đồng bộ lên Atlas.

Chỉ dùng thư mục tạm + mongomock: không chạm vào data/local_db thật và không kết nối Atlas.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import mongomock
import numpy as np
import pymongo
from fastapi.testclient import TestClient

from tests.helpers import reset_db
from meeting import app as appmod
from meeting import db, live, localstore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sdb, _ = localstore.open_store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def reopen(self):
        self.sdb.store.flush()
        return localstore.open_store(self.root)

    def test_round_trip_by_meeting_files(self):
        d = self.sdb
        d["meetings"].insert_many([{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])
        d["meeting_segments"].insert_many([{"meeting_id": 1, "seq": i, "text": f"câu {i}", "raw_embedding": [0.5] * 4}
                                           for i in range(3)])
        d["meeting_segments"].insert_one({"meeting_id": 2, "seq": 1, "text": "x"})
        d["meeting_segments"].update_many({"meeting_id": 1}, {"$set": {"speaker_label": "Hương"}})
        d["meeting_speakers"].update_one({"meeting_id": 2, "sid": 1}, {"$set": {"label": "Tuấn"}}, upsert=True)
        d["counters"].find_one_and_update({"_id": "meetings"}, {"$inc": {"seq": 1}}, upsert=True,
                                          return_document=pymongo.ReturnDocument.AFTER)
        d["llm_usage"].insert_one({"meeting_id": None, "purpose": "khác"})
        d.store.flush()
        for rel in ("meetings.json", "counters.json", "meeting_segments/1.json", "meeting_segments/2.json",
                    "meeting_speakers/2.json", "llm_usage/_none.json"):
            self.assertTrue((self.root / rel).exists(), rel)
        d2, n = self.reopen()
        self.assertEqual(n, 2 + 4 + 1 + 1 + 1)
        self.assertEqual({s["speaker_label"] for s in d2["meeting_segments"].find({"meeting_id": 1})}, {"Hương"})
        self.assertEqual(d2["meeting_speakers"].find_one({"meeting_id": 2})["label"], "Tuấn")
        self.assertEqual(d2["counters"].find_one({"_id": "meetings"})["seq"], 1)        # _id kiểu chuỗi giữ nguyên
        self.assertEqual(d2["meeting_segments"].find_one({"meeting_id": 1, "seq": 0})["raw_embedding"], [0.5] * 4)

    def test_only_touched_meeting_is_rewritten_and_deleted_meeting_removes_file(self):
        d = self.sdb
        d["meeting_segments"].insert_many([{"meeting_id": 1, "id": 10}, {"meeting_id": 2, "id": 11}])
        d.store.flush()
        d["meeting_segments"].insert_one({"meeting_id": 2, "id": 12})
        self.assertEqual(d.store.dirty, {"meeting_segments": {2}})                      # chỉ cuộc họp 2 phải ghi lại
        d["meeting_segments"].delete_many({"meeting_id": 1})
        d.store.flush()
        self.assertFalse((self.root / "meeting_segments" / "1.json").exists())
        doc = d["meeting_segments"].find_one({"id": 11})
        d["meeting_segments"].update_one({"_id": doc["_id"]}, {"$set": {"seq": 11}})  # bộ lọc không có meeting_id
        self.assertEqual(d.store.dirty["meeting_segments"], {localstore.ALL})
        d2, _ = self.reopen()
        self.assertEqual(d2["meeting_segments"].find_one({"id": 11})["seq"], 11)
        self.assertEqual(d2["meeting_segments"].count_documents({}), 2)

    def test_corrupt_file_does_not_break_loading(self):
        self.sdb["voices"].insert_one({"id": 1, "embedding": [1.0, 1.0, 1.0]})
        self.sdb["settings"].insert_one({"key": "a", "value": "b"})
        self.sdb.store.flush()
        (self.root / "settings.json").write_text("{hỏng", encoding="utf-8")       # tắt máy giữa lúc ghi...
        d2, _ = localstore.open_store(self.root)
        self.assertEqual(d2["voices"].find_one({"id": 1})["embedding"], [1.0, 1.0, 1.0])
        self.assertEqual(d2["settings"].count_documents({}), 0)

    def test_json_default_and_bulk_write_shards(self):
        self.assertEqual(localstore._json_default(np.float32(0.5)), 0.5)
        self.assertEqual(localstore._json_default(np.ones(2, dtype=np.float32)), [1.0, 1.0])
        ops = [pymongo.UpdateOne({"meeting_id": 4, "sid": 1}, {"$set": {"a": 1}}),
               pymongo.UpdateOne({"meeting_id": 7, "sid": 2}, {"$set": {"a": 1}})]
        self.assertEqual(localstore.shards_of("meeting_speakers", "bulk_write", (ops,), {}), {4, 7})
        self.assertEqual(localstore.shards_of("meeting_speakers", "bulk_write",
                                              ([pymongo.UpdateOne({"sid": 1}, {"$set": {"a": 1}})],), {}), {localstore.ALL})
        self.assertEqual(localstore.shards_of("meetings", "update_one", ({"id": 1},), {}), {localstore.ALL})

    def test_concurrent_writes_while_flushing(self):
        d, errors = self.sdb, []

        def writer():
            try:
                for i in range(300):
                    d["meeting_segments"].insert_one({"meeting_id": 5, "seq": i})
            except Exception as e:      # pragma: no cover - chỉ để báo lỗi
                errors.append(e)
        t = threading.Thread(target=writer)
        t.start()
        while t.is_alive():
            d.store.flush()
        t.join()
        d2, _ = self.reopen()
        self.assertEqual(errors, [])
        self.assertEqual(d2["meeting_segments"].count_documents({"meeting_id": 5}), 300)


class DbLocalModeTests(unittest.TestCase):
    """meeting.db khi không có Atlas: lưu trên máy, mã cuộc họp từ 9001, dữ liệu còn sau khi khởi động lại."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = (db._db, db._client, db._is_mock, db._mode, db._atlas_error)
        self.patches = [mock.patch.object(db, "FORCE_MOCK", False), mock.patch.object(db, "FORCE_LOCAL", True),
                        mock.patch.object(db, "LOCAL_DB_DIR", self.root)]
        for p in self.patches:
            p.start()
        db._db = None

    def tearDown(self):
        if db._mode == "local_file" and db._db is not None:
            db._db.store._stop.set()
        for p in self.patches:
            p.stop()
        db._db, db._client, db._is_mock, db._mode, db._atlas_error = self.saved
        self.tmp.cleanup()

    def restart(self):
        db.flush()
        db._db.store._stop.set()
        db._db = None

    def test_meetings_survive_restart_with_offset_ids(self):
        db.init()
        mid = db.create_meeting("Họp khi mất mạng")
        self.assertEqual(mid, 9001)
        db.add_segment(mid, 0, 3, "Người nói 1", "Xin chào")
        db.save_artifact(mid, "report", "Báo cáo: A", "# A")
        self.assertEqual(db.get_status()["mode"], "local_file")
        self.restart()
        self.assertEqual([m["id"] for m in db.list_meetings()], [9001])
        self.assertEqual([s["text"] for s in db.get_segments(9001)], ["Xin chào"])
        self.assertEqual(len(db.get_artifacts(9001)), 1)
        self.assertEqual(db.create_meeting("Họp 2"), 9002)
        info = db.storage_info()
        self.assertEqual((info["mode"], info["local_meetings"]), ("local_file", 2))
        with self.assertRaises(RuntimeError):
            db.sync_local_to_atlas()                      # chưa có Atlas thì không đồng bộ

    def test_sync_to_atlas_copies_once_and_maps_voices(self):
        db.init()
        v_huong = db.save_voice("Hương", [1.0] + [0.0] * 191)
        v_tuan = db.save_voice("Tuấn", [0.0, 1.0] + [0.0] * 190)
        mid = db.create_meeting("Họp offline")
        db.add_segment(mid, 0, 3, "Hương", "Bản mobile xong thứ sáu", speaker_id=v_huong)
        db.upsert_speakers(mid, [{"sid": 1, "label": "Hương", "voice_id": v_huong}, {"sid": 2, "label": "Tuấn", "voice_id": v_tuan}])
        clash = db.create_meeting("Trùng mã trên Atlas")
        self.restart()

        atlas = mongomock.MongoClient()["meeting_assistant"]
        atlas["voices"].insert_one({"id": 3, "name": "hương"})                      # đã có trên Atlas (khác hoa thường)
        atlas["meetings"].insert_one({"id": 1, "title": "Cuộc họp cũ trên Atlas", "created_at": 1.0})
        atlas["meetings"].insert_one({"id": clash, "title": "Cuộc họp khác", "created_at": 1.0})
        with mock.patch.object(db, "_mode", "atlas"), mock.patch.object(db, "_db", atlas):
            self.assertEqual([p["id"] for p in db.storage_info()["pending"]], [mid, clash])
            dry = db.sync_local_to_atlas(dry_run=True)
            self.assertEqual(atlas["meetings"].count_documents({"id": mid}), 0)        # chạy thử không ghi gì
            self.assertEqual([m["id"] for m in dry["meetings"]], [mid])
            rep = db.sync_local_to_atlas()
            self.assertEqual([m["id"] for m in rep["meetings"]], [mid])
            self.assertEqual([c["meeting"] for c in rep["conflicts"] if "meeting" in c], [clash])
            self.assertEqual(rep["voices"], ["Tuấn"])
            seg = atlas["meeting_segments"].find_one({"meeting_id": mid})
            self.assertEqual((seg["text"], seg["speaker_id"]), ("Bản mobile xong thứ sáu", 3))   # đổi sang giọng của Atlas
            spk = {s["label"]: s["voice_id"] for s in atlas["meeting_speakers"].find({"meeting_id": mid})}
            self.assertEqual(spk, {"Hương": 3, "Tuấn": v_tuan})
            self.assertEqual(atlas["meetings"].find_one({"id": clash})["title"], "Cuộc họp khác")  # không ghi đè
            again = db.sync_local_to_atlas()
            self.assertEqual(again["meetings"], [])
            self.assertEqual(atlas["meeting_segments"].count_documents({"meeting_id": mid}), 1)
            self.assertEqual([p["id"] for p in db.storage_info()["pending"]], [clash])


class StorageApiTests(unittest.TestCase):
    def setUp(self):
        reset_db()
        self.client = TestClient(appmod.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        live.SESSIONS.clear()

    def test_storage_endpoints_in_memory_mode(self):
        info = self.client.get("/api/storage").json()
        self.assertEqual(info["mode"], "memory")
        self.assertEqual(self.client.post("/api/storage/sync", json={}).status_code, 409)
        h = self.client.get("/api/health").json()["mongodb"]
        self.assertEqual((h["mode"], h["status"]), ("memory", "In-Memory (dữ liệu mất khi tắt server)"))

    def test_status_text_explains_atlas_failure(self):
        txt = appmod._db_status_text({"mode": "local_file", "database": "x",
                                      "atlas_error": db._explain_mongo_error(Exception(
                                          "SSL handshake failed: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error"))})
        self.assertIn("đang lưu trên máy", txt)
        self.assertIn("Network Access", txt)
        self.assertIn("mật khẩu", db._explain_mongo_error(Exception("bad auth : Authentication failed.")))


if __name__ == "__main__":
    unittest.main()
