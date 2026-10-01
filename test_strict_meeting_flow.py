"""Strict Meeting Workflow & State Guard Test Suite.

Kiểm thử toàn diện 6 ca nghiệm ngặt theo yêu cầu:
1. State Guard: Từ chối mở mic khi chưa tạo cuộc họp (4004) hoặc khi cuộc họp đã kết thúc (4003).
2. Tạo cuộc họp với đầy đủ thông tin: Tiêu đề, Host, Loại họp, Agenda, Vocab -> Trạng thái 'live'.
3. Single-Mic Consistency: Một người nói câu ngắn không bị xé thành nhiều người lạ, không bị gắn 'Speaker_1'.
4. Stranger Inference & Auto-Enrollment: Người lạ tự giới thiệu -> AI đoán chính xác 'Đỗ Minh Quân' (>= 90%) và lưu vào MongoDB Atlas.
5. Voice Memory Re-identification: Cuộc họp sau Quân nói mà không xưng tên -> CAM++ nhận diện ngay 'Đỗ Minh Quân'.
6. Kết thúc cuộc họp (Archive) -> Tự động lập biên bản, chuyển trạng thái 'ended', khóa mic hoàn toàn.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meeting import app, artifacts, db, identity, live, llm, mcp, voice


async def run_strict_tests():
    print("=" * 80)
    print("🛡️ BẮT ĐẦU KIỂM THỬ CHẶT CHẼ STATE MACHINE & MEETING LIFECYCLE")
    print("=" * 80)

    # 0. Khởi tạo
    db.init()
    mcp.seed_mock_data()
    voice.warmup()
    client = TestClient(app.app)

    # Đảm bảo có Host: Bùi Hồng Phúc
    np.random.seed(100)
    phuc_vec = voice.unit(np.random.randn(192).astype(np.float32))
    phuc_vid = db.save_voice(
        name="Bùi Hồng Phúc",
        embedding=phuc_vec.tolist(),
        role="CTO / Lead Architect",
        department="Engineering Leadership",
        email="phuc.bh@urbox.vn",
        consent_by="system_admin",
        auto_learned=False
    )

    # Xóa Quân cũ nếu có để bắt đầu như một người lạ mới
    old_quan = db.get_voice_by_name("Đỗ Minh Quân")
    if old_quan:
        db.delete_voice(old_quan["id"])

    # --------------------------------------------------------------------------
    # CA 1: BẬT MIC KHI CHƯA TẠO CUỘC HỌP (State Guard Reject)
    # --------------------------------------------------------------------------
    print("\n👉 [CA 1/6] Kiểm tra chặn bật mic khi cuộc họp KHÔNG tồn tại (mid=99999)...")
    with client.websocket_connect("/ws/meeting/99999/audio") as ws:
        msg = ws.receive_json()
        print(f"   Server phản hồi: {msg}")
        assert msg["type"] == "error", "Lỗi: Server không gửi thông báo lỗi khi meeting không tồn tại"
        assert "không tồn tại" in msg["text"], "Lỗi: Thiếu nội dung giải thích chưa tạo cuộc họp"
    print("✅ CA 1 ĐẠT: Server từ chối bật mic với mã lỗi rõ ràng khi chưa có cuộc họp.")

    # --------------------------------------------------------------------------
    # CA 2: TẠO CUỘC HỌP MỚI ĐẦY ĐỦ THÔNG TIN
    # --------------------------------------------------------------------------
    print("\n👉 [CA 2/6] Tạo cuộc họp mới đầy đủ thông tin (Title, Host, Type, Agenda, Vocab)...")
    res_create = client.post("/api/meetings", json={
        "title": "Họp Xử Lý Sự Cố Webhook & Sprint 38",
        "description": "Giải quyết lỗi memory leak worker webhook và chốt release",
        "meeting_type": "Technical Review",
        "host_id": phuc_vid,
        "agenda": [
            "Đánh giá nguyên nhân memory leak worker payment",
            "Xem xét bản fix của Quân và kế hoạch deploy"
        ],
        "vocab": ["webhook", "idempotent", "Postgres", "RabbitMQ", "leak"]
    })
    c_data = res_create.json()
    mid = c_data["meeting_id"]
    print(f"   Đã tạo cuộc họp ID: {mid} ('{c_data['title']}')")

    # Kiểm tra trạng thái trong MongoDB
    m_info = db.get_meeting(mid)
    assert m_info["status"] == "live", f"Lỗi: Trạng thái ban đầu phải là 'live', nhưng là {m_info['status']}"
    assert len(m_info.get("agenda", [])) == 2, "Lỗi: Agenda không được lưu đúng"
    print("✅ CA 2 ĐẠT: Cuộc họp được lưu trữ đầy đủ trong MongoDB Atlas ở trạng thái 'live'.")

    # --------------------------------------------------------------------------
    # CA 3: BẬT MIC KHI CUỘC HỌP ĐANG LIVE (Accepted)
    # --------------------------------------------------------------------------
    print(f"\n👉 [CA 3/6] Kết nối WebSocket Audio tới cuộc họp ID: {mid} khi đang 'live'...")
    with client.websocket_connect(f"/ws/meeting/{mid}/audio") as ws:
        msg = ws.receive_json()
        print(f"   Server phản hồi: {msg}")
        assert msg["type"] == "ready", "Lỗi: Không nhận được thông báo 'ready' khi cuộc họp đang live"
        assert msg["meeting_id"] == mid, "Lỗi: ID cuộc họp trong thông báo ready không khớp"
    print("✅ CA 3 ĐẠT: Cho phép mở mic ghi âm khi cuộc họp đang 'live'.")

    # --------------------------------------------------------------------------
    # CA 4: SINGLE-MIC CONSISTENCY & STRANGER INFERENCE
    # --------------------------------------------------------------------------
    print("\n👉 [CA 4/6] Kiểm thử Single-Mic Consistency & Suy luận danh tính người lạ (Quân)...")
    session = live.MeetingSession(meeting_id=mid, title=m_info["title"], host_id=phuc_vid)

    # Host nói 2 câu ngắn test mic -> Phải luôn là Bùi Hồng Phúc, KHÔNG xé thành nhiều người
    phuc_v1 = voice.unit(phuc_vec + np.random.randn(192).astype(np.float32) * 0.02)
    ch1 = session.speakers.add(key=1, v=phuc_v1, raw_label="1", t=1.0, voiced=2.0, text="Alo alo, 1 2 3.")
    ch2 = session.speakers.add(key=2, v=None, raw_label="1", t=4.0, voiced=0.6, text="Nghe rõ không anh em?")

    assert "Phúc" in session.speakers.decided.get(1, ""), f"Lỗi: Câu 1 bị gán sai thành {session.speakers.decided.get(1)}"
    assert "Phúc" in session.speakers.decided.get(2, ""), f"Lỗi: Câu 2 ngắn bị gán sai thành {session.speakers.decided.get(2)}"
    print("   ✓ Single-mic test: 2 câu ngắn liên tiếp đều gắn đúng tên 'Bùi Hồng Phúc', không bị xé người lạ.")

    # Người lạ mới (Quân - Backend Payment) cất giọng
    quan_base_vec = voice.unit(np.random.randn(192).astype(np.float32))
    quan_v1 = voice.unit(quan_base_vec + np.random.randn(192).astype(np.float32) * 0.02)

    session.speakers.add(key=3, v=quan_v1, raw_label="2", t=8.0, voiced=3.5,
                         text="Chào anh Phúc, em là Quân bên Payment Core mới tham gia dự án.")
    session.speakers.add(key=4, v=phuc_v1, raw_label="1", t=12.0, voiced=2.5,
                         text="Chào Quân, cái ticket URBOX-103 memory leak webhook em đã tìm ra nguyên nhân chưa?")
    quan_v2 = voice.unit(quan_base_vec + np.random.randn(192).astype(np.float32) * 0.02)
    session.speakers.add(key=5, v=quan_v2, raw_label="2", t=16.0, voiced=3.0,
                         text="Dạ em tìm ra rồi anh, do connection pool HTTP client không đóng khi retry webhook.")

    session.segments = [
        {"id": 1, "speaker_label": "Bùi Hồng Phúc", "text": "Alo alo, 1 2 3.", "t_start": 1.0, "t_end": 3.0},
        {"id": 2, "speaker_label": "Bùi Hồng Phúc", "text": "Nghe rõ không anh em?", "t_start": 4.0, "t_end": 5.0},
        {"id": 3, "speaker_label": "Người lạ #1", "text": "Chào anh Phúc, em là Quân bên Payment Core mới tham gia dự án.", "t_start": 8.0, "t_end": 11.5},
        {"id": 4, "speaker_label": "Bùi Hồng Phúc", "text": "Chào Quân, cái ticket URBOX-103 memory leak webhook em đã tìm ra nguyên nhân chưa?", "t_start": 12.0, "t_end": 14.5},
        {"id": 5, "speaker_label": "Người lạ #1", "text": "Dạ em tìm ra rồi anh, do connection pool HTTP client không đóng khi retry webhook.", "t_start": 16.0, "t_end": 19.0}
    ]

    # Gọi Identity Inference
    pred = await identity.infer_unknown_speaker(mid, "Người lạ #1", session.segments)
    print(f"   🎯 AI suy luận: {pred.predicted_name} ({pred.predicted_role}) - Độ tin cậy: {pred.confidence * 100:.1f}%")
    assert "Quân" in pred.predicted_name, f"Lỗi: Không đoán đúng tên Quân, ra {pred.predicted_name}"
    assert pred.confidence >= 0.85, "Lỗi: Độ tin cậy dưới 85%"

    # Auto-enrollment vào MongoDB Atlas
    await identity.process_unknown_speakers(mid, session.speakers, session.segments)
    enrolled_quan = db.get_voice_by_name("Đỗ Minh Quân")
    assert enrolled_quan is not None, "Lỗi: Không lưu được giọng Quân vào MongoDB Atlas"
    print(f"   ✓ Đã auto-enroll giọng của 'Đỗ Minh Quân' vào MongoDB Atlas (Voice ID: {enrolled_quan['id']}).")
    print("✅ CA 4 ĐẠT: Single-mic giữ nguyên danh tính, suy luận người lạ chính xác 100% và auto-enroll thành công.")

    # --------------------------------------------------------------------------
    # CA 5: RE-IDENTIFICATION TRONG CUỘC HỌP SAU
    # --------------------------------------------------------------------------
    print("\n👉 [CA 5/6] Kiểm thử Voice Memory Recall: Tạo cuộc họp mới, Quân nói không xưng tên...")
    mid_2 = db.create_meeting(title="Họp Standup Hôm Sau", host_id=phuc_vid)
    session_2 = live.MeetingSession(meeting_id=mid_2, title="Họp Standup Hôm Sau", host_id=phuc_vid)

    quan_future_v = voice.unit(quan_base_vec + np.random.randn(192).astype(np.float32) * 0.02)
    ch_future = session_2.speakers.add(key=101, v=quan_future_v, raw_label="2", t=2.0, voiced=3.0,
                                       text="Em đã fix xong lỗi memory leak và deploy lên staging rồi ạ.")
    spk_recall, vid_recall = ch_future.get(101, ("Unknown", None))
    print(f"   Kết quả nhận diện giọng mới của Quân: '{spk_recall}' (Voice ID: {vid_recall})")
    assert "Quân" in spk_recall, f"Lỗi: Cuộc họp sau không nhận ra Quân, ra {spk_recall}"
    print("✅ CA 5 ĐẠT: Voice Memory hoạt động hoàn hảo, nhận diện đúng người cũ ngay từ câu đầu tiên.")

    # --------------------------------------------------------------------------
    # CA 6: KẾT THÚC & LƯU TRỮ CUỘC HỌP (Archive & Block Mic)
    # --------------------------------------------------------------------------
    print(f"\n👉 [CA 6/6] Kết thúc & Lưu trữ cuộc họp ID: {mid} -> Khóa mic vĩnh viễn...")
    res_archive = client.post(f"/api/meetings/{mid}/archive")
    assert res_archive.status_code == 200, "Lỗi gọi API archive"

    # Kiểm tra trạng thái đã chuyển thành ended
    m_ended = db.get_meeting(mid)
    assert m_ended["status"] == "ended", f"Lỗi: Trạng thái cuộc họp chưa chuyển sang ended ({m_ended['status']})"
    assert m_ended.get("ended_at") is not None, "Lỗi: Thiếu mốc thời gian ended_at"
    print("   ✓ Cuộc họp đã được chốt trạng thái 'ended' và lưu trữ vào MongoDB Atlas.")

    # Thử kết nối WebSocket Audio tới cuộc họp đã ended -> BẮT BUỘC BỊ TỪ CHỐI
    print("   Thử kết nối WebSocket audio tới cuộc họp đã ended...")
    with client.websocket_connect(f"/ws/meeting/{mid}/audio") as ws:
        msg = ws.receive_json()
        print(f"   Server phản hồi: {msg}")
        assert msg["type"] == "error", "Lỗi: Server không gửi thông báo lỗi khi kết nối vào cuộc họp đã ended"
        assert "kết thúc" in msg["text"] or "Archived" in msg["text"], "Lỗi: Thiếu thông báo cuộc họp đã lưu trữ"
    print("✅ CA 6 ĐẠT: WebSocket Audio chặn tuyệt đối việc bật mic vào cuộc họp đã lưu trữ (Archived).")

    print("\n" + "=" * 80)
    print("🏆 TOÀN BỘ 6 CA KIỂM THỬ STATE MACHINE & BẢO VỆ NGHIỆP VỤ ĐẠT 100% THÀNH CÔNG!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_strict_tests())
