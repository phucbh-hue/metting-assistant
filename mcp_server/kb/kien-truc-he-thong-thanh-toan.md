# Kiến trúc hệ thống thanh toán (Payment Service)

> Tài liệu kỹ thuật giả lập phục vụ thử nghiệm. Các thông số và tên thành phần chỉ mang tính minh họa.
> Phiên bản 3.0 - cập nhật ngày 20/08/2026 - Nhóm Payment & Loyalty Core phụ trách.

## 1. Tổng quan

Payment Service xử lý toàn bộ luồng thanh toán khi khách hàng mua voucher, nạp điểm hoặc thanh toán đơn quà tặng doanh nghiệp. Hệ thống tích hợp với các cổng thanh toán nội địa (ví dụ VNPay, ví điện tử) và chuyển khoản ngân hàng qua mã QR.

## 2. Thành phần chính

- **Payment API** (Python FastAPI): nhận yêu cầu tạo giao dịch, sinh link hoặc mã QR thanh toán.
- **Webhook Worker**: nhận callback từ cổng thanh toán, xác thực chữ ký và cập nhật trạng thái giao dịch.
- **Ledger Service**: sổ cái ghi nhận bút toán kép (double-entry) cho mọi biến động số dư.
- **Reconciliation Job**: đối soát giao dịch với file sao kê của cổng thanh toán mỗi ngày lúc 02:00.
- **Message Queue** (RabbitMQ): truyền sự kiện "payment.succeeded" sang Voucher Engine để phát hành mã.
- **Cơ sở dữ liệu**: PostgreSQL cho giao dịch và Redis cho khóa chống trùng (idempotency key).

## 3. Luồng thanh toán chuẩn

1. Ứng dụng gọi POST /api/v1/payments kèm idempotency_key.
2. Payment API tạo giao dịch trạng thái pending và trả về link thanh toán.
3. Khách hàng thanh toán trên cổng; cổng gọi webhook POST /api/v1/webhooks/payment.
4. Webhook Worker xác thực chữ ký, chuyển giao dịch sang succeeded hoặc failed.
5. Sự kiện được đẩy lên RabbitMQ; Voucher Engine phát hành voucher và gửi cho khách hàng.
6. Ledger Service ghi bút toán; số dư được cập nhật trong vòng 2 giây.

## 4. Xử lý lỗi và retry

- Webhook được retry tối đa 5 lần với khoảng chờ tăng dần (1, 5, 30, 120, 600 giây).
- Giao dịch pending quá 15 phút được kiểm tra chủ động bằng API truy vấn của cổng thanh toán.
- Worker phải đóng kết nối HTTP client sau mỗi lần gọi để tránh rò rỉ bộ nhớ (memory leak) khi chạy dài hạn.
- Mọi giao dịch lệch trạng thái sau đối soát được đưa vào hàng đợi xử lý thủ công cho Tài chính.

## 5. Giới hạn và hiệu năng

- Công suất thiết kế: 500 giao dịch/giây ở giờ cao điểm (ví dụ chiến dịch 10.10).
- Độ trễ p95 của Payment API dưới 300 ms.
- Giá trị tối đa mỗi giao dịch bán lẻ: 20.000.000đ; giao dịch doanh nghiệp không giới hạn nhưng cần duyệt thủ công từ 500.000.000đ.

## 6. Bảo mật

- Chữ ký webhook dùng HMAC-SHA256 với secret xoay vòng mỗi 90 ngày.
- Không lưu số thẻ ngân hàng; dữ liệu thẻ do cổng thanh toán quản lý theo chuẩn PCI DSS.
- Log giao dịch che bớt thông tin cá nhân (số điện thoại, email) trước khi đẩy sang hệ thống giám sát.

## 7. Giám sát

Các chỉ số chính được theo dõi trên dashboard: tỷ lệ giao dịch thành công, số webhook thất bại, độ trễ hàng đợi RabbitMQ và mức dùng RAM của Webhook Worker. Cảnh báo được gửi vào kênh #payment-alerts khi tỷ lệ thất bại vượt 2% trong 5 phút.

## 8. Lộ trình nâng cấp

- Quý 4/2026: tách Ledger Service thành cơ sở dữ liệu riêng, bổ sung báo cáo đối soát tự động cho Tài chính.
- Quý 1/2027: hỗ trợ thanh toán trả sau cho khách hàng doanh nghiệp có hạn mức công nợ.
