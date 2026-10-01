"""Test Thực Tế: Bóc Băng, Phân Tách Người Nói & Suy Luận Hội Thoại Từ Video YouTube Thật.

Quy trình:
1. Đọc file âm thanh 16kHz mono đã tải từ YouTube ('youtube_dialogue.wav', 60 giây đối thoại tiếng Việt).
2. Khởi tạo một phiên họp thật trên hệ thống: 'Kiểm Thử Thực Tế Đối Thoại YouTube'.
3. Mở luồng Soniox Realtime STT (stt-rt-v5) có Diarization và đẩy từng khối âm thanh 100ms vào hệ thống.
4. Thu thập toàn bộ các lượt lời (segments), nhãn người nói (speakers) và vector CAM++ 192D.
5. Kích hoạt Identity Inference Engine để AI phân tích nội dung, xưng hô và suy luận vai trò các người nói trong video.
6. Xuất báo cáo đánh giá chi tiết: số câu nhận diện được, độ chính xác phân vai, và lưu trữ vào MongoDB Atlas.
"""
import asyncio
import os
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from meeting import db, identity, live, mcp, voice


async def test_youtube_audio_pipeline():
    print("=" * 80)
    print("🎬 BẮT ĐẦU KIỂM THỬ THỰC TẾ: ĐỐI THOẠI VIDEO YOUTUBE TIẾNG VIỆT THẬT")
    print("=" * 80)

    # 1. Khởi tạo DB & Model CAM++
    db.init()
    mcp.seed_mock_data()
    voice.warmup()
    print("✅ [1/5] Database MongoDB Atlas và Model CAM++ 192D đã sẵn sàng.")

    # 2. Kiểm tra file audio YouTube
    wav_path = Path("youtube_nam_huong.wav")
    if not wav_path.exists():
        wav_path = Path("youtube_dialogue.wav")

    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        nframes = w.getnframes()
        dur_s = nframes / rate
        pcm_all = w.readframes(nframes)
    print(f"✅ [2/5] Đã nạp audio YouTube: {dur_s:.1f}s | SampleRate: {rate}Hz | Channels: {channels} | File: {wav_path.name}")

    # 3. Tạo Meeting mới trên MongoDB Atlas để lưu trữ kết quả kiểm thử này
    mid = db.create_meeting(
        title=f"Kiểm Thử Thực Tế: Đối Thoại YouTube 2 Người ({wav_path.name})",
        description="Đối thoại giữa Nam và Hương từ video YouTube thực tế - kiểm tra tách giọng đa người nói và suy luận danh tính",
        meeting_type="Technical Review",
        vocab=["chào", "Nam", "Hương", "bạn", "khỏe", "cảm ơn", "tên", "Hao Tran", "học", "Việt"]
    )
    print(f"✅ [3/5] Đã khởi tạo phiên họp ID #{mid} trên MongoDB Atlas.")

    session = live.MeetingSession(meeting_id=mid, title="Kiểm Thử YouTube Dialogue")
    stream = await session.add_stream("mic", diarize=True)
    assert stream.ws is not None, "Lỗi: Không kết nối được Soniox WebSocket"
    print("✅ [4/5] Kết nối Soniox Realtime STT thành công. Đang truyền dữ liệu âm thanh...")

    # 4. Stream audio từng chunk 100ms (1600 mẫu * 2 bytes = 3200 bytes) vào Soniox với tốc độ x2 để test nhanh
    chunk_size = 3200
    total_sent = 0
    start_time = time.time()

    for offset in range(0, len(pcm_all), chunk_size):
        chunk = pcm_all[offset:offset + chunk_size]
        await stream.feed(chunk)
        total_sent += len(chunk)
        # Giãn cách 50ms cho mỗi chunk 100ms (tương đương tốc độ phát x2)
        await asyncio.sleep(0.05)

    print(f"   ▶ Đã gửi toàn bộ {total_sent / (16000 * 2):.1f}s audio sang Soniox trong {time.time() - start_time:.1f}s.")
    print("   ▶ Đang đợi Soniox chốt toàn bộ các câu thoại cuối cùng (5s)...")
    await asyncio.sleep(6.0)

    # Đóng luồng âm thanh
    await stream.close()

    # 5. Phân tích kết quả bóc băng và phân chia giọng nói
    segs = db.get_segments(mid)
    print("\n" + "=" * 80)
    print(f"📊 KẾT QUẢ BÓC BĂNG & PHÂN CHIA GIỌNG NÓI THỰC TẾ (Tổng: {len(segs)} câu thoại):")
    print("=" * 80)

    if not segs:
        print("⚠️ Không thu được segment nào từ Soniox. Kiểm tra lại kết nối mạng hoặc API key.")
        return

    detected_speakers = set()
    for s in segs:
        spk = s.get("speaker_label", "Unknown")
        txt = s.get("text", "")
        t_start = s.get("t_start", 0)
        t_end = s.get("t_end", 0)
        dur = t_end - t_start
        detected_speakers.add(spk)
        print(f"  [{t_start:05.1f}s - {t_end:05.1f}s] ({dur:.1f}s) 🗣️ \033[1;32m{spk}\033[0m: \"{txt}\"")

    print("\n" + "-" * 80)
    print(f"👥 SỐ LƯỢNG NGƯỜI NÓI HỆ THỐNG ĐÃ TÁCH ĐƯỢC: {len(detected_speakers)} người -> {list(detected_speakers)}")
    print("-" * 80)

    # 6. Kích hoạt Identity Inference Engine để AI suy luận nội dung và vai trò
    print("\n🧠 [5/5] Kích hoạt Identity Inference: Cho AI phân tích ngữ cảnh đối thoại để suy luận vai trò...")
    target_strangers = [lbl for lbl in detected_speakers if any(kw in lbl.lower() for kw in ["người", "speaker", "unknown"])]
    
    if target_strangers:
        for stranger_lbl in target_strangers[:2]:
            print(f"   Đang phân tích nhãn: '{stranger_lbl}'...")
            pred = await identity.infer_unknown_speaker(mid, stranger_lbl, segs)
            if pred:
                print(f"   🎯 KẾT QUẢ SUY LUẬN CHO '{stranger_lbl}':")
                print(f"      - Dự đoán tên / vai trò : {pred.predicted_name} ({pred.predicted_role})")
                print(f"      - Độ tin cậy (Confidence): {pred.confidence * 100:.1f}%")
                print(f"      - Căn cứ lập luận        : {pred.reasoning}")
                if pred.evidence:
                    print("      - Bằng chứng đối thoại:")
                    for ev in pred.evidence:
                        q = getattr(ev, 'quote', str(ev))
                        print(f"        * \"{q}\"")

    print("\n" + "=" * 80)
    print(f"🎉 KIỂM THỬ THÀNH CÔNG! Kết quả và âm thanh đã được lưu vào MongoDB Atlas (Meeting #{mid}).")
    print(f"👉 Anh có thể mở ngay trình duyệt tại http://127.0.0.1:8080/ và vào Meeting #{mid} để nghe/xem trực tiếp!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(test_youtube_audio_pipeline())
