# UrBox Meeting Copilot

Trợ lý cuộc họp nội bộ: bóc băng thời gian thực (Soniox), **tự tách và nhận ra từng người nói** (CAM++ 192D),
AI đoán tên người nói từ hội thoại, trợ lý (tên gọi tùy chọn, mặc định Jarvis) có khuôn mặt và giọng nói,
tra cứu dữ liệu nội bộ qua MCP, soạn và trình chiếu slide theo lệnh giọng nói, nhắc bài, tự lập biên bản,
vẽ sơ đồ, dựng dashboard số liệu, báo cáo nhanh và phác thảo trang web. Trợ lý nói bằng giọng tiếng Việt chạy
ngay trên máy (miễn phí), vừa làm vừa báo những gì tìm thấy.

- Phiên bản: 3.6 - cập nhật 01/10/2026 - phụ trách: phuc.bh@urbox.vn
- Dữ liệu lưu trên MongoDB Atlas (database `meeting_assistant`), tách biệt dự án phỏng vấn.

---

## 1. Có gì mới ở bản 3.6

### Tra cứu trên mạng
- "UrBox ơi, em search giúp anh coi giá vàng đang bao nhiêu": trợ lý tìm trên web (công cụ web_search của Claude, tối đa
  5 lượt tìm), viết báo cáo "Tra cứu: ..." gồm kết luận, chi tiết, liên hệ với cuộc họp và danh sách nguồn, chiếu lên
  màn hình, đọc kết luận. Yêu cầu và thời điểm tra cứu được ghi ở cuối báo cáo. Cần `ANTHROPIC_API_KEY`.
- Một lần tra cứu tốn khoảng 25.000-30.000 token vào (kết quả web tính vào input), tức khoảng 0,15-0,20 USD với Opus 4.7.

### Cổng gọi AI: đếm lần gọi và token
- Mọi lời gọi LLM (Claude, Gemini) đi qua một cổng ghi lại: cuộc họp nào, việc gì (slide, báo cáo, dashboard, sơ đồ,
  biên bản, nhận xét, đoán tên, tra cứu web, trợ lý...), token vào/ra, thời gian, lỗi, chi phí ước tính theo giá niêm yết.
- Xem ở **Cài đặt** (biểu tượng bánh răng) mục "Sử dụng AI": tổng, theo từng cuộc họp, theo loại việc. API: `GET /api/usage`.
  Dữ liệu lưu ở collection `llm_usage`.

### Xuất dữ liệu phân tích
- Menu "..." của cuộc họp -> **Xuất dữ liệu phân tích (JSON)** (`GET /api/meetings/{id}/export`): toàn bộ câu, vector giọng,
  hồ sơ người nói, sản phẩm AI, nhật ký gọi AI. Dùng khi server đang chạy DB tạm (in-memory) để không mất dữ liệu, hoặc để
  gửi cho người phân tích lỗi nhận diện người nói.

## 2. Bản 3.4: trợ lý thuyết trình, nội dung bám sát, MCP data server

### Trợ lý tự thuyết trình
- "Thanh ơi, thuyết trình giúp anh" / "present slide" / "tự chuyển slide và nói nội dung bên trong": trợ lý đọc nội dung
  từng slide (tiêu đề, các ý, ghi chú) rồi tự chuyển slide tiếp, nghỉ một nhịp giữa hai slide. Nút **Thuyết trình**
  trên màn hình trình chiếu làm việc tương tự.
- Dừng: nói "dừng thuyết trình" / "dừng lại", bấm **Dừng thuyết trình**, phím Esc, hoặc tự bấm chuyển slide.
  Trong lúc trợ lý đang nói, mic gửi khoảng lặng nên lệnh bằng giọng chỉ được nghe ở quãng nghỉ giữa hai slide.

### Slide chi tiết, thư viện slide trên máy, quay lại nội dung cũ
- Bộ slide do AI soạn giờ 8-14 slide, mỗi slide 3-6 ý chi tiết (tên người, số liệu, hạn chót) kèm `script`: lời
  thuyết trình 4-8 câu mà trợ lý đọc to khi thuyết trình, đúng trọng tâm, không đọc sơ sài. Bộ slide cũ hoặc nhập từ
  tệp chưa có `script` thì trợ lý tự soạn trước khi thuyết trình (lưu thành bản mới).
- "Jarvis ơi, mở slide ở folder mega sale": tìm trong thư viện `slides/` (đổi bằng `SLIDES_DIR`) theo tên thư mục /
  tên tệp, đọc `.md`, `.txt`, `.json`, `.pptx` thành bộ slide và đưa lên màn hình. Xem `slides/README.md`.
- "Quay lại slide cũ khi nãy em đã present", "quay lại dashboard lúc nãy", "quay lại sơ đồ lúc nãy": về đúng nội dung
  đã trình bày gần nhất của loại đó (vị trí slide được giữ).
- Thuyết trình cũng dùng được cho dashboard (đọc KPI và điểm chính).

### Nội dung bám sát cuộc họp
- Slide, báo cáo, dashboard, sơ đồ đọc TOÀN BỘ transcript (trước đây chỉ 40 câu cuối) và không còn đưa thống kê
  "ai nói nhiều, ai nói ít" vào trừ khi được hỏi đúng điều đó.
- Bộ skill cho agent trong `meeting/skills/` (sơ đồ, slide, báo cáo, dashboard, cách làm việc của trợ lý) được nối
  vào system prompt của từng loại sản phẩm. Muốn trợ lý làm khác đi, sửa file skill, không cần sửa code.
- Sơ đồ: chọn loại theo câu hỏi cần trả lời (quy trình, trình tự gọi, trạng thái, kế hoạch), 5-12 nút, nhãn lấy từ
  cuộc họp, tô màu có nghĩa; mã Mermaid được kiểm tra cú pháp cơ bản và tự sửa một lần nếu hỏng.

### MCP data server thật (chuẩn bị cho kho tri thức)
- `mcp_server/`: server MCP "urbox-meeting-data" (stdio hoặc streamable-http) với 5 tool dữ liệu cũ và 2 tool kho tri
  thức `search_knowledge`, `read_document` trên tài liệu markdown trong `mcp_server/kb/` (6 tài liệu giả lập).
- Trợ lý gọi qua MCP khi đặt `MCP_SERVER_CMD` hoặc `MCP_SERVER_URL` trong `.env`; không đặt thì dùng dữ liệu tại chỗ.
  Kiểm tra: `python scripts/mcp_check.py` (stdio) và `python scripts/mcp_check.py --http`. Chi tiết: `mcp_server/README.md`.
- Sau này nối KB thật: thay `mcp_server/kb_search.py` bằng vector store, giữ nguyên hợp đồng 2 tool.

### Lời gọi và người nói
- Lời gọi bị Soniox cắt giữa chừng ("Thanh ơi, em hãy" | "tổng kết lại...") được chờ nói nốt rồi xử lý một lần.
- Người khác chen ngang nói dài dưới cùng nhãn Soniox: câu dài khác hẳn giọng hồ sơ đang nói (hồ sơ phải "chặt")
  thành người nói mới ngay, không bị gộp vào người đang nói; câu ngắn theo người vừa được nhận ra bằng giọng.
- Model trợ lý mặc định đổi sang `claude-opus-4-7` (`CLAUDE_MODEL` trong `.env`); muốn nhanh và rẻ hơn dùng `claude-sonnet-5-5`.

## 3. Bản 3.3: slide theo lời trình bày, ghép lệnh bị cắt

### Slide tự chuyển theo lời trình bày

- Đang chiếu bộ slide mà người trình bày nói sang ý của slide khác thì slide tự chuyển theo, trợ lý hiện phụ đề
  "Theo lời trình bày: slide 3 - ..." (không đọc to để không cắt lời). Ý đang được nói tới trên slide được tô sáng,
  các ý khác mờ đi.
- Cách so: lời vừa nói so với tiêu đề, các ý và ghi chú của từng slide; từ chỉ có ở một slide được tính nặng hơn,
  cụm hai từ ("ngân sách", "tiến độ") nặng hơn từ đơn.
- Chống nhảy lung tung: phải khớp rõ hơn slide hiện tại mới chuyển; sang slide kế tiếp thì chuyển ngay, nhảy xa hoặc
  lùi lại phải khớp hai câu liên tiếp; chờ 6 giây sau mỗi lần đổi slide và 12 giây sau khi người dùng tự chuyển.
- Slide vừa tự chuyển mà người trình bày nói "chuyển slide" trong 10 giây thì trợ lý hiểu là đúng slide đó, không
  nhảy thêm.
- Nút **Tự chuyển: bật/tắt** trên màn hình trình chiếu, hoặc nói "bật / tắt tự chuyển slide".

### Lời gọi trợ lý bị cắt giữa chừng

- Lỗi thật ở #37: Soniox cắt "Thanh ơi, em hãy" | "tổng kết lại các cuộc gọi... báo cáo nhanh cho anh", trợ lý làm
  theo "em hãy" nên ra báo cáo không đúng yêu cầu. Bây giờ câu gọi chưa có dấu kết câu thì trợ lý chờ người đó nói
  nốt (hết chữ tạm 1,2 giây), ghép lại rồi mới xử lý một lần. Câu gọi trọn vẹn vẫn xử lý ngay.

### Phân biệt người nói

- Câu quá ngắn chưa đủ tính giọng ("Ồ, trời ơi", "Ok em.", "Chuyển slide.") theo người vừa được xác định bằng giọng gần
  nhất của cùng nhãn Soniox, không theo tổng phiếu cả buổi; câu có giọng xác định được người nói thì các câu ngắn cùng
  nhãn ngay trước đó theo luôn. Soniox hay dùng chung một nhãn cho người dẫn podcast, khách mời và người trong phòng.
- Đo trên 5 cuộc họp thật có nhãn đúng gần đúng: 87,0% -> 89,2% câu (#37: 91% -> 100%, #36: 83% -> 86%), các
  cuộc họp khác không đổi.

## 4. Bản 3.2: kết quả luôn hiện ra, dashboard, giọng đọc chạy trên máy

### Trợ lý trả lời có nội dung thật và luôn hiện kết quả

- Lỗi cũ: hướng dẫn hệ thống cho AI gọi công cụ bằng dòng `TOOL_CALL` nhưng mã không thực thi. Gặp yêu cầu cần
  dữ liệu (ví dụ "vẽ chart số liệu"), AI chỉ trả về đúng dòng đó, nên trợ lý báo "Em đã tiếp nhận yêu cầu và xử lý
  xong ạ!" mà không hiện gì (3/4 lần gọi ở cuộc họp #31).
- Bây giờ trợ lý thực sự tra cứu (tối đa 2 lượt), đọc kết quả rồi trả lời bằng số liệu, tên người, hạn chót cụ thể.
  Câu chung chung bị thay bằng nội dung thật; tạo sản phẩm lỗi thì nói rõ lỗi.
- Sản phẩm mới, tự mở trên màn hình trình chiếu khi xong:
  - **Dashboard** kiểu Power BI: thẻ KPI, biểu đồ cột, cột ngang, cột chồng, đường, vùng, tròn, bảng. Mỗi biểu đồ có
    nút "Xem bảng"; số liệu minh họa có nhãn riêng. Sửa bằng lời: "đổi biểu đồ cột thành đường".
  - **Báo cáo nhanh**: tóm tắt, gạch đầu dòng, bảng việc cần làm (tab **Báo cáo**, chung với biên bản).
- Số liệu thật: dashboard dùng dữ liệu tra cứu (Jira được tổng hợp sẵn theo trạng thái, người phụ trách, quá hạn) và
  **thống kê cuộc họp** tính tự động (thời lượng nói, số câu, nhịp trao đổi theo phút). KPI không có số thật thì bỏ.

### Vừa làm vừa báo, như một trợ lý ngồi trong phòng họp

- Đáp ngay khi được gọi: "Dạ, em dựng dashboard ngay, anh chị chờ em chút nhé."
- Có dữ liệu là nói ngay điều tìm thấy, tính trực tiếp từ dữ liệu: "Em tìm thấy 6 ticket: 3 đang làm, 2 chưa làm,
  1 chờ review. URBOX-102 của Lê Văn Tuấn đã quá hạn từ 30/09/2026."
- Trong lúc dựng sản phẩm thì nêu nhận xét (rủi ro, việc chưa có người nhận, điểm cần cải thiện); quá 20 giây chưa
  xong thì báo "Sắp xong rồi ạ". Có kết quả rồi thì bỏ các câu "em đang làm..." còn chờ đọc.
- Sản phẩm lâu (dashboard, slide, trang web, sơ đồ) được dựng song song với lời đáp: yêu cầu vẽ chart ở #31 từ khoảng
  43 giây còn khoảng 27 giây (đo với Claude, có 2 lần tra cứu Jira).
- Trợ lý xưng "em", gọi "anh chị".

### Giọng đọc tiếng Việt chạy trên máy, miễn phí

- Giọng Piper (vais1000, 22 kHz) chạy qua sherpa-onnx có sẵn trong dự án: không cần API, không gửi nội dung ra
  ngoài, nhanh (khoảng 0,07 giây cho 1 giây âm thanh).
- Tải giọng một lần: `python scripts/download_tts.py` (khoảng 67 MB vào `models/tts/`, không đưa lên git).
- Tự đọc đúng số tiền, ngày, giờ, phần trăm, mã ticket và một số từ tiếng Anh hay dùng (dashboard, slide, ticket...).
- Miệng khuôn mặt cử động theo âm lượng thật của giọng đọc; câu sau được tải trước trong lúc đọc câu trước.
- Chưa có giọng trên máy chủ thì dùng giọng của trình duyệt, cuối cùng là phụ đề.

### Phân biệt người nói

- Tiêu chí tách mới: so từng câu với tâm cụm của chính nó và của cụm kia. Tâm cụm lớn bị "làm mượt" nên so tâm-tâm
  đánh giá quá cao độ giống. Đo trên 3 cuộc họp thật (anh nói trực tiếp + podcast phát qua loa): đúng 82% -> 87% câu
  (#32: 72% -> 100%), không đổi kết quả ở #15, #26, #30.
- Hồ sơ vừa được tách không tự gộp lại, tránh vòng lặp tách-gộp.
- Menu của từng câu: **Từ câu này trở đi là người khác**. Dùng khi hai giọng quá giống nhau bị gộp chung, ví dụ #31:
  người trong video nấu ăn và khách mời podcast cùng phát qua loa giống nhau 0,76 (ngang mức cùng một người).
  Mô phỏng trên #31: bấm một lần ở câu đầu của khách mời thì đúng 95% câu.
- Giới hạn: giọng phát qua loa máy tính bị kênh âm thanh làm giống nhau; họp trực tiếp với người thật tách tốt hơn.

## 5. Bản 3.1: khuôn mặt, màn hình trình chiếu, tên gọi

### Trợ lý có khuôn mặt, giọng nói và màn hình trình chiếu

- Bấm biểu tượng toàn màn hình ở góc khung AI, hoặc nói "Jarvis ơi, mở màn hình trình bày", để mở màn hình
  trình chiếu: bên trái là khuôn mặt trợ lý, bên phải là nội dung đang trình bày (slide, biên bản, sơ đồ, giao diện).
- Khuôn mặt đổi theo trạng thái: **chờ**, **lắng nghe** (vòng sáng co giãn theo âm lượng mic), **suy nghĩ**
  (vòng nét đứt xoay, mắt nhìn lên), **nói** (miệng mấp máy theo lời). Nét mặt vui hoặc lo lắng theo nội dung.
- Giọng nói: từ bản 3.2 dùng giọng Piper chạy trên máy chủ (xem mục 4); giọng của trình duyệt chỉ là dự phòng.
  Giọng trực tuyến "Online (Natural)" của Microsoft Edge chỉ được dùng khi người dùng bấm cho phép (xem mục Lưu ý
  dữ liệu). Nút "Giọng nói" hoặc phím V để bật/tắt; bấm vào khuôn mặt để trợ lý dừng nói.
- Khi trợ lý đang nói, mic gửi khoảng lặng thay cho âm thanh để giọng trợ lý không lọt vào transcript
  (mốc thời gian vẫn khớp). Lời xác nhận chuyển slide chỉ hiện phụ đề để không cắt lời người trình bày.
- Trong lúc chờ xử lý (soạn slide, lập biên bản cuối buổi), trợ lý nêu nhận xét: rủi ro, việc chưa có người nhận,
  điểm cần cải thiện, điểm tốt. Nút **Nhận xét nhanh** để trợ lý xem lại cả cuộc họp bất kỳ lúc nào.

### Bộ slide và điều khiển bằng lời

"Jarvis ơi, làm slide báo cáo tiến độ sprint" -> trợ lý soạn bộ slide (5 kiểu bố cục: tiêu đề, gạch đầu dòng,
hai cột, trích dẫn, số liệu; kèm ghi chú cho người trình bày) rồi đưa lên màn hình. Các lệnh trình chiếu dưới đây
chạy ngay, không gọi LLM:

| Nói sau tên gọi (hoặc gõ vào ô lệnh) | Kết quả |
|---|---|
| "chuyển slide", "slide tiếp theo", "tiếp đi" | Sang slide sau |
| "quay lại slide trước" | Về slide trước |
| "mở slide số 3", "mở slide về ngân sách" | Nhảy tới slide theo số thứ tự hoặc chủ đề |
| "quay lại phần trình bày lúc nãy" | Về nội dung trình bày trước đó, ví dụ từ biên bản về bộ slide |
| "nhắc bài", "ý tiếp theo" | Mở khung nhắc bài và đọc ý tiếp theo chưa nói |
| "mở màn hình trình bày", "thu nhỏ màn hình trình bày" | Mở hoặc thu nhỏ màn hình trình chiếu |
| "bật tự chuyển slide", "tắt tự chuyển slide" | Bật/tắt slide tự chuyển theo lời trình bày (mặc định bật) |
| "thuyết trình giúp anh", "present slide", "dừng thuyết trình" | Trợ lý tự đọc từng slide và chuyển tiếp / dừng |
| "mở slide ở folder mega sale" | Mở bộ slide từ thư viện trên máy (`slides/`) |
| "search giúp anh giá vàng hôm nay", "tra cứu trên mạng tỷ giá USD" | Tìm trên web, báo cáo có nguồn, chiếu lên màn hình |
| "quay lại slide cũ khi nãy em đã present", "quay lại dashboard lúc nãy" | Về nội dung đã trình bày gần nhất của loại đó |
| "nhận xét nhanh về cuộc họp" | Trợ lý xem lại cuộc họp và nêu nhận xét |
| "sửa slide này thêm số liệu doanh thu" | Sửa đúng slide đang chiếu (có gọi LLM), lưu thành phiên bản mới |

- Khung nhắc bài đánh dấu ý đã nói dựa trên transcript từ lúc slide được chiếu và tô sáng ý tiếp theo.
- Phím tắt trên màn hình trình chiếu: mũi tên trái/phải (chuyển slide), N (nhắc bài), V (giọng nói),
  F (toàn màn hình), Esc (thu nhỏ).
- Câu gõ trong ô chat đi cùng luồng với câu gọi bằng giọng nói (`POST /api/meetings/{id}/command`).

### Đặt tên gọi cho trợ lý

Biểu tượng bánh răng ở thanh bên trái -> đổi tên gọi (mặc định "Jarvis") và các tên gọi khác. Cách gọi hợp lệ:
"Bông ơi, ...", "Bông, ...", "Bông tóm tắt giúp anh", hoặc gọi chung "trợ lý ơi". Gọi tên rồi ngừng thì trợ lý
chờ câu yêu cầu tiếp theo trong 8 giây. Câu chỉ nhắc tới tên (ví dụ "bông hoa đẹp quá") không đánh thức trợ lý.

### Phân biệt người nói

- Tự phát hiện khi Soniox gán chung một nhãn cho hai người (thường gặp khi có người thứ ba vào giữa buổi):
  tách hồ sơ theo giọng (CAM++), báo trên giao diện, có thể gộp lại nếu tách sai.
- Menu "..." của cuộc họp -> **Phân tích lại người nói**: chạy lại bộ phân vai trên toàn bộ câu đã lưu.
  Tên đã đặt được giữ cho người nói xuất hiện sớm nhất trong nhóm.

## 6. Bản 3.0: sửa lỗi phân biệt người nói

| Vấn đề ở bản 2.5 | Nguyên nhân gốc | Cách sửa |
|---|---|---|
| Có 2-3 người nói nhưng chỉ hiện "Người lạ #1" | Ngưỡng tái dùng nhãn người lạ (cosine 0.46 trên centroid) thấp hơn ngưỡng gộp cụm 0.50, nên người thứ 2 luôn bị gộp vào người thứ 1. Ngưỡng 0.35 cho Host và bỏ qua kiểm tra chênh lệch khi chỉ có 1 mẫu giọng. Nhãn diarization của Soniox gần như bị bỏ qua. | Viết lại bộ phân vai (`voice.MeetingSpeakers`): kết hợp nhãn Soniox (rất chính xác trong một phiên stream) với CAM++ (nhận lại người quen, nối lại sau mất kết nối, sửa lỗi Soniox). Ngưỡng đo trên dữ liệu thật. |
| Đặt tên xong, câu mới lại hiện nhãn tạm; tên mất khi khởi động lại server | Tên gắn với chuỗi nhãn, nhãn đổi theo cụm; không lưu hồ sơ người nói theo cuộc họp; nhãn mới không ghi lại xuống DB | Mỗi người nói là một **hồ sơ ổn định** (`sid`), lưu ở collection `meeting_speakers`; mọi thay đổi ghi xuống DB theo thứ tự; phiên họp được khôi phục từ DB khi server khởi động lại |
| Timestamp sai sau khi Soniox nối lại | `tok_off` được tính nhưng không dùng | Mỗi kết nối Soniox là một "epoch" có offset riêng; audio chưa chốt được phát lại khi nối lại (không mất chữ) |
| Tốn phí Soniox khi tắt mic | Stream giữ mở vô thời hạn bằng keepalive | Tắt mic: gửi `finalize`, nghỉ quá `SONIOX_IDLE_CLOSE_S` giây thì đóng |
| Gọi LLM đoán tên sau **mỗi** câu | Không có debounce | Chỉ chạy khi có tín hiệu tên ("em là...", "... ơi", lời chào) hoặc đủ câu mới; một lần gọi cho tất cả người chưa định danh; mỗi tên chỉ gán cho một người |
| Giao diện: 2 nút "Tạo cuộc họp", 4 nút "Thu mẫu giọng", dashboard số liệu giả | | Mỗi hành động một chỗ; trang chủ chỉ hiện số liệu thật; phòng họp 3 cột với danh sách người nói, đặt tên, gộp, sửa người nói của từng câu |
| Test ghi vector ngẫu nhiên vào MongoDB thật | Test dùng DB thật | Bộ test offline dùng DB in-memory (`MEETING_DB=mock`), không gọi API ngoài |

Khác:
- Mẫu giọng chỉ được lưu khi người dùng xác nhận (vector giọng nói là dữ liệu sinh trắc học theo Nghị định 13/2023/NĐ-CP). Bật `AUTO_ENROLL_VOICES=1` nếu muốn tự lưu khi AI đoán tên với độ tin cậy từ 85%.
- Hồ sơ giọng thu thủ công không bị dữ liệu tự học ghi đè; đặt tên trùng một hồ sơ giọng đã có nhưng giọng khác hẳn thì không trộn vector.
- Nội dung do AI sinh ra được làm sạch (DOMPurify) và chạy trong iframe sandbox cô lập.
- Kết thúc cuộc họp trả về ngay, biên bản lập ở chế độ nền.

## 7. Cách phân biệt người nói

```
Soniox (nhãn người nói theo phiên)  ─┐
                                     ├─> MeetingSpeakers ─> hồ sơ "Người nói N" (sid ổn định)
CAM++ (vector giọng 192D mỗi câu)  ──┘          │               │
                                                │               ├─ khớp mẫu giọng đã lưu -> tên thật
                                                │               ├─ người dùng đặt tên / AI đoán tên
                                                │               └─ lưu meeting_speakers + segments
```

1. Nhãn Soniox đã gắn với một hồ sơ trong phiên -> câu thuộc hồ sơ đó. CAM++ chỉ ghi đè khi rất chắc chắn.
2. Nhãn Soniox mới -> Soniox cho rằng đây là người khác những người đang có nhãn: so giọng với các hồ sơ còn lại, không đủ giống thì tạo **người nói mới ngay câu đầu tiên**.
3. Sau khi Soniox nối lại (nhãn đánh số lại từ đầu), nhận lại từng người bằng giọng.
4. Câu quá ngắn ("dạ", "ừ") không đủ tính vector: theo nhãn Soniox.
5. Hai hồ sơ có giọng trùng khớp mạnh thì tự gộp; người dùng có thể gộp, tách, đổi người nói của từng câu.
6. Một nhãn Soniox chứa hai giọng khác hẳn nhau (mỗi nhóm từ 3 câu, 6 giây trở lên) thì tách thành hai hồ sơ.

Ngưỡng được đo trên dữ liệu thật của model CAM++: cùng người câu ngắn 0.3-0.6, câu dài 0.6-0.87; khác người 0.0-0.4.

## 8. Cấu trúc thư mục

```
meeting-assistant/
├── meeting/
│   ├── app.py          # FastAPI: REST + WebSocket
│   ├── live.py         # Luồng Soniox (epoch, phát lại audio, finalize) + MeetingSession
│   ├── voice.py        # CAM++ embedding + MeetingSpeakers (theo dõi người nói)
│   ├── identity.py     # IdentityEngine: AI đoán tên người nói (debounce, gợi ý xác nhận)
│   ├── llm.py          # Tên gọi trợ lý, lệnh trình chiếu, vòng tra cứu -> trả lời -> sản phẩm, câu "em tìm thấy..."
│   ├── tts.py          # Giọng đọc tiếng Việt chạy trên máy (Piper qua sherpa-onnx) + chuẩn hóa số, ngày, tiền
│   ├── follow.py       # Tự chuyển slide theo lời trình bày (so khớp lời nói với từng slide, chống nhảy)
│   ├── decks.py        # Thư viện slide trên máy: tìm theo tên thư mục / tệp, đọc md, txt, json, pptx
│   ├── mcp_client.py   # Client MCP thật (stdio / streamable-http), dùng khi đặt MCP_SERVER_CMD hoặc MCP_SERVER_URL
│   ├── skills/         # Skill cho agent: diagram, slides, report, dashboard, assistant (nối vào system prompt)
│   ├── artifacts.py    # Biên bản, báo cáo nhanh, dashboard, bộ slide, nhận xét, sơ đồ, trang web, co-design
│   ├── mcp.py          # Mock MCP: danh bạ, Jira, kiến trúc, lịch sử họp
│   ├── db.py           # MongoDB (hoặc in-memory khi MEETING_DB=mock)
│   └── index.html      # Giao diện web (SPA), gồm màn hình trình chiếu và khuôn mặt trợ lý
├── mcp_server/         # MCP data server "urbox-meeting-data" + kho tri thức mẫu (kb/*.md)
├── tests/              # Test offline (unittest), không gọi API ngoài
├── scripts/
│   ├── demo_replay.py              # Server demo phát lại cuộc họp mẫu (có slide + dashboard mẫu), không cần mic
│   ├── download_tts.py             # Tải giọng đọc tiếng Việt vào models/tts/
│   └── integration/                # Kiểm thử với Soniox / LLM thật (tính phí)
├── models/speaker.onnx # Model CAM++ (không commit, ~28 MB)
├── models/tts/         # Giọng đọc Piper (không commit, ~67 MB)
└── static/
```

## 9. Chạy

Cách nhanh nhất: bấm đúp **`run.cmd`** (hoặc gõ `run` trong terminal) -> server chạy ở http://127.0.0.1:8080 và tự mở
trình duyệt; lần đầu tự tải giọng đọc tiếng Việt nếu chưa có. `run.cmd demo` chạy bản demo dữ liệu giả lập ở cổng 8090.
Chạy tay:

```powershell
cd "C:\work\cralwer with ai\ASR\meeting-assistant"
$py = "C:\work\cralwer with ai\ASR\interviewer-assistant-AI-circle\.venv\Scripts\python.exe"

# Giọng đọc tiếng Việt chạy trên máy (một lần)
& $py scripts/download_tts.py

# Server
& $py -m uvicorn meeting.app:app --host 127.0.0.1 --port 8080

# Demo không cần mic (DB in-memory, giọng tổng hợp, có sẵn bộ slide mẫu): mở http://127.0.0.1:8090
& $py scripts/demo_replay.py --port 8090

# Test offline (~15 giây)
& $py -m unittest discover -s tests -t . -v

# Kiểm thử tích hợp (gọi Soniox / LLM thật, có tính phí, DB vẫn in-memory)
& $py scripts/integration/soniox_wav_replay.py youtube_nam_huong.wav --expect-speakers 2
& $py scripts/integration/llm_identity_check.py
& $py scripts/integration/agent_check.py      # yêu cầu vẽ chart / báo cáo phải ra sản phẩm có nội dung
```

Luồng sử dụng: **Tạo cuộc họp** (góc trên bên phải) -> **Bật mic** -> người nói mới tự xuất hiện ở cột trái với
màu riêng -> bấm **Đặt tên** (hoặc xác nhận gợi ý của AI, tick "Lưu mẫu giọng" nếu người đó đồng ý) ->
**Kết thúc cuộc họp** để lập biên bản. Sau khi kết thúc vẫn đặt tên, gộp người nói và sửa từng câu được.

Trình bày: nhờ trợ lý soạn slide (hoặc chọn tab **Slide**) -> bấm biểu tượng toàn màn hình ở khung AI ->
nói "<tên gọi> ơi, chuyển slide", "nhắc bài"... Cần giọng Tiếng Việt cài trên Windows để trợ lý nói thành tiếng.

## 10. Cấu hình

Xem `.env.example`. Các biến quan trọng:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `SONIOX_API_KEY` | | Bắt buộc để ghi âm |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | | Trợ lý AI, đoán tên, biên bản |
| `MONGODB_URL` | | Không có thì dùng MongoDB local, cuối cùng là in-memory |
| `MEETING_DB` | | `mock` = luôn dùng in-memory (test/demo) |
| `AUTO_ENROLL_VOICES` | `0` | `1` = tự lưu mẫu giọng khi AI đoán tên từ 85% |
| `SONIOX_IDLE_CLOSE_S` | `30` | Tắt mic quá ngần này giây thì đóng stream |
| `SONIOX_LANGUAGE_HINTS` | `vi,en` | Gợi ý ngôn ngữ cho Soniox |
| `IDENTITY_MIN_INTERVAL_S` | `20` | Khoảng cách tối thiểu giữa 2 lần AI đoán tên |
| `TTS_VOICE` | `vits-piper-vi_VN-vais1000-medium` | Giọng đọc trong `models/tts/` |
| `TTS_SPEED` | `1.05` | Tốc độ đọc |
| `TTS_MODEL_DIR` | | Đường dẫn thư mục giọng đọc nếu để ngoài `models/tts/` |
| `SLIDES_DIR` | `slides/` | Thư viện slide trên máy cho lệnh "mở slide ở folder ..." |
| `MCP_SERVER_CMD` / `MCP_SERVER_URL` | | Trỏ trợ lý tới MCP server thật (stdio hoặc http); không đặt thì dùng dữ liệu tại chỗ |

## 11. API chính

| Method | Đường dẫn | Mô tả |
|---|---|---|
| GET/POST | `/api/meetings` | Danh sách (kèm thống kê) / tạo cuộc họp |
| GET/PUT/DELETE | `/api/meetings/{id}` | Chi tiết (segments, speakers, gợi ý AI, artifacts) / sửa / xóa |
| POST | `/api/meetings/{id}/archive` | Kết thúc + lập biên bản nền |
| POST | `/api/meetings/{id}/speakers/{sid}/rename` | Đặt tên người nói (`save_voice` để lưu mẫu giọng) |
| POST | `/api/meetings/{id}/speakers/{sid}/merge` | Gộp vào người nói khác |
| POST | `/api/meetings/{id}/speakers/{sid}/save-voice` | Lưu mẫu giọng của người nói |
| POST | `/api/meetings/{id}/segments/{seq}/speaker` | Đổi người nói của một câu (`target_sid: null` = người mới) |
| POST | `/api/meetings/{id}/infer-speakers` | AI đoán tên ngay |
| POST | `/api/meetings/{id}/reanalyze` | Phân tích lại người nói trên toàn bộ câu đã lưu |
| POST | `/api/meetings/{id}/segments/{seq}/split-after` | Từ câu này trở đi là người nói mới |
| GET | `/api/usage` | Thống kê gọi AI: lần gọi, token vào/ra, chi phí ước tính theo cuộc họp / việc / model |
| GET | `/api/meetings/{id}/export` | Xuất toàn bộ dữ liệu cuộc họp (kèm vector giọng) dạng JSON |
| GET / POST | `/api/tts/status`, `/api/tts` | Trạng thái giọng đọc / đọc văn bản thành WAV (chạy trên máy) |
| POST | `/api/meetings/{id}/command` | Câu lệnh cho trợ lý, xử lý như khi gọi bằng giọng nói; trả về danh sách sự kiện |
| POST | `/api/meetings/{id}/stage` | Điều khiển màn hình trình chiếu: `show`, `next`, `prev`, `goto`, `topic`, `back`, `follow` (kèm `follow: true/false`) |
| POST | `/api/meetings/{id}/insights` | Trợ lý nêu nhận xét về cuộc họp |
| POST | `/api/meetings/{id}/co-design` | Sửa một sản phẩm AI (slide đang chiếu được ưu tiên) |
| GET/PUT | `/api/settings/assistant` | Tên gọi trợ lý và các tên gọi khác |
| POST | `/api/meetings/{id}/inferences/{iid}/accept` \| `dismiss` | Xác nhận / bỏ qua gợi ý của AI |
| GET/POST/PATCH/DELETE | `/api/voices` | Hồ sơ giọng nói; `POST /api/voices/enroll-audio` để thu mẫu |
| WS | `/ws/meeting/{id}/audio` | PCM16 16kHz mono từ mic (một thiết bị ghi mỗi cuộc họp) |
| WS | `/ws/meeting/{id}/events` | Sự kiện: `segment`, `segments_relabeled`, `speakers`, `speakers_split`, `interim`, `identity_suggestion`, `ai_progress`, `ai_say`, `ai_insights`, `stage_state`, `stage_command`, `stage_prompt`... |

## 12. Lưu ý dữ liệu

- Bộ test của bản 2.5 ghi vector **ngẫu nhiên** vào DB thật (hồ sơ có `consent_by` = `system_admin` hoặc
  `meeting_host`, 1-2 mẫu). Nếu từng chạy các test đó, hãy rà trang **Hồ sơ giọng nói**, xóa hồ sơ không đúng
  rồi thu mẫu lại bằng **Thu mẫu giọng**.
- Cuộc họp tạo từ bản cũ vẫn mở được: hồ sơ người nói được dựng lại từ nhãn cũ ở lần mở đầu tiên.
- Ảnh `static/wealth-bg.webp` (ảnh chụp một thiết kế của bên thứ ba) không còn được giao diện sử dụng.
- Giọng đọc Piper chạy ngay trên máy chủ, nội dung không gửi ra ngoài. Dữ liệu huấn luyện giọng vais1000:
  VAIS-1000 Vietnamese Speech Synthesis Corpus, giấy phép CC BY 4.0.
- Giọng nói dự phòng của trình duyệt mặc định dùng giọng cài trên máy, không gửi dữ liệu ra ngoài. Giọng "Online (Natural)" của
  Microsoft Edge gửi nội dung câu trả lời tới dịch vụ đọc của Microsoft nên chỉ dùng khi người dùng bấm cho phép;
  không bật cho cuộc họp có nội dung nhạy cảm khi chưa được phê duyệt.
