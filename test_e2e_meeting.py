"""End-to-End Test Suite cho Meeting Assistant AI.

Kiểm thử toàn diện 5 kịch bản cốt lõi:
1. Nhận diện người quen qua Voice ID (CAM++ Embedding & Cosine Match).
2. Phát hiện Người lạ (Unknown Speaker) và AI ĐỌC NGỮ CẢNH HỘI THOẠI ĐỂ ĐOÁN TÊN & DANH TÍNH.
3. Tự động Cập nhật lùi (Retroactive Renaming) và Lưu giọng người lạ vào Voice Registry (Auto-Enrollment).
4. Gọi tên AI (Wake-Word "Jarvis"), kích hoạt Thinking Engine & Gọi Mock MCP Database (Jira).
5. Sinh Web Sandbox tương tác và Co-Design đàm thoại 2 chiều tinh chỉnh giao diện theo phiên bản.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import numpy as np

# Ensure root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from meeting import artifacts, db, identity, live, llm, mcp, voice


async def run_e2e_test():
    print("=" * 75)
    print("🚀 BẮT ĐẦU KIỂM THỬ TOÀN DIỆN MEETING ASSISTANT AI (END-TO-END)")
    print("=" * 75)

    # 1. Khởi tạo DB & Mock MCP
    db.init()
    mcp.seed_mock_data()
    voice.warmup()
    # Xoá giọng Tuấn cũ (nếu có từ lần chạy trước) để đảm bảo Tuấn bắt đầu như một "Người lạ"
    old_tuan = db.get_voice_by_name("Lê Văn Tuấn")
    if old_tuan:
        db.delete_voice(old_tuan["id"])
    print("✅ [1/7] Database Neon/SQLite, Mock MCP và Model CAM++ đã sẵn sàng.")

    # 2. Đăng ký trước 1 giọng nói người quen (Host: Bùi Hồng Phúc)
    # Tạo vector đặc trưng mẫu cho Phúc
    np.random.seed(42)
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
    print(f"✅ [2/7] Đã nạp Voice Anchor cho Host: 'Bùi Hồng Phúc' (ID: {phuc_vid}).")

    # 3. Tạo một cuộc họp mới
    mid = db.create_meeting(
        title="Họp Review Kiến Trúc & Sprint 38 UrBox",
        description="Đánh giá kết nối Neon Postgres v18 và chuẩn bị chiến dịch 10.10",
        host_id=phuc_vid
    )
    session = live.MeetingSession(meeting_id=mid, title="Họp Review Sprint 38", host_id=phuc_vid)
    print(f"✅ [3/7] Đã tạo Meeting Session ID: {mid}.")

    # 4. Giả lập Turn 1: Bùi Hồng Phúc cất tiếng nói (Giọng khớp với Anchor)
    # Giọng của Phúc hơi biến thiên một chút do phòng (scale 0.02 cho cosine ~ 0.96)
    phuc_noisy = voice.unit(phuc_vec + np.random.randn(192).astype(np.float32) * 0.02)
    ch1 = session.speakers.add(key=1, v=phuc_noisy, raw_label="1", t=1.0, voiced=3.0,
                              text="Chào anh em, sáng nay mình họp nhanh về k8s và chiến dịch Mega Sale 10.10 nhé.")
    spk1_name, spk1_id = ch1.get(1, ("Unknown", None))
    print(f"   ▶ Turn 1: Speaker nhận diện được = '{spk1_name}' (Voice ID: {spk1_id})")
    assert "Phúc" in spk1_name, f"Lỗi: Kỳ vọng nhận diện Bùi Hồng Phúc, nhưng ra {spk1_name}"

    # 5. Giả lập Turn 2 & 4: Người lạ (Tuấn - chưa có Voice ID trong DB)
    tuan_base_vec = voice.unit(np.random.randn(192).astype(np.float32))
    tuan_v1 = voice.unit(tuan_base_vec + np.random.randn(192).astype(np.float32) * 0.02)

    ch2 = session.speakers.add(key=2, v=tuan_v1, raw_label="2", t=5.0, voiced=3.5,
                              text="Chào anh Phúc và cả nhà, em là Tuấn bên DevOps mới sang hỗ trợ team UrBox.")
    spk2_name, _ = ch2.get(2, ("Unknown", None))
    print(f"   ▶ Turn 2: Speaker mới xuất hiện = '{spk2_name}' (Chưa có trong Voice Registry)")
    assert "Người lạ" in spk2_name or "Unknown" in spk2_name, "Lỗi: Người mới chưa có mẫu phải mang nhãn Người lạ"

    # Turn 3: Phúc hỏi
    session.speakers.add(key=3, v=phuc_noisy, raw_label="1", t=10.0, voiced=2.5,
                         text="Tuấn ơi, cụm k8s trên staging em đã migrate sang Neon Postgres v18 xong chưa?")

    # Turn 4: Tuấn trả lời
    tuan_v2 = voice.unit(tuan_base_vec + np.random.randn(192).astype(np.float32) * 0.02)
    session.speakers.add(key=4, v=tuan_v2, raw_label="2", t=15.0, voiced=3.0,
                         text="Dạ em kiểm tra kết nối với Postgres v18 rồi, pool kết nối chạy mượt mà lắm anh Phúc.")

    # Gom các segments vào danh sách session
    session.segments = [
        {"id": 1, "speaker_label": spk1_name, "text": "Chào anh em, sáng nay mình họp nhanh về k8s và chiến dịch Mega Sale 10.10 nhé.", "t_start": 1.0},
        {"id": 2, "speaker_label": spk2_name, "text": "Chào anh Phúc và cả nhà, em là Tuấn bên DevOps mới sang hỗ trợ team UrBox.", "t_start": 5.0},
        {"id": 3, "speaker_label": spk1_name, "text": "Tuấn ơi, cụm k8s trên staging em đã migrate sang Neon Postgres v18 xong chưa?", "t_start": 10.0},
        {"id": 4, "speaker_label": spk2_name, "text": "Dạ em kiểm tra kết nối với Postgres v18 rồi, pool kết nối chạy mượt mà lắm anh Phúc.", "t_start": 15.0}
    ]

    # 6. Kích hoạt Identity Inference Engine (AI suy luận danh tính người lạ)
    print("\n🧠 [4/7] Kích hoạt Identity Inference Engine: Đọc ngữ cảnh đối thoại & MCP tra cứu...")
    inference_result = await identity.infer_unknown_speaker(mid, spk2_name, session.segments)

    assert inference_result is not None, "Lỗi: Suy luận danh tính trả về None"
    print(f"   🎯 KẾT QUẢ SUY LUẬN AI:")
    print(f"      - Tên dự đoán   : {inference_result.predicted_name}")
    print(f"      - Vai trò       : {inference_result.predicted_role}")
    print(f"      - Độ tin cậy    : {inference_result.confidence * 100:.1f}%")
    print(f"      - Lập luận      : {inference_result.reasoning}")

    assert "Tuấn" in inference_result.predicted_name, f"Lỗi: Không đoán đúng tên Tuấn, ra {inference_result.predicted_name}"
    assert inference_result.confidence >= 0.85, f"Lỗi: Độ tin cậy không đủ cao ({inference_result.confidence})"

    # Thực hiện quy trình tự động cập nhật lùi và lưu giọng
    await identity.process_unknown_speakers(
        meeting_id=mid,
        meeting_speakers=session.speakers,
        segments=session.segments
    )
    # Kiểm tra xem giọng Tuấn đã được ghi vào DB chưa
    enrolled_tuan = db.get_voice_by_name(inference_result.predicted_name)
    assert enrolled_tuan is not None, "Lỗi: Giọng người lạ chưa được auto-enroll vào bảng voices"
    print(f"✅ Tự động lưu Voice Memory cho '{enrolled_tuan['name']}' (ID: {enrolled_tuan['id']}, auto_learned={enrolled_tuan.get('auto_learned')}).")

    # 7. Kiểm thử Gọi tên AI (Wake-Word "Jarvis") & Thinking Engine với Mock MCP
    print("\n🤖 [5/7] Kiểm thử Wake-Word kích hoạt AI Assistant ('Jarvis') & Tra cứu Mock MCP...")
    ai_command = "Jarvis ơi, tra cứu ticket của Tuấn trên Jira rồi thiết kế một trang Dashboard quản lý tiến độ task bằng Tailwind CSS"

    # Test wake-word parser
    wake_detect = llm.detect_wake_word(ai_command)
    assert wake_detect is not None, "Lỗi: Không phát hiện được wake-word 'Jarvis'"
    wake_word, prompt_cmd = wake_detect
    print(f"   ▶ Bắt được Wake-Word: '{wake_word}' | Lệnh: '{prompt_cmd}'")

    # Thực thi Thinking Engine & Tool call & Artifact generation
    ai_run = await llm.think_and_act(
        meeting_id=mid,
        prompt=prompt_cmd,
        segments=session.segments,
        trigger="voice_wake_word"
    )

    print(f"   ▶ Thinking trace:")
    for line in ai_run["thinking"].split("\n"):
        print(f"      {line}")
    print(f"   ▶ Phản hồi trợ lý: {ai_run['chat_response']}")

    # Kiểm tra xem Mock MCP tool có được gọi không
    assert len(ai_run["tool_calls"]) > 0, "Lỗi: AI không gọi Mock MCP tool"
    print(f"   ▶ MCP Tool đã gọi: {ai_run['tool_calls'][0]['tool']}")

    # Kiểm tra xem Web Sandbox Artifact có được tạo không
    art = ai_run.get("artifact")
    assert art is not None and art.get("kind") == "web_design", "Lỗi: Không sinh được Web Sandbox Artifact"
    print(f"   ▶ Web Sandbox đã sinh: '{art['title']}' (ID: {art['id']}, {len(art['content'])} bytes HTML)")
    assert "<script src=\"https://cdn.tailwindcss.com\"></script>" in art["content"] or "tailwindcss" in art["content"].lower(), "Lỗi: Thiếu Tailwind CSS trong mã web"

    # 8. Kiểm thử Co-Design đàm thoại hai chiều (Conversational Co-Design Loop)
    print("\n🎨 [6/7] Kiểm thử Co-Design Đàm Thoại 2 Chiều (Tinh chỉnh trực tiếp giao diện)...")
    user_feedback = "Jarvis, đổi giao diện sang nền dark mode sang trọng và bổ sung thêm nút 'Export CSV' ở góc trên nhé"
    print(f"   ▶ Góp ý của người dùng: '{user_feedback}'")

    refined_art = await artifacts.co_design_refine(
        meeting_id=mid,
        artifact_id=art["id"],
        user_feedback=user_feedback
    )

    print(f"   ▶ Lời đáp Co-Design: {refined_art['chat_message']}")
    print(f"   ▶ Phiên bản mới: v{refined_art['version']} (Parent ID: {refined_art['parent_id']})")
    assert refined_art["version"] == 2, "Lỗi: Phiên bản không được tăng lên v2"
    assert "Export CSV" in refined_art["content"] or "export" in refined_art["content"].lower(), "Lỗi: Mã mới không phản ánh yêu cầu Export CSV"

    # 9. Kiểm thử Nhận diện tức thời lần họp sau (Voice Memory Recall)
    print("\n🎤 [7/7] Kiểm thử Voice Memory Recall: Tuấn cất giọng trong lần sau...")
    tuan_future_vec = voice.unit(tuan_base_vec + np.random.randn(192).astype(np.float32) * 0.02)
    ch_future = session.speakers.add(key=99, v=tuan_future_vec, raw_label="2", t=60.0, voiced=2.5,
                                     text="Em đồng ý với đề xuất này của anh Phúc.")
    future_spk, future_id = ch_future.get(99, ("Unknown", None))
    print(f"   ▶ Kết quả nhận diện câu mới của Tuấn: '{future_spk}' (Voice ID: {future_id})")
    assert "Tuấn" in future_spk, f"Lỗi: Voice Memory không nhận ra Tuấn ở câu sau, ra {future_spk}"

    print("\n" + "=" * 75)
    print("🎉 TẤT CẢ 7 KỊCH BẢN KIỂM THỬ END-TO-END ĐỀU ĐẠT 100% THÀNH CÔNG!")
    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(run_e2e_test())
