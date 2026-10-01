# Tiến độ Mega Sale 10.10 (bản mẫu)

## Mục tiêu chiến dịch
- Doanh số voucher 10 ngày: 2.500.000.000đ
- 120 thương hiệu tham gia, 35 thương hiệu mới
- Tỉ lệ đổi voucher trong 7 ngày đầu: 65%
Chiến dịch chạy từ 01/10/2026 đến 10/10/2026 trên app UrBox và kênh đối tác.

## Tiến độ landing page
- Bản desktop đã xong, đang chạy thử trên staging
- Bản mobile còn phần banner, dự kiến xong 03/10/2026
- Cần số liệu đổi voucher theo ngày để làm báo cáo cho đối tác
Hương phụ trách, nhấn mạnh hạn thứ sáu.

## Hạ tầng và hiệu năng
- Danh mục voucher đã cache bằng Redis, độ trễ còn 80 ms
- Migrate Postgres 18: chạy thử staging cuối tuần, Tuấn phụ trách
- Theo dõi tải đỉnh 10.10: dự kiến gấp 4 lần ngày thường

## Rủi ro và việc cần chốt
- Banner mobile trễ sẽ ảnh hưởng ngày mở bán 01/10/2026
- Chưa có người nhận việc gửi số liệu đổi voucher cho đối tác
- Cần chốt ngân sách quảng cáo bổ sung 300.000.000đ trước 05/10/2026
