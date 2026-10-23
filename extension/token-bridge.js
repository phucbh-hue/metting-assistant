// Content script chạy TRÊN trang Meeting Copilot (cùng origin với API). Đọc token đăng nhập mà app lưu trong
// localStorage ('ma_token') rồi đưa cho extension, để extension gọi API và WebSocket DƯỚI QUYỀN người đang đăng nhập.
// Cookie/session không bao giờ tới được origin chrome-extension://, nên đây là cách lấy quyền sạch nhất: người dùng chỉ
// cần đăng nhập app một lần trong một tab, không phải dán token tay.
(function () {
  if (window.__maBridge) return;            // manifest + executeScript có thể tiêm hai lần
  window.__maBridge = true;

  function readToken() {
    try { return localStorage.getItem('ma_token') || ''; } catch (e) { return ''; }
  }
  function push() {
    try {
      const p = chrome.runtime.sendMessage({ cmd: 'bridge:token', token: readToken(), base: location.origin });
      if (p && p.catch) p.catch(() => {});  // service worker đang ngủ: lần focus/interval sau gửi lại
    } catch (e) { /* extension vừa reload: context cũ chết, bỏ qua */ }
  }
  push();
  window.addEventListener('focus', push);   // token đổi khi đăng nhập / đăng xuất
  setInterval(push, 60000);

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg && msg.cmd === 'bridge:read') { reply({ token: readToken(), base: location.origin }); return true; }
  });
})();
