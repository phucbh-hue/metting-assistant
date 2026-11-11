# -*- coding: utf-8 -*-
"""Xem MongoDB Atlas đang chiếm dung lượng ở đâu (CHỈ ĐỌC, không sửa gì).

Chạy từ thư mục gốc repo:  python scripts/db_sizes.py
In ra: tổng dung lượng, từng collection (dữ liệu + index), 10 artifact nặng nhất, 10 cuộc họp có transcript nặng nhất,
để quyết định dọn gì (ảnh slide cũ trong ai_artifacts, segments cuộc họp cũ, log...) trước khi nghĩ tới nâng gói.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import pymongo                                                     # noqa: E402

from meeting.db import MONGODB_DB_NAME, MONGODB_URL, _srv_fallback   # noqa: E402

MB = 1024 * 1024


def connect():
    if not MONGODB_URL:
        sys.exit("Chưa có MONGODB_URL trong .env")
    for url in (MONGODB_URL, _srv_fallback(MONGODB_URL)):          # DNS văn phòng không tra được SRV thì dùng danh sách máy chủ
        try:
            c = pymongo.MongoClient(url, serverSelectionTimeoutMS=8000)
            c.admin.command("ping")
            return c
        except Exception as e:                                     # noqa: BLE001
            print("Không nối được:", str(e)[:160])
    sys.exit(1)


def main():
    client = connect()
    print("Các database trên cluster (lưu trữ + index); database mẫu sample_* của Atlas xóa được nếu không dùng:")
    for name in client.list_database_names():
        if name in ("admin", "local", "config"):
            continue
        try:
            s = client[name].command("dbStats")
            print(f"  {(s['storageSize'] + s['indexSize']) / MB:7.1f} MB | {name}{'   <- app đang dùng' if name == MONGODB_DB_NAME else ''}")
        except Exception as e:                                     # noqa: BLE001
            print(f"  ? | {name} ({str(e)[:60]})")
    db = client[MONGODB_DB_NAME]
    st = db.command("dbStats")
    print(f"DB {MONGODB_DB_NAME}: dữ liệu {st['dataSize'] / MB:.1f} MB, lưu trữ {st['storageSize'] / MB:.1f} MB, "
          f"index {st['indexSize'] / MB:.1f} MB, tổng {(st['storageSize'] + st['indexSize']) / MB:.1f} MB "
          f"(gói M0 miễn phí: 512 MB)")
    rows = []
    for name in db.list_collection_names():
        try:
            cs = db.command("collStats", name)
        except Exception:                                          # noqa: BLE001
            continue
        rows.append((cs.get("storageSize", 0) + cs.get("totalIndexSize", 0), cs.get("size", 0), cs.get("count", 0), name))
    print("\nTừng collection (lưu trữ + index | dữ liệu thô | số bản ghi):")
    for tot, raw, n, name in sorted(rows, reverse=True):
        print(f"  {tot / MB:7.1f} MB | {raw / MB:7.1f} MB | {n:7d} | {name}")

    if "ai_artifacts" in db.list_collection_names():
        print("\n10 artifact nặng nhất (ảnh slide base64 thường nằm ở đây):")
        pipe = [{"$project": {"_id": 0, "id": 1, "kind": 1, "meeting_id": 1, "title": 1, "size": {"$bsonSize": "$$ROOT"}}},
                {"$sort": {"size": -1}}, {"$limit": 10}]
        for a in db["ai_artifacts"].aggregate(pipe):
            print(f"  {a['size'] / MB:6.2f} MB | #{a.get('id')} {a.get('kind')} | họp {a.get('meeting_id')} | {str(a.get('title', ''))[:60]}")

    if "meeting_segments" in db.list_collection_names():
        print("\n10 cuộc họp có transcript nặng nhất:")
        pipe = [{"$group": {"_id": "$meeting_id", "n": {"$sum": 1}, "size": {"$sum": {"$bsonSize": "$$ROOT"}}}},
                {"$sort": {"size": -1}}, {"$limit": 10}]
        for m in db["meeting_segments"].aggregate(pipe):
            print(f"  {m['size'] / MB:6.2f} MB | {m['n']:5d} câu | cuộc họp #{m['_id']}")


if __name__ == "__main__":
    main()
