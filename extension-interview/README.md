# UrBox Interview — extension thu buổi phỏng vấn

Thu buổi phỏng vấn online và đưa lời thoại vào đúng buổi trên UrBox Interview, **gán vai theo nguồn** (không cần
tách người nói):

- **mic** (micro của bạn) → vai **người phỏng vấn** → `/ws/interview/{sid}/ext?stream=mic`
- **tiếng tab** (ứng viên nói qua Google Meet / Zoom / Teams web) → vai **ứng viên** → `?stream=tab`

Tách biệt với extension "thu tiếng tab" của Meeting Copilot: bản này xác thực bằng **mã kết nối của từng buổi**
(không đọc token đăng nhập), nên dùng được cả khi người phỏng vấn không phải người tạo buổi.

## Cài đặt (Chrome / Edge)
1. Mở `chrome://extensions`, bật **Developer mode**.
2. **Load unpacked** → chọn thư mục `extension-interview`.
3. Lần đầu bật mic: extension mở một tab xin quyền micro — bấm **Cấp quyền micro** (phải làm ở tab riêng, không xin
   được từ popup).

## Dùng
1. Trên UrBox Interview, mở ứng viên → **Bắt đầu phỏng vấn** để tạo buổi (`/interview#/s/{id}`).
2. Ở trang buổi, bấm **Copy mã kết nối** (dạng `địa_chỉ|mã_buổi|token`).
3. Mở tab cuộc gọi (Meet/Zoom/Teams), bấm biểu tượng extension **UrBox Interview**, **dán mã**, Lưu.
4. Tích **Thu cả tiếng tôi nói (mic)** nếu muốn thu cả người phỏng vấn, rồi **Bắt đầu thu**.
5. Lời thoại hiện dần ở trang buổi (tự làm mới). Xong bấm **Kết thúc & chấm** để AI chấm theo tiêu chí.

## Lưu ý
- Mở popup **ngay trên tab đang phỏng vấn** (tiếng tab bắt theo tab đang mở).
- Nên **đeo tai nghe**: không thì tiếng ứng viên phát ra loa lọt vào mic; extension có chống vọng âm (tương quan
  chéo mic↔tab) nhưng tai nghe là chắc nhất.
- Mute trong Meet/Teams/Zoom thì extension tự im luồng mic (content script `mute-watch.js`). Nền tảng khác: bấm
  "Im theo nút tắt mic của trang này" một lần để cấp quyền đọc trang.
- Âm thanh chỉ đi tới server UrBox Interview (Soniox chép lời realtime). File gốc không lưu.
