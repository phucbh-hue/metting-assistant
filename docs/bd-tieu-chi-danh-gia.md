---
title: Tiêu chí đánh giá gợi ý của Bảng BD
version: 1.0
updated: 16/11/2026
owner: phuc.bh@urbox.vn (Engineering)
---

## 1. Mục đích

Bảng BD (trong phòng họp của Meeting Copilot và bảng nổi trên tab họp) gợi ý câu trả lời khi khách hỏi. Tài liệu này
cho đội BD chấm từng gợi ý theo cùng một thước đo, để Engineering biết gợi ý sai ở đâu (nguồn, hiểu câu hỏi, cách
viết, tốc độ) và sửa đúng chỗ.

Mỗi gợi ý gồm 5 phần, chấm theo đúng 5 phần đó:

| Phần | Trên bảng hiện là |
|---|---|
| Câu hỏi | Dòng "Khách hỏi" (trợ lý tự nhận ra) hoặc câu đội BD gõ / đánh dấu |
| Câu trả lời | Đoạn gợi ý để nói với khách, có mã nguồn K / M / U / W ngay sau ý dùng |
| Ghi chú cho đội mình | Những điều không nói với khách: khác biệt giữa nguồn, nên hỏi lại gì, lưu ý |
| Nguồn | K = tài liệu nhóm, M = cuộc họp trước, U = WikiBox, W = tra mạng |
| Độ chắc | cao / vừa / thấp do trợ lý tự đánh giá |

## 2. Thang điểm chung

Mỗi tiêu chí chấm 1-5. Không chấm nửa điểm. Khi lưỡng lự giữa hai mức, chọn mức thấp.

| Điểm | Nghĩa |
|---|---|
| 5 | Dùng ngay được, không sửa |
| 4 | Dùng được, sửa nhỏ (một vài chữ) |
| 3 | Dùng được một phần, phải tự bổ sung ý chính |
| 2 | Hầu như không dùng được, phải viết lại |
| 1 | Sai hoặc có hại nếu nói với khách |

## 3. Bảy tiêu chí

### C1. Đúng câu hỏi (hiểu khách muốn gì)

Câu trả lời có trả lời đúng điều khách đang hỏi không, kể cả khi khách nói vòng vo hoặc hỏi gộp hai ý.

- 5: trúng ý, đủ các vế khách hỏi.
- 3: trả lời một vế, bỏ sót vế còn lại; hoặc trả lời đúng chủ đề nhưng khác góc khách hỏi (khách hỏi phí, trả lời quy trình).
- 1: trả lời chuyện khác, hoặc trợ lý nhận nhầm câu không phải câu hỏi thành câu hỏi.

### C2. Đúng sự thật theo nguồn (không bịa)

Mọi con số, thời hạn, điều kiện, tên sản phẩm trong câu trả lời phải có trong nguồn được dẫn. Đây là tiêu chí quan trọng
nhất: một gợi ý 1 điểm ở C2 là gợi ý có hại dù các tiêu chí khác cao.

- 5: mọi ý đều đúng với nguồn, không thêm gì ngoài nguồn.
- 3: ý chính đúng, có một chi tiết suy diễn thêm hoặc làm tròn (ví dụ "khoảng 7 ngày" khi nguồn ghi 5 ngày làm việc).
- 1: nêu số liệu, chính sách, cam kết không có trong nguồn; hoặc trái với nguồn.

Khi không có nguồn phù hợp, câu trả lời đúng là nói rõ chưa có tài liệu và xin kiểm tra lại (như trợ lý vẫn làm). Trường
hợp đó chấm C2 = 5 và ghi lỗi loại "thiếu nguồn" (mục 5), không trừ điểm trợ lý vì đã không bịa.

### C3. Nguồn rõ và đúng chỗ

Mã nguồn đặt ngay sau ý dùng; bấm vào nguồn thì thấy đúng đoạn tài liệu nói về ý đó. Nguồn mới nhất được ưu tiên
(tài liệu thắng cuộc họp trước; cuộc họp gần thắng cuộc họp xa).

- 5: mỗi ý có nguồn, mở nguồn là thấy ngay đoạn tương ứng.
- 3: có nguồn nhưng chung chung (dẫn cả tài liệu, không trúng đoạn), hoặc một ý không có nguồn.
- 1: nguồn không liên quan tới ý, hoặc dùng nguồn cũ trong khi có nguồn mới hơn nói khác.

### C4. Nói được ngay với khách (độ dài và văn phong)

Người BD phải đọc được trong 5-10 giây và nói lại tự nhiên trong lúc khách đang chờ.

- 5: 2-4 câu, đi thẳng vào câu trả lời, xưng hô đúng chuẩn UrBox (bên em / anh chị), không thuật ngữ nội bộ.
- 3: đúng nhưng dài (trên 6 câu) hoặc văn viết, phải rút gọn mới nói được; hoặc có một từ nội bộ khách không hiểu.
- 1: lan man, lặp ý, hoặc giọng không phù hợp để nói với khách (ra lệnh, hứa thay công ty, chê đối thủ).

### C5. Ghi chú cho đội mình hữu ích

Phần ghi chú có giúp người BD xử lý tình huống tốt hơn không: chỉ ra chỗ nguồn khác nhau, điều cần hỏi lại khách, rủi
ro, cơ hội bán thêm.

- 5: ghi chú đúng điều người BD đang cần biết mà câu trả lời không nói.
- 3: ghi chú đúng nhưng hiển nhiên, hoặc lặp lại câu trả lời.
- 1: ghi chú sai, gây hiểu nhầm, hoặc khuyên điều trái chính sách.

Không có ghi chú trong khi tình huống rõ ràng, không có gì để lưu ý: chấm 4.

### C6. Độ chắc khớp thực tế

Trợ lý tự ghi "độ chắc: cao / vừa / thấp". Chấm xem mức đó có đúng không.

- 5: ghi "cao" và câu trả lời đúng; hoặc ghi "thấp" và quả thật nguồn mỏng.
- 3: ghi "vừa" cho mọi trường hợp, không giúp phân biệt.
- 1: ghi "cao" nhưng câu trả lời sai (nguy hiểm nhất), hoặc ghi "thấp" cho câu trả lời đúng có nguồn đầy đủ.

### C7. Kịp lúc

Tính từ lúc khách hỏi xong đến lúc gợi ý hiện trên bảng.

- 5: dưới 10 giây.
- 4: 10-20 giây.
- 3: 20-40 giây, vẫn kịp dùng.
- 2: trên 40 giây, khách đã chuyển chủ đề.
- 1: không hiện, hoặc hiện sau khi cuộc họp đã qua phần đó.

Nếu bấm "Tra thêm trên mạng" thì tính riêng thời gian của phần tra mạng (mục tiêu dưới 30 giây).

## 4. Trọng số và điểm tổng

| Tiêu chí | Trọng số |
|---|---|
| C2 Đúng sự thật theo nguồn | 30% |
| C1 Đúng câu hỏi | 20% |
| C4 Nói được ngay | 15% |
| C3 Nguồn rõ | 15% |
| C7 Kịp lúc | 10% |
| C5 Ghi chú hữu ích | 5% |
| C6 Độ chắc khớp | 5% |

Điểm tổng = tổng (điểm × trọng số), thang 5. Mục tiêu giai đoạn này: trung bình từ 4,0 trở lên, và **không có gợi ý nào
1 điểm ở C2**. Bất kỳ gợi ý nào C2 = 1 phải gửi ngay cho Engineering kèm cuộc họp và câu hỏi, không chờ tổng hợp.

## 5. Phân loại lỗi (chọn một hoặc nhiều cho gợi ý dưới 4 điểm)

Phần này quan trọng hơn điểm số, vì mỗi loại lỗi sửa ở một chỗ khác nhau.

| Mã | Lỗi | Dấu hiệu | Thường sửa ở đâu |
|---|---|---|---|
| L1 | Không nhận ra câu hỏi | Khách hỏi mà bảng không hiện gì | Bộ nhận câu hỏi từ lời nói |
| L2 | Nhận nhầm | Bảng hiện "khách hỏi" nhưng khách không hỏi | Bộ nhận câu hỏi |
| L3 | Chép lời sai | Câu hỏi trên bảng khác câu khách nói | Nhận dạng giọng nói, từ điển thuật ngữ |
| L4 | Thiếu nguồn | Bảng ghi "chưa có tài liệu" trong khi tài liệu có thật | Tài liệu chưa đưa vào nhóm / WikiBox, hoặc tìm kiếm không ra |
| L5 | Nguồn sai | Dẫn tài liệu không liên quan hoặc đã cũ | Xếp hạng tìm kiếm, ưu tiên nguồn mới |
| L6 | Bịa | Có chi tiết không có trong nguồn | Lời dặn trợ lý, kiểm tra sau khi trả lời |
| L7 | Dài / văn viết | Phải rút gọn mới nói được | Lời dặn trợ lý, giới hạn độ dài |
| L8 | Sai văn phong | Xưng hô, giọng điệu không hợp khách | Lời dặn trợ lý, mẫu câu chuẩn UrBox |
| L9 | Chậm | Trên 20 giây | Số câu hỏi xử lý song song, nguồn AI |
| L10 | Ghi chú vô ích | Ghi chú lặp hoặc sai | Lời dặn phần ghi chú |
| L11 | Độ chắc sai | "Cao" mà sai, "thấp" mà đúng | Cách tính độ chắc |

## 6. Mẫu phiếu chấm

Mỗi dòng là một gợi ý. Chép mã cuộc họp (#id trên đầu phòng họp) và câu hỏi để Engineering tra lại được. **Không chép
tên, số điện thoại, email của khách** vào phiếu; thay bằng "khách A".

| Cuộc họp | Câu hỏi (rút gọn) | C1 | C2 | C3 | C4 | C5 | C6 | C7 | Lỗi | Câu trả lời đáng ra phải là / nhận xét |
|---|---|---|---|---|---|---|---|---|---|---|
| #584 | Voucher điện tử có đổi trả được không | 5 | 5 | 4 | 4 | 3 | 5 | 4 | L10 | Ghi chú lặp lại câu trả lời |
| #584 | Phí tích hợp API là bao nhiêu | 5 | 1 | 2 | 4 | 2 | 1 | 5 | L6, L11 | Bảng giá không có trong nguồn, trợ lý vẫn nêu số và ghi độ chắc cao |
| #591 | (khách hỏi về hạn mức tín dụng, bảng không hiện) | | | | | | | 1 | L1 | Đội BD phải gõ tay |

Cột "Câu trả lời đáng ra phải là" là phần giá trị nhất: Engineering dùng chính câu này để chỉnh lời dặn và bổ sung
tài liệu.

## 7. Cách chấm cho đỡ tốn công

- Không cần chấm mọi gợi ý. Mỗi cuộc họp với khách chọn **5-8 gợi ý**: mọi gợi ý mà đội BD thấy sai hoặc không dùng,
  cộng 2-3 gợi ý đã dùng tốt (để biết cái gì đang đúng).
- Chấm ngay sau cuộc họp, khi còn nhớ khách hỏi gì. Bảng BD trong phòng họp giữ lại toàn bộ gợi ý và nguồn, mở lại
  được sau khi họp.
- Mỗi tuần gửi phiếu về Engineering một lần (Chat nhóm hoặc Drive). Riêng gợi ý C2 = 1 gửi ngay.
- Người chấm ghi thêm bối cảnh cuộc họp nếu có: khách thuộc ngành gì, họp lần mấy, nhóm nào trong Copilot. Nhóm có
  tài liệu đầy đủ chấm khác nhóm chưa có tài liệu.

## 8. Engineering làm gì với kết quả

Hàng tuần tổng hợp: điểm trung bình từng tiêu chí, tỷ lệ gợi ý dưới 4 điểm, số lỗi theo mã L1-L11, thời gian phản hồi
trung bình. Ưu tiên sửa theo thứ tự: mọi L6 (bịa) và L11 (độ chắc sai), rồi mã lỗi có số lần nhiều nhất. Mỗi lần sửa
ghi vào README bản tương ứng và báo lại đội BD để chấm lại cùng loại câu hỏi ở tuần sau.
