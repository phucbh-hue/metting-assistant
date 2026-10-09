# Giai đoạn 3 và 4: nhớ xuyên cuộc họp trong nhóm, chế độ BD - thiết kế

- Ngày: 09/10/2026
- Người yêu cầu và duyệt: phuc.bh@urbox.vn
- Phạm vi: bản web và bản chạy trên máy (bản trên máy không có chia sẻ nhóm).

## Các quyết định đã chốt

- **Giai đoạn 3, cách nhắc: chỉ ghi trong biên bản.**
  - Không nhắc, không ngắt lời trong lúc họp.
  - Biên bản của cuộc họp trong nhóm có mục "Thay đổi so với các buổi trước". Mỗi điểm viết theo mẫu: "Dạ thưa anh chị,
    theo như mình đã bàn ở buổi dd/mm thì là A chứ không phải B, anh chị có thể xem xét lại nha ạ".
- **Giai đoạn 4, gợi ý BD hiện trên bảng riêng.**
  - Bảng chỉ mở trên máy của người trong nhóm, không lên màn hình trình chiếu.
  - Trợ lý không đọc thành tiếng, để khách không thấy, không nghe.
- **Nguồn tri thức BD: tài liệu của nhóm và các cuộc họp trước.**
  - Chủ nhóm tải lên PDF, Word, PowerPoint, Markdown (bảng giá, hồ sơ năng lực, FAQ, chính sách).
  - Cộng thêm biên bản và lời nói của các cuộc họp trước trong nhóm.
- **Chế độ BD bật theo nhóm.**
  - Chủ nhóm bật "Nhóm BD". Mọi cuộc họp trong nhóm có bảng BD.
  - Chủ nhóm có khung trò chuyện, báo cáo trên toàn bộ cuộc họp của nhóm (như yêu cầu ban đầu: "owner của group đó có
    thể query trò chuyện").

## Giai đoạn 3: nhớ xuyên cuộc họp

- Lập biên bản cho cuộc họp thuộc nhóm (tự động cuối buổi hoặc theo yêu cầu) thì thêm một lời gọi AI:
  - Vào:
    - các điều đã chốt của tối đa 12 buổi trước trong nhóm, mỗi điều có mã P1, P2..., ngày và tên buổi;
    - biên bản vừa lập.
  - Ra (JSON):
    - `facts`: các điều đã chốt của buổi này (chủ đề, giá trị, loại: quyết định / con số / hạn chót / người phụ trách /
      chính sách);
    - `changes`: các điều nói khác buổi trước (chủ đề, trước đây, mã buổi trước, bây giờ).
- Lưu vào `meetings.memory`:
  - `facts`, `changes`;
  - mã biên bản, thời điểm.
- Mục "Thay đổi so với các buổi trước" được ghép vào cuối biên bản trước khi lưu, nên lưu Drive và tải Word đều có.
- Buổi trước chưa có `memory` (họp trước bản này, hoặc mới chuyển vào nhóm): rút `facts` từ biên bản của buổi đó, tối
  đa 4 buổi mỗi lần, để giới hạn chi phí.
- Cuộc họp không thuộc nhóm: không làm gì thêm.

## Giai đoạn 4: chế độ BD

### Tài liệu của nhóm (KB)
- `group_docs`:
  - tên, định dạng, dung lượng, số ký tự, người tải, thời điểm;
  - các đoạn chữ khoảng 900 ký tự, chồng 150 ký tự.
- Chỉ lưu chữ đã trích, không giữ tệp gốc trên server.
- Giới hạn:
  - 25 MB mỗi tệp;
  - 400.000 ký tự mỗi tài liệu;
  - 100 tài liệu mỗi nhóm.
- Quyền:
  - chủ nhóm tải lên, xóa;
  - thành viên xem danh sách.

### Tìm kiếm
- BM25 viết tay, không thêm thư viện, không gọi API nhúng (embedding).
- Từ khóa:
  - âm tiết bỏ dấu, chữ thường;
  - cặp âm tiết liền nhau ("phat_hanh") để bắt từ ghép.
- Kho của nhóm gồm:
  - đoạn tài liệu;
  - từng mục của biên bản các buổi trước;
  - lời nói các buổi trước, cắt thành khung khoảng 700 ký tự, có tên người nói và giờ.
- Kho dựng lại khi có tài liệu mới hoặc cuộc họp mới kết thúc. Nhớ trong RAM theo nhóm.

### Bảng BD trong cuộc họp (`#/m/<id>/bd`)
- Mở trong tab hoặc cửa sổ riêng. Phòng họp có nút "Bảng BD" mở tab mới.
- Mỗi người trên bảng BD đánh dấu ai là **Khách**, ai là **Đội mình**. Lưu trong `meetings.bd.roles`.
- Có câu mới thì chờ 1,5 giây cho người nói nói hết, rồi lấy 90 giây gần nhất làm câu hỏi tìm kiếm.
  - Lọc trước bằng luật đơn giản để tiết kiệm: dấu hỏi, các từ "bao nhiêu", "thế nào", "có ... không", "khi nào", "giá",
    "phí"..., hoặc khách nói dài.
  - Tối đa một lượt mỗi 6 giây, tối đa 2 lượt đang chạy.
- Mỗi lượt gọi **song song** 2 lời gọi AI:
  - **Theo tài liệu**: 6 đoạn tài liệu liên quan nhất.
    - Ra: câu khách hỏi, gợi ý trả lời ngắn (nói được ngay), các ý chính, nguồn.
  - **Theo các buổi trước**: 6 đoạn biên bản và lời nói liên quan nhất.
    - Ra: lần trước đã nói, hứa, báo giá gì; điểm cần lưu ý (khác với lần trước).
  - Lời gọi nào xong trước thì hiện trước. Không có gì đáng gợi ý thì không hiện.
- Ô **Hỏi nhanh**: người trong đội BD gõ câu hỏi. Hai lời gọi song song như trên, gắn nhãn "Hỏi nhanh".
- Thẻ gợi ý lưu trong `bd_cards`, mở lại bảng vẫn thấy.
- Gửi qua kênh riêng `/ws/meeting/<id>/bd`:
  - kênh sự kiện chung của phòng họp (màn hình trình chiếu) không có nội dung gợi ý;
  - chỉ người xem được cuộc họp và nhóm đang bật BD mới nối vào được.

### Trò chuyện của chủ nhóm (`#/g/<id>/chat`)
- Chủ nhóm hỏi: "Báo cáo các câu phàn nàn của khách", "Khách đang gặp vấn đề gì?"...
- Cách trả lời:
  1. Xếp hạng các cuộc họp của nhóm theo mức liên quan (BM25 trên tiêu đề, biên bản, lời nói). Chọn tối đa 8 cuộc họp
     (ít hơn thì lấy hết), luôn kèm 2 buổi gần nhất.
  2. **Đọc song song** từng cuộc họp (tối đa 4 lời gọi cùng lúc): rút các ý liên quan câu hỏi, kèm trích dẫn và giờ.
  3. Tổng hợp thành câu trả lời hoặc báo cáo Markdown, ghi nguồn (tên buổi, ngày, link mở cuộc họp). Có thêm đoạn tài
     liệu liên quan nếu câu hỏi về chính sách hoặc giá.
- Chạy nền: câu hỏi hiện ngay với trạng thái "Đang đọc 3/8 cuộc họp...", trang tự cập nhật.
- Lưu trong `group_chats`, giữ 50 tin gần nhất. Chủ nhóm xóa được lịch sử.

### Gọi AI
- Mọi lời gọi qua cổng chung (`artifacts._call_llm`), ghi chi phí theo cuộc họp và việc ("BD gợi ý", "trò chuyện nhóm").
- Gói AI riêng:
  - cuộc họp dùng gói của người tạo;
  - trò chuyện nhóm dùng gói của chủ nhóm.
  - Thêm ngữ cảnh "người dùng hiện tại" cho lời gọi không thuộc cuộc họp nào.

## Kiểm tra
- Test với AI giả, gồm:
  - mục thay đổi trong biên bản; chỉ so với buổi trước cùng nhóm; rút bù buổi cũ;
  - tải, xóa tài liệu; chỉ chủ nhóm;
  - BM25 có và không có dấu;
  - gợi ý BD chạy song song, lọc câu, giới hạn tần suất, hỏi nhanh, kênh riêng chặn nhóm không bật BD;
  - trò chuyện nhóm: đọc song song, chỉ chủ nhóm, nguồn đúng.
- Chạy thử giao diện trên Chromium với AI giả.
