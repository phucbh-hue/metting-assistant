---
title: Kịch bản demo UrBox Meeting Copilot
description: >
  Kịch bản buổi demo nội bộ cho trưởng phòng Engineering: chuẩn bị, phần Jarvis tự trình bày 15 slide theo
  ghi chú trong tệp, demo trực tiếp theo thứ tự ưu tiên (BD, phỏng vấn, nhận giọng, phần cơ bản), câu hỏi dự
  kiến kèm gợi ý trả lời, và thông điệp, lời Jarvis của từng slide.
updated: 11/10/2026
owner: phuc.bh@urbox.vn
---

# Kịch bản demo UrBox Meeting Copilot

- Ngày soạn: 11/10/2026. Phụ trách: phuc.bh@urbox.vn.
- Người nghe chính: anh Tân, trưởng phòng Engineering. Bộ slide xếp theo thứ tự ưu tiên:
  - điểm khác biệt và tính năng riêng cho BD trước;
  - rồi phỏng vấn, nhận ra người nói, thời gian thực, kiến trúc;
  - phần cơ bản giống các công cụ khác để cuối.
- Thời lượng dự kiến khoảng 30 phút:
  - Jarvis tự trình bày 15 slide: khoảng 10 phút;
  - demo trực tiếp: khoảng 12 phút;
  - hỏi đáp: khoảng 8 phút.
- Tệp dùng trong buổi demo:
  - `slides/demo/UrBox Meeting Copilot - Demo 10.2026.pptx`: Jarvis mở và trình bày tệp này;
  - `docs/demo/UrBox Meeting Copilot - Demo 10.2026.pdf`: cùng nội dung, để gửi người tham dự;
  - `docs/demo/bang-gia-mau.md`: bảng giá giả cho phần họp với khách;
  - `docs/demo/jd-backend-mau.md`: mô tả công việc giả cho phần phỏng vấn.
- Mọi tên người, tên khách và con số trong buổi demo là dữ liệu mẫu.
- Số đo trên slide (thời gian, chi phí, độ chính xác) lấy từ các lượt chạy thật ngày 10-11/10/2026 với dữ liệu
  mẫu. Khi trình bày, nói rõ đó là số đo thử nghiệm.

## 1. Chuẩn bị trước buổi demo

Làm trước khoảng 30 phút:

| Việc | Cách làm |
|---|---|
| Mở ứng dụng | Bản web: Chrome hoặc Edge, đăng nhập bằng tài khoản @urbox.vn. Bản trên máy: chạy `run.cmd` |
| Âm thanh | Nối loa phòng họp. Đặt mic gần người nói. Trong phòng họp, nút "Giọng nói" ở trạng thái bật |
| Giọng đọc | Cài đặt, mục Giọng đọc của trợ lý: chọn Soniox (giọng tự nhiên), bấm Nghe thử |
| Tệp slide (bản web) | Chép tệp PPTX vào một thư mục trên máy. Cài đặt, mục Thư mục tài liệu: chọn thư mục đó, bấm Cho phép |
| Tệp slide (bản trên máy) | Tệp đã có sẵn trong thư mục `slides/demo` của dự án, không cần làm gì thêm |
| Mẫu giọng người dẫn | Hồ sơ giọng nói, Thu mẫu giọng cho người dẫn (khoảng 10 giây, tick đồng ý), để khi demo tên hiện tự động |
| Cuộc họp cho phần trình bày | Tạo cuộc họp "Demo nội bộ Meeting Copilot", không thuộc nhóm |
| Nhóm và cuộc họp BD | Tạo nhóm "BD Khách hàng ABC", bật Nhóm BD, tải lên `bang-gia-mau.md`; tạo cuộc họp "Họp khách hàng ABC" trong nhóm |
| Nhóm và cuộc họp phỏng vấn | Tạo nhóm "Tuyển dụng Backend", bật Nhóm BD, tải lên `jd-backend-mau.md`; tạo cuộc họp "Phỏng vấn Backend (mẫu)" |
| Dự phòng | Mở sẵn video demo và bản PDF trên Google Drive, tắt thông báo trên máy trình bày |

## 2. Phần 1: Jarvis tự trình bày (khoảng 10 phút)

1. Mở cuộc họp "Demo nội bộ Meeting Copilot".
2. Bấm biểu tượng "Trình chiếu toàn màn hình cùng trợ lý" ở góc thẻ trợ lý. Kéo cửa sổ sang màn hình chung,
   bấm **Toàn màn hình**.
3. Bấm **Mở tài liệu**, chọn **UrBox Meeting Copilot - Demo 10.2026**. Hoặc nói: "Jarvis ơi, mở file demo
   Meeting Copilot".
4. Jarvis hỏi cách trình bày: bấm **Theo ghi chú trong tệp**.
5. Jarvis đọc lời trình bày của từng slide rồi tự chuyển slide, hết 15 slide.
6. Ở slide 15, Jarvis mời người dẫn demo trực tiếp. Bấm **Thu nhỏ** để về phòng họp.

Điều khiển trong lúc Jarvis trình bày:

| Thao tác | Tác dụng |
|---|---|
| Esc hoặc nút Dừng thuyết trình | Dừng thuyết trình, ví dụ khi anh Tân muốn hỏi ngay ở một slide |
| Bấm lại nút Thuyết trình | Trình bày tiếp |
| Mũi tên trái / phải | Chuyển slide bằng tay |
| V | Tắt / bật giọng nói của trợ lý (chỉ còn phụ đề) |
| N | Hiện các ý chính của slide (nhắc bài) |

## 3. Phần 2: Demo trực tiếp (khoảng 12 phút)

Cần 2 người: người dẫn (đã thu mẫu giọng) và một đồng nghiệp đóng vai khách, ứng viên, rồi người mới.

**A. Họp với khách (ưu tiên số 1)**

| Bước | Người dẫn làm hoặc nói | Kết quả mong đợi |
|---|---|---|
| 1 | Mở cuộc họp "Họp khách hàng ABC", bấm **Bật mic**, bấm **Bảng BD** | Bảng BD mở ở tab mới |
| 2 | Khách: "Chào em, bên chị đang chuẩn bị quà Tết cho nhân viên." Trên Bảng BD, đánh dấu người này là **Khách**, người dẫn là **Đội mình** | Lời chào không tạo câu hỏi |
| 3 | Khách: "Phí tích hợp API bên em là bao nhiêu?" | Câu hỏi hiện ngay, 4-8 giây sau có câu trả lời 5.000.000đ, dấu nguồn K1 |
| 4 | Khách: "Nếu bên chị thanh toán bằng đô la thì quy đổi theo tỷ giá nào?" | Câu trả lời độ chắc thấp (tài liệu chưa có) |
| 5 | Bấm **Tra thêm trên mạng** dưới câu trả lời đó | 15-25 giây sau có khối "Trên mạng" với nguồn W1, W2 |

**B. Phỏng vấn**

| Bước | Người dẫn làm hoặc nói | Kết quả mong đợi |
|---|---|---|
| 6 | Mở cuộc họp "Phỏng vấn Backend (mẫu)", bấm **Bật mic**, bấm **Bảng BD**; đánh dấu đồng nghiệp là **Khách** | Bảng riêng của người phỏng vấn |
| 7 | Người dẫn: "Em giới thiệu về dự án đối soát giao dịch ở công ty cũ đi." Ứng viên: "Dạ bên em dùng Kafka để đẩy giao dịch sang hệ thống đối soát. Có lần consumer lỗi làm mất message, em xử lý bằng outbox pattern và thêm khóa idempotent." | Lời nói hiện ở cột trái |
| 8 | Gõ vào Bảng BD: "Gợi ý 3 câu hỏi khai thác sâu hơn về sự cố mất message ứng viên vừa kể" | 3 câu hỏi bám tiêu chí trong JD, dấu nguồn K1 |
| 9 | Gõ: "Outbox pattern là gì, ứng viên trả lời vậy có hợp lý không?", rồi bấm **Tra thêm trên mạng** | Giải thích, ghi chú phần nào là kiến thức chung, rồi khối "Trên mạng" có nguồn |
| 10 | Ứng viên: "Cho em hỏi bên mình làm việc remote được không ạ?" | Tự trả lời theo JD: hybrid 3 ngày văn phòng, 2 ngày từ xa |

**C. Nhận ra người nói**

| Bước | Người dẫn làm hoặc nói | Kết quả mong đợi |
|---|---|---|
| 11 | Tạo cuộc họp "Review dự án quà Tết", bấm **Bật mic**. Người dẫn: "Chào mọi người, hôm nay mình review tiến độ dự án quà Tết." | Tên người dẫn hiện tự động, kèm nhãn "nhận ra giọng" |
| 12 | Đồng nghiệp: "Em là Bình bên đối soát. Bên em đã xong tích hợp cổng thanh toán, còn lỗi hoàn tiền một phần, dự kiến sửa xong trước ngày 17/10." | Hiện là Người nói 2, vài giây sau AI gắn tên Bình hoặc hiện gợi ý để xác nhận |

**D. Phần cơ bản**

| Bước | Người dẫn làm hoặc nói | Kết quả mong đợi |
|---|---|---|
| 13 | "Ok, mình chốt phát hành voucher Tết ngày 15/12, Bình phụ trách đối soát." Rồi: "Jarvis ơi, tóm tắt các quyết định từ đầu buổi" | Jarvis trả lời bằng giọng nói, nêu ngày 15/12 và người phụ trách |
| 14 | Bấm **Kết thúc cuộc họp**, mở thẻ **Báo cáo** | Biên bản: tóm tắt, quyết định, bảng phân công |

Lưu ý khi demo:
- Trong buổi họp thật, Bảng BD mở trên máy của đội mình, không chiếu lên màn hình chung. Buổi demo chiếu lên
  để mọi người cùng xem.
- Nói rõ, từng người một. Câu gọi trợ lý nên có cả tên và yêu cầu: "Jarvis ơi, ...".
- Bước 5 và 9 tra cứu thật trên internet và có tính phí theo lượt tìm.
- Trên bảng riêng, nhãn vẫn là "Khách / Đội mình" và "Bảng BD" kể cả khi dùng cho phỏng vấn: nói trước để
  người xem không thắc mắc.

Nếu trục trặc:

| Hiện tượng | Cách xử lý |
|---|---|
| Jarvis không phản hồi khi gọi | Gõ yêu cầu vào ô "Hỏi Jarvis về cuộc họp này" |
| AI trả lời chậm hơn 30 giây | Giới thiệu tiếp tính năng khác, quay lại khi có kết quả |
| AI chưa gắn tên người mới | Bấm **AI đoán tên**, hoặc bấm **Đặt tên** để gắn tay |
| Mic không thu hoặc báo lỗi quyền micro | Bấm biểu tượng ổ khóa trên thanh địa chỉ, cho phép micro, bật mic lại |
| Mất mạng | Chiếu video demo trên Google Drive (4 phút 38 giây) |
| Jarvis đọc sai slide | Bấm Esc, chuyển đến slide cần bằng mũi tên, bấm Thuyết trình để đọc tiếp |

## 4. Phần 3: Hỏi đáp

Câu hỏi anh Tân nhiều khả năng đặt ra, gộp từ các slide. Câu về chi phí, hợp đồng, pháp lý chỉ trả lời ở mức
tham khảo và hẹn xác nhận lại với bộ phận phụ trách.

| Câu hỏi | Gợi ý trả lời |
|---|---|
| Khác gì Gemini trong Google Meet, Otter, Fireflies? | Các công cụ đó mạnh ở chép lời, tóm tắt sau buổi họp. Meeting Copilot tập trung vào lúc đang họp, dựa trên tài liệu và lịch sử họp của UrBox, nhận giọng bằng hồ sơ chung toàn công ty, chạy cả họp trực tiếp |
| Độ trễ thực tế bao nhiêu? | Đo với AI thật: gợi ý trả lời 4-8 giây, 4 câu song song 7-9 giây, tra thêm trên mạng 15-25 giây, vector giọng 0,11 giây cho mỗi giây âm thanh. Chưa đo khi nhiều phòng họp cùng lúc |
| Nhận giọng chính xác đến đâu? | Trên 2 cuộc họp thật nội bộ đã gán nhãn tay: 97% và 98% câu gán đúng người. Dữ liệu còn ít, cần đo thêm trên nhiều cuộc họp, nhiều loại mic |
| Chi phí vận hành? | Mỗi câu gợi ý khoảng 250đ (0,007-0,013 USD, đo thật). Chép lời Soniox tính theo thời lượng âm thanh, AI tính theo token; Cài đặt, mục Sử dụng AI có số liệu theo cuộc họp. Tổng chi phí tháng cần đo sau giai đoạn thử, xác nhận với Tài chính |
| Kiến trúc có mở rộng được không? | Một server FastAPI, phòng họp đang diễn ra giữ trong bộ nhớ nên chạy một bản. Muốn chạy nhiều bản cần tách trạng thái phòng họp ra khỏi bộ nhớ |
| Phụ thuộc nhà cung cấp nào? | Soniox (chép lời), Anthropic (AI), Recall (bot Meet). AI đổi được sang Gemini hoặc gói ChatGPT; giọng đọc có bản Piper chạy trên server |
| Dữ liệu ra ngoài những đâu? | Soniox, Anthropic, Recall (xóa sau 1 giờ), Google Drive của người dùng. Cần IT và Pháp chế review điều khoản trước khi dùng rộng |
| Nối Jira, hệ thống nội bộ được không? | Trợ lý gọi công cụ qua chuẩn MCP; Jira hiện là dữ liệu mẫu. Nối thật cần quyền đọc Jira và một MCP server, cần team Engineering hỗ trợ |
| Kiểm thử, bảo trì thế nào? | 376 test tự động; bản web cập nhật từ nhánh release, chỉ đẩy khi không có cuộc họp đang diễn ra |
| Dùng cho phỏng vấn có ổn về dữ liệu cá nhân? | Bảng riêng không đọc thành tiếng, không chiếu. Ghi âm và mẫu giọng chỉ khi ứng viên đồng ý, theo Nghị định 13/2023; cần Pháp chế xác nhận quy trình |
| Người dùng cần cài gì? | Bản web chỉ cần Chrome hoặc Edge và tài khoản @urbox.vn |

## 5. Từng slide: thông điệp, câu hỏi anh Tân có thể đặt ra, lời Jarvis

Mỗi slide có một thông điệp, các câu anh Tân có thể hỏi kèm gợi ý trả lời, và lời Jarvis đọc (ghi chú trong
tệp PPTX). Muốn đổi nội dung thì sửa `docs/demo/build_deck.py` rồi chạy lại: tệp PPTX, PDF và mục này được
dựng lại cùng lúc.

<!-- LOI_TRINH_BAY -->

### Slide 1. UrBox Meeting Copilot

**Thông điệp:** Meeting Copilot hỗ trợ ngay trong lúc họp, không chỉ ghi chép sau buổi họp.

**Anh Tân có thể hỏi:**

- *Cái này khác gì Gemini ghi chú trong Google Meet hay Otter, Fireflies?* Các công cụ đó mạnh ở chép lời và
  tóm tắt sau buổi họp. Meeting Copilot tập trung vào lúc đang họp, dựa trên tài liệu và lịch sử họp của chính
  UrBox, và chạy cả khi họp trực tiếp. Slide 3 nói rõ 5 điểm khác.

**Lời Jarvis:**

Dạ em chào anh chị. Em là Jarvis, trợ lý họp của UrBox. Hôm nay em tự trình bày về chính mình: UrBox Meeting
Copilot. Điều em muốn anh chị nhớ nhất là: em hỗ trợ ngay trong lúc đang họp, không chỉ ghi chép sau buổi họp.
Em sẽ đi qua những điểm khác biệt, hai tình huống dùng thực tế là họp với khách và phỏng vấn, cách em hoạt
động về kỹ thuật, rồi đến phần demo trực tiếp.

### Slide 2. Ghi chú sau buổi họp đã có nhiều công cụ. Lúc cần trợ giúp nhất lại là trong buổi họp

**Thông điệp:** Khoảng trống không nằm ở ghi chép, mà ở trợ giúp ngay lúc đang họp, bằng dữ liệu riêng của
UrBox.

**Anh Tân có thể hỏi:**

- *Đây có phải vấn đề thật không, hay dùng Gemini là đủ?* Nếu chỉ cần biên bản thì công cụ sẵn có là đủ. Bốn
  tình huống bên phải xảy ra giữa buổi họp và cần dữ liệu riêng của UrBox (bảng giá, JD, lịch sử họp, hồ sơ
  giọng), công cụ bên ngoài không có dữ liệu này.

**Lời Jarvis:**

Ghi chú họp hiện có nhiều công cụ tốt, như Gemini trong Google Meet, Otter hay Fireflies: chép lời, tóm tắt,
liệt kê việc cần làm sau buổi họp. Nhưng lúc cần trợ giúp nhất lại xảy ra ngay giữa buổi: khách hỏi một điều
đội BD chưa nắm, ứng viên nhắc tới một kỹ thuật mà người phỏng vấn muốn hỏi sâu hơn, buổi này nói khác điều đã
chốt ở buổi trước, hay họp trực tiếp đông người mà biên bản không rõ ai nói gì. Những lúc đó cần dữ liệu riêng
của UrBox, và cần ngay.

### Slide 3. 5 điểm khác so với các công cụ ghi chú họp phổ biến

**Thông điệp:** Khác biệt nằm ở hỗ trợ ngay lúc họp, dựa trên dữ liệu riêng của UrBox; phần ghi chép là cơ
bản.

**Anh Tân có thể hỏi:**

- *Trong 5 điểm này, điểm nào công cụ khác thật sự không làm được?* Điểm 1, 2, 4 cần tài liệu nội bộ và lịch
  sử họp của UrBox, công cụ bên ngoài không có. Điểm 3 dùng hồ sơ giọng chung toàn công ty, chạy cả họp trực
  tiếp không cần bot. Mình không khẳng định không công cụ nào có từng phần; điểm mạnh là gộp cả 5 trên dữ liệu
  của mình.

**Lời Jarvis:**

Em có năm điểm khác so với các công cụ ghi chú họp phổ biến. Một, khi khách hỏi, em gợi ý câu trả lời ngay,
lấy từ tài liệu và các buổi họp trước của UrBox, có dấu nguồn, trên một bảng riêng chỉ đội mình thấy. Đây là
tính năng em ưu tiên nhất. Hai, khi phỏng vấn, em là trợ lý riêng của người phỏng vấn. Ba, em nhận ra người
nói bằng mẫu giọng, kể cả khi họp trực tiếp. Bốn, em nhớ xuyên các cuộc họp. Năm, em nói tiếng Việt và dữ liệu
nằm trên hạ tầng của UrBox. Còn chép lời, biên bản là phần cơ bản, em để ở cuối.

### Slide 4. Khách hỏi điều BD chưa nắm: vài giây sau đã có gợi ý trả lời, kèm nguồn

**Thông điệp:** BD trả lời được ngay cả câu chưa nắm, và biết nguồn để tin.

**Anh Tân có thể hỏi:**

- *AI có bịa giá hay chính sách không?* Chỉ dùng nguồn trong tài liệu nhóm và các buổi trước; mỗi ý có dấu K
  (tài liệu) hoặc M (cuộc họp trước), bấm xem được. Thiếu thông tin thì gợi ý 'bên em kiểm tra và gửi lại', độ
  chắc thấp.
- *Khách có nhìn thấy không?* Không. Bảng mở trên máy đội BD, không lên màn hình chung, trợ lý không đọc thành
  tiếng.

**Lời Jarvis:**

Tình huống đầu tiên, cũng là tính năng em ưu tiên nhất: họp với khách. Khách hỏi: phí tích hợp API bao nhiêu,
có khác lần trước không? Chỉ vài giây sau, trên bảng riêng của đội BD đã có gợi ý: năm triệu đồng, thu một
lần, lấy từ bảng giá. Em còn ghi chú: buổi ngày hai tháng mười, đội mình đã báo bốn triệu, nên giải thích phần
chênh lệch. Đội BD trả lời được ngay, kể cả khi chưa nắm câu đó, và biết thông tin lấy từ đâu.

### Slide 5. Câu trả lời chỉ lấy từ nguồn của UrBox, và chỉ đội mình thấy

**Thông điệp:** Gợi ý có căn cứ, có kiểm soát, chi phí thấp.

**Anh Tân có thể hỏi:**

- *Tìm kiếm làm thế nào, có cần vector database không?* Không. Tìm theo từ khóa (BM25, bỏ dấu tiếng Việt, ghép
  cặp từ) chạy ngay trên server, không gửi tài liệu đi lập chỉ mục ở dịch vụ ngoài. Đổi sang tìm theo nghĩa
  khi cần là việc có thể làm sau.
- *Mỗi câu tốn bao nhiêu, có gọi AI liên tục không?* Đo 4 lượt với Claude Sonnet 5.5: 0,007-0,013 USD mỗi câu,
  4-8 giây. Chỉ gọi AI khi câu có dấu hiệu là câu hỏi, tối đa một câu tự nhận ra mỗi 6 giây.

**Lời Jarvis:**

Về cách hoạt động: em chỉ xét câu của người được đánh dấu là khách, bỏ qua lời chào và câu xác nhận. Em tìm
trong tài liệu của nhóm như bảng giá, hồ sơ năng lực, và trong biên bản, lời nói của các buổi trước. Rồi em
viết gợi ý ngắn, có dấu nguồn, kèm ghi chú cho đội và mức độ chắc chắn. Cần thông tin bên ngoài thì đội bấm
tra thêm trên mạng. Việc tìm kiếm chạy ngay trên server, không cần cơ sở dữ liệu vector. Mỗi câu gợi ý tốn
khoảng hai trăm năm mươi đồng.

### Slide 6. Phỏng vấn: gợi ý câu hỏi đào sâu và giải thích thuật ngữ ngay trong buổi

**Thông điệp:** Người phỏng vấn có trợ lý riêng, hỏi sâu hơn mà không cần là chuyên gia mọi mảng.

**Anh Tân có thể hỏi:**

- *Team Engineering tuyển người có dùng được ngay không?* Được, với tính năng hiện có: tạo nhóm tuyển dụng,
  bật chế độ BD, tải JD và tiêu chí. Chưa có giao diện riêng cho phỏng vấn: nhãn trên bảng vẫn là Khách / Đội
  mình.
- *Ứng viên có biết không, có vấn đề dữ liệu cá nhân không?* Bảng mở trên máy người phỏng vấn, không đọc thành
  tiếng. Ghi âm và lưu mẫu giọng chỉ khi ứng viên đồng ý, theo Nghị định 13/2023.

**Lời Jarvis:**

Tình huống thứ hai là phỏng vấn. Ứng viên kể đã xử lý sự cố mất message bằng outbox pattern. Người phỏng vấn
gõ vào bảng riêng: gợi ý ba câu hỏi khai thác sâu. Em trả lời ngay ba câu bám theo tiêu chí trong mô tả công
việc, như cách phát hiện sự cố và cách thiết kế chống ghi trùng. Gặp thuật ngữ chưa rõ, người phỏng vấn hỏi
em, em giải thích và tra thêm trên mạng có nguồn. Khi ứng viên hỏi về chế độ làm việc, em trả lời theo mô tả
công việc. Tính năng này dùng được ngay cho các buổi tuyển dụng của team Engineering.

### Slide 7. Biết ai đang nói nhờ mẫu giọng, kể cả người lần đầu tham gia

**Thông điệp:** Biên bản biết chính xác ai nói gì; người mới cũng được nhận ra từ buổi sau.

**Anh Tân có thể hỏi:**

- *Độ chính xác bao nhiêu, đo thế nào?* Đo trên 2 cuộc họp thật nội bộ đã gán nhãn tay: 97% và 98% câu gán
  đúng người. Dữ liệu còn ít, cần đo thêm trên nhiều cuộc họp và nhiều loại mic.
- *Người mới thì xử lý thế nào?* Lần đầu hiện là Người nói N. Khi họ tự giới thiệu hoặc được gọi tên, AI đoán
  tên: chắc từ 70% thì tự gắn, thấp hơn thì hiện gợi ý để xác nhận. Người đó đồng ý thì lưu mẫu giọng, buổi
  sau tự nhận ra.
- *Dữ liệu giọng lưu thế nào, có xóa được không?* Mỗi người một vector 192 số, không lưu âm thanh. Chỉ lưu khi
  có đồng ý theo Nghị định 13/2023; xóa hồ sơ bất cứ lúc nào.

**Lời Jarvis:**

Phần nhận ra người nói. Với mỗi câu nói, em tính một vector giọng gồm một trăm chín mươi hai con số, chạy ngay
trên server, rồi so với hồ sơ giọng nói của công ty. Khớp thì em hiện đúng tên, kèm số câu và thời lượng nói
của từng người. Người lần đầu tham gia sẽ hiện là người nói mới; khi họ tự giới thiệu hoặc được gọi tên, em
đoán tên và gắn vào, chưa chắc thì em hỏi lại. Nếu người đó đồng ý, em lưu mẫu giọng, từ buổi sau em tự nhận
ra. Đo trên hai cuộc họp thật nội bộ, em gán đúng người nói chín mươi bảy và chín mươi tám phần trăm số câu.

### Slide 8. Mọi thứ chạy trong lúc họp: số đo thực tế

**Thông điệp:** Đủ nhanh để dùng ngay trong cuộc nói chuyện, chi phí mỗi lượt thấp.

**Anh Tân có thể hỏi:**

- *Số đo ở đâu ra, lặp lại được không?* Đo bằng script chạy AI thật với dữ liệu mẫu (có trong repo); chạy lại
  được bất cứ lúc nào. Chưa đo khi nhiều phòng họp chạy cùng lúc.

**Lời Jarvis:**

Về thời gian thực, em đưa số đo thực tế. Chữ hiện ngay khi đang nói. Tính vector giọng chỉ mất khoảng không
phẩy mười một giây cho mỗi giây âm thanh. Gợi ý trả lời khi khách hỏi mất từ bốn đến tám giây. Bốn câu hỏi
cùng lúc xong trong bảy đến chín giây vì chạy song song. Tra thêm trên mạng mất mười lăm đến hai mươi lăm
giây. Mỗi câu gợi ý tốn khoảng hai trăm năm mươi đồng. Các số này đo với dữ liệu mẫu, sẽ thay đổi theo mạng và
tải.

### Slide 9. Kiến trúc gọn: một server, dịch vụ AI qua API, dữ liệu ở Atlas của UrBox

**Thông điệp:** Kiến trúc đơn giản, dễ vận hành, không khóa chặt vào một nhà cung cấp.

**Anh Tân có thể hỏi:**

- *Một server có phải điểm nghẽn không?* Phòng họp đang diễn ra giữ trong bộ nhớ nên hiện chạy một bản. Chưa
  đo tải nhiều phòng cùng lúc; muốn mở rộng cần tách trạng thái phòng họp ra khỏi bộ nhớ.
- *Phụ thuộc những nhà cung cấp nào?* Soniox (chép lời), Anthropic (AI), Recall (bot Meet). Phần AI đã đổi
  được sang Gemini hoặc gói ChatGPT; giọng đọc có bản Piper chạy ngay trên server.
- *Kiểm thử và triển khai thế nào?* 376 test tự động; bản web cập nhật từ nhánh release, chỉ đẩy khi không có
  cuộc họp nào đang diễn ra.

**Lời Jarvis:**

Phần này dành cho anh chị bên kỹ thuật. Âm thanh đến từ mic trên trình duyệt khi họp trực tiếp, hoặc từ bot
Google Meet với mỗi người một luồng riêng, gửi qua WebSocket tới một server FastAPI chạy Docker trên Render.
Server tự tách người nói, tính vector giọng và tìm kiếm tài liệu. Chép lời dùng Soniox, phần AI dùng Claude
Sonnet năm phẩy năm qua API của công ty, dữ liệu lưu ở MongoDB Atlas của UrBox. Nhà cung cấp AI đổi được, ví
dụ sang Gemini. Mã nguồn có ba trăm bảy mươi sáu bài kiểm thử tự động.

### Slide 10. Biên bản tự nhắc khi buổi này nói khác điều đã chốt ở buổi trước

**Thông điệp:** Không ai phải tự nhớ đã chốt gì ở buổi nào.

**Anh Tân có thể hỏi:**

- *Có nhắc sai không, ai quyết định cuối cùng?* AI so các điều đã chốt và ghi lại kèm buổi họp để người đọc
  xem xét; trợ lý không tự sửa quyết định.

**Lời Jarvis:**

Em nhớ xuyên các cuộc họp trong cùng một nhóm. Khi lập biên bản, em so với những điều nhóm đã chốt ở tối đa
mười hai buổi trước: quyết định, con số, hạn chót, người phụ trách. Nếu buổi này nói khác, ví dụ ngày phát
hành voucher là hai mươi tháng mười hai trong khi buổi trước đã chốt mười lăm, em ghi rõ trong biên bản kèm
buổi họp nào. Em không ngắt lời trong lúc họp, chỉ ghi lại để mọi người xem xét.

### Slide 11. Gọi trợ lý bằng giọng nói tiếng Việt, tra cứu ngay trong buổi họp

**Thông điệp:** Không cần rời cuộc họp để tra cứu hay ghi chú.

**Anh Tân có thể hỏi:**

- *Nối được Jira thật không, cần gì?* Trợ lý gọi công cụ qua MCP; hiện dùng dữ liệu mẫu. Nối Jira thật cần
  quyền đọc Jira và một MCP server cho Jira, cần team Engineering hỗ trợ.

**Lời Jarvis:**

Trong lúc họp, anh chị gọi em bằng tiếng Việt như gọi một đồng nghiệp: Jarvis ơi, tóm tắt các quyết định từ
đầu buổi, hay liệt kê việc cần làm và người phụ trách. Em trả lời bằng giọng nói ngay, vừa làm vừa báo em đang
làm gì. Em tra được thông tin trên mạng và dữ liệu nội bộ qua chuẩn MCP. Hiện phần Jira đang dùng dữ liệu mẫu;
nối Jira thật là bước tiếp theo, cần sự hỗ trợ của team Engineering.

### Slide 12. Phần cơ bản giống các công cụ khác, kèm vài tiện ích bổ sung

**Thông điệp:** Phần cơ bản có đủ; trọng tâm vẫn là hỗ trợ trong lúc họp.

**Anh Tân có thể hỏi:**

- *Các tiện ích như slide, dashboard có cần không?* Không phải trọng tâm, có sẵn khi cần. Bộ slide đang xem là
  ví dụ: trợ lý mở tệp PowerPoint và tự trình bày theo ghi chú.

**Lời Jarvis:**

Phần còn lại là những tính năng cơ bản, giống các công cụ khác: chép lời, tóm tắt, biên bản Word, việc cần
làm, bot vào Google Meet theo lịch, lưu Google Drive. Ngoài ra em có vài tiện ích bổ sung: soạn slide, dựng
dashboard, vẽ sơ đồ tư duy bằng một câu nói, và mở tệp PowerPoint để tự trình bày, như chính bộ slide anh chị
đang xem. Đây không phải trọng tâm, nhưng có sẵn khi cần.

### Slide 13. Biết rõ dữ liệu đi đâu, ai xem được

**Thông điệp:** Luồng dữ liệu minh bạch, có đồng ý rõ ràng, phạm vi xem theo nhóm.

**Anh Tân có thể hỏi:**

- *Có cần review bảo mật trước khi dùng rộng không?* Có. Đề xuất team Engineering cùng IT, Pháp chế review
  luồng dữ liệu ra ngoài và điều khoản của Soniox, Anthropic, Recall trước khi mở rộng.

**Lời Jarvis:**

Về dữ liệu, em liệt kê rõ những gì gửi ra ngoài: âm thanh tới Soniox để chép lời, nội dung cần AI xử lý tới
Anthropic, âm thanh Google Meet qua Recall và được xóa sau một giờ, biên bản lưu ở Google Drive của chính
người dùng. Chỉ tài khoản Google của công ty đăng nhập được, mỗi cuộc họp chỉ người tạo và thành viên nhóm xem
được. Ghi âm và mẫu giọng chỉ lưu khi có đồng ý. Em đề xuất các anh chị review luồng dữ liệu này trước khi mở
rộng.

### Slide 14. Đề xuất: thử ở team Engineering và team BD bằng cuộc họp thật

**Thông điệp:** Sẵn sàng thử thật, bắt đầu nhỏ; cần Engineering đồng hành ở bảo mật, Jira và thử nghiệm.

**Anh Tân có thể hỏi:**

- *Muốn team Engineering làm gì cụ thể?* Ba việc ở cột phải: review bảo mật và hạ tầng, hỗ trợ nối Jira thật,
  cho thử ở sprint review và các buổi phỏng vấn tuyển dụng.
- *Ai bảo trì sau khi thử nghiệm?* Hiện do phuc.bh phát triển; mã nguồn có test và tài liệu, team Engineering
  có thể tham gia review từng bản.

**Lời Jarvis:**

Hiện bản web đã chạy, có ba trăm bảy mươi sáu bài kiểm thử, chế độ BD và phỏng vấn đã thử với AI thật. Những
phần chưa kiểm chứng gồm lưu Google Drive, lịch và bot Google Meet với tài khoản thật, độ chính xác nhận giọng
trên nhiều cuộc họp hơn, và khi nhiều phòng họp chạy cùng lúc. Em đề xuất thử ở team Engineering và team BD
bằng các cuộc họp thật. Từ team Engineering, em cần ba việc: review bảo mật và hạ tầng, hỗ trợ nối Jira thật,
và cho em thử ở các buổi sprint review và phỏng vấn tuyển dụng.

### Slide 15. Demo trực tiếp

**Thông điệp:** Xem tận mắt theo đúng thứ tự ưu tiên: BD, phỏng vấn, nhận giọng, rồi phần cơ bản.

**Lời Jarvis:**

Phần trình bày của em đến đây là hết. Bây giờ em mời anh Phúc demo trực tiếp, theo đúng thứ tự ưu tiên: họp
với khách trước, rồi phỏng vấn, nhận ra người nói, cuối cùng là tóm tắt và biên bản. Em cảm ơn anh chị đã lắng
nghe.
