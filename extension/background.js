// Service worker: cầu nối giữa popup, trang app (token-bridge) và offscreen document.
// MV3 không cho service worker dùng getUserMedia / AudioContext, và chrome.tabCapture.capture() chỉ chạy ở foreground;
// nên phần bắt audio nằm trong offscreen document, ở đây chỉ xin streamId của tab rồi chuyển qua.
const OFFSCREEN = 'offscreen.html';
const APP_HOSTS = ['https://urbox-meeting-api.onrender.com/*', 'http://localhost:8080/*', 'http://127.0.0.1:8080/*'];

chrome.runtime.onInstalled.addListener(async () => {
  try { if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument(); } catch (e) {}
  await chrome.storage.session.set({ running: null });
});

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN,
    reasons: ['USER_MEDIA', 'AUDIO_PLAYBACK'],
    justification: 'Thu tiếng tab cuộc họp rồi gửi về Meeting Copilot để chép lời.'
  });
}

async function auth() {
  const { ma } = await chrome.storage.local.get('ma');
  return ma || { base: '', token: '' };
}

/** Tìm một tab Meeting Copilot đã đăng nhập và đọc lại token từ đó (khi chưa có, hoặc token đã hết hạn). */
async function refreshToken() {
  let tabs = [];
  try { tabs = await chrome.tabs.query({ url: APP_HOSTS }); } catch (e) {}
  for (const t of tabs) {
    try {
      const r = await chrome.tabs.sendMessage(t.id, { cmd: 'bridge:read' });
      if (r && r.token) { await chrome.storage.local.set({ ma: { base: r.base, token: r.token } }); return { ok: true, base: r.base }; }
    } catch (e) { /* tab chưa có content script (mở trước khi cài): bỏ qua */ }
  }
  return { ok: false, error: 'Chưa thấy tab Meeting Copilot nào đã đăng nhập. Mở app, đăng nhập rồi bấm "Lấy lại quyền".' };
}

async function apiCall(path, method, body) {
  const { base, token } = await auth();
  if (!base || !token) return { ok: false, error: 'Chưa có quyền. Mở và đăng nhập Meeting Copilot trong một tab rồi bấm "Lấy lại quyền".' };
  try {
    const r = await fetch(new URL('/api' + path, base), {
      method: method || 'GET',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: body ? JSON.stringify(body) : undefined
    });
    const text = await r.text();
    if (r.status === 401) return { ok: false, error: 'Phiên đăng nhập đã hết hạn. Mở app, đăng nhập lại rồi bấm "Lấy lại quyền".' };
    if (!r.ok) return { ok: false, error: `${r.status} ${text.slice(0, 160)}` };
    return { ok: true, data: text ? JSON.parse(text) : null };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
}

async function start({ mid }) {
  const { base, token } = await auth();
  if (!base || !token) return { ok: false, error: 'Chưa có quyền đăng nhập.' };
  await ensureOffscreen();
  await chrome.runtime.sendMessage({ cmd: 'offscreen:stop' }).catch(() => {});   // dọn bộ thu còn sót
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return { ok: false, error: 'Không tìm thấy tab đang mở.' };
  let streamId;
  try {
    // getMediaStreamId chạy được trong service worker (Chrome 116+). Id dùng MỘT lần, hết hạn sau vài giây nếu không xài ngay.
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  } catch (e) {
    return { ok: false, error: `Không thu được tiếng tab này (${e.message || e}). Mở popup ngay trên tab cuộc họp rồi bấm lại.` };
  }
  const r = await chrome.runtime.sendMessage({ cmd: 'offscreen:start', base, mid, token, streamId });
  if (r && r.ok) await chrome.storage.session.set({ running: { mid, tabId: tab.id, since: Date.now() } });
  return r;
}

async function stop() {
  if (await chrome.offscreen.hasDocument()) {
    await chrome.runtime.sendMessage({ cmd: 'offscreen:stop' }).catch(() => {});
    await chrome.offscreen.closeDocument().catch(() => {});
  }
  await chrome.storage.session.set({ running: null });
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.cmd === 'bridge:token') {           // token-bridge.js trên trang app đẩy token sang
    if (msg.token) chrome.storage.local.set({ ma: { base: msg.base, token: msg.token } });
    return;
  }
  if (msg.cmd === 'bg:start') { start(msg).then(r => reply(r || { ok: true })).catch(e => reply({ ok: false, error: String(e) })); return true; }
  if (msg.cmd === 'bg:stop') { stop().then(() => reply({ ok: true })).catch(e => reply({ ok: false, error: String(e) })); return true; }
  if (msg.cmd === 'bg:state') { chrome.storage.session.get('running').then(d => reply(d.running || null)); return true; }
  if (msg.cmd === 'bg:meetings') { apiCall('/meetings').then(reply); return true; }
  if (msg.cmd === 'bg:create') { apiCall('/meetings', 'POST', { title: (msg.title || 'Cuộc họp online').slice(0, 200) }).then(reply); return true; }
  if (msg.cmd === 'bg:auth') { auth().then(reply); return true; }
  if (msg.cmd === 'bg:refresh') { refreshToken().then(reply); return true; }
  if (msg.cmd === 'bg:report') {              // offscreen báo lỗi luồng -> gắn vào trạng thái đang chạy để popup đọc
    if (msg.error) chrome.storage.session.get('running').then(({ running }) => {
      if (running) chrome.storage.session.set({ running: { ...running, error: msg.error } });
    });
    return;
  }
});
