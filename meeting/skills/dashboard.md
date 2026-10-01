# Skill: Dashboard số liệu có ý nghĩa

Dashboard trả lời một câu hỏi của cuộc họp bằng số: tiến độ đến đâu, cái gì quá hạn, phân bổ ra sao.

## Chọn số liệu
1. Ưu tiên số liệu NGHIỆP VỤ được nói trong cuộc họp hoặc có trong dữ liệu tra cứu: ticket theo trạng thái, việc theo
   người phụ trách, hạn chót, ngân sách, doanh số, độ trễ hệ thống.
2. Thống kê thời lượng phát biểu chỉ đưa vào khi người dùng hỏi đúng về việc đó ("ai nói nhiều", "thời gian nói").
3. Không có số thật thì KPI bỏ, không ước lượng. Biểu đồ minh họa phải đặt "sample": true.

## Chọn biểu đồ theo câu hỏi
- So sánh giữa các nhóm (người, trạng thái, đội): `bar` hoặc `hbar` (nhãn dài dùng hbar).
- Xu hướng theo thời gian: `line` / `area`, trục thời gian đúng thứ tự.
- Tỉ trọng vài phần của một tổng (tối đa 6 phần): `donut`.
- Hai chiều (trạng thái theo người): `stacked_bar`.
- Danh sách cần xem từng dòng (ticket quá hạn, việc cần làm): `table`.
Không vẽ hai biểu đồ cùng nói một điều. 2-4 KPI, 2-4 biểu đồ, 1 bảng là đủ.

## Viết nhãn
- Tiêu đề biểu đồ nói ra kết luận khi có thể: "URBOX-102 và URBOX-103 đã quá hạn" hơn là "Ticket theo trạng thái".
- highlights: 1-3 câu kết luận cụ thể (tên, số, hạn), là điều người xem cần nhớ.
- Tiếng Việt, tiền 1.000.000đ, ngày dd/mm/yyyy, không dùng gạch dài.
