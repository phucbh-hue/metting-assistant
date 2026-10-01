# Thư viện slide trên máy

Đặt bộ slide vào đây theo thư mục, ví dụ `slides/mega-sale/ke-hoach-q4.md`. Trong cuộc họp nói:
"Jarvis ơi, mở slide ở folder mega sale" hoặc "mở slide kế hoạch q4" thì trợ lý tìm theo tên thư mục / tên tệp,
đọc thành bộ slide và đưa lên màn hình trình chiếu (có thể thuyết trình, chuyển slide, nhắc bài như slide do AI soạn).

Định dạng đọc được:
- `.md` / `.txt`: dòng `# ` là tên bộ slide, mỗi `## ` là một slide, gạch đầu dòng `- ` là ý, đoạn văn thường là ghi chú cho người trình bày.
- `.json`: đúng cấu trúc `{"title": "...", "slides": [{"title": "...", "layout": "bullets", "bullets": ["..."], "notes": "...", "script": "..."}]}`.
- `.pptx`: lấy chữ trong từng slide và phần ghi chú (cần gói `python-pptx`).

Đổi thư mục gốc bằng biến môi trường `SLIDES_DIR`. Thư mục `demo/` là ví dụ với dữ liệu giả lập.
