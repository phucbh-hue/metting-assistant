# Extension Chrome: nghe tiếng cuộc họp qua tab, không cần bot

Nguồn âm thanh thứ tư cho Meeting Copilot, bên cạnh mic, tệp và bot Recall.ai. Bản hiện tại: **0.4.0**.

## Làm gì

Bật lên thì extension thu **hai nguồn** rồi gửi về Meeting Copilot, chép lời như mic:
- **tiếng người online** phát ra từ tab (Google Meet, Teams web, Zoom web, webinar...) -> luồng **"tab"**
  (`/ws/meeting/{mid}/tab`);
- **tiếng anh chị nói** qua micro -> luồng **"mic"** (`/ws/meeting/{mid}/audio`). Bỏ chọn được nếu chỉ muốn thu tab.

Nhờ vậy một công tắc là thu cả cuộc họp, **không cần bot Recall vào phòng** và **không tốn credit Recall**. Lần đầu bật
mic, extension mở một tab xin **quyền micro** (chỉ một lần).

> Chỉ dùng khi những người trong cuộc họp **đã được xin phép ghi âm từ trước** (chính sách chung của công ty).
> Extension không hiện thông báo trong phòng họp.

## Popup: "Tiếp tục" và "Tạo mới" là hai nút riêng (0.4.0)

- **Tiếp tục nghe #id**: nối lời thoại vào một cuộc họp **đang mở** (ô chọn chỉ liệt kê cuộc họp chưa kết thúc, server
  lọc `?live=1`). Cuộc họp extension nghe lần gần nhất được chọn sẵn. Nút này **không bao giờ tạo cuộc họp mới**.
- **Tạo cuộc họp mới và nghe**: nhập tên (bắt buộc), chọn nhóm nếu cần, luôn tạo cuộc họp mới. **Không bao giờ chạy vào
  cuộc họp cũ.**
- Vì sao đổi: bản 0.3 chỉ có một nút "Bắt đầu nghe" lấy cuộc họp đầu tiên trong ô chọn; ô này tải chậm (danh sách cả kho)
  và tự chọn, nên lúc tạo mới dù muốn nối tiếp, lúc chạy vào cuộc họp cũ dù đã gõ tên.
- Đang nghe: popup hiện cuộc họp đang nhận lời thoại, tình trạng kết nối, số khối đã gửi; **Dừng nghe**; **Mở phòng họp**.

## Mất mạng thì tự nối lại (0.4.0)

Mỗi WebSocket (tab, mic, bảng BD) tự nối lại vào **đúng cuộc họp đang nghe** khi rớt (lùi dần 1 -> 15 giây, không giới
hạn số lần); trước mỗi lần nối hỏi background token mới nhất (server gia hạn trượt qua `/api/auth/me`, hoặc người dùng
vừa đăng nhập lại ở tab app). Popup báo "đang nối lại (lần n)". Không bao giờ tự tạo cuộc họp mới.
- Server báo cuộc họp đã kết thúc (4003) hoặc máy khác tiếp quản luồng (4009) thì dừng luồng đó, popup giải thích.
- Tab họp bị đóng / tải lại khiến Chrome thu hồi quyền bắt tiếng: extension dừng và nhắc **mở popup trên tab họp, bấm
  "Tiếp tục nghe #id"** (Chrome bắt buộc có thao tác của người dùng trên tab để bắt tiếng lại).
- Lúc không có mạng, âm thanh không chép được (bỏ, không gửi bù): nối lại thì server tự nối dòng thời gian.

## Bảng gợi ý BD ngay trên tab họp (0.4.0)

Cuộc họp thuộc **nhóm bật chế độ BD** thì sau khi bắt đầu nghe, một bảng nổi "Gợi ý BD" hiện góc dưới phải của tab họp
(`bd-panel.js`, shadow DOM): câu hỏi khách vừa hỏi mà trợ lý tự nhận ra, câu trả lời gợi ý có nguồn K/M/U, ghi chú cho đội
mình, nút **Chép**, **Tra thêm trên mạng**, ô gõ câu hỏi. Kéo thanh tiêu đề để di chuyển, **kéo góc dưới phải để đổi cỡ
bảng**, nút **Aa** đổi cỡ chữ (13 / 15 / 17 px); vị trí, cỡ bảng, cỡ chữ nhớ cho lần sau (`chrome.storage.local`). Thu gọn
(có đếm câu trả lời mới), ẩn / hiện bằng nút trong popup. Dữ liệu đi: offscreen giữ `/ws/meeting/{mid}/bd` -> background -> bảng (content script không tự mở
WebSocket, tránh CSP của trang họp).

> **Chia sẻ cả màn hình thì khách thấy bảng này.** Chia sẻ một cửa sổ / tab khác thì không. Bảng chỉ dành cho đội BD.

## Vì sao có token-bridge

WebSocket và `fetch` từ `chrome-extension://` không mang được cookie đăng nhập của app. Nên có `token-bridge.js`
chạy trên chính trang Meeting Copilot, đọc token đăng nhập (`localStorage ma_token`) và đưa cho extension. Người
dùng chỉ cần **đăng nhập app một lần trong một tab** là extension gọi API và WebSocket dưới đúng quyền của mình -
không phải dán token tay. Token hết hạn thì mở lại app, đăng nhập, rồi bấm **Lấy lại quyền đăng nhập**.

## Cài (bản dev, chưa lên store)

1. Mở `chrome://extensions` (Chrome hoặc Edge) → bật **Developer mode**.
2. **Load unpacked** → chọn thư mục `extension/`.
3. Mở Meeting Copilot (`https://urbox-meeting-api.onrender.com`) trong một tab và **đăng nhập**.
4. Mở tab cuộc họp (Google Meet...), bấm biểu tượng extension.
5. **Tiếp tục nghe** một cuộc họp đang mở, hoặc nhập tên → **Tạo cuộc họp mới và nghe**.
6. Trong phòng Meeting Copilot, lời thoại online hiện ra; kết thúc thì bấm **Dừng nghe** (hoặc kết thúc cuộc họp
   trên app).

Muốn chạy với server trên máy thì mở app ở `http://localhost:8080` (đã nằm trong `host_permissions`).

### Cập nhật trên từng máy

Bản dev (load unpacked) **không tự cập nhật**. Mỗi máy: kéo code mới về (`git pull`), vào `chrome://extensions` bấm
**reload** ở thẻ extension (hoặc **Load unpacked** lại nếu đổi thư mục), rồi F5 tab app và tab họp để nạp lại content
script. Số bản hiện ở thẻ extension phải là **0.4.0**; máy nào còn 0.1.x / 0.3.x là chưa cập nhật.

Dễ vấp: Chrome đọc `popup.html` / `popup.js` **mới ngay từ đĩa** mỗi lần mở popup, nhưng `background.js` và `manifest.json`
chỉ nạp lại khi bấm **Reload**. Kéo code về mà chưa Reload thì popup mới chạy với phần nền cũ: không thấy nút "Dừng nghe"
dù đang nghe, ô nhóm trống, không có bảng BD. Từ 0.4.0 popup hỏi phần nền (`bg:ping`) và báo thẳng "chưa nạp bản mới".

## File

| File | Việc |
|---|---|
| `manifest.json` | MV3, quyền `tabCapture` + `offscreen`; content script đọc token trên origin của app; `mute-watch.js` + `bd-panel.js` trên trang họp |
| `token-bridge.js` | Content script trên trang app: đọc `ma_token` rồi đưa cho extension |
| `popup.html` / `popup.js` | Tiếp tục cuộc họp đang mở / tạo cuộc họp mới (hai nút riêng), Dừng, tình trạng kết nối, bật tắt bảng BD |
| `background.js` | Service worker: kiểm tra cuộc họp còn mở, lấy `streamId` của tab, mở offscreen, gọi API, nhớ cuộc họp nghe gần nhất, chuyển sự kiện BD lên tab họp |
| `offscreen.js` | Bắt audio (mic + tab), chống vọng âm, ba WebSocket tự nối lại (tab, mic, bd) |
| `bd-panel.js` | Content script trên tab họp: bảng gợi ý BD nổi (shadow DOM), hỏi thêm, tra mạng, chép |
| `pcm-worklet.js` | Float32 → PCM S16LE 16kHz, gom khối 100ms, kèm đường bao năng lượng (cho bộ chống vọng âm) |
| `permission.html` / `permission.js` | Tab xin quyền micro (không xin được từ popup vì popup mất focus rồi đóng) |
| `mute-watch.js` | Content script trên tab họp: đọc nút tắt mic của app rồi báo offscreen im theo (fail-open) |

Server: WebSocket `/ws/meeting/{mid}/tab?token=<token người dùng>` (`meeting/app.py`), thành luồng `"tab"` trong
`meeting/live.py` (không chiếm "nguồn mic", chạy song song). `GET /api/meetings?live=1` trả cuộc họp chưa kết thúc.

## Mấy điều dễ vấp

- **Bắt tiếng tab làm Chrome tắt tiếng tab đó.** `offscreen.html` có thẻ `<audio>` phát lại để vẫn nghe được họp;
  bỏ thẻ đó đi là ngồi họp trong im lặng.
- **Phải bấm trên đúng tab họp.** `getMediaStreamId` lấy tab đang mở; mở popup ở tab khác là thu nhầm.
- **Vọng âm khi không đeo tai nghe.** Thu cả mic lẫn tab mà nghe qua loa ngoài thì tiếng người online lọt vào mic.
  `offscreen.js` có bộ chống vọng âm bằng tương quan chéo (khối mic bám theo đường bao của tab ở độ trễ dương thì gửi
  im lặng), nhưng **đeo tai nghe vẫn chắc hơn**.
- **Reload extension không tự sửa tab đang mở.** Sau khi sửa code, vào `chrome://extensions` bấm **reload** rồi F5
  tab app để nạp lại `token-bridge.js` và tab họp để nạp lại `mute-watch.js` / `bd-panel.js`.

## Im theo nút tắt mic của app họp

`mute-watch.js` đọc nút tắt mic của nền tảng; khi anh chị mute trong app, extension gửi im lặng cho luồng mic (vẫn giữ
đúng mốc thời gian). **Fail-open**: không đọc được nút thì coi như KHÔNG mute (thà lọt vài câu còn hơn nuốt sạch tiếng).
- **Google Meet** đọc `data-is-muted`; **Teams / Zoom** và nền tảng khác đọc nhãn nút ("Unmute"/"Bật tiếng" = đang tắt).
- Meet / Teams / Zoom tự chạy (trong `host_permissions`). Nền tảng khác (**Callio**...): bấm **"Im theo nút tắt mic của
  trang này"** trong popup một lần để cấp quyền đọc trang đó.
- Chưa chắc đúng với mọi nền tảng vì mỗi app một DOM: nếu mute mà popup vẫn báo "Mic: gửi ..." tăng đều (không có "im
  theo app"), gửi em HTML của nút tắt mic để chỉnh bộ dò.

## Chưa làm

- Tự phát hiện cuộc họp kết thúc để dừng thu (hiện bấm **Dừng nghe**; hoặc để cuộc họp tự kết thúc sau 30 phút không
  ai nói; server báo đã kết thúc thì extension dừng).
- Tự bắt tiếng lại sau khi tab họp tải lại trang (Chrome đòi thao tác của người dùng trên tab).
- Đóng gói lên Chrome Web Store (chế độ Private cho tên miền `urbox.vn`) để các máy tự cập nhật.
