// Service worker: cầu nối giữa popup, trang app (token-bridge), offscreen document và bảng gợi ý BD trên tab họp.
// MV3 không cho service worker dùng getUserMedia / AudioContext, và chrome.tabCapture.capture() chỉ chạy ở foreground;
// nên phần bắt audio nằm trong offscreen document, ở đây chỉ xin streamId của tab rồi chuyển qua.
// Trạng thái "đang nghe" nằm ở chrome.storage.session: service worker có thể bị Chrome tắt giữa chừng, offscreen vẫn chạy.
const OFFSCREEN = 'offscreen.html';
const APP_HOSTS = ['https://urbox-meeting-api.onrender.com/*', 'http://localhost:8080/*', 'http://127.0.0.1:8080/*'];
const BD_KEEP = 60;                 // số lượt hỏi đáp BD giữ lại để vẽ lại bảng khi tab họp tải lại trang
// Popup (đọc tươi từ đĩa mỗi lần mở) hỏi số này để biết service worker có đang chạy bản cũ không: Chrome chỉ nạp lại
// background.js / manifest khi bấm Reload ở chrome://extensions, còn popup.html thì đọc mới ngay -> nửa cũ nửa mới.
const PROTO = 2;

chrome.runtime.onInstalled.addListener(async () => {
  try { if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument(); } catch (e) {}
  await chrome.storage.session.set({ running: null, bdTurns: [], note: '' });
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
  return { ok: false, error: 'Chưa thấy tab Meeting Copilot nào đã đăng nhập. Mở app, đăng nhập rồi bấm "Lấy lại quyền đăng nhập".' };
}

async function apiCall(path, method, body) {
  const { base, token } = await auth();
  if (!base || !token) return { ok: false, status: 0, error: 'Chưa có quyền. Mở và đăng nhập Meeting Copilot trong một tab rồi bấm "Lấy lại quyền đăng nhập".' };
  try {
    const r = await fetch(new URL('/api' + path, base), {
      method: method || 'GET',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: body ? JSON.stringify(body) : undefined
    });
    const text = await r.text();
    if (r.status === 401) return { ok: false, status: 401, error: 'Phiên đăng nhập đã hết hạn. Mở app, đăng nhập lại rồi bấm "Lấy lại quyền đăng nhập".' };
    if (!r.ok) {
      let detail = '';
      try { detail = JSON.parse(text).detail; } catch (e) {}
      return { ok: false, status: r.status, error: typeof detail === 'string' && detail ? detail : `${r.status} ${text.slice(0, 160)}` };
    }
    return { ok: true, status: r.status, data: text ? JSON.parse(text) : null };
  } catch (e) {
    return { ok: false, status: 0, error: String(e && e.message || e) };
  }
}

/** Token còn dùng được không. Server gia hạn trượt thì lưu token mới; hết hạn thì thử đọc lại từ tab app đang mở. */
async function checkToken() {
  const { base, token } = await auth();
  if (!base || !token) return { ok: false, auth: false, error: 'Chưa có quyền đăng nhập.' };
  const r = await apiCall('/auth/me');
  if (r.ok) {
    if (r.data && r.data.token) await chrome.storage.local.set({ ma: { base, token: r.data.token } });
    return { ok: true, auth: true };
  }
  if (r.status === 401) {
    const t = await refreshToken();
    return t.ok ? { ok: true, auth: true } : { ok: false, auth: false, error: r.error };
  }
  return { ok: false, auth: true, error: r.error };      // mạng / server: token chưa chắc sai
}

/** Offscreen hỏi trước mỗi lần nối lại WebSocket. */
async function freshToken() {
  const c = await checkToken();
  const { base, token } = await auth();
  return { token, base, auth: c.auth, error: c.error || '' };
}

async function getRunning() { return (await chrome.storage.session.get('running')).running || null; }
async function setRunning(patch) {
  const r = await getRunning();
  if (!r) return null;
  const n = { ...r, ...patch };
  await chrome.storage.session.set({ running: n });
  return n;
}

/** Cuộc họp extension nghe lần gần nhất, chỉ khi cùng server đang dùng (localhost và onrender đánh số riêng). */
async function lastFor() {
  const { last } = await chrome.storage.local.get('last');
  const { base } = await auth();
  return last && last.base === base ? last : null;
}

function panelSend(tabId, msg) {
  if (tabId == null) return;
  try { const p = chrome.tabs.sendMessage(tabId, msg); if (p && p.catch) p.catch(() => {}); } catch (e) {}
}

async function panelState() {
  const { running, bdTurns } = await chrome.storage.session.get(['running', 'bdTurns']);
  if (!running || !running.bd) return null;
  const { base } = await auth();
  return { mid: running.mid, title: running.title, base, open: running.bdOpen !== false, turns: bdTurns || [] };
}

async function onBdEvent(ev) {
  const { running, bdTurns } = await chrome.storage.session.get(['running', 'bdTurns']);
  if (!running || !ev) return;
  let turns = bdTurns || [];
  if (ev.type === 'bd_init') turns = (ev.turns || []).slice(-BD_KEEP);
  else if (ev.type === 'bd_turn' && ev.turn) {
    const i = turns.findIndex(t => t.id === ev.turn.id);
    if (ev.turn.status === 'none') { if (i >= 0) turns.splice(i, 1); }   // trợ lý thấy không phải câu hỏi: gỡ
    else if (i >= 0) turns[i] = ev.turn;
    else turns.push(ev.turn);
    turns = turns.slice(-BD_KEEP);
  }
  await chrome.storage.session.set({ bdTurns: turns });
  panelSend(running.tabId, { cmd: 'bdpanel:event', ev });
}

async function bdToggle() {
  const r = await getRunning();
  if (!r || !r.bd) return { open: false };
  const open = r.bdOpen === false;
  await setRunning({ bdOpen: open });
  if (open) {
    try { await chrome.scripting.executeScript({ target: { tabId: r.tabId }, files: ['bd-panel.js'] }); } catch (e) {}
    panelSend(r.tabId, { cmd: 'bdpanel:show', state: await panelState() });
  } else {
    panelSend(r.tabId, { cmd: 'bdpanel:hide' });
  }
  return { open };
}

async function withRunning(fn) {
  const r = await getRunning();
  if (!r) return { ok: false, error: 'Extension đang không nghe cuộc họp nào.' };
  return fn(r);
}

async function start({ mid, micOn }) {
  const { base, token } = await auth();
  if (!base || !token) return { ok: false, error: 'Chưa có quyền đăng nhập.' };
  // Hỏi server trước: cuộc họp còn mở không, tên gì. Chạy vào cuộc họp đã kết thúc chỉ sinh lỗi 4003 rồi im lặng.
  const info = await apiCall(`/meetings/${mid}`);
  if (!info.ok) return { ok: false, error: info.status === 404 ? `Không tìm thấy cuộc họp #${mid}.` : info.error };
  const m = (info.data && info.data.meeting) || {};
  if (m.status === 'ended') return { ok: false, error: `Cuộc họp #${mid} đã kết thúc nên không nối tiếp được. Tạo cuộc họp mới.` };
  const prev = await getRunning();
  if (prev) panelSend(prev.tabId, { cmd: 'bdpanel:stop' });                     // bảng BD của cuộc họp trước (nếu có) gỡ đi
  await ensureOffscreen();
  await chrome.runtime.sendMessage({ cmd: 'offscreen:stop' }).catch(() => {});   // dọn bộ thu còn sót
  await chrome.storage.session.set({ running: null, bdTurns: [] });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return { ok: false, error: 'Không tìm thấy tab đang mở.' };
  let streamId;
  try {
    // getMediaStreamId chạy được trong service worker (Chrome 116+). Id dùng MỘT lần, hết hạn sau vài giây nếu không xài ngay.
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  } catch (e) {
    return { ok: false, error: `Không thu được tiếng tab này (${e.message || e}). Mở popup ngay trên tab cuộc họp rồi bấm lại.` };
  }
  const bdInfo = await apiCall(`/meetings/${mid}/bd`);
  const bd = !!(bdInfo.ok && bdInfo.data && bdInfo.data.enabled);
  const r = await chrome.runtime.sendMessage({ cmd: 'offscreen:start', base, mid, token, streamId, micOn: micOn !== false, bd });
  if (r && r.needMic) {        // chưa cấp quyền mic: mở tab xin quyền (không xin được từ popup/offscreen)
    await chrome.tabs.create({ url: chrome.runtime.getURL('permission.html') });
    return { ok: false, error: 'Cần cấp quyền micro: một tab vừa mở ra, bấm "Cấp quyền micro" rồi quay lại bấm Tiếp tục / Tạo mới. (Hoặc bỏ chọn "Thu cả tiếng tôi nói" nếu chỉ muốn thu tiếng người online.)' };
  }
  if (r && r.ok) {
    const title = String(m.title || '').slice(0, 120);
    await chrome.storage.session.set({
      running: { mid, title, tabId: tab.id, micOn: r.micOn, since: Date.now(), bd, bdOpen: bd, links: {} },
      bdTurns: bd ? (bdInfo.data.turns || []).slice(-BD_KEEP) : [],
      note: ''
    });
    await chrome.storage.local.set({ last: { mid, title, base, at: Date.now() } });   // để lần sau "Tiếp tục" đúng cuộc họp này
    // Đồng bộ mute: tiêm mute-watch.js vào tab họp (tab mở trước khi cài extension thì content script chưa có sẵn).
    // Chỉ chạy được với host đã có quyền (Meet/Teams/Zoom trong manifest, hoặc nền tảng khác đã được cấp quyền).
    try { await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['mute-watch.js'] }); } catch (e) {}
    if (bd) {
      try { await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['bd-panel.js'] }); } catch (e) {}
      panelSend(tab.id, { cmd: 'bdpanel:show', state: await panelState() });
    }
  }
  return r;
}

async function stop(note) {
  const running = await getRunning();
  if (await chrome.offscreen.hasDocument()) {
    await chrome.runtime.sendMessage({ cmd: 'offscreen:stop' }).catch(() => {});
    await chrome.offscreen.closeDocument().catch(() => {});
  }
  if (running) panelSend(running.tabId, { cmd: 'bdpanel:stop' });
  await chrome.storage.session.set({ running: null, bdTurns: [], note: note || '' });
}

/** Offscreen báo tình trạng từng luồng: ok / reconnecting / auth / taken / ended / tab-ended. */
async function onStatus(msg) {
  const running = await getRunning();
  if (!running) return;
  if (msg.conn === 'ended') {
    return stop(`Cuộc họp #${running.mid} đã kết thúc trên server nên extension đã dừng nghe.${msg.text ? ' ' + msg.text : ''}`);
  }
  if (msg.conn === 'tab-ended') {
    return stop(`Tab họp đã đóng hoặc đổi trang nên extension đã dừng. Mở popup trên tab họp và bấm "Tiếp tục nghe #${running.mid}" để nối tiếp.`);
  }
  const links = { ...(running.links || {}), [msg.stream]: { conn: msg.conn, tries: msg.tries || 0, text: msg.text || '' } };
  await setRunning({ links });
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const r = await getRunning();
  if (r && r.tabId === tabId) await stop(`Tab họp đã đóng nên extension đã dừng. Mở lại tab họp và bấm "Tiếp tục nghe #${r.mid}" để nối tiếp.`);
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (!msg || !msg.cmd) return;
  const run = (p) => {
    p.then(r => reply(r === undefined ? { ok: true } : r)).catch(e => reply({ ok: false, error: String(e && e.message || e) }));
    return true;
  };
  switch (msg.cmd) {
    case 'bridge:token':              // token-bridge.js trên trang app đẩy token sang
      if (msg.token) chrome.storage.local.set({ ma: { base: msg.base, token: msg.token } });
      return;
    case 'bg:ping': return run(Promise.resolve({ ok: true, proto: PROTO }));
    case 'bg:start': return run(start(msg));
    case 'bg:stop': return run(stop(''));
    case 'bg:state':
      return run(chrome.storage.session.get(['running', 'note']).then(d => ({ running: d.running || null, note: d.note || '' })));
    case 'bg:meetings': return run(apiCall('/meetings?live=1'));
    case 'bg:groups': return run(apiCall('/groups'));
    case 'bg:create':
      return run(apiCall('/meetings', 'POST', { title: (msg.title || 'Cuộc họp online').slice(0, 200), group_id: msg.group_id || null, source: 'tab' }));
    case 'bg:auth': return run(auth());
    case 'bg:refresh': return run(refreshToken());
    case 'bg:last': return run(lastFor());
    case 'bg:token': return run(freshToken());
    case 'bg:bdToggle': return run(bdToggle());
    case 'bg:injectMute':             // bật đồng bộ mute cho nền tảng ngoài Meet/Teams/Zoom (popup đã xin quyền host)
      return run(chrome.scripting.executeScript({ target: { tabId: msg.tabId }, files: ['mute-watch.js'] }).then(() => ({ ok: true })));
    case 'bg:status': onStatus(msg); return;
    case 'bg:report':                 // offscreen báo lỗi server trả về cho một luồng -> popup đọc
      if (msg.error) setRunning({ error: msg.error });
      return;
    case 'bd:event': onBdEvent(msg.ev); return;
    case 'bdpanel:hello': return run(panelState());
    case 'bdpanel:ask':
      return run(withRunning(r => apiCall(`/meetings/${r.mid}/bd/ask`, 'POST', { question: String(msg.question || '').slice(0, 500) })));
    case 'bdpanel:web':
      return run(withRunning(r => apiCall(`/meetings/${r.mid}/bd/web`, 'POST', { turn_id: String(msg.turn_id || '').slice(0, 40) })));
    case 'bdpanel:hide': setRunning({ bdOpen: false }); return;
    default: return;
  }
});
