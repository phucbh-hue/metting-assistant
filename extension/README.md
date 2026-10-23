# Extension Chrome: nghe tiếng cuộc họp qua tab, không cần bot

Nguồn âm thanh thứ tư cho Meeting Copilot, bên cạnh mic, tệp và bot Recall.ai.

## Làm gì

Bật lên thì extension thu **tiếng của tab đang mở** (Google Meet, Teams web, Zoom web, webinar...) rồi gửi về
Meeting Copilot thành luồng **"tab"**, chép lời như mic. Chạy **song song với mic** (họp nửa online nửa trực tiếp:
mic lo tiếng người trong phòng, tab lo tiếng người online), nên **không cần bot Recall vào phòng** và **không tốn
credit Recall**.

> Chỉ dùng khi những người trong cuộc họp **đã được xin phép ghi âm từ trước** (chính sách chung của công ty).
> Extension không hiện thông báo trong phòng họp.

## Vì sao có token-bridge

WebSocket và `fetch` từ `chrome-extension://` không mang được cookie đăng nhập của app. Nên có `token-bridge.js`
chạy trên chính trang Meeting Copilot, đọc token đăng nhập (`localStorage ma_token`) và đưa cho extension. Người
dùng chỉ cần **đăng nhập app một lần trong một tab** là extension gọi API và WebSocket dưới đúng quyền của mình -
không phải dán token tay. Token hết hạn (12 giờ) thì mở lại app, đăng nhập, rồi bấm **Lấy lại quyền**.

## Cài (bản dev, chưa lên store)

1. Mở `chrome://extensions` (Chrome hoặc Edge) → bật **Developer mode**.
2. **Load unpacked** → chọn thư mục `extension/`.
3. Mở Meeting Copilot (`https://urbox-meeting-api.onrender.com`) trong một tab và **đăng nhập**.
4. Mở tab cuộc họp (Google Meet...), bấm biểu tượng extension.
5. Chọn cuộc họp (hoặc nhập tên để tạo mới) → **Bắt đầu nghe tab này**.
6. Trong phòng Meeting Copilot, lời thoại online hiện ra; kết thúc thì bấm **Dừng nghe** (hoặc kết thúc cuộc họp
   trên app).

Muốn chạy với server trên máy thì mở app ở `http://localhost:8080` (đã nằm trong `host_permissions`).

## File

| File | Việc |
|---|---|
| `manifest.json` | MV3, quyền `tabCapture` + `offscreen`; content script đọc token trên origin của app |
| `token-bridge.js` | Content script trên trang app: đọc `ma_token` rồi đưa cho extension |
| `popup.html` / `popup.js` | Chọn / tạo cuộc họp, Bắt đầu / Dừng, hiện số khối đã gửi và tỉ lệ có tiếng |
| `background.js` | Service worker: lấy `streamId` của tab, mở offscreen, gọi API `/api/meetings` |
| `offscreen.js` | Nơi thật sự bắt audio (MV3 cấm service worker dùng `getUserMedia`/`AudioContext`) |
| `pcm-worklet.js` | Float32 → PCM S16LE 16kHz, gom khối 100ms |

Server: WebSocket `/ws/meeting/{mid}/tab?token=<token người dùng>` (`meeting/app.py`), thành luồng `"tab"` trong
`meeting/live.py` (không chiếm "nguồn mic", chạy song song).

## Mấy điều dễ vấp

- **Bắt tiếng tab làm Chrome tắt tiếng tab đó.** `offscreen.html` có thẻ `<audio>` phát lại để vẫn nghe được họp;
  bỏ thẻ đó đi là ngồi họp trong im lặng.
- **Phải bấm trên đúng tab họp.** `getMediaStreamId` lấy tab đang mở; mở popup ở tab khác là thu nhầm.
- **Vọng âm khi không đeo tai nghe.** Nếu máy vừa bật mic (app) vừa thu tab qua loa ngoài, mic nghe lại tiếng loa.
  Bản này chưa có bộ chống vọng âm (chỉ thu tab, không thu mic ở extension) - **nên đeo tai nghe** khi vừa dùng mic
  phòng vừa thu tab.
- **Reload extension không tự sửa tab đang mở.** Sau khi sửa code, vào `chrome://extensions` bấm **reload** rồi F5
  tab app để nạp lại `token-bridge.js`.

## Chưa làm

- Chống vọng âm mic↔tab (bản interview có; ở đây chưa cần vì chỉ thu tab).
- Tự phát hiện cuộc họp kết thúc để dừng thu (hiện bấm **Dừng nghe**; hoặc để cuộc họp tự kết thúc sau 30 phút không
  ai nói).
- Đóng gói lên Chrome Web Store (chế độ Private cho tên miền `urbox.vn`).
