# Câu hỏi thường gặp dành cho đối tác thương hiệu (FAQ)

> Tài liệu giả lập phục vụ thử nghiệm. Số liệu và điều khoản chỉ mang tính minh họa, không phải cam kết hợp đồng.
> Phiên bản 1.6 - cập nhật ngày 25/09/2026 - Phòng Phát triển đối tác phụ trách.

## 1. Đối tác cần làm gì để niêm yết voucher trên UrBox?

Đối tác ký hợp đồng hợp tác, cung cấp thông tin thương hiệu (logo, mô tả, danh sách cửa hàng áp dụng) và lựa chọn hình thức kết nối: cung cấp kho mã có sẵn (file mã) hoặc tích hợp API phát hành mã theo thời gian thực. Thời gian kích hoạt trung bình là 5 ngày làm việc sau khi hoàn tất hồ sơ.

## 2. Tỷ lệ chiết khấu được tính như thế nào?

Tỷ lệ chiết khấu được thỏa thuận theo từng ngành hàng và quy mô. Chiết khấu được trừ trực tiếp khi đối soát. Ví dụ minh họa: voucher mệnh giá 200.000đ với chiết khấu 8% thì UrBox thanh toán cho đối tác 184.000đ cho mỗi voucher đã sử dụng.

## 3. Khi nào đối tác được thanh toán?

UrBox đối soát hằng tháng. Biên bản đối soát được gửi trước ngày 05 của tháng kế tiếp; đối tác xác nhận trong 5 ngày làm việc; UrBox thanh toán trong vòng 10 ngày làm việc sau khi nhận hóa đơn hợp lệ.

## 4. Đối tác xem số liệu ở đâu?

Đối tác đăng nhập Merchant Portal để xem số voucher đã phát hành, đã sử dụng, doanh thu tạm tính và số dư đối soát. Dữ liệu được cập nhật gần thời gian thực, độ trễ tối đa 15 phút.

## 5. Xử lý thế nào khi khách hàng không dùng được voucher tại cửa hàng?

Nhân viên cửa hàng kiểm tra mã trên công cụ xác thực (web hoặc ứng dụng). Nếu mã báo lỗi, đối tác ghi nhận 4 ký tự cuối của mã và liên hệ đường dây hỗ trợ đối tác. UrBox phản hồi trong 1 giờ làm việc và hướng dẫn khách hàng các bước xử lý tiếp theo.

## 6. Đối tác có thể tạm ngừng chương trình không?

Có. Đối tác gửi thông báo bằng văn bản trước tối thiểu 30 ngày. Voucher đã phát hành vẫn phải được chấp nhận đến hết hạn sử dụng, trừ khi hai bên thống nhất phương án thay thế cho khách hàng.

## 7. Yêu cầu kỹ thuật khi tích hợp API là gì?

- Hỗ trợ HTTPS, xác thực bằng API key và chữ ký HMAC.
- Endpoint phát hành mã phản hồi dưới 2 giây ở p95.
- Endpoint xác thực mã (redeem) phải đảm bảo idempotent để tránh sử dụng trùng.
- Có môi trường sandbox để UrBox kiểm thử trước khi chạy thật.

## 8. Dữ liệu khách hàng được chia sẻ ra sao?

UrBox chỉ chia sẻ dữ liệu tối thiểu cần thiết cho việc sử dụng voucher (ví dụ mã voucher, thời điểm sử dụng). Thông tin cá nhân của người nhận quà không được chia sẻ nếu không có sự đồng ý theo Nghị định 13/2023/NĐ-CP.

## 9. Có hỗ trợ chiến dịch khuyến mại chung không?

Có. UrBox phối hợp với đối tác trong các chiến dịch như Mega Sale 10.10, Tết, 8/3 với ngân sách truyền thông chia sẻ. Đề xuất chiến dịch cần gửi trước tối thiểu 30 ngày để lên kế hoạch.

## 10. Liên hệ

Đầu mối phát triển đối tác (giả lập): anh Quốc Bảo - Partner Success Manager, kênh hỗ trợ đối tác trên Merchant Portal.
