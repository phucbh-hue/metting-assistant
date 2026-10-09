# Nhóm cuộc họp, Google Drive và lộ trình 4 giai đoạn - thiết kế

- Ngày: 09/10/2026
- Người yêu cầu và duyệt: phuc.bh@urbox.vn
- Phạm vi: bản web (`AUTH_REQUIRED=1`, Render). Bản chạy trên máy (`run.cmd`) vẫn chạy được, các phần cần đăng nhập
  (chia sẻ, Drive) thì tắt.

## Lộ trình (đã chốt thứ tự 1-2-3-4)

1. **Nhóm cuộc họp và lưu biên bản**: nhóm, chia sẻ nhóm, kết nối Google Drive, lưu biên bản và ghi âm vào thư mục
   nhóm (Drive và trên máy). Tài liệu này mô tả chi tiết giai đoạn 1.
2. **Google Calendar và Google Meet**: đọc lịch, tự tạo cuộc họp có tiêu đề và người tham gia, tự vào link Meet, nghe
   được cả người trong phòng lẫn người online. Hai cách vào Meet đã có sẵn ở dự án phỏng vấn:
   - Chrome extension (đã chạy thật, miễn phí, cần một trình duyệt đang ở trong Meet).
   - Bot Recall.ai (tự vào theo lịch, khoảng 0,5 USD/giờ, chủ phòng phải cho bot vào, chưa thử với cuộc họp thật).
   Sẽ chốt khi tới giai đoạn này.
3. **Nhớ xuyên cuộc họp trong nhóm**:
   - Sau mỗi cuộc họp, trích các quyết định, con số, cam kết thành "trí nhớ nhóm".
   - Trong cuộc họp sau, câu nói mâu thuẫn với trí nhớ thì trợ lý nhắc: "Dạ thưa anh, theo như mình đã bàn từ trước
     thì là A chứ không phải B, anh có thể xem xét lại nha ạ".
   - Biên bản có thêm mục "Thay đổi so với các cuộc họp trước".
4. **Chế độ BD** (bật theo nhóm):
   - Gợi ý câu trả lời liên tục từ KB và các cuộc họp trước của nhóm.
   - Hỏi đáp liên tục, chạy nhiều lời gọi AI song song cho nhanh.
   - Chủ nhóm hỏi và lấy báo cáo trên toàn bộ cuộc họp của nhóm, ví dụ "báo cáo các câu phàn nàn của khách", "khách
     đang gặp vấn đề gì".
   - Giai đoạn này dùng chung bộ tìm kiếm với giai đoạn 3.

## Giai đoạn 1: các quyết định đã chốt

### Nhóm và quyền

- `meeting_groups`:
  - Các trường: `id`, `name`, `owner` (email), `members` (danh sách email @urbox.vn), `drive` (thư mục nhóm),
    `recording_drive_days`, `created_at`.
  - Giai đoạn 4 thêm `mode` (`normal` | `bd`).
- Cuộc họp có thêm `group_id` (có thể trống). Có thể tạo cuộc họp trong nhóm, hoặc chuyển một cuộc họp đã có vào nhóm.
- Được xem một cuộc họp:
  - người tạo cuộc họp;
  - chủ nhóm và thành viên của nhóm chứa cuộc họp;
  - quản trị viên, chỉ với cuộc họp cũ chưa có chủ (như bản 3.16).
- Chủ nhóm được:
  - đổi tên nhóm, thêm hoặc bớt thành viên;
  - chọn thư mục Drive;
  - gỡ cuộc họp khỏi nhóm;
  - xóa nhóm. Cuộc họp không bị xóa theo mà thành "không thuộc nhóm".
- Thành viên được xem mọi cuộc họp và biên bản trong nhóm, tạo và tham gia cuộc họp trong nhóm. Thành viên không
  quản lý được danh sách thành viên.
- Trang chủ có cột Nhóm (Tất cả, từng nhóm, Tạo nhóm). Trang nhóm gồm danh sách cuộc họp, thành viên, Drive và thư
  mục trên máy.

### Google Drive

- Mỗi người tự kết nối Drive của mình (nút trong Cài đặt).
  - Chỉ xin quyền `drive.file` (không nhạy cảm): ứng dụng chỉ thấy file, thư mục do nó tạo hoặc do người dùng chọn
    qua Google Picker.
  - Tài khoản Google phải trùng email đăng nhập.
  - Khóa truy cập được mã hóa trước khi lưu lên Atlas. Ngắt kết nối thì thu hồi khóa.
- Biên bản của nhóm nằm trong Drive của **chủ nhóm**:
  - Đường dẫn: `Meeting Copilot / <Tên nhóm> / <dd-mm-yyyy HHhMM - Tiêu đề> / Biên bản (Google Docs) + Ghi âm.mp3`.
  - Thư mục nhóm tự tạo, hoặc chủ nhóm chọn thư mục có sẵn (kể cả Drive dùng chung) qua Picker. Picker là tùy chọn,
    cần một API key.
  - Ứng dụng tự chia sẻ thư mục nhóm cho thành viên với quyền **chỉnh sửa**, tự gỡ khi bớt người, không gửi email
    thông báo.
  - Cuộc họp không thuộc nhóm lưu vào `Meeting Copilot / Cuộc họp riêng /` trong Drive của người tạo.
- Chủ nhóm chưa kết nối Drive: biên bản vẫn có trong ứng dụng, trang nhóm nhắc kết nối, có nút "Lưu các cuộc họp
  chưa lưu".
- Việc làm một lần trên Google Cloud Console:
  - Bật Drive API.
  - Thêm redirect URI `https://urbox-meeting-api.onrender.com/api/google/callback`.
  - Thêm quyền `drive.file` vào màn hình đồng ý, nên để loại Internal.

### Lưu gì và khi nào

- Mỗi cuộc họp lưu **biên bản** và **ghi âm**. Không lưu chép lời đầy đủ và sản phẩm AI (người dùng đã chốt).
- Server tự lưu lên Drive sau khi biên bản xong:
  1. Tạo thư mục cuộc họp.
  2. Đưa biên bản lên dạng Google Docs (tạo lại biên bản thì cập nhật đúng file Docs cũ).
  3. Ghi âm (nếu có) nén thành MP3 rồi tải lên.
  - Trạng thái hiện trong trang cuộc họp: đang lưu, đã lưu kèm link, hoặc lỗi kèm nút lưu lại. Lỗi thì tự thử lại.
- Ghi âm trên Drive **tự xóa theo hạn như server** (mặc định 30 ngày, đổi được theo nhóm). Biên bản giữ mãi.
  - Hộp xác nhận bật ghi âm nói rõ âm thanh được lưu lên Drive của nhóm.
- Về máy (từng người, Chrome và Edge):
  - Chọn một thư mục gốc, ứng dụng tự tạo `<gốc>/<Tên nhóm>/<cuộc họp>/Biên bản.docx + Ghi âm.mp3`.
  - Hoặc chọn thư mục riêng cho từng nhóm.
  - Trình duyệt tự ghi khi biên bản xong nếu ứng dụng đang mở. Cuộc họp bị lỡ thì ghi bù lần mở sau (cần bấm Cho phép
    một lần).
  - Trình duyệt khác chỉ có nút Tải về.

### Thành phần

- Các module server:
  - `meeting/groups.py`: nhóm, thành viên, quyền. Middleware quyền xem cuộc họp dùng module này.
  - `meeting/google_oauth.py`: authorization code + refresh token, mã hóa AES-GCM (khóa sinh từ `AUTH_SECRET`), tự
    làm mới và thu hồi. Giai đoạn 2 dùng lại để xin thêm quyền Calendar.
  - `meeting/gdrive.py`: gọi REST của Drive bằng httpx (thư mục, Docs từ HTML, tải MP3 theo kiểu resumable, chia sẻ,
    xóa ghi âm hết hạn).
  - `meeting/recap_export.py`: biên bản thành HTML (cho Docs) và .docx (python-docx); ghi âm PCM thành MP3.
- Lưu chạy nền sau khi biên bản xong, thử lại tối đa 3 lần, trạng thái ghi vào `meeting.drive`. Lưu lỗi không bao giờ
  chặn biên bản.

### Xử lý lỗi

- Email thành viên không thuộc tên miền cho phép thì từ chối.
- Tên nhóm, tên cuộc họp có ký tự cấm thì làm sạch trước khi tạo thư mục.
- Nhóm bị xóa thì cuộc họp thành không nhóm.
- Google thu hồi quyền hoặc khóa hết hạn thì hiện "cần kết nối lại", biên bản không bị ảnh hưởng.

### Kiểm tra

- Test tự động:
  - Quyền theo vai: chủ nhóm, thành viên, người ngoài, quản trị viên, cuộc họp bị gỡ khỏi nhóm.
  - OAuth với một Google giả.
  - Lưu Drive với API Drive giả: thư mục, Docs, MP3, chia sẻ, cập nhật thay vì tạo trùng, tự xóa ghi âm.
  - Xuất .docx và .mp3.
- Chạy thử trên trình duyệt với 2 tài khoản.
- Drive thật thử sau khi cấu hình Google Cloud.
