"""Kiểm thử chuyên sâu: Lưu trữ Vector sinh trắc học và Tự động nhận diện (Voice Recall) không cần AI suy luận."""
import asyncio
import json
import os
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from meeting import db, live, voice

def test_recall():
    print("=" * 80)
    print("🎯 KIỂM THỬ TÍNH NĂNG VOICE MEMORY: TỰ ĐỘNG NHẬN DIỆN NGƯỜI CŨ KHÔNG CẦN AI SUY LUẬN")
    print("=" * 80)

    # 1. Khởi tạo DB
    db.init()
    voice.warmup()
    
    # Liệt kê danh sách hồ sơ giọng nói đang có trong MongoDB Atlas
    voices = db.list_voices()
    print(f"Tổng số hồ sơ giọng nói trong MongoDB Atlas: {len(voices)}")
    for v in voices:
        print(f"  - Voice #{v['id']}: {v['name']} ({v.get('role', '')}) - {v.get('n_samples', 1)} mẫu - auto={v.get('auto_learned')}")
    
    # Tìm voice của Nam và Hương
    nam_voice = db.get_voice_by_name("Nam")
    huong_voice = db.get_voice_by_name("Hương")
    assert nam_voice is not None, "Lỗi: Chưa tìm thấy hồ sơ của Nam trong DB voices"
    assert huong_voice is not None, "Lỗi: Chưa tìm thấy hồ sơ của Hương trong DB voices"
    print(f"✅ Đã xác nhận hồ sơ của Nam (ID #{nam_voice['id']}) và Hương (ID #{huong_voice['id']}) trong MongoDB Atlas.")

    # 2. Tạo một cuộc họp hoàn toàn mới (Meeting mới)
    mid = db.create_meeting(title="Họp Tuần Kế Tiếp: Review Dự Án")
    session = live.MeetingSession(meeting_id=mid, title="Họp Tuần Kế Tiếp")
    print(f"✅ Đã tạo cuộc họp mới ID #{mid}.")
    print(f"   Số lượng Anchors được nạp tự động vào RAM: {len(session.speakers.anchors)}")
    for vid, info in session.speakers.anchors.items():
        print(f"     Anchor #{vid}: {info['name']}")

    # 3. Giả lập Nam cất tiếng nói trong cuộc họp mới (giọng Nam có chút biến thiên âm học phòng)
    nam_vec = np.asarray(nam_voice["embedding"], dtype=np.float32)
    nam_turn_vec = voice.unit(nam_vec + np.random.randn(192).astype(np.float32) * 0.02)
    
    print("\n🎤 Lượt 1: Nam cất tiếng nói trong cuộc họp mới (không xưng tên)...")
    changes1 = session.speakers.add(
        key=1,
        v=nam_turn_vec,
        raw_label="0",
        t=1.0,
        voiced=3.0,
        text="Chào cả nhà, tôi đã có mặt trong phòng họp."
    )
    spk1_name, spk1_id = changes1.get(1, ("Unknown", None))
    print(f"   ▶ Kết quả nhận diện tức thời qua CAM++: '{spk1_name}' (Voice ID: {spk1_id})")
    assert "Nam" in spk1_name, f"Lỗi: Kỳ vọng nhận diện Nam, nhưng ra {spk1_name}"
    print("   ✅ CHÍNH XÁC: CAM++ nhận diện đúng Nam ngay từ câu đầu tiên mà KHÔNG cần AI suy luận!")

    # 4. Giả lập Hương cất tiếng nói trong cuộc họp mới
    huong_vec = np.asarray(huong_voice["embedding"], dtype=np.float32)
    huong_turn_vec = voice.unit(huong_vec + np.random.randn(192).astype(np.float32) * 0.02)
    
    print("\n🎤 Lượt 2: Hương cất tiếng nói tiếp theo (không xưng tên)...")
    changes2 = session.speakers.add(
        key=2,
        v=huong_turn_vec,
        raw_label="1",
        t=5.0,
        voiced=3.0,
        text="Xin chào mọi người, hôm nay chúng ta tiếp tục nhé."
    )
    spk2_name, spk2_id = changes2.get(2, ("Unknown", None))
    print(f"   ▶ Kết quả nhận diện tức thời qua CAM++: '{spk2_name}' (Voice ID: {spk2_id})")
    assert "Hương" in spk2_name, f"Lỗi: Kỳ vọng nhận diện Hương, nhưng ra {spk2_name}"
    print("   ✅ CHÍNH XÁC: CAM++ nhận diện đúng Hương ngay lập tức qua đối chiếu vector!")

    # 5. Dọn dẹp cuộc họp test
    db.delete_meeting(mid)
    print("\n" + "=" * 80)
    print("🎉 TOÀN BỘ BÀI TEST VOICE MEMORY RECALL ĐỀU ĐẠT 100% THÀNH CÔNG!")
    print("=" * 80)

if __name__ == "__main__":
    test_recall()
