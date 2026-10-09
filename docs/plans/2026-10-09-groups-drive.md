# Nhóm cuộc họp và lưu biên bản lên Google Drive - kế hoạch triển khai

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Giai đoạn 1 trong `docs/plans/2026-10-09-groups-drive-design.md`:
- nhóm cuộc họp có chia sẻ;
- kết nối Google Drive cho từng người;
- tự lưu biên bản (Google Docs) và ghi âm (MP3) vào thư mục nhóm trên Drive của chủ nhóm, kèm lưu về thư mục trên máy.

**Architecture:**
- Quyền nhóm nằm ở `meeting/groups.py` và được dùng chung bởi API, middleware quyền xem cuộc họp và việc lưu Drive.
- Google OAuth (authorization code + refresh token mã hóa AES-GCM) nằm ở `meeting/google_oauth.py`.
- Gọi Drive REST bằng httpx ở `meeting/gdrive.py`.
- Xuất biên bản (Markdown sang HTML/DOCX) và ghi âm (PCM sang MP3) ở `meeting/recap_export.py`.
- Việc lưu chạy nền, có thử lại, nằm ở `meeting/drive_sync.py`, được gọi sau khi biên bản xong.
- Trình duyệt ghi file về máy bằng File System Access API (`readwrite`).

**Tech Stack:**
- FastAPI, httpx (đã có), cryptography (AES-GCM, đã có), python-docx (đã có), soundfile/libsndfile 1.2 (MP3, đã có ở
  máy dev, thêm vào requirements).
- mongomock cho test, httpx.MockTransport để giả Google.

**Chạy test:**
`PYTHONIOENCODING=utf-8 MEETING_DB=mock "<venv>/python" -m unittest discover -s tests -t .`
(`<venv>` = `C:\work\cralwer with ai\ASR\interviewer-assistant-AI-circle\.venv\Scripts`)

**Quy ước:**
- Tiếng Việt trong chú thích và thông báo, dùng "-" không dùng gạch dài.
- Mỗi task một commit, push `main` (không đẩy `release` cho tới task cuối).

---

### Task 1: Dữ liệu và quyền của nhóm

**Files:**
- Create: `meeting/groups.py`
- Modify: `meeting/db.py`
  - `LOCAL_ID_BASE` thêm `"groups": 9000`.
  - `init()` thêm index `meeting_groups.id` (unique), `meeting_groups.members`.
  - `MEETING_EDITABLE` không thêm `group_id`: `group_id` chỉ đổi qua API riêng.
- Test: `tests/test_groups.py`

**Hàm** (đồng bộ, gọi qua `asyncio.to_thread` ở app):
- `create_group(name, owner) -> dict`
  - Tên được làm sạch, độ dài 1-80.
  - `members=[]`, `drive={}`, `recording_drive_days=None` (None nghĩa là theo `RECORDING_RETENTION_DAYS`).
- `get_group(gid)`
- `list_groups_for(email)`: các nhóm có `owner == email` hoặc `email in members`. Chạy trên máy (không đăng nhập) thì
  trả về mọi nhóm.
- `role_of(group, email) -> "owner" | "member" | None`
  - Chạy trên máy thì luôn là `"owner"`.
- `update_group(gid, name=None, members=None, recording_drive_days=None)`
  - `members` được chuẩn hóa: chữ thường, bỏ trùng, bỏ email của chủ nhóm, mỗi email phải `auth.domain_ok`. Sai thì
    `ValueError` kèm email sai.
- `delete_group(gid)`: `$unset group_id` khỏi các cuộc họp của nhóm, rồi xóa nhóm.
- `set_meeting_group(mid, gid | None)`
- `group_meeting_ids(gid)`
- Bộ nhớ đệm `_CACHE` (gid sang group), xóa khi có thay đổi. `tests/helpers.reset_db` gọi `groups._CACHE.clear()`.

**Test (viết trước, chạy thấy FAIL rồi mới làm):**
- Tạo nhóm, liệt kê theo chủ và theo thành viên, người ngoài không thấy.
- `role_of` đúng ba trường hợp.
- `update_group` từ chối email `@gmail.com` và bỏ email của chủ nhóm ra khỏi `members`.
- `delete_group` làm cuộc họp mất `group_id` nhưng không xóa cuộc họp.

### Task 2: Quyền xem cuộc họp tính cả nhóm

**Files:**
- Modify: `meeting/auth.py` (`can_access(user, meeting, group=None)`)
- Modify: `meeting/app.py`:
  - `_OWNERS` thành `_MEETING_KEYS` (mid sang `(owner, group_id)`), xóa khi chuyển nhóm.
  - `MeetingAccessMiddleware` thêm `/api/groups/{gid}`: không có vai trò trong nhóm thì 404.
  - `get_meetings` và `stats` dùng điều kiện "của mình hoặc thuộc nhóm mình".
- Modify: `meeting/db.py`: `owner_filter(email, include_unowned, group_ids=())` thêm `{"group_id": {"$in": group_ids}}`.
- Test: `tests/test_groups.py` (API), sửa `tests/test_web_library.py` nếu cần.

**Luật:**
- Được xem nếu là người tạo; hoặc có vai trò trong nhóm của cuộc họp; hoặc (cuộc họp chưa có chủ và không thuộc
  nhóm) là quản trị viên.

**Test:**
- Thành viên nhóm xem được cuộc họp do chủ nhóm tạo trong nhóm, cả qua API lẫn WebSocket.
- Người ngoài nhận 404.
- Gỡ cuộc họp khỏi nhóm thì thành viên mất quyền ngay (bộ nhớ đệm được xóa).
- Danh sách và thống kê của thành viên gồm cả cuộc họp trong nhóm.

### Task 3: API nhóm

**Files:**
- Modify: `meeting/app.py`
- Test: `tests/test_groups.py`

**API:**
- `GET /api/groups`: mỗi nhóm kèm `role`, `meeting_count`, `members`, `drive` (trạng thái).
- `POST /api/groups {name}`
- `GET /api/groups/{gid}`
- `PATCH /api/groups/{gid} {name?, members?, recording_drive_days?}`
  - Chỉ chủ nhóm, nếu không thì 403.
  - Đổi thành viên thì lên lịch đồng bộ quyền chia sẻ Drive (Task 8).
- `DELETE /api/groups/{gid}`: chỉ chủ nhóm.
- `PUT /api/meetings/{mid}/group {group_id | null}`:
  - Người gọi phải có quyền xem cuộc họp.
  - Người gọi phải là người tạo cuộc họp, hoặc chủ của nhóm hiện tại.
  - Người gọi phải có vai trò trong nhóm đích.
- `POST /api/meetings` nhận thêm `group_id`: phải có vai trò trong nhóm.
- `GET /api/meetings?group_id=` để lọc.

**Test:**
- Thành viên không đổi được tên nhóm (403).
- Thành viên tạo được cuộc họp trong nhóm.
- Người ngoài không chuyển được cuộc họp vào nhóm mình không thuộc.
- Xóa nhóm thì cuộc họp vẫn còn.

### Task 4: Giao diện nhóm

**Files:**
- Modify: `meeting/index.html`

**Nội dung:**
- Route `#/g/<gid>`: danh sách cuộc họp của nhóm. `#/` là tất cả.
- Trang chủ có cột trái (từ cỡ md trở lên) gồm "Tất cả", các nhóm (kèm số cuộc họp, nhãn "được chia sẻ" nếu
  không phải chủ) và nút "Tạo nhóm". Màn hình nhỏ dùng `<select>`.
- Đầu trang nhóm: tên, số thành viên, nút "Tạo cuộc họp trong nhóm", nút "Cài đặt nhóm" (chỉ chủ nhóm).
- Hộp Cài đặt nhóm:
  - đổi tên;
  - thành viên (thêm theo email, bỏ);
  - số ngày giữ ghi âm trên Drive;
  - trạng thái thư mục Drive (Task 9);
  - thư mục trên máy (Task 10);
  - xóa nhóm.
- Hộp tạo cuộc họp có ô chọn nhóm (mặc định là nhóm đang xem).
- Menu dòng cuộc họp có thêm "Chuyển vào nhóm..." (danh sách nhóm và "Không thuộc nhóm").
- Kiểm tra: `node --check` khối script, chạy Playwright với 2 token (chủ nhóm, thành viên).

### Task 5: Giờ Việt Nam trên server

**Files:**
- Modify: `Dockerfile` (`ENV TZ=ICT-7`, chuỗi POSIX nên không cần tzdata)
- Modify: `render.yaml` (`TZ=ICT-7`)
- Modify: `deploy/oracle/setup.sh` (`TZ` trong `prod.env`)
- Tên thư mục cuộc họp tính theo giờ +7 riêng trong `recap_export.folder_name` (không phụ thuộc TZ của máy).

**Lý do:** biên bản dùng `time.localtime`, nên trên Render (UTC) giờ bị lệch 7 tiếng.

### Task 6: Xuất biên bản và ghi âm

**Files:**
- Create: `meeting/recap_export.py`
- Modify: `requirements.txt` (thêm `soundfile>=0.12`, ghi rõ `httpx`, `cryptography`)
- Test: `tests/test_recap_export.py`

**Hàm:**
- `md_blocks(md)`: tách Markdown của biên bản thành các khối:
  - tiêu đề `#`, `##`, `###`;
  - đoạn văn;
  - gạch đầu dòng `-` / `*`, danh sách số `1.`;
  - bảng `|...|` (bỏ dòng `|---|`);
  - **đậm** trong dòng.
- `md_to_html(md, title) -> str`: HTML đầy đủ có CSS bảng đơn giản, để Drive chuyển thành Google Docs.
- `md_to_docx_bytes(md) -> bytes` (python-docx: Heading 1-3, List Bullet, List Number, bảng `Table Grid`, đậm).
- `recording_mp3(mid) -> Optional[Path]`: nối các lượt ghi PCM của `recording.runs(mid)` theo thứ tự, ghi MP3 mono
  16 kHz bằng `soundfile` vào tệp tạm. Không có ghi âm thì trả về `None`.
- `folder_name(meeting) -> "dd-mm-yyyy HHhMM - Tiêu đề"`: giờ +7, bỏ ký tự cấm `\/:*?"<>|`, tối đa 120 ký tự.

**Test:**
- Biên bản mẫu đúng cấu trúc `MINUTES_SYSTEM` cho ra HTML có `<table>`, `<h1>`, `<li>`, `<b>`.
- DOCX mở lại bằng python-docx có bảng và tiêu đề.
- MP3 của 2 lượt ghi 1 giây đọc lại được bằng soundfile, dài khoảng 2 giây.
- `folder_name` giờ +7 và không có ký tự cấm.

### Task 7: Google OAuth

**Files:**
- Create: `meeting/google_oauth.py`
- Modify: `meeting/app.py` (các API bên dưới)
- Test: `tests/test_google_oauth.py`

**Cấu hình:**
- Dùng `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`.
- `PUBLIC_BASE_URL` (mặc định lấy từ request) để tạo `redirect_uri = {base}/api/google/callback`.
- Quyền xin: `openid email https://www.googleapis.com/auth/drive.file`.

**Hàm:**
- `auth_url(email, return_to) -> str`
  - `state` ký HMAC (email, return_to, hạn 10 phút).
  - `access_type=offline`, `prompt=consent`, `include_granted_scopes=true`, `login_hint=email`.
- `async exchange(code, state) -> email`
  - Đổi mã ở `https://oauth2.googleapis.com/token`.
  - Đọc email trong `id_token`: phần payload đọc thẳng vì nhận trực tiếp từ Google qua TLS. Email phải trùng email
    trong `state`.
  - Lưu `google_tokens`: `{email, refresh_enc, scopes, google_email, connected_at}`.
- `async access_token(email)`
  - Nhớ trong RAM tới gần hết hạn, rồi làm mới.
  - Gặp `invalid_grant` thì xóa kết nối và ném `NeedReconnect`.
- `status(email)`
- `async disconnect(email)`: gọi revoke rồi xóa.
- Mã hóa AES-GCM, khóa = HKDF(`GOOGLE_TOKEN_KEY` hoặc `AUTH_SECRET`). Không có khóa cố định thì từ chối kết nối và
  báo cần `AUTH_SECRET`.

**API:**
- `GET /api/google/status`
- `POST /api/google/connect {return_to}` trả `{url}`. `return_to` phải cùng origin với server hoặc nằm trong
  `CORS_ORIGINS` (chặn chuyển hướng tùy ý).
- `GET /api/google/callback?code&state`: công khai (thêm vào `auth.PUBLIC_PATHS`), xong thì chuyển hướng về
  `return_to#/?drive=ok` hoặc `#/?drive=err`.
- `POST /api/google/disconnect`

**Test:** giả Google bằng `httpx.MockTransport`:
- `state` sai hoặc hết hạn bị từ chối.
- Email Google khác email đăng nhập bị từ chối.
- Refresh token lưu ra không phải chữ thường (đã mã hóa).
- Token tự làm mới khi hết hạn.
- `invalid_grant` thì thành chưa kết nối.
- `return_to` lạ bị từ chối.

### Task 8: Drive và việc lưu chạy nền

**Files:**
- Create: `meeting/gdrive.py`
- Create: `meeting/drive_sync.py`
- Modify:
  - `meeting/live.py`: `_finalize_minutes` xong thì gọi `drive_sync.schedule(self.id)` nếu ở bản web.
  - `meeting/app.py`: API, lifespan chạy dọn ghi âm hết hạn mỗi 12 giờ.
  - `meeting/recording.py`: thêm chữ trong hộp đồng ý ở index.html.
- Test: `tests/test_drive_sync.py`

**`gdrive.Drive(token_fn)`** (async httpx):
- `find_folder(name, parent)`
- `ensure_folder(name, parent, known_id=None)`: `known_id` còn và chưa vào thùng rác thì dùng lại, nếu không thì
  tìm, rồi mới tạo.
- `put_doc(name, html, parent, file_id=None)`: multipart, `mimeType=application/vnd.google-apps.document`.
  Có `file_id` thì PATCH upload.
- `put_file(name, mime, path, parent, file_id=None)`: resumable upload.
- `share(file_id, email, role="writer")`: `sendNotificationEmail=false`.
- `unshare(file_id, permission_id)`
- `delete(file_id)`
- `get(file_id)`
- Tên trong câu truy vấn được thoát `\` và `'`.

**`drive_sync`:**
- `save_meeting(mid)`:
  1. Xác định người giữ Drive: chủ nhóm, hoặc người tạo nếu không thuộc nhóm.
  2. Đi lần thư mục `Meeting Copilot` (id lưu trong `google_tokens.root_id`), rồi `<nhóm>` (id lưu
     `group.drive.folder_id`) hoặc `Cuộc họp riêng` (id lưu `google_tokens.private_id`), rồi
     `folder_name(meeting)` (id lưu `meeting.drive.folder_id`).
  3. Ghi Docs "Biên bản - <tiêu đề>" từ biên bản mới nhất (`meeting.drive.doc_id`, ghi đè khi biên bản đổi).
  4. Ghi `Ghi âm.mp3` nếu có ghi âm và số giây khác lần trước.
  5. Đồng bộ chia sẻ thư mục nhóm cho thành viên (`group.drive.permissions` từ email sang permission id).
  6. Ghi `meeting.drive = {status, error, folder_url, doc_url, audio_id, audio_at, saved_at}` và phát sự kiện
     `drive_status` cho phòng họp đang mở.
- `schedule(mid)`: tác vụ nền, thử lại sau 5 giây, 30 giây, 120 giây. `NeedReconnect` thì `status=need_connect`,
  không thử lại.
- `sync_group_sharing(gid)`: gọi khi đổi thành viên.
- `purge_recordings()`: xóa `Ghi âm.mp3` trên Drive quá `group.recording_drive_days` hoặc `RECORDING_RETENTION_DAYS`
  (tính từ `audio_at`), rồi ghi `audio_deleted_at`.

**API:**
- `POST /api/meetings/{mid}/drive-save`: lưu lại bằng tay.
- `GET /api/meetings/{mid}` trả thêm `drive`.

**Test:** Drive giả bằng `MockTransport`, giữ thư mục và file trong dict:
- Lần lưu đầu tạo đủ 3 cấp thư mục, Docs, MP3.
- Lưu lại không tạo trùng (PATCH đúng file).
- Thư mục bị xóa trên Drive thì tạo lại.
- Thêm hoặc bớt thành viên thì có hoặc mất quyền.
- Chủ nhóm chưa kết nối thì `need_connect`.
- Quá hạn thì xóa ghi âm, biên bản giữ nguyên.
- Cuộc họp không nhóm vào `Cuộc họp riêng` của người tạo.

### Task 9: Giao diện Drive

**Files:**
- Modify: `meeting/index.html`

**Nội dung:**
- Cài đặt có mục "Google Drive":
  - đã kết nối: email, nút Ngắt kết nối;
  - chưa: nút Kết nối Google Drive, mở URL đăng nhập Google cùng tab.
  - Lúc quay về, đọc `#/?drive=ok|err` để hiện thông báo.
- Trang nhóm: dòng trạng thái thư mục Drive (link mở, hoặc "Chủ nhóm chưa kết nối Drive").
- Phòng họp đã kết thúc:
  - dòng trạng thái lưu (đang lưu, đã lưu kèm Mở thư mục, lỗi kèm Lưu lại);
  - nút "Lưu lên Drive";
  - nghe sự kiện `drive_status`.
- Hộp đồng ý ghi âm thêm câu "và lưu lên Google Drive của nhóm, tự xóa sau N ngày".

### Task 10: Lưu về thư mục trên máy

**Files:**
- Modify: `meeting/app.py`
  - `GET /api/meetings/{mid}/recap.docx`
  - `GET /api/meetings/{mid}/recording.mp3`
  - `GET /api/recaps/ready?days=30`: các cuộc họp xem được, biên bản xong, kèm `minutes_id`, `group_id`,
    `group_name`, `folder_name`, `has_recording`.
- Modify: `meeting/index.html`
- Test: `tests/test_recap_export.py` (API), Playwright (luồng dự phòng `webkitdirectory` không ghi được, nên kiểm tra
  nút Tải về và gọi hàm ghi với thư mục giả lập trong IndexedDB nếu được).

**Nội dung:**
- IndexedDB `meeting-copilot` lên phiên bản 2, thêm store:
  - `save-dirs`: `root` và `group:<gid>`, mỗi mục giữ handle `readwrite`;
  - `saved`: khóa `<mid>:<minutes_id>`.
- Cài đặt có mục "Lưu biên bản về máy": chọn thư mục gốc. Cài đặt nhóm có thư mục riêng của nhóm.
- Tự lưu:
  - Khi ứng dụng mở và mỗi lần nhận `meeting_status` có `minutes_status=done`: gọi `/api/recaps/ready`, cuộc họp chưa
    lưu và có quyền ghi thì ghi
    `<gốc hoặc thư mục nhóm>/<Tên nhóm nếu dùng gốc>/<folder_name>/Biên bản.docx + Ghi âm.mp3`.
  - Chưa có quyền thì hiện một lần "Cho phép lưu biên bản về máy".
- Trình duyệt không có `showDirectoryPicker`: menu phòng họp có "Tải biên bản (.docx)" và "Tải ghi âm (.mp3)".

### Task 11: Tài liệu, phiên bản, triển khai

**Files:**
- `README.md`: mục "Bản 3.17". Hướng dẫn 3 bước Google Cloud:
  1. Bật Drive API.
  2. Thêm redirect URI.
  3. Thêm quyền `drive.file`.
- `package.json` (3.17.0).
- `render.yaml`: `TZ`, `PUBLIC_BASE_URL`.

**Các bước:**
- Chạy toàn bộ test, quét khóa bí mật trong diff, commit, push `main`.
- Hỏi anh Phúc trước khi đẩy `release` nếu đang có cuộc họp.

**Ngoài phạm vi giai đoạn 1 (YAGNI):**
- Google Picker để chọn thư mục Drive có sẵn: chỉ làm khi anh cần và có API key.
- Đồng bộ nhóm tạo lúc mất Atlas: nhóm chỉ tạo khi có Atlas, bản lưu trên máy thì báo lỗi.
