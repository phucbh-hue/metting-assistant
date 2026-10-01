# UrBox Meeting Copilot

Trợ lý cuộc họp nội bộ: bóc băng thời gian thực (Soniox), **tự tách và nhận ra từng người nói** (CAM++ 192D),
AI đoán tên người nói từ hội thoại, trợ lý Jarvis tra cứu dữ liệu nội bộ qua MCP, tự lập biên bản, vẽ sơ đồ
và phác thảo giao diện.

- Phiên bản: 3.0 - cập nhật 01/10/2026 - phụ trách: phuc.bh@urbox.vn
- Dữ liệu lưu trên MongoDB Atlas (database `meeting_assistant`), tách biệt dự án phỏng vấn.

---

## 1. Có gì mới ở bản 3.0

| Vấn đề ở bản 2.5 | Nguyên nhân gốc | Cách sửa |
|---|---|---|
| Có 2-3 người nói nhưng chỉ hiện "Người lạ #1" | Ngưỡng tái dùng nhãn người lạ (cosine 0.46 trên centroid) thấp hơn ngưỡng gộp cụm 0.50, nên người thứ 2 luôn bị gộp vào người thứ 1. Ngưỡng 0.35 cho Host và bỏ qua kiểm tra chênh lệch khi chỉ có 1 mẫu giọng. Nhãn diarization của Soniox gần như bị bỏ qua. | Viết lại bộ phân vai (`voice.MeetingSpeakers`): kết hợp nhãn Soniox (rất chính xác trong một phiên stream) với CAM++ (nhận lại người quen, nối lại sau mất kết nối, sửa lỗi Soniox). Ngưỡng đo trên dữ liệu thật. |
| Đặt tên xong, câu mới lại hiện nhãn tạm; tên mất khi khởi động lại server | Tên gắn với chuỗi nhãn, nhãn đổi theo cụm; không lưu hồ sơ người nói theo cuộc họp; nhãn mới không ghi lại xuống DB | Mỗi người nói là một **hồ sơ ổn định** (`sid`), lưu ở collection `meeting_speakers`; mọi thay đổi ghi xuống DB theo thứ tự; phiên họp được khôi phục từ DB khi server khởi động lại |
| Timestamp sai sau khi Soniox nối lại | `tok_off` được tính nhưng không dùng | Mỗi kết nối Soniox là một "epoch" có offset riêng; audio chưa chốt được phát lại khi nối lại (không mất chữ) |
| Tốn phí Soniox khi tắt mic | Stream giữ mở vô thời hạn bằng keepalive | Tắt mic: gửi `finalize`, nghỉ quá `SONIOX_IDLE_CLOSE_S` giây thì đóng |
| Gọi LLM đoán tên sau **mỗi** câu | Không có debounce | Chỉ chạy khi có tín hiệu tên ("em là...", "... ơi", lời chào) hoặc đủ câu mới; một lần gọi cho tất cả người chưa định danh; mỗi tên chỉ gán cho một người |
| Giao diện: 2 nút "Tạo cuộc họp", 4 nút "Thu mẫu giọng", dashboard số liệu giả | | Mỗi hành động một chỗ; trang chủ chỉ hiện số liệu thật; phòng họp 3 cột với danh sách người nói, đặt tên, gộp, sửa người nói của từng câu |
| Test ghi vector ngẫu nhiên vào MongoDB thật | Test dùng DB thật | Bộ test offline dùng DB in-memory (`MEETING_DB=mock`), không gọi API ngoài |

Khác:
- Mẫu giọng chỉ được lưu khi người dùng xác nhận (vector giọng nói là dữ liệu sinh trắc học theo Nghị định 13/2023/NĐ-CP). Bật `AUTO_ENROLL_VOICES=1` nếu muốn tự lưu khi AI đoán tên với độ tin cậy từ 85%.
- Hồ sơ giọng thu thủ công không bị dữ liệu tự học ghi đè; đặt tên trùng một hồ sơ giọng đã có nhưng giọng khác hẳn thì không trộn vector.
- Nội dung do AI sinh ra được làm sạch (DOMPurify) và chạy trong iframe sandbox cô lập.
- Kết thúc cuộc họp trả về ngay, biên bản lập ở chế độ nền.

## 2. Cách phân biệt người nói

```
Soniox (nhãn người nói theo phiên)  ─┐
                                     ├─> MeetingSpeakers ─> hồ sơ "Người nói N" (sid ổn định)
CAM++ (vector giọng 192D mỗi câu)  ──┘          │               │
                                                │               ├─ khớp mẫu giọng đã lưu -> tên thật
                                                │               ├─ người dùng đặt tên / AI đoán tên
                                                │               └─ lưu meeting_speakers + segments
```

1. Nhãn Soniox đã gắn với một hồ sơ trong phiên -> câu thuộc hồ sơ đó. CAM++ chỉ ghi đè khi rất chắc chắn.
2. Nhãn Soniox mới -> Soniox cho rằng đây là người khác những người đang có nhãn: so giọng với các hồ sơ còn lại, không đủ giống thì tạo **người nói mới ngay câu đầu tiên**.
3. Sau khi Soniox nối lại (nhãn đánh số lại từ đầu), nhận lại từng người bằng giọng.
4. Câu quá ngắn ("dạ", "ừ") không đủ tính vector: theo nhãn Soniox.
5. Hai hồ sơ có giọng trùng khớp mạnh thì tự gộp; người dùng có thể gộp, tách, đổi người nói của từng câu.

Ngưỡng được đo trên dữ liệu thật của model CAM++: cùng người câu ngắn 0.3-0.6, câu dài 0.6-0.87; khác người 0.0-0.4.

## 3. Cấu trúc thư mục

```
meeting-assistant/
├── meeting/
│   ├── app.py          # FastAPI: REST + WebSocket
│   ├── live.py         # Luồng Soniox (epoch, phát lại audio, finalize) + MeetingSession
│   ├── voice.py        # CAM++ embedding + MeetingSpeakers (theo dõi người nói)
│   ├── identity.py     # IdentityEngine: AI đoán tên người nói (debounce, gợi ý xác nhận)
│   ├── llm.py          # Wake-word "Jarvis" + Thinking/ReAct với Mock MCP
│   ├── artifacts.py    # Biên bản, sơ đồ Mermaid, giao diện web, co-design
│   ├── mcp.py          # Mock MCP: danh bạ, Jira, kiến trúc, lịch sử họp
│   ├── db.py           # MongoDB (hoặc in-memory khi MEETING_DB=mock)
│   └── index.html      # Giao diện web (SPA)
├── tests/              # Test offline (unittest), không gọi API ngoài
├── scripts/
│   ├── demo_replay.py              # Server demo phát lại cuộc họp mẫu, không cần mic
│   └── integration/                # Kiểm thử với Soniox / LLM thật (tính phí)
├── models/speaker.onnx # Model CAM++ (không commit, ~28 MB)
└── static/
```

## 4. Chạy

```powershell
cd "C:\work\cralwer with ai\ASR\meeting-assistant"
$py = "C:\work\cralwer with ai\ASR\interviewer-assistant-AI-circle\.venv\Scripts\python.exe"

# Server
& $py -m uvicorn meeting.app:app --host 127.0.0.1 --port 8080

# Demo không cần mic (DB in-memory, giọng tổng hợp): mở http://127.0.0.1:8090
& $py scripts/demo_replay.py --port 8090

# Test offline (~5 giây)
& $py -m unittest discover -s tests -t . -v

# Kiểm thử tích hợp (gọi Soniox / LLM thật, có tính phí, DB vẫn in-memory)
& $py scripts/integration/soniox_wav_replay.py youtube_nam_huong.wav --expect-speakers 2
& $py scripts/integration/llm_identity_check.py
```

Luồng sử dụng: **Tạo cuộc họp** (góc trên bên phải) -> **Bật mic** -> người nói mới tự xuất hiện ở cột trái với
màu riêng -> bấm **Đặt tên** (hoặc xác nhận gợi ý của AI, tick "Lưu mẫu giọng" nếu người đó đồng ý) ->
**Kết thúc cuộc họp** để lập biên bản. Sau khi kết thúc vẫn đặt tên, gộp người nói và sửa từng câu được.

## 5. Cấu hình

Xem `.env.example`. Các biến quan trọng:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `SONIOX_API_KEY` | | Bắt buộc để ghi âm |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | | Trợ lý AI, đoán tên, biên bản |
| `MONGODB_URL` | | Không có thì dùng MongoDB local, cuối cùng là in-memory |
| `MEETING_DB` | | `mock` = luôn dùng in-memory (test/demo) |
| `AUTO_ENROLL_VOICES` | `0` | `1` = tự lưu mẫu giọng khi AI đoán tên từ 85% |
| `SONIOX_IDLE_CLOSE_S` | `30` | Tắt mic quá ngần này giây thì đóng stream |
| `SONIOX_LANGUAGE_HINTS` | `vi,en` | Gợi ý ngôn ngữ cho Soniox |
| `IDENTITY_MIN_INTERVAL_S` | `20` | Khoảng cách tối thiểu giữa 2 lần AI đoán tên |

## 6. API chính

| Method | Đường dẫn | Mô tả |
|---|---|---|
| GET/POST | `/api/meetings` | Danh sách (kèm thống kê) / tạo cuộc họp |
| GET/PUT/DELETE | `/api/meetings/{id}` | Chi tiết (segments, speakers, gợi ý AI, artifacts) / sửa / xóa |
| POST | `/api/meetings/{id}/archive` | Kết thúc + lập biên bản nền |
| POST | `/api/meetings/{id}/speakers/{sid}/rename` | Đặt tên người nói (`save_voice` để lưu mẫu giọng) |
| POST | `/api/meetings/{id}/speakers/{sid}/merge` | Gộp vào người nói khác |
| POST | `/api/meetings/{id}/speakers/{sid}/save-voice` | Lưu mẫu giọng của người nói |
| POST | `/api/meetings/{id}/segments/{seq}/speaker` | Đổi người nói của một câu (`target_sid: null` = người mới) |
| POST | `/api/meetings/{id}/infer-speakers` | AI đoán tên ngay |
| POST | `/api/meetings/{id}/inferences/{iid}/accept` \| `dismiss` | Xác nhận / bỏ qua gợi ý của AI |
| GET/POST/PATCH/DELETE | `/api/voices` | Hồ sơ giọng nói; `POST /api/voices/enroll-audio` để thu mẫu |
| WS | `/ws/meeting/{id}/audio` | PCM16 16kHz mono từ mic (một thiết bị ghi mỗi cuộc họp) |
| WS | `/ws/meeting/{id}/events` | Sự kiện: `segment`, `segments_relabeled`, `speakers`, `interim`, `identity_suggestion`... |

## 7. Lưu ý dữ liệu

- Bộ test của bản 2.5 ghi vector **ngẫu nhiên** vào DB thật (hồ sơ có `consent_by` = `system_admin` hoặc
  `meeting_host`, 1-2 mẫu). Nếu từng chạy các test đó, hãy rà trang **Hồ sơ giọng nói**, xóa hồ sơ không đúng
  rồi thu mẫu lại bằng **Thu mẫu giọng**.
- Cuộc họp tạo từ bản cũ vẫn mở được: hồ sơ người nói được dựng lại từ nhãn cũ ở lần mở đầu tiên.
- Ảnh `static/wealth-bg.webp` (ảnh chụp một thiết kế của bên thứ ba) không còn được giao diện sử dụng.
