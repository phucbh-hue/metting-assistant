// Nơi thật sự bắt audio. MV3 cấm service worker dùng getUserMedia / AudioContext, nên phần này nằm trong offscreen
// document. Một nguồn duy nhất: tiếng của tab cuộc họp (chrome.tabCapture) -> PCM16 16kHz -> WebSocket /ws/meeting/{mid}/tab.
// Không thu mic ở đây (mic do chính app lo, luồng riêng) nên không cần chống vọng âm.
const RATE = 16000;
let ctx = null, ws = null, node = null, src = null, tracks = [], ready = false;
let stats = { sent: 0, loud: 0, wait: 0, rms: 0 };

function wsUrl(base, mid, token) {
  const u = new URL(`/ws/meeting/${mid}/tab`, base);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  u.searchParams.set('token', token);       // WebSocket không gắn header được: token đi theo query (AuthMiddleware đọc)
  return u.toString();
}
function report(error) {
  try { const p = chrome.runtime.sendMessage({ cmd: 'bg:report', error }); if (p && p.catch) p.catch(() => {}); } catch (e) {}
}

async function start({ base, mid, token, streamId }) {
  if (ctx || ws) stop();                      // phòng hờ: không bao giờ chạy hai bộ thu
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId } }
  });
  document.getElementById('playback').srcObject = stream;   // trả tiếng lại cho người dùng

  ws = new WebSocket(wsUrl(base, mid, token));
  ws.binaryType = 'arraybuffer';
  ready = false;
  ws.onmessage = (e) => {
    try {
      const m = JSON.parse(e.data);
      if (m.type === 'ready') ready = true;
      else if (m.type === 'error') report(m.text);
    } catch (err) { /* frame lạ: bỏ qua */ }
  };
  ws.onclose = (e) => report(`Kết nối tới máy chủ đóng (${e.code})`);
  ws.onerror = () => report('Lỗi kết nối tới máy chủ');

  ctx = new AudioContext({ sampleRate: RATE });   // Chrome tự hạ tần số từ 48k về 16k
  await ctx.audioWorklet.addModule('pcm-worklet.js');
  src = ctx.createMediaStreamSource(stream);
  node = new AudioWorkletNode(ctx, 'pcm', { processorOptions: { size: RATE / 10 } });
  node.port.onmessage = (ev) => {
    const { pcm, rms } = ev.data;
    stats.rms = rms;
    // Chỉ gửi khi server đã báo "ready": gửi sớm thì server dồn lại rồi đẩy một lèo cho Soniox, lệch nhịp realtime.
    if (!ws || ws.readyState !== WebSocket.OPEN || !ready) { stats.wait++; return; }
    stats.sent++;
    if (rms >= 0.01) stats.loud++;
    ws.send(pcm);
  };
  src.connect(node);                          // KHÔNG nối ra ctx.destination (đã phát lại bằng thẻ <audio>)
  tracks = stream.getTracks();
  return { ok: true };
}

function stop() {
  if (ws) {
    ws.onclose = ws.onerror = ws.onmessage = null;   // onclose của chính cú đóng này không được báo lỗi ngược lên
    try { if (ws.readyState === WebSocket.OPEN) ws.send('stop'); } catch (e) {}
    try { ws.close(); } catch (e) {}
    ws = null;
  }
  try { src && src.disconnect(); node && node.disconnect(); } catch (e) {}
  src = node = null;
  tracks.forEach(t => { try { t.stop(); } catch (e) {} });
  tracks = [];
  if (ctx) { try { ctx.close(); } catch (e) {} ctx = null; }
  const d = document.getElementById('playback'); if (d) d.srcObject = null;
  stats = { sent: 0, loud: 0, wait: 0, rms: 0 };
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.cmd === 'offscreen:start') {
    start(msg).then(reply).catch(e => { stop(); reply({ ok: false, error: String(e && e.message || e) }); });
    return true;
  }
  if (msg.cmd === 'offscreen:stop') { stop(); reply({ ok: true }); return true; }
  if (msg.cmd === 'offscreen:stats') { reply(stats); return true; }
});
