// Content script trên tab cuộc họp: đọc trạng thái nút tắt mic của nền tảng (Google Meet, Teams, Zoom, Callio...) rồi báo
// offscreen. Vì sao cần: extension mở mic bằng getUserMedia riêng, độc lập với app họp; bấm mute trong app chỉ chặn app
// GỬI tiếng đi, mic phần cứng vẫn chạy nên extension vẫn thu. Khi app báo đang mute thì offscreen gửi IM LẶNG cho luồng mic.
//
// FAIL OPEN: không đọc được nút thì coi như KHÔNG mute. Đoán nhầm thành "đang mute" sẽ nuốt sạch tiếng người dùng mà
// không để lại dấu vết; đoán nhầm sang "không mute" chỉ lọt vài câu - dễ thấy và sửa được.
(function () {
  if (typeof window.__maMuteStop === 'function') window.__maMuteStop();   // bản cũ nhường chỗ cho bản mới (sau reload)
  const alive = () => !!(chrome.runtime && chrome.runtime.id);
  const MIC = /micro|micrô|mic\b|tiếng|audio|âm thanh/i;
  const host = location.hostname;

  // Google Meet ghi thẳng trạng thái lên nút: data-is-muted="true|false". Cả nút mic lẫn camera đều có thuộc tính này
  // nên phải lọc theo nhãn mic, không thì tắt camera cũng bị hiểu thành tắt mic.
  function meetState() {
    for (const el of document.querySelectorAll('[data-is-muted]')) {
      const label = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('data-tooltip') || ''}`;
      if (/micro|micrô|mic\b/i.test(label)) return el.getAttribute('data-is-muted') === 'true';
    }
    return null;
  }
  // Nút điều khiển của CHỦ phòng, không phải nút mic của mình: "Mute all", "Ask to unmute", "yêu cầu bật mic"... Bỏ qua,
  // không thì quét nhầm làm tưởng mình đang mute rồi nuốt tiếng.
  const HOST_CTRL = /\b(all|everyone|participant|guests?)\b|tất cả|mọi người|người tham gia|ask to|yêu cầu/i;

  // Teams / Zoom / Callio...: nút mic mời hành động - "Unmute"/"Bật tiếng" nghĩa là ĐANG tắt; "Mute"/"Tắt tiếng" là đang mở.
  function labelState() {
    const sels = ['#microphone-button', '[data-tid="toggle-mute"]', '[data-tid="microphone-button"]',
                  'button[aria-label]', '[role="button"][aria-label]'];
    for (const sel of sels) {
      for (const el of document.querySelectorAll(sel)) {
        const lab = `${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('data-tooltip') || ''}`;
        if (!MIC.test(lab) || HOST_CTRL.test(lab)) continue;
        const s = lab.toLowerCase();
        if (/\bunmute\b|bật\s*(tiếng|micro|mic|âm)|mở\s*(tiếng|mic)/.test(s)) return true;   // kiểm "unmute" trước "mute"
        if (/\bmute\b|tắt\s*(tiếng|micro|mic|âm)/.test(s)) return false;
      }
    }
    return null;
  }
  function readMuted() {
    // Google Meet: CHỈ dùng data-is-muted (chuẩn, tin được). Không đọc được thì coi như KHÔNG mute - tuyệt đối không quét
    // nhãn nút lung tung trên trang Meet (nhiều nút "unmute" của chủ phòng -> dương tính giả -> nuốt tiếng người dùng).
    if (host.endsWith('meet.google.com')) return meetState();
    const m = labelState();
    if (m !== null) return m;
    return meetState();                      // nền tảng khác: thử data-is-muted phòng khi cũng dùng
  }

  let last = null;
  function tick(force) {
    if (!alive()) return stop();
    const m = readMuted() === true;          // null -> false: đúng ý fail open
    if (!force && m === last) return;
    last = m;
    try { const p = chrome.runtime.sendMessage({ cmd: 'meet:mute', muted: m }); if (p && p.catch) p.catch(() => {}); }
    catch (e) { stop(); }                    // extension vừa bị reload
  }
  const timer = setInterval(() => tick(false), 600);
  const beat = setInterval(() => tick(true), 3000);   // offscreen sinh SAU content script: cần nhịp tim để nó biết trạng thái
  function stop() { clearInterval(timer); clearInterval(beat); if (window.__maMuteStop === stop) delete window.__maMuteStop; }
  window.__maMuteStop = stop;
  tick(true);
})();
