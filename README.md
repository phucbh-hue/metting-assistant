# UrBox Meeting Copilot

Trợ lý cuộc họp nội bộ: bóc băng thời gian thực (Soniox), **tự tách và nhận ra từng người nói** (CAM++ 192D),
AI đoán tên người nói từ hội thoại, trợ lý (tên gọi tùy chọn, mặc định Jarvis) có khuôn mặt và giọng nói,
tra cứu dữ liệu nội bộ qua MCP, soạn và trình chiếu slide theo lệnh giọng nói, nhắc bài, tự lập biên bản,
vẽ sơ đồ tư duy kiểu NotebookLM (bấm vào ý nào cũng nghe giải thích), dựng dashboard số liệu, báo cáo nhanh và phác thảo trang web, mở PDF / PowerPoint / Word trên máy để
trình bày (tự trình bày hoặc đọc theo kịch bản). Trợ lý nói bằng giọng người Việt tự nhiên của Soniox, đọc rõ cả từ tiếng
Anh, phát ngay theo thời gian thực (hoặc giọng Piper chạy trên máy, miễn phí), vừa làm vừa báo những gì tìm thấy. AI chạy bằng API key hoặc gói đăng ký Claude.ai / ChatGPT / Gemini.

- Phiên bản: 3.13.3 - cập nhật 07/10/2026 - phụ trách: phuc.bh@urbox.vn
- Dữ liệu lưu trên MongoDB Atlas (database `meeting_assistant`), tách biệt dự án phỏng vấn.

---

## 1. Có gì mới ở bản 3.13: giọng đọc tự nhiên, đọc rõ tiếng Anh

### Giọng Soniox thay cho Piper
- Lỗi cũ: giọng Piper (vais1000) chỉ đọc được tiếng Việt; từ tiếng Anh trong câu trả lời đọc theo bảng phiên âm nên rất khó
  nghe ("dashboard" thành "đát bọt", "Power BI" thành "pao ơ bi ai").
- Bây giờ trợ lý mặc định đọc bằng Soniox TTS (`tts-rt-v2`, cùng nhà cung cấp đang nhận dạng giọng nói, dùng chung
  `SONIOX_API_KEY`): giọng người Việt tự nhiên, đọc đúng từ tiếng Anh xen giữa câu, số tiền, ngày tháng, mã ticket mà không
  cần chuẩn hóa ("1.800.000.000đ", "10/2026", "URBOX-102").
- Đo ngày 04/10/2026: cho Soniox STT nghe lại các bản đọc cùng một câu Việt-Anh.
  - Piper: sai phần lớn từ tiếng Anh ("sprint review" thành "The Pain Review", "Google Cloud qua webhook" thành
    "VOOL Airline qua wire", "IT helpdesk" thành "IT Health").
  - Soniox: gần như đúng hết (giọng Mai đúng hết, Linh nhầm "sprint" thành "Spring", Huong nhầm "pull request").
- Ba giọng người Việt: **Linh** (mặc định, trẻ, nhẹ nhàng), **Mai** (miền Nam, chậm, êm), **Huong** (miền Nam, tươi sáng,
  rõ ràng). Chọn trong **Cài đặt > Giọng đọc của trợ lý**, có nút Nghe thử.

### Đọc theo thời gian thực
- Máy chủ giữ một kết nối WebSocket tới Soniox, trình duyệt phát từng mảnh âm thanh ngay khi về, không chờ đọc xong cả câu
  (trước đây phải tải trọn từng đoạn mới phát).
- Đo qua máy chủ: kết nối đang mở thì có tiếng sau 0,7-0,9 giây (mở kết nối mới thêm khoảng 0,9 giây); câu lặp lại ("Dạ,
  để em xem.") phát ngay từ bộ nhớ đệm. Khi có người gọi trợ lý, máy chủ mở sẵn kết nối trong lúc trợ lý đang nghĩ.
- Chỉ tải trước câu đang đọc và câu kế tiếp; bấm dừng hoặc ngắt lời thì hủy luôn phần đang tải.

### Chi phí, dữ liệu, dự phòng
- Khoảng 0,70 USD cho mỗi giờ giọng đọc (một câu trả lời 1 phút chưa tới 0,02 USD).
- Câu trợ lý nói được gửi tới Soniox để đọc. Muốn không gửi ra ngoài, chọn **Trên máy (Piper)** trong Cài đặt, hoặc đặt
  `TTS_ENGINE=piper`.
- Không có khóa Soniox, mất mạng hoặc Soniox lỗi trước khi có tiếng: tự đọc bằng Piper trên máy như cũ.

### Phân biệt người nói khi đã lưu mẫu giọng (bản 3.13.1)
- Lỗi thật:
  - #48: phòng 4 người nhưng chỉ ra 2 người, hồ sơ "Bùi Hồng Phúc" tự tách rồi tự gộp lại 115 lần.
  - #57: 2 người (anh Phúc, anh Duy) nhưng cả 81 câu dồn vào "Bùi Hồng Phúc".
- Nguyên nhân: mẫu giọng thu ở điều kiện khác (máy, mic, khoảng cách) giống mọi người trong phòng khoảng 0,5. Ở #57, mẫu
  giọng giống anh Phúc 0,50 và anh Duy cũng 0,50.
  - Kho chỉ có một mẫu giọng nên phép so "giống mẫu này hơn hẳn mẫu khác" luôn qua.
  - Hồ sơ mang mẫu giọng gộp mọi hồ sơ mới giống nó từ 0,45, bỏ qua "không gộp lại" và nhãn Soniox.
- Bây giờ:
  - Mẫu giọng chỉ gắn vào hồ sơ giống nó hơn hẳn mọi hồ sơ khác trong buổi họp (chênh ít nhất 0,06). Chưa có ai khác để
    so thì phải khớp rất rõ (từ 0,65).
  - Mẫu giọng không còn tự gộp hồ sơ. Hai hồ sơ thật sự là một người thì gộp theo luật chung.
- Chạy lại, có mẫu giọng: #48 ra đúng 4 người (95% câu đúng), #57 ra đúng 2 người (98%, trước là 54%).
- Bộ ngưỡng chỉnh tay chưa commit (05/10/2026) được trả về bản 3.13.0: trên #48 bộ đó chỉ ra 3 người (79%). Ngưỡng tách
  2 giọng gần nhau tăng lên 0,30 nên không tách được 2 giọng nữ.
- Cuộc họp cũ muốn áp dụng: khởi động lại `run.cmd`, mở cuộc họp, nút Tùy chọn cuộc họp > Phân tích lại người nói. Nên thu
  lại mẫu giọng trong phòng họp, bằng đúng mic hay dùng, để mẫu giọng nhận ra đúng người.

### Thuyết trình sơ đồ tư duy đi tới cả các ý xa gốc (bản 3.13.2)
- Lỗi cũ: trợ lý chỉ thuyết trình gốc và các nhánh cấp 1 ("nói gộp ý con"); các ý ở tầng 2, 3 không bao giờ được nói tới.
- Bây giờ đi theo chiều sâu: gốc, nhánh 1, rồi lần lượt các ý con của nhánh 1 (ý con có ý con thì nói tiếp xuống), xong mới
  sang nhánh 2. Ý có ý con hoặc có ghi chú là một bước riêng (sáng lên, mở nhánh, đưa vào giữa màn hình); ý lá chỉ có tên
  được nhắc trong lời của ý cha. Sơ đồ quá lớn (trên 24 ý cần nói) thì bỏ bớt tầng sâu nhất.
- Mỗi bước là lời GIẢI THÍCH theo cuộc họp (ý đó nghĩa là gì, vì sao quan trọng, ai nói, con số, hạn chót, điều còn chưa
  rõ), không đọc lại nhãn trên màn hình.
- Bản 3.13.3: AI bỏ sót ý nào thì trợ lý nhờ AI soạn riêng cho các ý đó ở lượt thứ hai. Chỉ khi AI lỗi hẳn (mất mạng, hết
  hạn mức) mới đọc ý đó theo cấu trúc (tên, ghi chú, các ý con). Bước chốt AI gắn vào gốc luôn được đọc cuối cùng.
- Chạy thật với Claude Sonnet 5.5 trên sơ đồ mẫu 5 nhánh: ý tầng 2 "Dồn quảng cáo 20-22 giờ" được giải thích kèm lý do
  (40% đơn năm ngoái rơi vào khung này), 1 lượt gọi, 0,015 USD.
- Sơ đồ đã soạn lời thuyết trình kiểu cũ (chỉ tới nhánh cấp 1) được soạn lại ở lần bấm Thuyết trình tiếp theo.

## 2. Bản 3.12: sơ đồ tư duy kiểu NotebookLM, bấm vào ý để nghe giải thích, thuyết trình sơ đồ và dashboard

### Sơ đồ tư duy kiểu NotebookLM
- "Bông ơi, vẽ sơ đồ..." mặc định ra sơ đồ tư duy: chủ đề ở gốc bên trái, các nhánh tỏa sang phải, mỗi nhánh một màu, ý
  ngắn gọn kèm nhãn trạng thái (Rủi ro, Cần làm, Đang làm, Đã xong, Ý tưởng). AI viết sẵn 1-2 câu ghi chú cho gốc và các
  nhánh chính, lấy từ nội dung cuộc họp.
- Bấm vòng tròn ở mép phải một ý để mở / thu nhánh (đang thu thì hiện số ý con). Kéo nền để di chuyển, cuộn chuột để phóng
  to, nút Vừa khung, Mở hết, Thu gọn. Bàn phím: Enter để nghe giải thích, mũi tên phải / trái để mở / thu, lên / xuống để
  sang ý bên cạnh.
- Chỉ khi nội dung cần mũi tên nối các bước có rẽ nhánh và gộp nhánh, tương tác giữa hệ thống theo thời gian (API,
  webhook), lược đồ dữ liệu, lịch Gantt (hoặc người dùng nói rõ flowchart / sequence / ERD / Gantt) thì AI mới vẽ Mermaid
  như trước. Sơ đồ cũ (Mermaid) vẫn mở được.

### Bấm vào một ý để trợ lý giải thích
- Bấm vào một ý (trên màn hình trình chiếu hoặc khung Sơ đồ bên phải): khung giải thích mở bên cạnh sơ đồ, ghi đường dẫn
  từ gốc tới ý đó, ghi chú có sẵn, rồi trợ lý giải thích 2-4 câu theo nội dung cuộc họp (ai nói, con số, hạn chót, điều
  còn chưa rõ) và đọc to. Ý đó cùng đường dẫn tới nó sáng lên. Nút Nghe lại, Hỏi thêm về ý này.
- Nói cũng được: "Bông ơi, giải thích nhánh nạp voucher", "nói rõ hơn ý banner mobile" khi đang chiếu sơ đồ. Câu chung
  chung ("giải thích vì sao doanh số giảm") mà không khớp ý nào trong sơ đồ thì trợ lý trả lời như câu hỏi thường.
- Lời giải thích được nhớ theo từng ý của từng sơ đồ: bấm lại không gọi AI lần nữa. Không có AI thì trợ lý đọc ghi chú có
  sẵn trong sơ đồ.
- Sơ đồ Mermaid cũng bấm vào từng nút để nghe giải thích được.

### Thuyết trình sơ đồ và dashboard
- Nút Thuyết trình trên màn hình trình chiếu (hoặc nói "thuyết trình sơ đồ này", "trình bày dashboard giúp anh", "đi qua
  biểu đồ"): lần đầu AI soạn lời thuyết trình theo cuộc họp (lưu thành bản mới, lần sau trình bày ngay), rồi trợ lý đi qua
  từng phần và đọc to:
  - Sơ đồ tư duy: giới thiệu tổng quan, rồi từng nhánh lớn; nhánh đang nói mở ra, các nhánh khác thu lại và mờ đi, khung
    nhìn tự đưa nhánh đó vào giữa.
  - Dashboard: các chỉ số, từng biểu đồ, rồi điểm kết luận; phần đang nói sáng viền vàng, biểu đồ vẽ lại từ đầu cho người
    xem dõi theo, phần khác mờ đi.
  - Sơ đồ Mermaid: làm sáng từng nút đang nói tới.
- "Vẽ sơ đồ quy trình rồi thuyết trình luôn", "làm dashboard doanh số rồi trình bày luôn": trợ lý tạo xong rồi trình bày
  ngay. Đang chiếu slide mà nói "thuyết trình sơ đồ" thì trợ lý mở sơ đồ mới nhất rồi trình bày.
- Không có AI thì trợ lý đọc theo cấu trúc (tên nhánh, ghi chú, giá trị cao nhất của từng biểu đồ, điểm kết luận).
- Bộ slide (bản 3.12.1): câu nhắc tới một slide thì trợ lý chỉ trình bày đúng slide đó rồi dừng, không chạy tới cuối bộ:
  "thuyết trình trong slide này", "đọc lại slide hiện tại", "trình bày lại slide 9" (chuyển tới slide 9 rồi trình bày).
  Muốn đi tiếp tới hết thì nói "thuyết trình từ slide 9", "thuyết trình từ đây đến hết", "thuyết trình cả bộ slide";
  "thuyết trình giúp anh" vẫn trình bày từ slide đang chiếu tới hết như trước. Tệp mở từ máy chưa chọn cách trình bày thì
  trợ lý vẫn hỏi trước, trả lời xong vẫn chỉ trình bày slide đã chọn.
- Chạy thử ngày 04/10/2026 với Claude Sonnet 5.5 trên một cuộc họp mẫu 7 câu: vẽ sơ đồ (19 ý, 4 nhánh) mất khoảng 19 giây,
  giải thích một ý khoảng 3 giây, soạn lời thuyết trình 6 bước khoảng 5 giây; 4 lượt gọi AI tốn 0,062 USD.

### Phân biệt người nói (bản 3.12.2)
- Lỗi thật ở #44: một người nói suốt 27 phút, Soniox chỉ có một nhãn, nhưng từ phút 15 giọng đổi dần (đổi tư thế, xa/gần
  mic) nên bị tự tách thành "Người nói 2" và không bao giờ gộp lại (22/46 câu sai người). Bây giờ khi Soniox chưa từng nghe
  thấy người thứ hai trong phiên, hệ thống chỉ tự tách khi hai giọng khác hẳn nhau. Hai giọng gần nhau (ví dụ podcast phát
  qua loa) vẫn được tách như trước khi Soniox đã nghe thấy nhiều người.
- Chạy lại trên 17 cuộc họp thật có dữ liệu giọng: #44 về đúng 1 người (46/46 câu), 16 cuộc họp còn lại không đổi; 5 cuộc
  họp có nhãn đúng giữ nguyên 93,3% câu. Cuộc họp cũ muốn áp dụng: mở cuộc họp, nút Tùy chọn cuộc họp > Phân tích lại
  người nói.

### AI tự đoán tên người nói, không cần bấm nút (bản 3.12.3)
- Lỗi cũ: phải bấm "AI đoán tên" mới ra tên. Khi ai đó được gọi tên ("Tuấn ơi, xong chưa?"), AI chạy ngay sau 2 giây
  lúc Tuấn chưa trả lời nên không biết ai là Tuấn; câu trả lời đến sau không kích hoạt lại, phải chờ 40 câu. Ngoài ra
  tên đoán đúng ở mức 75-80% vẫn chỉ là gợi ý chờ bấm Xác nhận (#38-#42: 7/7 gợi ý như vậy đều được bấm xác nhận).
- Bây giờ trợ lý tự đoán đúng lúc:
  - Có người tự giới thiệu ("tôi là Phúc", "mình tên Lan", "em là Tuấn bên DevOps"): đoán ngay.
  - Có người được gọi tên ("Tuấn ơi", "mời anh Duy Leo", "chào thầy Minh"): chờ người đó đáp lời rồi đoán. Chưa ai đáp
    sau 12 giây vẫn đoán, vì câu có thể nhắc người vừa nói ("cảm ơn anh Tuấn", "người nãy giờ nói là thầy Trung").
  - Dự phòng như cũ: cứ 40 câu mới mà người chưa có tên nói thêm thì đoán lại một lần.
- Tự đặt tên từ 70% (trước là 85%). Dưới 70%, hoặc tên đang thuộc về người khác (xác nhận sẽ gộp 2 người), mới hiện gợi ý
  chờ xác nhận. Đoán lại ra cùng tên thì cập nhật thẻ gợi ý cũ, không hiện nhiều thẻ trùng nhau như trước. Tên sai thì
  bấm vào tên để sửa. Tự lưu mẫu giọng (khi bật `AUTO_ENROLL_VOICES`) vẫn chỉ từ 85%.
- Bớt lượt gọi vô ích: chữ "giới thiệu" chung chung ("giới thiệu nội dung hôm nay") không còn bị coi là manh mối tên;
  người chỉ nói một câu ngắn (tiếng vọng, người đi ngang) không được tự đoán (bấm nút vẫn đoán).
- AI đọc đúng hơn: cuộc họp dài vẫn gửi kèm các câu giới thiệu ở đầu buổi; AI được báo tên gọi trợ lý để không nhầm
  "Thanh ơi..." là gọi người; tên có chữ "Anh" ở cuối (Lan Anh, Đức Anh) không còn bị cắt thành "Lan", "Đức".
- Chạy lại bằng AI thật (Claude Sonnet 5.5, DB giả lập) trên lời thoại 5 cuộc họp thật, không bấm nút lần nào:
  - #30: tự đặt "Đặng Thế Trung"; #40: "Bùi Hồng Phúc" và "Duy Leo" (trước chỉ là gợi ý); #41: "Bùi Hồng Phúc" và
    "Trung" (câu mang đúng tên 10 -> 38).
  - #44 không có ai xưng tên nên không đoán.
  - Tổng 26 lượt gọi, chi phí 0,34 USD.
- Không có manh mối tên trong lời nói thì AI không đoán được (ví dụ chỉ ra lệnh cho trợ lý). Muốn trợ lý luôn nhận ra
  một người, hãy thu mẫu giọng của người đó (trang Hồ sơ giọng nói > Thu mẫu giọng, khi người đó đồng ý).

## 3. Bản 3.11: cài trên máy mới bằng 3 lệnh, nhập khóa và kết nối gói ngay trên giao diện

### Cài trên máy mới
Cần sẵn: Node.js 20 trở lên, pnpm (`corepack enable pnpm` hoặc https://pnpm.io), Python 3.10 trở lên (khuyên 3.12) và git.

```powershell
git clone <repo> meeting-assistant
cd meeting-assistant
pnpm mst-urbox install   # .venv + thư viện Python + Claude Code, Codex CLI, Gemini CLI cài riêng cho dự án
pnpm mst-urbox build     # .env, model nhận diện giọng (28 MB), giọng đọc (67 MB), trình duyệt tra cứu, tự kiểm tra
pnpm mst-urbox web       # chạy ở http://127.0.0.1:8080 và mở trình duyệt
```

Mở Cài đặt (biểu tượng bánh răng), rồi:
- **Kết nối dịch vụ**: dán Soniox API key (bắt buộc để ghi âm), MongoDB Atlas nếu có (để trống thì lưu trên máy ở
  `data/local_db`).
- **Nguồn AI**: chọn một nguồn.
  - API key: dán key vào ô của Claude API hoặc Gemini API rồi bấm **Lưu key**. Key ghi vào `.env` của máy, dùng được ngay.
  - Gói đăng ký: bấm **Kết nối** ở Claude.ai, ChatGPT hoặc Gemini, đăng nhập tài khoản trên trình duyệt. Xong thì nguồn đó
    được chọn luôn.

| Lệnh | Việc làm |
|---|---|
| `pnpm mst-urbox install` | tạo `.venv`, cài `requirements.txt`, cài 3 CLI vào `node_modules` (`--skip-cli`, `--python <đường dẫn>`) |
| `pnpm mst-urbox build` | tạo `.env` (khóa để trống), tải model CAM++ (kiểm tra kích thước và SHA-256), giọng đọc Piper, Playwright Chromium (`--skip-browser`) |
| `pnpm mst-urbox web` | chạy ứng dụng, tự chọn cổng khác nếu 8080 bận, đang chạy sẵn thì chỉ mở trình duyệt (`--port`, `--no-open`) |
| `pnpm mst-urbox demo` / `test` / `doctor` | bản demo dữ liệu giả lập / bộ test offline / kiểm tra môi trường |

- Lần chạy lệnh pnpm đầu tiên tự tải 3 CLI (khoảng 300 MB). `pnpm-workspace.yaml` cho phép script cài đặt của Claude Code
  (đặt tệp chạy gốc) và chặn hai module native của Gemini CLI không cần dùng (`keytar`, `node-pty`).
- Windows: đặt dự án ở thư mục có đường dẫn ngắn (ví dụ `C:\work\meeting-assistant`), đường dẫn quá dài làm pip báo lỗi
  "tên tệp quá dài" khi cài Playwright; hoặc bật Long Paths của Windows.
- `run.cmd` vẫn dùng được như cũ (ưu tiên `.venv` của dự án nếu có).
- Đã thử ngày 03/10/2026 trên máy này: cài mới vào một môi trường Python trống, 227 test qua, `web` chạy được; tải model
  CAM++ trùng mã SHA-256 với bản đang dùng.

### Nhập khóa và kết nối gói ngay trên giao diện
- Khóa (Soniox, Anthropic, Gemini, MongoDB) ghi vào `.env` của máy đang chạy ứng dụng, không lưu lên MongoDB; giao diện chỉ
  hiện "đã có" và 4 ký tự cuối. Chỉ đổi được từ chính máy đó (127.0.0.1).
- Nút **Kết nối** chạy đúng lệnh đăng nhập của công cụ chính chủ:
  - Claude.ai: `claude auth login --claudeai`. Trên Windows mở một cửa sổ đăng nhập riêng vì Claude Code cần terminal.
  - ChatGPT: `codex login`.
  - Gemini: đăng nhập Google của Gemini CLI.
- Trình duyệt tự mở trang đăng nhập. Không mở được thì giao diện hiện đường dẫn để bấm, mã xác nhận (nếu có) và ô dán mã.
- Ứng dụng biết đăng nhập xong khi tệp đăng nhập của công cụ được ghi mới, hoặc công cụ báo thành công; tối đa 6 phút.
- Claude Code, Codex, Gemini CLI cài riêng trong `node_modules` của dự án được tìm tự động, không cần cài toàn cục.

## 4. Bản 3.10: quay lại nội dung đã trình bày, màu UrBox, linh vật linh động

### Quay lại nội dung đã trình bày
Nguyên nhân (xem lại cuộc họp #41, #42): câu "quay lại cái bộ slide mà em vừa trình bày trước đó" không khớp lệnh nào nên
được chuyển cho AI; AI không biết những gì đã chiếu và không có cách mở lại nên dựng bộ slide mới hoặc nói "em không điều
khiển được màn hình". Lịch sử trình chiếu chỉ nằm trong bộ nhớ nên chạy lại run.cmd là mất. "Slide hai cách hiểu về khai
phóng" còn bị hiểu thành slide số 2.

- Lịch sử trình chiếu lưu trong DB (`meetings.stage_log`): chạy lại server, mở lại cuộc họp vẫn quay lại được, màn hình
  trình chiếu mở lại đúng nội dung đang chiếu lần trước.
- Nhận rộng các câu mở lại (không gọi AI):

| Câu nói | Trợ lý làm |
|---|---|
| "quay lại cái bộ slide em vừa trình bày trước đó", "slide cũ", "slide lúc nãy" | bộ slide đã chiếu gần nhất (khác bộ đang chiếu) |
| "cho anh xem lại cái dashboard vừa rồi", "mở lại sơ đồ lúc nãy" | đúng loại đó, cái đã chiếu gần nhất |
| "quay lại cái phần slide điểm cộng", "quay về sơ đồ kiến trúc" | tìm theo chủ đề trong mọi bộ đã tạo (tên và nội dung từng slide, không phân biệt dấu), bản mới nhất |
| "quay lại nội dung em vừa trình bày" | bất kỳ nội dung vừa chiếu trước đó |
| "chiếu lại cái slide đầu tiên em làm" | bộ được tạo đầu tiên |
| "thuyết trình lại cái slide hồi nãy" | mở lại bộ đó rồi trình bày |
| "quay lại slide 4", "slide số bảy", "slide trước" | như cũ: chuyển slide trong bộ đang chiếu |

- Cuộc họp cũ chưa có lịch sử: mở nội dung gần nhất khác cái đang chiếu; chỉ có một bộ thì lùi về slide trước.
- Câu không rõ (ví dụ nhận dạng giọng nói chép "slide" thành "spotlight"): AI nhận thêm danh sách "Nội dung đã có" (mã,
  loại, tên, phiên bản, đã chiếu chưa, tên từng slide) và trả về lệnh mở lại đúng mục, không tạo sản phẩm mới. Mã AI đưa ra
  được kiểm tra với danh sách của cuộc họp.

### Màu thương hiệu UrBox
- Toàn bộ giao diện chuyển từ xanh lá sang màu của urbox.vn (đọc trực tiếp trên trang ngày 02/10/2026): tím chủ đạo
  #6821C2 / #8235E4 / #A426ED, vàng #FFB700, hồng #EF65C5, nền tím than.
- Nút chính dùng dải màu #842EDA đến #A426ED như nút trên urbox.vn; chữ và viền nhấn dùng tím sáng #B98AFF để đủ tương phản
  trên nền tối; trạng thái đang ghi dùng vàng; slide, biểu đồ (2 màu đầu tím và vàng), sơ đồ Mermaid cùng tông.

### Linh vật linh động hơn
- Chuyển động theo lò xo (có quán tính, nảy nhẹ, co giãn khi bật nhảy và tiếp đất), tóc nảy theo người, quầng sáng tím phía
  sau thở theo nhịp và sáng lên khi nghe / nói.
- Lúc rảnh tự làm các động tác: nhìn quanh, nhảy, vẫy tay, vươn vai, nhún nhảy, ngó nghiêng; mắt nhìn theo con trỏ chuột;
  rê chuột vào thì vẫy tay chào.
- Nghe: nghiêng về phía người nói, đưa tay lên tai, gật gù theo từng câu. Nghĩ: gãi đầu, nhịp chân, mắt nhìn lên. Nói: nhún
  theo âm lượng, hai tay luân phiên nhấn ý. Có kết quả: nhảy ăn mừng và tung hoa giấy tím vàng hồng.
- Áp dụng cho cả linh vật UrBox, hộp quà và khuôn mặt tròn; máy bật giảm chuyển động thì chỉ giữ chớp mắt và miệng.

## 5. Bản 3.9: mở tài liệu, kịch bản, gói đăng ký, chọn model

### Mở tài liệu trên máy để trình bày
- Nói "<tên gọi> ơi, mở file kế hoạch Q4 trong Downloads", "mở cái PowerPoint mega sale", "chiếu file pdf báo cáo tháng 9"
  hoặc bấm **Mở tài liệu** trên màn hình trình chiếu (có ô tìm theo tên tệp / thư mục).
- Thư mục được tìm: `slides/` của dự án, Desktop, Documents, Downloads, OneDrive và các thư mục thêm trong
  **Cài đặt > Thư mục tài liệu**. Chỉ quét tên tệp; nội dung chỉ được đọc khi mở. Chỉ mở được tệp nằm trong các thư mục này.
- Định dạng:

| Tệp | Hiển thị | Ghi chú |
|---|---|---|
| PDF | đúng hình từng trang (dựng ảnh trên máy bằng pypdfium2) | chữ của trang dùng để tự chuyển slide và viết lời |
| PowerPoint .pptx | chữ, bảng, hình lớn nhất của từng slide, ghi chú người trình bày | máy có LibreOffice thì hiện đúng thiết kế từng slide |
| PowerPoint .ppt | cần LibreOffice | hoặc lưu lại thành .pptx / .pdf |
| Word .docx | mỗi tiêu đề thành một slide, gạch đầu dòng thành ý | |
| .md / .txt / .json | như thư viện slide cũ | |

- Máy hiện chưa có PowerPoint / LibreOffice: muốn PowerPoint hiện đúng thiết kế thì lưu thêm bản PDF
  (File > Save As > PDF) rồi mở bản PDF, hoặc cài LibreOffice (tự nhận, không cần cấu hình).
- Ảnh từng trang lưu ở `data/deck_assets/` (không commit), mở lại tệp chưa sửa thì dùng lại ảnh cũ.

### Hỏi "tự trình bày hay theo kịch bản"
Mở tài liệu xong (hoặc khi được nhờ thuyết trình một tài liệu mở từ máy), trợ lý hỏi: "Anh chị muốn em tự trình bày hay
trình bày theo kịch bản ạ? Nếu theo kịch bản thì anh chị chỉ em kịch bản ở đâu để em trình bày." Tệp có ghi chú người
trình bày thì trợ lý nói luôn có bao nhiêu slide có ghi chú. Trả lời bằng nút trên thẻ hỏi hoặc bằng giọng nói, không cần
gọi tên trong 40 giây sau câu hỏi (người khác nói chuyện khác thì trợ lý bỏ qua):

| Trả lời | Trợ lý làm |
|---|---|
| "em tự trình bày đi", "không cần kịch bản" | viết lời thuyết trình chi tiết cho từng slide (chia lô 10 slide, chạy song song) rồi trình bày |
| "theo ghi chú trong file" | đọc đúng ghi chú người trình bày của từng slide |
| "kịch bản ở file kich-ban-q4 trong Downloads", nút **Chọn tệp kịch bản** | đọc tệp Word / PDF / Markdown / văn bản / PowerPoint làm kịch bản |
| nút **Dán kịch bản** | dùng văn bản dán vào |
| "để anh trình bày", nút **Để tôi trình bày** | không đọc; bật tự chuyển slide theo lời người trình bày |
| "theo kịch bản" (chưa nói ở đâu) | hỏi tiếp "kịch bản nằm ở đâu ạ?" |

- Ghép kịch bản với slide: có đánh dấu "Slide 1:", "Trang 2." thì chia theo số; số đoạn bằng số slide thì mỗi đoạn một
  slide; còn lại AI ghép từng đoạn vào slide hợp nội dung, giữ nguyên câu chữ. Slide không có lời thì đọc nội dung trên slide.
- Theo kịch bản thì trợ lý đọc đúng từng chữ, không viết lại. Mỗi cách trình bày lưu thành một phiên bản mới của bộ slide.
- Nói gộp một câu cũng được: "<tên gọi> ơi, mở file mega sale rồi trình bày theo ghi chú".

### Chọn model từ danh sách (bản 3.9.1)
- Claude API mặc định chuyển từ Opus 4.7 sang **Sonnet 5.5** (`claude-sonnet-5-5`, đổi trong `.env` ngày 02/10/2026): giá
  2 / 10 USD cho 1 triệu token vào / ra so với 5 / 25 USD của Opus 4.7, tức rẻ hơn 60% mỗi token; hai model dùng cùng
  bộ tách token. Muốn quay lại Opus thì chọn trong danh sách, không cần sửa `.env`.
- **Cài đặt > Nguồn AI**: mỗi nguồn có một danh sách model, chọn xong bấm **Lưu nguồn AI** (lưu model của cả năm nguồn
  một lần, có hiệu lực ngay, không cần chạy lại server). Mục **Khác (tự nhập)** để gõ tên model chưa có trong danh sách;
  nút tải lại để lấy danh sách mới.

| Nguồn | Danh sách lấy từ | Ví dụ |
|---|---|---|
| Claude API | API Anthropic (`models.list`, không tốn token), kèm giá từng model | Sonnet 5.5 (khuyên dùng), Opus 5.5, Fable 5.1, Haiku 4.5, Opus 4.7 |
| Gemini API | API Google (`models.list`), bỏ các model đọc giọng / tạo ảnh | Gemini 3.8 Flash, Gemini Pro Latest |
| Claude.ai qua Claude Code | bí danh của Claude Code + model thêm mà tài khoản được dùng (`~/.claude.json`) | `sonnet`, `opus`, `haiku`, Fable 5.1 (1 triệu token ngữ cảnh) |
| ChatGPT qua Codex CLI | `codex debug models` (đã đăng nhập thì là danh mục của gói ChatGPT) | GPT-6.1-Sol, GPT-6-Astra, GPT-6-Luna |
| Gemini qua Gemini CLI | bí danh + các model khai báo trong Gemini CLI đã cài | `auto`, `pro`, `flash`, Gemini 3.8 Flash |

- Chưa có API key, chưa cài CLI hoặc lỗi mạng thì dùng danh sách có sẵn (ghi rõ dưới ô chọn). Danh sách nhớ 10 phút.
- Bảng giá Claude dùng để ước tính chi phí đã cập nhật đủ các model (trang Pricing của Anthropic, lấy ngày 02/10/2026);
  tên có hậu tố ngày (`claude-haiku-4-5-20251001`) hoặc `[1m]` tính theo giá của tên gốc.

### Chạy AI bằng gói đăng ký thay cho API key
**Cài đặt > Nguồn AI** chọn một trong năm nguồn; mọi việc của trợ lý (trả lời, slide, lời thuyết trình, đoán tên, biên
bản) đi qua nguồn đã chọn:

| Nguồn | Cần | Chi phí |
|---|---|---|
| Claude API, Gemini API | API key trong `.env` | tính theo token |
| Claude.ai qua Claude Code | gói Claude Pro / Max / Team / Enterprise; đăng nhập: `claude` rồi `/login` | trong hạn mức của gói |
| ChatGPT qua Codex CLI | gói ChatGPT Plus / Pro / Business...; cài `npm install -g @openai/codex`, đăng nhập `codex login` | trong hạn mức của gói |
| Gemini qua Gemini CLI | tài khoản Google; cài `npm install -g @google/gemini-cli`, đăng nhập: `gemini` rồi Login with Google | trong hạn mức |

- Ứng dụng gọi đúng CLI chính chủ ở chế độ không tương tác (`claude -p`, `codex exec`, `gemini -p`), trong thư mục tạm trống,
  không công cụ, không MCP, không đọc cấu hình cá nhân; gỡ biến API key khỏi tiến trình con để chắc chắn dùng gói đăng ký.
- Mỗi lần gọi qua CLI chậm hơn API khoảng 2-4 giây. Hạn mức gói tính theo giờ / ngày; hết hạn mức thì báo lỗi, hoặc gọi
  tiếp bằng API key nếu bật "Gói đăng ký lỗi thì gọi tiếp bằng API key" (có tính phí, mặc định tắt).
- Model cho từng nguồn chọn trong danh sách ở Cài đặt (xem mục dưới). Nút **Kiểm tra** gửi một câu chào ngắn bằng đúng
  model đang chọn để thử đăng nhập.
- Cài đặt > Sử dụng AI ghi cả lời gọi qua gói đăng ký (số lần, token), chi phí ghi 0 USD.
- Đã thử thật ngày 02/10/2026: Claude Code 2.1.287 với gói Claude Team trả lời trong khoảng 3,6 giây. Codex CLI 0.160.0 và
  Gemini CLI 0.62.0 đã cài nhưng chưa đăng nhập nên chưa thử thật (phần đọc kết quả có test với đầu ra mẫu).
- Lưu ý điều khoản và dữ liệu (thông tin tham khảo, cần bộ phận IT / Pháp chế xác nhận trước khi dùng chính thức):
  - Gói đăng ký dành cho cá nhân người đăng ký. Dùng chung cho nhiều người hoặc chạy liên tục cả ngày nên dùng API key.
  - Gói cá nhân (Claude Free / Pro / Max, ChatGPT Plus / Pro, tài khoản Google cá nhân) có thể dùng nội dung để cải thiện
    mô hình nếu chưa tắt trong cài đặt quyền riêng tư của gói. Nội dung cuộc họp nội bộ nên đi qua gói doanh nghiệp
    (Claude Team / Enterprise, ChatGPT Business / Enterprise, Google Workspace) hoặc API key.

## 6. Bản 3.8: tối ưu chi phí, linh vật UrBox

### Tối ưu chi phí gọi AI
Phạm vi: mọi chỗ gọi Claude của ứng dụng, chạy trên Claude API của Anthropic, model `claude-opus-4-7`. Dữ liệu: nhật ký
gọi AI thật của cuộc họp #40 (Atlas, collection `llm_usage`), bảng giá và hướng dẫn tối ưu chi phí của Anthropic lấy
ngày 02/10/2026. Kiểm tra chất lượng: bộ test offline (không có bộ đánh giá chất lượng câu trả lời), nên chỉ áp dụng các
thay đổi không đánh đổi chất lượng.

Cuộc họp #40 (khoảng 30 phút thử nghiệm) tốn 2,835 USD:

| Việc | Lượt gọi | Chi phí | Tỷ lệ |
|---|---|---|---|
| Trợ lý trả lời và tra cứu | 34 | 1,326 USD | 47% |
| Đoán tên người nói (chạy nền) | 27 | 0,737 USD | 26% |
| Soạn slide | 5 | 0,576 USD | 20% |
| Dashboard, sơ đồ | 3 | 0,196 USD | 7% |

Các thay đổi đã áp dụng (đều không làm giảm chất lượng), xếp theo mức tiết kiệm ước tính trên nhật ký #40:

| Thay đổi | Loại | Tiết kiệm ước tính | Nguồn số liệu |
|---|---|---|---|
| Đoán tên chỉ chạy khi có tên người mới được nhắc ("anh Tuấn", "Tuấn ơi", "tôi là Phúc") | không đánh đổi | khoảng 22% hóa đơn | phát lại câu thoại thật: 87 lượt còn 25 trên 5 cuộc họp |
| Nhận đúng lệnh điều hướng thật ("slide gần nhất", "slide v1", "slide điểm cộng", "quay lại slide em trình bày hồi nãy", "đừng soạn nữa", "trình bày nội dung trong slide") thay vì gửi AI và soạn lại slide mới | không đánh đổi | khoảng 20% hóa đơn (4 lần soạn lại slide và 7 lượt trợ lý) | lịch sử tương tác #40 |
| Prompt cache cho trợ lý: hướng dẫn hệ thống, transcript chia khối 30 câu và kết quả tra cứu cũ được đọc lại từ cache | không đánh đổi | khoảng 16% hóa đơn (36% chi phí trợ lý) | mô phỏng trên chuỗi lượt gọi thật |
| Bỏ lượt AI đổi từ khóa khi chính trợ lý đã viết sẵn từ khóa tìm web | không đánh đổi | khoảng 1% | 9 lượt trong #40 |
| Câu gọi dở dang ("em có thể") chờ người nói tiếp thay vì gửi AI | không đánh đổi | khoảng 1% | 1 lượt trong #40 |

Bảng xếp theo mức tiết kiệm, không phải thứ tự áp dụng. Các mức trên có phần chồng nhau và là ước tính, chưa phải số đo
sau khi chạy thật. Gộp lại, một buổi như #40 ước tính tốn khoảng một nửa.

- Đo thật sau khi dùng: Cài đặt > Sử dụng AI có thêm tỷ lệ token đọc lại từ cache. Nhật ký `llm_usage` ghi riêng token
  ghi cache (1,25 lần giá) và đọc cache (0,1 lần giá), và phí 0,01 USD mỗi lượt tìm của công cụ web_search của Claude.
- Đề xuất có đánh đổi, CHƯA áp dụng (cần anh duyệt và một bộ đánh giá nhỏ khoảng 20 yêu cầu thật, ước tính 1-2 USD):
  - Chuyển `CLAUDE_MODEL` sang `claude-opus-5-5`: rẻ hơn 20% mỗi token, đọc cache 0,05 lần giá, là model Anthropic
    khuyên dùng làm điểm bắt đầu; nhưng model này luôn suy nghĩ trước khi trả lời nên số token ra có thể tăng.
  - Dùng model rẻ hơn (Sonnet 5.5) cho việc đoán tên; sau khi đã giảm số lượt, phần này còn nhỏ nên ưu tiên thấp.
  - Giảm `effort` cho các vòng trợ lý chỉ chọn công cụ.
- Không áp dụng vì không hợp với cách dùng: Batch API (mọi lời gọi đều có người đang chờ), tìm công cụ trì hoãn (danh sách
  công cụ nhỏ), nén ngữ cảnh (vòng lặp ngắn), thu nhỏ ảnh (không gửi ảnh).
- `IDENTITY_FALLBACK_SEGMENTS` (mặc định 40): không có tên mới thì cứ bấy nhiêu câu mới đoán lại tên một lần.

### Linh vật UrBox
- Hình đại diện mặc định là linh vật UrBox chính thức, vẽ từ đúng tệp `static/mascot/urbox-mascot.svg` (bản chép của
  `brandhome_mascot_first_05f635d4d4.svg`). Không vẽ lại: 53 nét gốc được gom thành bộ phận (mắt, miệng, hai tay, chỏm tóc,
  chân) để cử động. Thay tệp này bằng bản mới cùng thứ tự nét là linh vật đổi theo.
  - Chờ: nhún nhẹ, chớp mắt, đảo mắt, chỏm tóc lắc nhẹ.
  - Nghe: nghiêng người về phía người nói, đưa găng trái lên cạnh đầu, có sóng âm và vòng vàng sáng.
  - Nghĩ: đưa tay phải lên gãi đầu, mắt nhìn lên, bong bóng suy nghĩ "...", vòng vàng nét đứt xoay.
  - Nói: miệng mở khép theo âm lượng giọng đọc, tay phải đưa nhịp, người nhún theo lời.
  - Có kết quả tốt: nhảy lên và lấp lánh. Tôn trọng cài đặt giảm chuyển động của hệ điều hành.
- Thêm lựa chọn hộp quà UrBox (thiết kế theo màu urbox.vn: tím #6821C2, #8235E4, #A426ED, vàng #FFB700): nắp bật theo giọng
  nói, nơ lắc, vẫy tay khi nghe.
- Đổi qua lại giữa linh vật UrBox, hộp quà và khuôn mặt tròn: Cài đặt > Hình đại diện trợ lý.

## 7. Bản 3.7: lưu trên máy khi mất Atlas, tra cứu bằng Playwright

### Không còn mất lịch sử cuộc họp khi Atlas lỗi
- Nguyên nhân mất lịch sử: máy hiện không kết nối được MongoDB Atlas (Atlas từ chối bắt tay TLS, thường do IP của máy chưa
  có trong **Network Access** hoặc mục IP tạm thời đã hết hạn). Trước đây server lặng lẽ chuyển sang bộ nhớ tạm, nên mỗi
  lần tắt `run.cmd` là mất toàn bộ cuộc họp.
- Bây giờ khi không kết nối được Atlas, server lưu trên máy tại `data/local_db/` (ghi xuống đĩa 2 giây một lần và khi tắt,
  mỗi cuộc họp một tệp), lần chạy sau nạp lại. Mã cuộc họp lưu trên máy bắt đầu từ 9001 để không trùng với Atlas.
- Trang danh sách cuộc họp và **Cài đặt > Lưu trữ dữ liệu** hiện rõ đang lưu ở đâu, lý do không vào được Atlas và cách sửa.
- Khi Atlas kết nối lại: bấm **Đồng bộ lên Atlas** để chép các cuộc họp ghi lúc mất kết nối (câu thoại, người nói, sản
  phẩm AI, nhật ký gọi AI). Dữ liệu đang có trên Atlas không bị ghi đè; hồ sơ giọng trùng tên thì dùng hồ sơ của Atlas.
- Khắc phục Atlas: cloud.mongodb.com > project > **Network Access** > **Add IP Address** > **Add Current IP Address**,
  chờ 1-2 phút rồi chạy lại `run.cmd`.
- Giới hạn khi lưu trên máy: mẫu giọng đã lưu trên Atlas không có sẵn, nên chưa tự nhận ra tên người quen bằng giọng.
- `data/` chứa nội dung họp và vector giọng nói (dữ liệu sinh trắc học theo Nghị định 13/2023/NĐ-CP): đã đưa vào
  `.gitignore`, không chia sẻ thư mục này.

### Tra cứu trên mạng bằng trình duyệt (Playwright)
- "Search giúp anh ..." giờ mặc định dùng trình duyệt thật: AI đổi câu nói thành từ khóa ("hiện tại giá vàng đang bao
  nhiêu" thành "giá vàng hôm nay"), Chromium chạy ngầm tìm trên Bing, bỏ kết quả lạc đề, đọc 4 trang ở các nguồn khác
  nhau (kể cả bảng giá vẽ bằng JavaScript hoặc nằm trong iframe), rồi AI tóm tắt có đánh số nguồn [1], [2].
- Trình duyệt lỗi hoặc bị chặn thì tự chuyển sang công cụ web_search của Claude (`WEB_SEARCH_PROVIDER=auto`).
- Google và DuckDuckGo chặn trình duyệt tự động nên dùng Bing.
- Agent cũng có công cụ `web_search`, dùng được trong yêu cầu khác (ví dụ "so sánh giá vàng các thương hiệu rồi vẽ dashboard").
- Chạy thử riêng phần tìm kiếm: `python -m meeting.websearch "giá vàng SJC hôm nay"`.

| Cách tra cứu (đo với Opus 4.7, câu hỏi giá vàng) | Thời gian | Chi phí một lần |
|---|---|---|
| Trình duyệt Playwright + AI tóm tắt | khoảng 25 giây | khoảng 0,055 USD |
| Công cụ web_search của Claude | khoảng 36 giây | khoảng 0,18 USD |

### run.cmd
- Chạy `run.cmd` khi server cũ vẫn còn ở cửa sổ khác: hỏi dừng server cũ (Y) hay giữ server cũ và chỉ mở trình duyệt
  (N, mặc định sau 20 giây), thay vì báo lỗi trùng cổng 8080.

## 8. Bản 3.6: cổng gọi AI, xuất dữ liệu

### Tra cứu trên mạng
- "UrBox ơi, em search giúp anh coi giá vàng đang bao nhiêu": trợ lý tìm trên web (bản 3.6 dùng công cụ web_search của
  Claude, tối đa 5 lượt tìm; từ bản 3.7 mặc định dùng trình duyệt, xem mục 7), viết báo cáo "Tra cứu: ..." gồm kết luận, chi tiết, liên hệ với cuộc họp và danh sách nguồn, chiếu lên
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

## 9. Bản 3.4: trợ lý thuyết trình, nội dung bám sát, MCP data server

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

## 10. Bản 3.3: slide theo lời trình bày, ghép lệnh bị cắt

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

## 11. Bản 3.2: kết quả luôn hiện ra, dashboard, giọng đọc chạy trên máy

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

## 12. Bản 3.1: khuôn mặt, màn hình trình chiếu, tên gọi

### Trợ lý có khuôn mặt, giọng nói và màn hình trình chiếu

- Bấm biểu tượng toàn màn hình ở góc khung AI, hoặc nói "Jarvis ơi, mở màn hình trình bày", để mở màn hình
  trình chiếu: bên trái là khuôn mặt trợ lý, bên phải là nội dung đang trình bày (slide, biên bản, sơ đồ, giao diện).
- Khuôn mặt đổi theo trạng thái: **chờ**, **lắng nghe** (vòng sáng co giãn theo âm lượng mic), **suy nghĩ**
  (vòng nét đứt xoay, mắt nhìn lên), **nói** (miệng mấp máy theo lời). Nét mặt vui hoặc lo lắng theo nội dung.
- Giọng nói: từ bản 3.2 dùng giọng Piper chạy trên máy chủ (xem mục 11); giọng của trình duyệt chỉ là dự phòng.
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

## 13. Bản 3.0: sửa lỗi phân biệt người nói

| Vấn đề ở bản 2.5 | Nguyên nhân gốc | Cách sửa |
|---|---|---|
| Có 2-3 người nói nhưng chỉ hiện "Người lạ #1" | Ngưỡng tái dùng nhãn người lạ (cosine 0.46 trên centroid) thấp hơn ngưỡng gộp cụm 0.50, nên người thứ 2 luôn bị gộp vào người thứ 1. Ngưỡng 0.35 cho Host và bỏ qua kiểm tra chênh lệch khi chỉ có 1 mẫu giọng. Nhãn diarization của Soniox gần như bị bỏ qua. | Viết lại bộ phân vai (`voice.MeetingSpeakers`): kết hợp nhãn Soniox (rất chính xác trong một phiên stream) với CAM++ (nhận lại người quen, nối lại sau mất kết nối, sửa lỗi Soniox). Ngưỡng đo trên dữ liệu thật. |
| Đặt tên xong, câu mới lại hiện nhãn tạm; tên mất khi khởi động lại server | Tên gắn với chuỗi nhãn, nhãn đổi theo cụm; không lưu hồ sơ người nói theo cuộc họp; nhãn mới không ghi lại xuống DB | Mỗi người nói là một **hồ sơ ổn định** (`sid`), lưu ở collection `meeting_speakers`; mọi thay đổi ghi xuống DB theo thứ tự; phiên họp được khôi phục từ DB khi server khởi động lại |
| Timestamp sai sau khi Soniox nối lại | `tok_off` được tính nhưng không dùng | Mỗi kết nối Soniox là một "epoch" có offset riêng; audio chưa chốt được phát lại khi nối lại (không mất chữ) |
| Tốn phí Soniox khi tắt mic | Stream giữ mở vô thời hạn bằng keepalive | Tắt mic: gửi `finalize`, nghỉ quá `SONIOX_IDLE_CLOSE_S` giây thì đóng |
| Gọi LLM đoán tên sau **mỗi** câu | Không có debounce | Chỉ chạy khi có tín hiệu tên: tự giới thiệu thì đoán ngay, gọi tên người khác thì chờ người đó đáp lời (tối đa 12 giây), hoặc đủ câu mới; một lần gọi cho tất cả người chưa định danh; mỗi tên chỉ gán cho một người |
| Giao diện: 2 nút "Tạo cuộc họp", 4 nút "Thu mẫu giọng", dashboard số liệu giả | | Mỗi hành động một chỗ; trang chủ chỉ hiện số liệu thật; phòng họp 3 cột với danh sách người nói, đặt tên, gộp, sửa người nói của từng câu |
| Test ghi vector ngẫu nhiên vào MongoDB thật | Test dùng DB thật | Bộ test offline dùng DB in-memory (`MEETING_DB=mock`), không gọi API ngoài |

Khác:
- Mẫu giọng chỉ được lưu khi người dùng xác nhận (vector giọng nói là dữ liệu sinh trắc học theo Nghị định 13/2023/NĐ-CP). Bật `AUTO_ENROLL_VOICES=1` nếu muốn tự lưu khi AI đoán tên với độ tin cậy từ 85%.
- Hồ sơ giọng thu thủ công không bị dữ liệu tự học ghi đè; đặt tên trùng một hồ sơ giọng đã có nhưng giọng khác hẳn thì không trộn vector.
- Nội dung do AI sinh ra được làm sạch (DOMPurify) và chạy trong iframe sandbox cô lập.
- Kết thúc cuộc họp trả về ngay, biên bản lập ở chế độ nền.

## 14. Cách phân biệt người nói

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

## 15. Cấu trúc thư mục

```
meeting-assistant/
├── meeting/
│   ├── app.py          # FastAPI: REST + WebSocket
│   ├── live.py         # Luồng Soniox (epoch, phát lại audio, finalize) + MeetingSession
│   ├── voice.py        # CAM++ embedding + MeetingSpeakers (theo dõi người nói)
│   ├── identity.py     # IdentityEngine: AI đoán tên người nói (debounce, gợi ý xác nhận)
│   ├── llm.py          # Tên gọi trợ lý, lệnh trình chiếu, vòng tra cứu -> trả lời -> sản phẩm, câu "em tìm thấy..."
│   ├── tts.py          # Giọng đọc: Soniox TTS (luồng, mặc định) hoặc Piper trên máy + chuẩn hóa số, ngày, tiền
│   ├── follow.py       # Tự chuyển slide theo lời trình bày (so khớp lời nói với từng slide, chống nhảy)
│   ├── decks.py        # Tài liệu trên máy: tìm theo tên thư mục / tệp, đọc pdf (ảnh trang), pptx, docx, md, txt, json; chia kịch bản
│   ├── cli_llm.py      # Gọi AI bằng gói đăng ký qua CLI chính chủ: Claude Code, Codex CLI, Gemini CLI; nút Kết nối (đăng nhập)
│   ├── envfile.py      # Đọc / ghi khóa dịch vụ trong .env khi nhập trên giao diện
│   ├── mcp_client.py   # Client MCP thật (stdio / streamable-http), dùng khi đặt MCP_SERVER_CMD hoặc MCP_SERVER_URL
│   ├── skills/         # Skill cho agent: diagram, slides, report, dashboard, assistant (nối vào system prompt)
│   ├── artifacts.py    # Biên bản, báo cáo nhanh, dashboard, bộ slide, nhận xét, sơ đồ, trang web, co-design
│   ├── mcp.py          # Mock MCP: danh bạ, Jira, kiến trúc, lịch sử họp
│   ├── db.py           # MongoDB Atlas; không kết nối được thì lưu trên máy (localstore), MEETING_DB=mock: in-memory
│   ├── localstore.py   # Kho trên máy data/local_db (ghi theo từng cuộc họp) + đồng bộ lên Atlas
│   ├── websearch.py    # Tra cứu web bằng trình duyệt thật (Playwright, Bing): tìm, đọc trang, lọc phần liên quan
│   └── index.html      # Giao diện web (SPA), gồm màn hình trình chiếu và khuôn mặt trợ lý
├── mcp_server/         # MCP data server "urbox-meeting-data" + kho tri thức mẫu (kb/*.md)
├── data/local_db/      # Dữ liệu lưu trên máy khi mất kết nối Atlas (không commit, có vector giọng)
├── data/deck_assets/   # Ảnh từng trang PDF / slide đã dựng khi mở tài liệu (không commit)
├── static/mascot/      # Linh vật UrBox chính thức (SVG) dùng làm hình đại diện trợ lý
├── tests/              # Test offline (unittest), không gọi API ngoài
├── package.json        # pnpm mst-urbox install | build | web; Claude Code, Codex, Gemini CLI là phụ thuộc tùy chọn
├── pnpm-workspace.yaml # cho phép / chặn script cài đặt của các gói phụ thuộc
├── scripts/
│   ├── mst-urbox.mjs               # Bộ lệnh cài và chạy trên máy mới (Node.js, không cần thư viện ngoài)
│   ├── demo_replay.py              # Server demo phát lại cuộc họp mẫu (có slide + dashboard mẫu), không cần mic
│   ├── download_tts.py             # Tải giọng đọc tiếng Việt vào models/tts/
│   └── integration/                # Kiểm thử với Soniox / LLM thật (tính phí)
├── models/speaker.onnx # Model CAM++ (không commit, ~28 MB)
├── models/tts/         # Giọng đọc Piper (không commit, ~67 MB)
└── static/
```

## 16. Chạy

Máy mới: `pnpm mst-urbox install`, `pnpm mst-urbox build`, `pnpm mst-urbox web` (xem mục 3).
Máy đã cài: bấm đúp **`run.cmd`** (hoặc gõ `run` trong terminal) -> server chạy ở http://127.0.0.1:8080 và tự mở
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

## 17. Cấu hình

Xem `.env.example`. Các biến quan trọng:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `SONIOX_API_KEY` | | Bắt buộc để ghi âm |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | | Trợ lý AI, đoán tên, biên bản |
| `CLAUDE_MODEL` / `AGENT_MODEL` | `claude-sonnet-5-5` / `gemini-3.6-flash` | Model mặc định của Claude API / Gemini API; chọn trong Cài đặt được ưu tiên |
| `MONGODB_URL` | | Không có thì dùng MongoDB local, cuối cùng là in-memory |
| `MEETING_DB` | | `mock` = luôn dùng in-memory (test/demo); `local` = bỏ qua Atlas, lưu trên máy (làm việc offline) |
| `LOCAL_DB_DIR` | `data/local_db` | Thư mục lưu trên máy khi không kết nối được Atlas |
| `WEB_SEARCH_PROVIDER` | `auto` | `auto` = trình duyệt Playwright, lỗi thì Claude web_search; `playwright`; `claude` |
| `WEB_SEARCH_PAGES` | `4` | Số trang kết quả trình duyệt mở đọc mỗi lần tra cứu |
| `AUTO_ENROLL_VOICES` | `0` | `1` = tự lưu mẫu giọng khi AI đoán tên từ 85% |
| `SONIOX_IDLE_CLOSE_S` | `30` | Tắt mic quá ngần này giây thì đóng stream |
| `SONIOX_LANGUAGE_HINTS` | `vi,en` | Gợi ý ngôn ngữ cho Soniox |
| `IDENTITY_MIN_INTERVAL_S` | `20` | Khoảng cách tối thiểu giữa 2 lần AI đoán tên |
| `IDENTITY_AUTO_APPLY_T` | `0.70` | AI đoán tên từ mức này thì tự đặt tên, thấp hơn thì chỉ gợi ý chờ xác nhận |
| `IDENTITY_FALLBACK_SEGMENTS` | `40` | Không có tên người mới được nhắc thì cứ bấy nhiêu câu mới đoán lại tên một lần |
| `TTS_ENGINE` | `auto` | `auto` = Soniox nếu có `SONIOX_API_KEY`, `soniox`, `piper` (trên máy); đổi được trong Cài đặt |
| `TTS_SONIOX_VOICE` | `Linh` | Giọng Soniox: `Linh`, `Mai`, `Huong` |
| `SONIOX_TTS_MODEL` | `tts-rt-v2` | Model giọng đọc của Soniox |
| `TTS_VOICE` | `vits-piper-vi_VN-vais1000-medium` | Giọng Piper trong `models/tts/` |
| `TTS_SPEED` | `1.05` | Tốc độ đọc (Piper) |
| `TTS_MODEL_DIR` | | Đường dẫn thư mục giọng đọc nếu để ngoài `models/tts/` |
| `SLIDES_DIR` | `slides/` | Thư viện slide trên máy cho lệnh "mở slide ở folder ..." |
| `LLM_PROVIDER` | `claude` | `claude`, `gemini` (API key) hoặc `claude-cli`, `codex-cli`, `gemini-cli` (gói đăng ký); chọn trong Cài đặt được ưu tiên |
| `LLM_API_FALLBACK` | `0` | `1` = gói đăng ký lỗi / hết hạn mức thì gọi tiếp bằng API key |
| `CLAUDE_CLI_MODEL` / `CODEX_MODEL` / `GEMINI_CLI_MODEL` | `sonnet` / mặc định gói | Model cho từng gói đăng ký (đổi được trong Cài đặt) |
| `CLAUDE_CLI_EFFORT` / `CODEX_EFFORT` | `medium` | Mức suy nghĩ khi gọi qua CLI |
| `CLI_LLM_TIMEOUT` / `CLI_LLM_CONCURRENCY` | `240` / `3` | Giây chờ tối đa mỗi lần gọi CLI / số lời gọi CLI cùng lúc |
| `LIBRARY_DIRS` | | Thư mục tài liệu thêm, cách nhau bằng `;` (Windows); thêm được trong Cài đặt |
| `LIBRARY_DEFAULTS` | `1` | `0` = không tìm trong Desktop / Documents / Downloads / OneDrive |
| `DECK_MAX_PAGES` | `60` | Số trang tối đa đọc từ một tệp |
| `DECK_ASSETS_DIR` | `data/deck_assets` | Nơi lưu ảnh từng trang đã dựng |
| `MCP_SERVER_CMD` / `MCP_SERVER_URL` | | Trỏ trợ lý tới MCP server thật (stdio hoặc http); không đặt thì dùng dữ liệu tại chỗ |

## 18. API chính

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
| GET | `/api/storage` | Đang lưu ở Atlas hay trên máy, lý do, các cuộc họp trên máy chưa đồng bộ |
| POST | `/api/storage/sync` | Đồng bộ cuộc họp ghi lúc mất kết nối lên Atlas (`{"dry_run": true}` để chạy thử) |
| GET | `/api/meetings/{id}/export` | Xuất toàn bộ dữ liệu cuộc họp (kèm vector giọng) dạng JSON |
| GET / POST | `/api/tts/status`, `/api/tts` | Trạng thái giọng đọc / đọc văn bản thành WAV (Piper trên máy) |
| POST | `/api/tts/stream` | Đọc văn bản, trả PCM 16-bit từng mảnh (header `X-Sample-Rate`, `X-Tts-Engine`) |
| GET/PUT | `/api/settings/tts` | Nguồn giọng đọc (auto / soniox / piper) và giọng Soniox |
| POST | `/api/meetings/{id}/command` | Câu lệnh cho trợ lý, xử lý như khi gọi bằng giọng nói; trả về danh sách sự kiện |
| POST | `/api/meetings/{id}/stage` | Điều khiển màn hình trình chiếu: `show`, `next`, `prev`, `goto`, `topic`, `back`, `follow` (kèm `follow: true/false`) |
| POST | `/api/meetings/{id}/insights` | Trợ lý nêu nhận xét về cuộc họp |
| POST | `/api/meetings/{id}/co-design` | Sửa một sản phẩm AI (slide đang chiếu được ưu tiên) |
| GET/PUT | `/api/settings/assistant` | Tên gọi trợ lý và các tên gọi khác |
| GET | `/api/library?q=` | Tệp PDF / PowerPoint / Word / Markdown trong các thư mục tài liệu |
| GET/PUT | `/api/settings/library` | Thư mục tài liệu (`{"dirs": [...]}`), có LibreOffice hay không |
| POST | `/api/meetings/{id}/open-file` | Mở một tệp trong thư mục tài liệu lên màn hình trình chiếu, rồi hỏi cách trình bày |
| POST | `/api/meetings/{id}/explain` | Giải thích một ý của sơ đồ (`artifact_id`, `node` hoặc `label`), đọc to và hiện bên cạnh sơ đồ |
| POST | `/api/meetings/{id}/present` | Thuyết trình sơ đồ / dashboard đang chiếu: soạn lời nếu chưa có rồi trình bày từng phần |
| POST | `/api/meetings/{id}/present-mode` | Trả lời cách trình bày: `{"mode": "auto" \| "human" \| "script", "source": "notes" \| "file" \| "text", "path", "text"}` |
| GET | `/api/deck-assets/{key}/{name}` | Ảnh từng trang của tài liệu đã mở |
| GET | `/api/llm/providers` | Các nguồn AI: đã cài / đã đăng nhập / model |
| PUT | `/api/llm/provider` | Chọn nguồn AI (`provider`, `api_fallback`, `model`) |
| POST | `/api/llm/test` | Gửi một câu ngắn qua nguồn AI để kiểm tra (`provider`, `model`) |
| GET/PUT | `/api/settings/secrets` | Khóa dịch vụ trong .env: xem đã có chưa / lưu (`name`, `value`), chỉ từ 127.0.0.1 |
| POST/GET/DELETE | `/api/llm/connect/{provider}` | Kết nối gói đăng ký: bắt đầu đăng nhập / xem tiến trình / hủy; `POST .../code` để dán mã |
| GET | `/api/llm/models?provider=` | Danh sách model của một nguồn (`refresh=true` để lấy lại); `PUT /api/llm/provider` nhận `models: {nguồn: model}` |
| POST | `/api/meetings/{id}/inferences/{iid}/accept` \| `dismiss` | Xác nhận / bỏ qua gợi ý của AI |
| GET/POST/PATCH/DELETE | `/api/voices` | Hồ sơ giọng nói; `POST /api/voices/enroll-audio` để thu mẫu |
| WS | `/ws/meeting/{id}/audio` | PCM16 16kHz mono từ mic (một thiết bị ghi mỗi cuộc họp) |
| WS | `/ws/meeting/{id}/events` | Sự kiện: `segment`, `segments_relabeled`, `speakers`, `speakers_split`, `interim`, `identity_suggestion`, `ai_progress`, `ai_say`, `ai_insights`, `stage_state`, `stage_command`, `stage_prompt`... |

## 19. Lưu ý dữ liệu

- `data/local_db/` (lưu trên máy khi mất kết nối Atlas) chứa nội dung cuộc họp và vector giọng nói: không chia sẻ, không đưa
  lên git (đã có trong `.gitignore`). Xóa thư mục này khi chưa đồng bộ sẽ mất các cuộc họp trong đó.
- Mở tài liệu: chỉ quét tên tệp trong các thư mục tài liệu; nội dung tệp chỉ được đọc khi người dùng mở, và được gửi cho
  nguồn AI đang chọn khi nhờ trợ lý tự viết lời hoặc ghép kịch bản. Ảnh trang lưu ở `data/deck_assets/` trên máy.
- Gói đăng ký (Claude.ai, ChatGPT, Gemini): nội dung cuộc họp đi theo điều khoản của gói đó, không phải điều khoản API.
  Xem lưu ý ở mục 5 trước khi dùng cho cuộc họp có nội dung nhạy cảm.
- Tra cứu trên mạng gửi từ khóa tìm kiếm tới Bing và mở các trang kết quả; không đưa nội dung cuộc họp vào từ khóa.
  Nội dung trang đã đọc (cùng tối đa 2.000 ký tự cuối của cuộc họp để liên hệ) được gửi cho AI để tóm tắt.

- Bộ test của bản 2.5 ghi vector **ngẫu nhiên** vào DB thật (hồ sơ có `consent_by` = `system_admin` hoặc
  `meeting_host`, 1-2 mẫu). Nếu từng chạy các test đó, hãy rà trang **Hồ sơ giọng nói**, xóa hồ sơ không đúng
  rồi thu mẫu lại bằng **Thu mẫu giọng**.
- Cuộc họp tạo từ bản cũ vẫn mở được: hồ sơ người nói được dựng lại từ nhãn cũ ở lần mở đầu tiên.
- Ảnh `static/wealth-bg.webp` (ảnh chụp một thiết kế của bên thứ ba) không còn được giao diện sử dụng.
- Giọng đọc mặc định (bản 3.13) là Soniox: câu trợ lý nói được gửi tới Soniox để đọc. Cuộc họp có nội dung nhạy cảm
  chưa được phê duyệt gửi ra ngoài thì chọn **Trên máy (Piper)** trong Cài đặt.
- Giọng đọc Piper chạy ngay trên máy chủ, nội dung không gửi ra ngoài. Dữ liệu huấn luyện giọng vais1000:
  VAIS-1000 Vietnamese Speech Synthesis Corpus, giấy phép CC BY 4.0.
- Giọng nói dự phòng của trình duyệt mặc định dùng giọng cài trên máy, không gửi dữ liệu ra ngoài. Giọng "Online (Natural)" của
  Microsoft Edge gửi nội dung câu trả lời tới dịch vụ đọc của Microsoft nên chỉ dùng khi người dùng bấm cho phép;
  không bật cho cuộc họp có nội dung nhạy cảm khi chưa được phê duyệt.
