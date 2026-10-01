# Quy trình phát hành voucher cho khách hàng doanh nghiệp

> Tài liệu nội bộ giả lập phục vụ thử nghiệm. Tên người và số liệu chỉ mang tính minh họa.
> Phiên bản 1.4 - cập nhật ngày 02/09/2026 - Phòng Vận hành phụ trách.

## 1. Mục đích

Quy trình này chuẩn hóa các bước phát hành voucher điện tử theo đơn đặt hàng của khách hàng doanh nghiệp (chương trình phúc lợi nhân viên, tri ân khách hàng, quà tặng sự kiện), đảm bảo đúng số lượng, đúng mệnh giá và đúng thời hạn.

## 2. Các vai trò tham gia

- **Sales/Account Manager**: tiếp nhận nhu cầu, lập báo giá và đơn đặt hàng.
- **Vận hành (Ops)**: kiểm tra tồn kho mã từ đối tác thương hiệu, tạo lô phát hành.
- **Tài chính**: xác nhận thanh toán hoặc hạn mức công nợ.
- **Engineering**: hỗ trợ khi phát hành qua API hoặc lô lớn trên 50.000 mã.

## 3. Các bước phát hành

### Bước 1 - Tiếp nhận đơn hàng

Sales tạo đơn trên hệ thống quản trị với các thông tin: danh mục thương hiệu, mệnh giá, số lượng, thời hạn sử dụng, kênh nhận (email, SMS, API hoặc file Excel). Đơn hàng phải có mã hợp đồng hoặc phụ lục hợp lệ.

### Bước 2 - Xác nhận thanh toán

Tài chính xác nhận khách hàng đã thanh toán hoặc còn hạn mức công nợ. Đơn có giá trị từ 200.000.000đ trở lên cần Kế toán trưởng ký duyệt. Ví dụ: đơn 2.000 voucher mệnh giá 100.000đ tương ứng 200.000.000đ thuộc diện ký duyệt.

### Bước 3 - Kiểm tra tồn kho mã

Ops kiểm tra số lượng mã khả dụng trên Voucher Engine. Nếu tồn kho không đủ, Ops gửi yêu cầu nhập mã từ đối tác thương hiệu, thời gian chờ thông thường từ 1 đến 3 ngày làm việc.

### Bước 4 - Tạo lô phát hành

Ops tạo lô (batch) với mã lô theo định dạng BATCH-YYYYMMDD-XXX. Hệ thống tự động gán mã voucher, sinh link nhận quà và ghi log kiểm toán. Lô trên 50.000 mã chạy ở chế độ hàng đợi (queue) ngoài giờ cao điểm.

### Bước 5 - Kiểm tra chất lượng (QC)

Trước khi gửi, Ops lấy mẫu ngẫu nhiên 1% số mã (tối thiểu 5 mã) để kiểm tra trạng thái, mệnh giá và hạn sử dụng. Mọi sai lệch phải được xử lý trước khi chuyển sang bước tiếp theo.

### Bước 6 - Gửi voucher

Voucher được gửi theo kênh khách hàng chọn. Với kênh API, khách hàng gọi endpoint phát hành bằng API key riêng; với file Excel, file được mã hóa bằng mật khẩu gửi qua kênh riêng.

### Bước 7 - Đối soát

Cuối mỗi tháng, Tài chính đối soát số voucher đã phát hành, đã sử dụng và đã hết hạn với từng khách hàng. Biên bản đối soát được gửi trước ngày 05 của tháng kế tiếp.

## 4. Thời hạn xử lý

- Đơn dưới 1.000 mã: hoàn tất trong 4 giờ làm việc sau khi xác nhận thanh toán.
- Đơn từ 1.000 đến 50.000 mã: hoàn tất trong 1 ngày làm việc.
- Đơn trên 50.000 mã: hoàn tất trong 2 ngày làm việc, cần thông báo trước cho Engineering.

## 5. Lưu ý

- Không phát hành voucher khi chưa có xác nhận của Tài chính.
- Voucher đã phát hành chỉ được thu hồi theo chính sách riêng của Phòng CSKH.
- Mọi thay đổi mệnh giá sau khi phát hành phải tạo lô mới, không chỉnh sửa trực tiếp trên lô cũ.
- Lịch phát hành cho các chiến dịch lớn (ví dụ Mega Sale 10.10) cần chốt trước ít nhất 5 ngày làm việc.
