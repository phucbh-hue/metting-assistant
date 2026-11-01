// Service worker extension PHỎNG VẤN: cầu nối popup <-> offscreen. Khác bản họp: xác thực bằng MÃ KẾT NỐI của BUỔI
// (base|sid|token lấy từ trang buổi phỏng vấn), không đọc bearer của người dùng. MV3 không cho service worker dùng
// getUserMedia/AudioContext nên phần thu nằm trong offscreen; ở đây chỉ xin streamId của tab rồi chuyển qua.
const OFFSCREEN = 'offscreen.html';

chrome.runtime.onInstalled.addListener(async () => {
  try { if (await chrome.offscreen.hasDocument()) await chrome.offscreen.closeDocument(); } catch (e) {}
  await chrome.storage.session.set({ running: null });
});

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN,
    reasons: ['USER_MEDIA', 'AUDIO_PLAYBACK'],
    justification: 'Thu tiếng buổi phỏng vấn (mic + tiếng tab) rồi gửi về UrBox Interview để chép lời.'
  });
}

async function conn() {
  const { iv } = await chrome.storage.local.get('iv');
  return iv || { base: '', sid: 0, token: '' };
}

/** Mã kết nối dạng "base|sid|token" (trang buổi phỏng vấn cung cấp nút Copy). */
function parseCode(code) {
  const parts = String(code || '').trim().split('|');
  if (parts.length < 3) return null;
  const base = parts[0].trim().replace(/\/+$/, '');
  const sid = parseInt(parts[1].trim(), 10);
  const token = parts.slice(2).join('|').trim();
  if (!/^https?:\/\//.test(base) || !sid || !token) return null;
  return { base, sid, token };
}

async function start({ micOn }) {
  const { base, sid, token } = await conn();
  if (!base || !sid || !token) return { ok: false, error: 'Chưa có mã kết nối buổi. Mở trang buổi phỏng vấn, bấm "Copy mã kết nối" rồi dán vào đây.' };
  await ensureOffscreen();
  await chrome.runtime.sendMessage({ cmd: 'offscreen:stop' }).catch(() => {});
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return { ok: false, error: 'Không tìm thấy tab đang mở.' };
  let streamId;
  try {
    streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  } catch (e) {
    return { ok: false, error: `Không thu được tiếng tab này (${e.message || e}). Mở popup ngay trên tab buổi phỏng vấn rồi bấm lại.` };
  }
  const r = await chrome.runtime.sendMessage({ cmd: 'offscreen:start', base, sid, token, streamId, micOn: micOn !== false });
  if (r && r.needMic) {
    await chrome.tabs.create({ url: chrome.runtime.getURL('permission.html') });
    return { ok: false, error: 'Cần cấp quyền micro: một tab vừa mở ra, bấm "Cấp quyền micro" rồi quay lại bấm Bắt đầu. (Hoặc bỏ chọn "Thu cả tiếng tôi nói".)' };
  }
  if (r && r.ok) {
    await chrome.storage.session.set({ running: { sid, tabId: tab.id, micOn: r.micOn, since: Date.now() } });
    try { await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['mute-watch.js'] }); } catch (e) {}
  }
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
  if (msg.cmd === 'iv:save') {
    const c = parseCode(msg.code);
    if (!c) { reply({ ok: false, error: 'Mã kết nối không đúng. Copy lại từ trang buổi phỏng vấn (dạng địa_chỉ|mã_buổi|token).' }); return true; }
    chrome.storage.local.set({ iv: c }).then(() => reply({ ok: true, base: c.base, sid: c.sid }));
    return true;
  }
  if (msg.cmd === 'iv:conn') { conn().then(reply); return true; }
  if (msg.cmd === 'bg:start') { start(msg).then(r => reply(r || { ok: true })).catch(e => reply({ ok: false, error: String(e) })); return true; }
  if (msg.cmd === 'bg:stop') { stop().then(() => reply({ ok: true })).catch(e => reply({ ok: false, error: String(e) })); return true; }
  if (msg.cmd === 'bg:state') { chrome.storage.session.get('running').then(d => reply(d.running || null)); return true; }
  if (msg.cmd === 'bg:injectMute') {
    chrome.scripting.executeScript({ target: { tabId: msg.tabId }, files: ['mute-watch.js'] })
      .then(() => reply({ ok: true })).catch(e => reply({ ok: false, error: String(e && e.message || e) }));
    return true;
  }
  if (msg.cmd === 'bg:report') {
    if (msg.error) chrome.storage.session.get('running').then(({ running }) => {
      if (running) chrome.storage.session.set({ running: { ...running, error: msg.error } });
    });
    return;
  }
});
