---
title: Kịch bản demo UrBox Meeting Copilot
description: >
  Kịch bản buổi demo nội bộ: chuẩn bị, phần Jarvis tự trình bày bộ slide theo ghi chú trong tệp, phần
  demo trực tiếp có lời thoại mẫu và phương án dự phòng, gợi ý trả lời hỏi đáp, và toàn bộ lời Jarvis
  đọc trên từng slide.
updated: 10/10/2026
owner: phuc.bh@urbox.vn
---

# Kịch bản demo UrBox Meeting Copilot

- Ngày soạn: 10/10/2026. Phụ trách: phuc.bh@urbox.vn.
- Thời lượng dự kiến khoảng 25 phút:
  - Jarvis tự trình bày: khoảng 8 phút;
  - demo trực tiếp: khoảng 10 phút;
  - hỏi đáp: khoảng 7 phút.
- Tệp dùng trong buổi demo:
  - `slides/demo/UrBox Meeting Copilot - Demo 10.2026.pptx`: Jarvis mở và trình bày tệp này;
  - `docs/demo/UrBox Meeting Copilot - Demo 10.2026.pdf`: cùng nội dung, để gửi người tham dự;
  - `docs/demo/bang-gia-mau.md`: bảng giá giả cho phần chế độ BD.
- Mọi tên người, tên khách và con số trong buổi demo là dữ liệu mẫu.

## 1. Chuẩn bị trước buổi demo

Làm trước khoảng 30 phút:

| Việc | Cách làm |
|---|---|
| Mở ứng dụng | Bản web: Chrome hoặc Edge, đăng nhập bằng tài khoản @urbox.vn. Bản trên máy: chạy `run.cmd` |
| Âm thanh | Nối loa phòng họp. Đặt mic gần người nói. Trong phòng họp, nút "Giọng nói" ở trạng thái bật |
| Giọng đọc | Cài đặt, mục Giọng đọc của trợ lý: chọn Soniox (giọng tự nhiên), bấm Nghe thử |
| Tệp slide (bản web) | Chép tệp PPTX vào một thư mục trên máy. Cài đặt, mục Thư mục tài liệu: chọn thư mục đó, bấm Cho phép |
| Tệp slide (bản trên máy) | Tệp đã có sẵn trong thư mục `slides/demo` của dự án, không cần làm gì thêm |
| Cuộc họp cho phần trình bày | Tạo cuộc họp "Demo nội bộ Meeting Copilot", không thuộc nhóm |
| Nhóm cho phần BD | Tạo nhóm "BD Khách hàng ABC", bật Nhóm BD, tải lên `bang-gia-mau.md` |
| Cuộc họp cho phần BD | Tạo cuộc họp "Họp khách hàng ABC" trong nhóm đó |
| Dự phòng | Mở sẵn video demo và bản PDF trên Google Drive, tắt thông báo trên máy trình bày |

## 2. Phần 1: Jarvis tự trình bày (khoảng 8 phút)

1. Mở cuộc họp "Demo nội bộ Meeting Copilot".
2. Bấm biểu tượng "Trình chiếu toàn màn hình cùng trợ lý" ở góc thẻ trợ lý. Kéo cửa sổ sang màn hình chung,
   bấm **Toàn màn hình**.
3. Bấm **Mở tài liệu**, chọn **UrBox Meeting Copilot - Demo 10.2026**. Hoặc nói: "Jarvis ơi, mở file demo
   Meeting Copilot".
4. Jarvis hỏi cách trình bày: bấm **Theo ghi chú trong tệp**.
5. Jarvis đọc lời trình bày của từng slide rồi tự chuyển slide, hết 13 slide.
6. Ở slide 13, Jarvis mời người dẫn demo trực tiếp. Bấm **Thu nhỏ** để về phòng họp.

Điều khiển trong lúc Jarvis trình bày:

| Thao tác | Tác dụng |
|---|---|
| Esc hoặc nút Dừng thuyết trình | Dừng thuyết trình |
| Bấm lại nút Thuyết trình | Trình bày tiếp |
| Mũi tên trái / phải | Chuyển slide bằng tay |
| V | Tắt / bật giọng nói của trợ lý (chỉ còn phụ đề) |
| N | Hiện các ý chính của slide (nhắc bài) |

## 3. Phần 2: Demo trực tiếp (khoảng 10 phút)

Cần 2 người nói: người dẫn (vai An) và một đồng nghiệp (vai Bình). Ở phần BD, đồng nghiệp đóng vai khách.

| Bước | Người dẫn làm hoặc nói | Kết quả mong đợi |
|---|---|---|
| 1 | Tạo cuộc họp "Review dự án quà Tết", bấm **Tạo và vào phòng**, bấm **Bật mic** | Thấy "Đang nghe" |
| 2 | An: "Chào mọi người, hôm nay mình review tiến độ dự án quà Tết." | Chữ hiện ngay, tách thành Người nói 1 |
| 3 | Bình: "Bên em đã xong tích hợp cổng thanh toán, còn lỗi hoàn tiền một phần, dự kiến sửa xong trước ngày 17/10." | Tách thành Người nói 2 |
| 4 | An: "Ok, mình chốt phát hành voucher Tết ngày 15/12, Bình phụ trách đối soát." | Thêm một câu của Người nói 1 |
| 5 | Bấm **Đặt tên** cho từng người nói: An, Bình | Lời nói hiện đúng tên |
| 6 | Nói: "Jarvis ơi, tóm tắt các quyết định từ đầu buổi" | Jarvis trả lời bằng giọng nói, nêu ngày 15/12 và người phụ trách |
| 7 | Nói: "Jarvis ơi, vẽ dashboard tiến độ dự án" | Dashboard có biểu đồ (ghi "Số liệu minh họa" khi cuộc họp chưa có số liệu thật) |
| 8 | Bấm **Kết thúc cuộc họp**, mở thẻ **Báo cáo** | Biên bản: tóm tắt, quyết định, bảng phân công |
| 9 | Mở cuộc họp "Họp khách hàng ABC", bấm **Bật mic**, bấm **Bảng BD** | Bảng BD mở ở tab mới |
| 10 | Khách: "Chào em, bên chị đang chuẩn bị quà Tết cho nhân viên." Trên Bảng BD, đánh dấu người này là **Khách**, người dẫn là **Đội mình** | Lời chào không tạo câu hỏi |
| 11 | Khách: "Phí tích hợp API bên em là bao nhiêu?" | Câu hỏi hiện ngay, vài giây sau có câu trả lời 5.000.000đ, dấu nguồn K1 |
| 12 | Khách: "Nếu bên chị thanh toán bằng đô la thì quy đổi theo tỷ giá nào?" | Câu trả lời độ chắc thấp (tài liệu chưa có) |
| 13 | Bấm **Tra thêm trên mạng** dưới câu trả lời đó | Khoảng 15-30 giây sau có khối "Trên mạng" với nguồn W1, W2 |

Lưu ý khi demo:
- Trong buổi họp thật, Bảng BD mở trên máy của đội mình, không chiếu lên màn hình chung. Buổi demo chiếu lên
  để mọi người cùng xem.
- Nói rõ, từng người một. Câu gọi trợ lý nên có cả tên và yêu cầu: "Jarvis ơi, ...".
- Bước 13 tra cứu thật trên internet và có tính phí theo lượt tìm.

Nếu trục trặc:

| Hiện tượng | Cách xử lý |
|---|---|
| Jarvis không phản hồi khi gọi | Gõ yêu cầu vào ô "Hỏi Jarvis về cuộc họp này" |
| AI trả lời chậm hơn 30 giây | Giới thiệu tiếp tính năng khác, quay lại khi có kết quả |
| Mic không thu hoặc báo lỗi quyền micro | Bấm biểu tượng ổ khóa trên thanh địa chỉ, cho phép micro, bật mic lại |
| Mất mạng | Chiếu video demo trên Google Drive (4 phút 38 giây) |
| Jarvis đọc sai slide | Bấm Esc, chuyển đến slide cần bằng mũi tên, bấm Thuyết trình để đọc tiếp |

## 4. Phần 3: Hỏi đáp

Gợi ý trả lời các câu hay gặp. Câu về chi phí, hợp đồng, pháp lý chỉ trả lời ở mức tham khảo và hẹn xác nhận
lại với bộ phận phụ trách.

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Chi phí bao nhiêu? | AI tính theo lượng chữ xử lý, qua API key của công ty. Cài đặt, mục Sử dụng AI có số lần gọi AI, token và chi phí ước tính. Tra cứu trên mạng thêm khoảng 0,01 USD mỗi lượt tìm. Con số cụ thể xin xác nhận lại với bộ phận Tài chính |
| Dữ liệu có ra ngoài công ty không? | Lời nói gửi tới Soniox để chép lời. Nội dung cần AI xử lý gửi tới Claude (Anthropic). Biên bản lưu trên Google Drive của nhóm. Bot Google Meet chạy qua Recall, bản ghi giữ 1 giờ rồi xóa. Phạm vi này cần IT và Pháp chế xác nhận trước khi dùng rộng |
| Ai xem được cuộc họp? | Người tạo cuộc họp và thành viên nhóm của cuộc họp. Người ngoài nhóm mở link sẽ nhận "không tìm thấy" |
| Có ghi âm không? | Mặc định tắt. Chỉ bật khi mọi người trong phòng đồng ý, ứng dụng lưu người xác nhận và thời điểm. Ghi âm tự xóa sau 30 ngày |
| Tách người nói có chính xác không? | Phụ thuộc chất lượng mic và việc nói chồng lên nhau. Người đã lưu mẫu giọng được nhận ra tốt hơn. Gán nhầm thì sửa bằng một cú bấm |
| Dùng được cho họp online không? | Có: bot vào Google Meet theo lịch Google. Phần này đang được kiểm tra với tài khoản thật |
| Khi nào dùng được? | Bản web đang chạy. Đề xuất thử ở 2 đến 3 phòng ban trước, rồi mở rộng sau khi có góp ý |

## 5. Lời Jarvis trình bày trên từng slide

Đây là phần ghi chú trong tệp PPTX. Jarvis đọc đúng các đoạn này khi chọn "Theo ghi chú trong tệp". Muốn đổi
lời trình bày thì sửa ghi chú của slide trong PowerPoint, hoặc sửa nội dung trong `docs/demo/build_deck.py`
rồi dựng lại tệp.

<!-- LOI_TRINH_BAY -->

### Slide 1. UrBox Meeting Copilot

Dạ em chào anh chị. Em là Jarvis, trợ lý họp của UrBox. Hôm nay em xin phép tự giới thiệu về chính mình: UrBox
Meeting Copilot. Bộ slide anh chị đang xem cũng do em mở và trình bày. Em sẽ nói khoảng mười phút về những
việc em làm được trong một cuộc họp. Sau đó anh Phúc sẽ demo trực tiếp trên hệ thống để anh chị thấy em làm
việc thật.

### Slide 2. Mỗi cuộc họp đều để lại việc phải làm thêm

Trước hết là chuyện quen thuộc. Trong mỗi cuộc họp luôn có một người vừa nghe vừa chép, nên rất dễ sót ý hoặc
nhầm ai nói gì. Họp xong lại mất thêm thời gian để viết biên bản, liệt kê quyết định, người phụ trách và hạn
chót. Sang buổi sau, có khi mình nói khác điều đã chốt ở buổi trước mà không ai nhận ra. Còn với đội kinh
doanh, khi khách hỏi giá hay chính sách, anh chị phải mở tài liệu, tìm lại cuộc họp cũ ngay trước mặt khách.

### Slide 3. Một trợ lý ngồi trong mọi cuộc họp

Meeting Copilot giải quyết bằng một trợ lý ngồi trong mọi cuộc họp, làm bốn việc. Một, em nghe và chép lời
theo thời gian thực, nhận ra từng người nói. Hai, anh chị gọi em bằng giọng nói để tóm tắt, soạn slide, vẽ
dashboard hay tra cứu. Ba, em ghi nhớ: tự lập biên bản, lưu theo nhóm, và so với các buổi trước. Bốn, với đội
kinh doanh, em gợi ý câu trả lời khi khách hỏi. Anh chị dùng ngay trên trình duyệt, đăng nhập bằng tài khoản
Google của công ty.

### Slide 4. Chép lời và nhận ra từng người nói

Đầu tiên là chép lời. Khi mọi người nói, chữ hiện ra ngay, mỗi người một màu. Ai đã lưu mẫu giọng thì em tự
nhận ra và hiện đúng tên. Khi có người tự giới thiệu hoặc được gọi tên, em đoán tên và hỏi lại nếu chưa chắc.
Nếu em gán nhầm người nói, anh chị chỉ cần bấm vào câu đó để sửa. Mẫu giọng chỉ được lưu khi người đó đồng ý.

### Slide 5. Gọi Jarvis bằng giọng nói, như gọi một đồng nghiệp

Trong lúc họp, anh chị gọi em như gọi một đồng nghiệp: Jarvis ơi, tóm tắt các quyết định từ đầu buổi. Hoặc:
liệt kê việc cần làm, người phụ trách và hạn chót. Em trả lời ngay, vừa làm vừa báo em đang làm gì. Em cũng
tra được thông tin trên mạng, ví dụ tỷ giá hôm nay. Nếu không tiện nói, anh chị gõ vào khung chat, không cần
gọi tên.

### Slide 6. Nói một câu, có ngay slide, dashboard, sơ đồ

Chỉ với một câu nói, em soạn được bộ slide, dựng dashboard có biểu đồ, hoặc vẽ sơ đồ tư duy các ý chính của
cuộc họp. Muốn sửa, anh chị cũng chỉ cần nói, ví dụ: đổi biểu đồ cột thành biểu đồ đường. Mỗi lần sửa là một
phiên bản mới, nên anh chị luôn quay lại được bản cũ. Trên sơ đồ tư duy, bấm vào ý nào là em giải thích ý đó.

### Slide 7. Chiếu lên màn hình chung và tự thuyết trình

Mọi thứ em soạn đều chiếu được lên màn hình chung. Em có thể tự thuyết trình từng slide, hoặc lắng nghe người
đang trình bày và tự chuyển slide theo lời nói. Em cũng mở được tệp PDF, PowerPoint trên máy để trình bày,
theo ghi chú trong tệp hoặc theo kịch bản anh chị đưa. Và thật ra, anh chị đang xem chính tính năng này: bộ
slide hôm nay em mở từ tệp PowerPoint và đọc theo ghi chú trong tệp.

### Slide 8. Họp xong là có biên bản, kèm điều nói khác buổi trước

Khi kết thúc cuộc họp, em lập biên bản gồm tóm tắt, các quyết định, bảng phân công và rủi ro. Với cuộc họp
trong nhóm, em còn so với những điều nhóm đã chốt ở tối đa mười hai buổi trước. Có điểm nào nói khác, em ghi
rõ trong biên bản, như câu nhắc trên slide về ngày phát hành voucher. Biên bản tải được dạng Word, lưu lên
Google Drive của nhóm hoặc vào thư mục trên máy.

### Slide 9. Nhóm cuộc họp, Google Drive và bot vào Google Meet

Cuộc họp được gom theo nhóm, theo chủ đề hay phòng ban. Thành viên nhóm xem chung cuộc họp và biên bản, và
biên bản tự lưu vào thư mục của nhóm trên Google Drive. Nếu anh chị kết nối lịch Google, bot của em tự vào các
cuộc họp Google Meet trên lịch, chép lời từng người online đúng tên trong Meet. Họp nửa online nửa trực tiếp
vẫn chép đủ cả hai phía.

### Slide 10. Chế độ BD: khách hỏi, trợ lý gợi ý câu trả lời

Với đội kinh doanh, có chế độ BD. Khi khách hỏi, em tự nhận ra câu hỏi và gợi ý câu trả lời trên một bảng
riêng chỉ đội mình thấy, không lên màn hình chung và em không đọc thành tiếng. Câu trả lời lấy từ tài liệu của
nhóm như bảng giá, chính sách, và từ các buổi họp trước, có đánh dấu nguồn. Em cũng ghi chú cho đội mình, ví
dụ lần trước đã báo giá khác. Cần thông tin bên ngoài thì bấm tra thêm trên mạng, em ghi rõ đây là thông tin
chưa kiểm chứng. Chủ nhóm còn hỏi được báo cáo trên toàn bộ cuộc họp, ví dụ các câu phàn nàn của khách.

### Slide 11. Dữ liệu cuộc họp ở trong phạm vi công ty

Về dữ liệu: chỉ tài khoản Google của công ty mới đăng nhập được. Mỗi cuộc họp chỉ người tạo và thành viên
trong nhóm xem được. Ghi âm mặc định tắt, chỉ bật khi mọi người trong phòng đồng ý, và tự xóa sau ba mươi
ngày. Mẫu giọng là dữ liệu sinh trắc học theo Nghị định mười ba, nên chỉ lưu khi người đó đồng ý. Tài liệu của
nhóm chỉ giữ phần chữ, tệp gốc không lưu trên máy chủ.

### Slide 12. Sẵn sàng dùng thử, cần thêm cuộc họp thật

Hiện trên bản web đã có đăng nhập Google, chép lời và nhận ra người nói, trợ lý Jarvis, biên bản tự động, nhóm
cuộc họp và chế độ BD. Trước khi mở rộng, nhóm cần thử với cuộc họp thật ở vài phòng ban, kiểm tra lưu Google
Drive, lịch Google và bot Meet với tài khoản thật, và thống nhất cùng IT, Pháp chế về ghi âm và việc dùng gói
AI cá nhân. Em rất mong nhận được góp ý của anh chị sau buổi hôm nay.

### Slide 13. Demo trực tiếp

Phần trình bày của em đến đây là hết. Bây giờ em xin mời anh Phúc demo trực tiếp trên hệ thống: tạo cuộc họp,
gọi em tóm tắt và vẽ dashboard, kết thúc để xem biên bản, và thử bảng BD khi khách hỏi giá. Em cảm ơn anh chị
đã lắng nghe.
