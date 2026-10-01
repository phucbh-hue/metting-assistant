# Quy định họp Sprint của khối Engineering và Product

> Tài liệu nội bộ giả lập phục vụ thử nghiệm Meeting Assistant.
> Phiên bản 1.2 - cập nhật ngày 10/09/2026 - Văn phòng PMO phụ trách.

## 1. Chu kỳ Sprint

Mỗi Sprint kéo dài 2 tuần, bắt đầu vào thứ Hai và kết thúc vào thứ Sáu của tuần thứ hai. Ví dụ: Sprint 38 bắt đầu ngày 21/09/2026 và kết thúc ngày 02/10/2026.

## 2. Các cuộc họp bắt buộc

### Sprint Planning

- Thời gian: sáng thứ Hai đầu Sprint, tối đa 2 giờ.
- Thành phần: Product Manager, Tech Lead, toàn bộ thành viên đội.
- Đầu ra: danh sách ticket cam kết (Sprint Backlog), tổng story point và mục tiêu Sprint.
- Quy tắc: chỉ đưa vào Sprint những ticket đã có tiêu chí nghiệm thu (acceptance criteria) rõ ràng.

### Daily Standup

- Thời gian: 9:15 hằng ngày, tối đa 15 phút.
- Mỗi thành viên trả lời ba câu hỏi: hôm qua làm gì, hôm nay làm gì, có vướng mắc (blocker) gì không.
- Blocker phải được ghi lại và có người phụ trách xử lý trong ngày.

### Sprint Review

- Thời gian: chiều thứ Sáu cuối Sprint, tối đa 1 giờ.
- Đội trình diễn (demo) các tính năng đã hoàn thành cho Product và các phòng ban liên quan.
- Chỉ những ticket đạt Definition of Done mới được demo.

### Sprint Retrospective

- Thời gian: ngay sau Sprint Review, tối đa 45 phút.
- Nội dung: điều gì làm tốt, điều gì cần cải thiện, hành động cụ thể cho Sprint sau.
- Mỗi Retrospective chốt tối đa 3 hành động cải tiến, có người phụ trách và hạn hoàn thành.

## 3. Definition of Done

Một ticket được coi là hoàn thành khi:

- Code đã được review bởi ít nhất 1 thành viên khác và merge vào nhánh chính.
- Có unit test cho logic mới, độ phủ không giảm so với trước.
- Đã kiểm thử trên môi trường staging và QA xác nhận.
- Tài liệu kỹ thuật hoặc hướng dẫn sử dụng được cập nhật nếu có thay đổi hành vi.

## 4. Biên bản họp

- Mọi cuộc họp Planning, Review và Retrospective đều phải có biên bản.
- Biên bản ghi rõ ngày họp (dd/mm/yyyy), người tham dự, quyết định, hành động và người phụ trách.
- Meeting Assistant có thể tự động lập biên bản; người chủ trì chịu trách nhiệm rà soát trước khi gửi.
- Biên bản được lưu trong không gian chung của đội trong vòng 24 giờ sau cuộc họp.

## 5. Quy tắc tham dự

- Thành viên vắng mặt cần báo trước ít nhất 2 giờ và cập nhật trạng thái ticket bằng văn bản.
- Bật camera khi họp trực tuyến nếu điều kiện cho phép; tắt micro khi không phát biểu.
- Ghi âm cuộc họp chỉ được thực hiện khi tất cả người tham dự đồng ý, đặc biệt khi có nhận diện giọng nói.

## 6. Thay đổi phạm vi giữa Sprint

Việc thêm ticket mới vào Sprint đang chạy chỉ được chấp nhận khi là sự cố nghiêm trọng (mức Critical) hoặc được Product Manager và Tech Lead cùng đồng ý. Khi thêm ticket, cần rút ra một lượng story point tương đương để giữ nguyên tải của đội.

## 7. Chỉ số theo dõi

PMO theo dõi ba chỉ số chính sau mỗi Sprint: tỷ lệ hoàn thành cam kết (mục tiêu trên 85%), số ticket bị chuyển sang Sprint sau và thời gian trung bình xử lý blocker. Kết quả được trình bày ngắn gọn trong Sprint Review.
