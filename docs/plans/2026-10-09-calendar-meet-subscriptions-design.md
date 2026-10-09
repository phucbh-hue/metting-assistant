# Giai đoạn 2: Google Calendar, bot vào Google Meet, gói AI riêng của từng người - thiết kế

- Ngày: 09/10/2026
- Người yêu cầu và duyệt: phuc.bh@urbox.vn
- Phạm vi: bản web (Render). Bản chạy trên máy (`run.cmd`) giữ nguyên.

## Các quyết định đã chốt

- **Vào Google Meet bằng bot Recall.ai.**
  - Bot tự xin vào link Meet trên lịch, chủ phòng cho vào. Khoảng 0,5 USD/giờ.
  - `.env` đã có `RECALLAI_API_KEY`.
- **Công tắc theo lịch, mỗi người một công tắc.**
  - Bật "Tự vào cuộc họp trên lịch" thì từ đó bot vào mọi cuộc họp có link Meet trên lịch của người đó. Tắt thì thôi.
  - Thêm nút "Bỏ qua buổi này" cho từng buổi (để tránh buổi nhạy cảm).
- **Họp nửa online nửa trực tiếp.**
  - Người trong phòng mở mic trên ứng dụng như bây giờ.
  - Người online lấy tiếng qua bot, mỗi người một luồng riêng có tên Meet.
- **Gói AI riêng của từng người.**
  - Mỗi người tự kết nối gói Claude hoặc ChatGPT của mình.
  - AI trong cuộc họp người đó tạo thì dùng gói của chính họ. Chưa kết nối thì dùng API key của công ty.
  - Không có gói dùng chung, vì điều khoản Anthropic và OpenAI cấm nhiều người dùng chung một tài khoản.

## A. Gói AI riêng của từng người

- Image Docker cài Node và Claude Code, Codex (`package.json`).
- Mỗi người một thư mục đăng nhập trên ổ lưu lâu dài: `/app/data/cli-users/<mã băm email>/`.
  - Gọi CLI thì đặt `HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` trỏ vào thư mục đó.
  - Đăng nhập của người này không lẫn sang người khác.
- Đăng nhập không cần trình duyệt trên server:
  - Claude Code (`claude auth login --claudeai`): trang hiện đường dẫn, người dùng đăng nhập rồi dán mã vào.
  - Codex (`codex login --device-auth`): trang hiện đường dẫn và mã thiết bị.
- `user_settings.llm_provider`: `claude-cli` | `codex-cli` | trống (dùng API của công ty).
  - Lời gọi AI xác định người tạo cuộc họp đang xử lý (`artifacts.set_meeting`), rồi dùng gói của người đó nếu đã đăng
    nhập.
  - Gói lỗi hoặc hết hạn mức thì dùng API của công ty nếu bật `LLM_API_FALLBACK`. Không bật thì báo lỗi như hiện nay.
- Bản web:
  - Bỏ `LLM_API_ONLY` / `CLI_LLM_DISABLED` trong image.
  - Nguồn AI chung của server vẫn chỉ là API: quản trị viên chọn, chỉ được claude hoặc gemini.
  - Các API `/api/llm/connect*` áp dụng cho chính người gọi, không cần quyền quản trị.
- Giao diện, Cài đặt, mục "Gói AI của tôi":
  - Kết nối, ngắt kết nối từng gói.
  - Chọn gói dùng cho các cuộc họp của tôi.
  - Ghi chú: chỉ kết nối gói của chính mình.

## B. Google Calendar

- Kết nối Google xin thêm quyền `calendar.events.readonly` (nhạy cảm; Internal thì không cần thẩm định).
  - Người đã kết nối Drive phải bấm kết nối lại để cho thêm quyền Lịch.
  - Trạng thái hiện riêng Drive và Lịch.
- `gcalendar.upcoming(email)`: sự kiện từ 15 phút trước tới 7 ngày tới (`singleEvents`), giữ lại:
  - tiêu đề, giờ bắt đầu, giờ kết thúc;
  - link Meet (`hangoutLink` hoặc `conferenceData`);
  - người tham gia (tên, email, đã nhận lời hay chưa);
  - mô tả (làm chương trình họp);
  - mã chuỗi lặp lại.
  - Nhớ 2 phút.
- `user_settings`:
  - `calendar_autojoin` (công tắc);
  - `skip_events` (các buổi bỏ qua);
  - `series_groups` (chuỗi lặp lại sang nhóm: buổi sau tự vào đúng nhóm).

## C. Bot Recall.ai và lịch chạy

- `meet_bots` (mỗi buổi một bản ghi):
  - `owner`, `event_id`, `meet_url`, `title`, `start`, `end`, `attendees`, `group_id`;
  - `bot_id`, `token`, `status`, `status_msg`, `meeting_id`, `ws_open`, `ws_closed_at`.
- Lịch chạy nền mỗi 60 giây (bản web, có key Recall):
  - **Hẹn bot:**
    - Điều kiện: người bật công tắc, buổi có link Meet, chưa từ chối, chưa bỏ qua, bắt đầu trong 30 phút tới hoặc
      đang diễn ra.
    - Tạo bot với `join_at` = giờ bắt đầu, tạo trước ít nhất 10 phút theo khuyến nghị của Recall. Buổi đang diễn ra
      thì vào ngay.
    - Một link Meet ở một giờ bắt đầu chỉ một bot, dù nhiều người cùng bật công tắc.
  - **Hủy bot:** buổi bị hủy, đổi giờ, bỏ qua, hoặc tắt công tắc mà bot chưa vào thì xóa bot. Đổi giờ thì tạo lại ở
    lượt sau.
  - **Theo dõi:**
    - Quanh giờ vào (tối đa 20 phút), đọc `status_changes` mỗi phút để hiện trạng thái: "đang xin vào, chờ chủ phòng
      cho vào", "không vào được", "đã vào".
    - Ngoài khoảng đó không hỏi, theo khuyến nghị của Recall.
  - **Kết thúc:** kết nối âm thanh của bot đóng quá 2 phút và Recall báo `call_ended` / `done` / `fatal` thì kết thúc
    cuộc họp (biên bản, rồi lưu Drive như giai đoạn 1).
- Bot:
  - Tên `RECALL_BOT_NAME` (mặc định "Trợ lý họp UrBox").
  - Lúc vào gửi tin nhắn chung: cuộc họp được chép lời và tóm tắt, không lưu ghi âm trừ khi chủ phòng bật.
  - Tự rời khi mọi người đã rời.
  - Vùng Recall: `RECALL_REGION`. Không đặt thì tự dò (gọi thử API chỉ đọc) và nhớ lại.
- Bấm "Kết thúc cuộc họp" trong ứng dụng thì bot rời Meet (`leave_call`).

## D. Nhận âm thanh từ bot và họp nửa online nửa trực tiếp

- `/ws/recall/{id}/?token=...`: Recall kết nối vào đây. Token ngẫu nhiên của từng bản ghi. Không cần đăng nhập người
  dùng.
- Gói tin đầu tiên tạo cuộc họp, nếu chưa có:
  - người tạo là người bật công tắc;
  - tiêu đề lấy theo lịch;
  - người tham dự lấy từ lịch (giúp đoán tên);
  - chương trình lấy từ mô tả;
  - nhóm lấy theo chuỗi lặp lại;
  - `source="meet"`.
- Mỗi người trong Meet một luồng `meet<id>` (Soniox, có tách người nói). Luồng im lặng lâu thì tự đóng để đỡ tốn
  tiền.
  - Luồng chỉ có một giọng thì hồ sơ người nói lấy tên trong Meet.
  - Luồng của máy trong phòng có nhiều giọng thì tách người như mic.
- **Bỏ tiếng trùng khi họp nửa online nửa trực tiếp:**
  - Khi có người mở mic trong ứng dụng, bot bỏ qua luồng Meet của người đó. Người đó trong Meet được nhận ra bằng
    tên hoặc email trùng tên đăng nhập ứng dụng: máy trong phòng thường vào Meet bằng chính tài khoản đó.
  - Phòng họp có danh sách "Người trong Meet" để tự đánh dấu "máy trong phòng (bỏ qua)" khi tên không khớp.
- Ghi âm vẫn theo luật cũ: chỉ lưu khi chủ phòng bật và có sự đồng ý. Luồng Meet cũng được ghi.

## Giao diện

- Cài đặt, mục Google: kết nối (Drive + Lịch), trạng thái từng quyền, công tắc "Tự cho bot vào các cuộc họp có link
  Meet trên lịch của tôi".
- Trang chủ, mục "Lịch sắp tới" (khi đã kết nối Lịch):
  - Mỗi buổi gồm giờ, tiêu đề, số người.
  - Trạng thái: bot sẽ vào, đã bỏ qua, đang chờ vào, đang chép lời, đã xong (kèm link cuộc họp).
  - Thao tác: nút Bỏ qua / Cho vào lại, ô chọn nhóm (áp dụng cho cả chuỗi lặp lại).
- Phòng họp từ Meet:
  - chip trạng thái bot;
  - menu "Người trong Meet..." để bật hoặc tắt bỏ qua từng người.

## Xử lý lỗi

- Recall từ chối (key sai, hết hạn mức), Google thu hồi quyền Lịch, chủ phòng không cho bot vào: hiện trạng thái trên
  mục Lịch sắp tới, không ảnh hưởng cuộc họp khác.
- Server khởi động lại giữa cuộc họp: Recall tự nối lại kết nối âm thanh (30 lần, mỗi lần cách 3 giây), cuộc họp tiếp
  tục.
- Gói AI của người dùng hết hạn mức: dùng API của công ty nếu bật `LLM_API_FALLBACK`, không thì báo lỗi.

## Kiểm tra

- Test với Google Calendar giả và Recall giả (`httpx.MockTransport`):
  - hẹn, hủy, đổi giờ, bỏ qua, tắt công tắc;
  - một bot cho mỗi link;
  - trạng thái quanh giờ vào.
- Test kết nối âm thanh của bot:
  - tạo cuộc họp từ gói tin đầu;
  - mỗi người một luồng, đặt tên theo Meet;
  - bỏ luồng trùng với mic;
  - token sai bị từ chối;
  - kết thúc khi bot rời.
- Test gói AI: thư mục đăng nhập riêng từng người, chọn gói theo người tạo cuộc họp, gói lỗi thì dùng API.
- Chạy thử trên Chromium. Recall và Google thật chỉ thử được sau khi anh cấu hình:
  - bật Calendar API;
  - thêm quyền Lịch;
  - đặt `RECALLAI_API_KEY` trên Render.
