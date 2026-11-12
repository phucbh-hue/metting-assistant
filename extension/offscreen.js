// Nơi thật sự bắt audio (MV3 cấm service worker dùng getUserMedia / AudioContext). Thu HAI nguồn vật lý riêng:
//
//   tab = chrome.tabCapture  -> tiếng người online phát ra từ tab (Google Meet, Teams, Zoom...) -> luồng "tab"
//   mic = getUserMedia       -> tiếng chính anh chị nói                                            -> luồng "mic"
//
// Hai nguồn riêng, hai WebSocket riêng (/tab và /audio), nên server gán đúng luồng. Một AudioContext 16kHz cho cả hai;
// KHÔNG nối node nào vào ctx.destination - tiếng tab được phát lại bằng thẻ <audio>, nối mic vào destination là vọng ngay.
//
// Mất mạng / server khởi động lại: mỗi WebSocket TỰ NỐI LẠI vào đúng cuộc họp đang nghe (lùi dần 1s -> 15s, không giới
// hạn số lần), trước mỗi lần hỏi background token mới nhất. Bản 0.3 không nối lại nên người dùng phải bấm lại, và dễ bấm
// nhầm thành tạo cuộc họp mới -> cuộc họp bị tách đôi.
// Luồng thứ ba (tùy chọn) "bd": kênh bảng gợi ý BD của cuộc họp, chuyển sự kiện cho background đưa lên tab họp.
const RATE = 16000;

// --- Dò vọng âm bằng tương quan chéo -----------------------------------------
// Không đeo tai nghe thì tiếng người online phát ra loa lọt vào mic, Soniox nghe được và gán nhầm thành lời anh chị.
// Mình BIẾT chính xác âm thanh đang phát ra loa (chính là luồng tab), nên hỏi: đường bao năng lượng của mic có bám theo
// đường bao của tab sau một độ trễ nào đó không. Tương quan Pearson bất biến với biên độ (AGC không phá được). CHIỀU của
// độ trễ mới quyết định: tab đi TRƯỚC mic 20-200ms = tiếng loa lọt vào mic -> CHẶN; mic đi trước = tiếng của mình -> GIỮ.
const ECHO = { floor: 0.010, corr: 0.70, leadMs: 200, followMs: 800, histMs: 2000 };
const ENV_MS = 20;
const HIST = Math.round(ECHO.histMs / ENV_MS);
const LAG_HI = Math.round(ECHO.leadMs / ENV_MS);     // độ trễ dương: tab trước
const LAG_LO = -Math.round(ECHO.followMs / ENV_MS);  // độ trễ âm: mic trước
const BACKOFF = [1000, 2000, 4000, 8000, 15000];     // ms chờ giữa các lần nối lại

let ctx = null;
let session = null;   // {base, mid, token} của cuộc họp đang nghe
let links = {};       // tên luồng -> {name, ws, ready, tries, timer, stopped, onMsg, ping}
let nodes = [];       // giữ tham chiếu, không thì bị GC giữa chừng
let tracks = [];
let stopping = false;
let echoGuard = true;
let meetMuted = false;        // nút tắt mic của app họp (mute-watch.js báo sang): đang mute thì gửi im lặng cho luồng mic
let env = { mic: [], tab: [] };
let silence = null;
let stats = freshStats();

function freshStats() { return { sent: 0, muted: 0, meet: 0, tabSent: 0, tabLoud: 0, wait: 0, corr: 0, lag: 0, links: {} }; }

function wsUrl(base, mid, token, kind) {
  const path = kind === 'tab' ? `/ws/meeting/${mid}/tab` : kind === 'bd' ? `/ws/meeting/${mid}/bd` : `/ws/meeting/${mid}/audio`;
  const u = new URL(path, base);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  u.searchParams.set('token', token);            // WebSocket không gắn header được: token đi theo query (AuthMiddleware đọc)
  return u.toString();
}
function tell(msg) {
  try { const p = chrome.runtime.sendMessage(msg); if (p && p.catch) p.catch(() => {}); } catch (e) {}
}
function report(error) { tell({ cmd: 'bg:report', error }); }
function status(name, conn, text, tries) {
  stats.links[name] = { conn, tries: tries || 0, text: text || '' };
  tell({ cmd: 'bg:status', stream: name, conn, tries: tries || 0, text: text || '' });
}

/** Tương quan Pearson giữa mic[i] và tab[i-lag]. lag>0: tab đi trước mic. */
function pearson(mic, tab, lag) {
  const lo = Math.max(0, lag), hi = Math.min(mic.length, tab.length + lag);
  const n = hi - lo;
  if (n < 10) return 0;
  let sm = 0, st = 0, smm = 0, stt = 0, smt = 0;
  for (let i = lo; i < hi; i++) { const a = mic[i], b = tab[i - lag]; sm += a; st += b; smm += a * a; stt += b * b; smt += a * b; }
  const num = smt - sm * st / n;
  const den = Math.sqrt(Math.max(0, smm - sm * sm / n) * Math.max(0, stt - st * st / n));
  return den > 1e-9 ? num / den : 0;
}
/** Khối mic hiện tại có phải vọng âm của tiếng tab không? */
function isEcho() {
  if (!echoGuard) return false;
  const m = env.mic, t = env.tab;
  if (m.length < HIST / 2 || t.length < HIST / 2) return false;
  const recent = Math.round(500 / ENV_MS);
  if (Math.max(...t.slice(-recent)) < ECHO.floor) { stats.corr = 0; stats.lag = 0; return false; }  // tab đang im
  let best = -1, bestLag = 0;
  for (let lag = LAG_LO; lag <= LAG_HI; lag++) { const c = pearson(m, t, lag); if (c > best) { best = c; bestLag = lag; } }
  stats.corr = best;
  stats.lag = bestLag * ENV_MS;
  return bestLag >= 0 && best >= ECHO.corr;       // đỉnh nằm ở chiều tab-đi-trước và đủ cao -> vọng âm
}

// --- WebSocket tự nối lại --------------------------------------------------------
async function connect(L) {
  if (L.stopped || stopping || !session) return;
  let { base, mid, token } = session;
  if (L.tries > 0) {
    // Nối lại: hỏi background token mới nhất (server gia hạn trượt, hoặc người dùng vừa đăng nhập lại ở tab app).
    try {
      const t = await chrome.runtime.sendMessage({ cmd: 'bg:token' });
      if (t && t.token) { token = t.token; session.token = t.token; }
      if (t && t.auth === false) status(L.name, 'auth', t.error || 'Phiên đăng nhập hết hạn', L.tries);
    } catch (e) { /* service worker đang khởi động lại: dùng token cũ */ }
    if (L.stopped || stopping || !session) return;
  }
  const sock = new WebSocket(wsUrl(base, mid, token, L.name));
  sock.binaryType = 'arraybuffer';
  L.ws = sock;
  L.ready = false;
  sock.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch (err) { return; }        // frame lạ
    if (m.type === 'ready' || m.type === 'bd_init') { L.ready = true; L.tries = 0; status(L.name, 'ok'); }
    else if (m.type === 'error') {
      if (m.code === 4003 || m.code === 4004) { L.stopped = true; status(L.name, 'ended', m.text); }      // cuộc họp đã kết thúc / không có
      else if (m.code === 4009) { L.stopped = true; status(L.name, 'taken', m.text); }                    // máy khác tiếp quản luồng này
      else report(m.text);
    }
    if (L.onMsg) L.onMsg(m);
  };
  sock.onclose = (e) => {
    if (L.ws !== sock) return;
    L.ws = null;
    L.ready = false;
    if (L.stopped || stopping) return;
    schedule(L, e.code);
  };
  sock.onerror = () => { /* luôn kèm onclose */ };
}
function schedule(L, code) {
  L.tries++;
  const delay = BACKOFF[Math.min(L.tries - 1, BACKOFF.length - 1)] + Math.random() * 500;
  status(L.name, 'reconnecting', `Luồng ${L.name} mất kết nối (mã ${code}), nối lại sau ${Math.round(delay / 1000)} giây`, L.tries);
  clearTimeout(L.timer);
  L.timer = setTimeout(() => connect(L), delay);
}
function openLink(name, onMsg) {
  const L = links[name] = { name, ws: null, ready: false, tries: 0, timer: null, stopped: false, onMsg, ping: null };
  if (name === 'bd') {      // kênh BD không có audio chảy liên tục: ping để proxy không đóng vì nhàn rỗi
    L.ping = setInterval(() => { if (L.ws && L.ws.readyState === WebSocket.OPEN) L.ws.send('{"type":"ping"}'); }, 25000);
  }
  connect(L);
  return L;
}
function closeLink(L) {
  L.stopped = true;
  clearTimeout(L.timer);
  clearInterval(L.ping);
  const s = L.ws;
  L.ws = null;
  if (!s) return;
  s.onclose = s.onerror = s.onmessage = null;   // onclose của chính cú đóng này không báo lỗi ngược lên
  try { if (L.name !== 'bd' && s.readyState === WebSocket.OPEN) s.send('stop'); } catch (e) {}
  try { s.close(); } catch (e) {}
}

function pipe(stream, name) {
  const L = openLink(name, null);
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, 'pcm', { processorOptions: { size: RATE / 10 } });
  node.port.onmessage = (ev) => {
    const { pcm, env: e } = ev.data;
    const buf = env[name];
    for (const v of e) buf.push(v);
    if (buf.length > HIST) buf.splice(0, buf.length - HIST);
    // Gửi sớm khi server chưa "ready" thì server dồn lại rồi đẩy một lèo cho Soniox, lệch nhịp realtime.
    // Đang mất kết nối thì khối này bỏ (không thể chép lời lúc không có mạng); nối lại thì server tự nối dòng thời gian.
    const sock = L.ws;
    if (!sock || sock.readyState !== WebSocket.OPEN || !L.ready) { stats.wait++; return; }
    if (name === 'tab') { stats.tabSent++; if (Math.max(...e) >= ECHO.floor) stats.tabLoud++; }
    if (name === 'mic') {
      const echo = isEcho();                   // gọi kể cả khi sẽ chặn vì mute: đây là chỗ duy nhất cập nhật corr/lag cho popup
      if (meetMuted || echo) {
        // Gửi IM LẶNG chứ không bỏ khối: bỏ khối làm dòng thời gian của luồng mic co lại, mốc giờ lệch dần so với tab.
        if (!silence || silence.byteLength !== pcm.byteLength) silence = new ArrayBuffer(pcm.byteLength);
        sock.send(silence);
        stats.muted++;
        if (meetMuted) stats.meet++;
        return;
      }
    }
    sock.send(pcm);
    if (name === 'mic') stats.sent++;
  };
  src.connect(node);            // KHÔNG nối ra ctx.destination
  nodes.push(src, node);
  stream.getTracks().forEach(t => tracks.push(t));
}

async function start({ base, mid, token, streamId, micOn, bd }) {
  if (ctx || Object.keys(links).length) stop();      // không bao giờ chạy hai bộ thu
  stopping = false;
  session = { base, mid, token };
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId } }
  });
  document.getElementById('playback').srcObject = tabStream;   // trả tiếng lại cho người dùng
  const tabTrack = tabStream.getAudioTracks()[0];
  if (tabTrack) {
    // Tab họp bị đóng / Chrome thu hồi: không tự thu lại được (cần người dùng bấm trên tab), báo để popup hướng dẫn "Tiếp tục".
    tabTrack.addEventListener('ended', () => { if (!stopping && session) status('tab', 'tab-ended', 'Tab họp đã đóng hoặc đổi trang'); });
  }

  let micStream = null;
  if (micOn) {
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false }
      });
    } catch (e) {
      stop();
      // Chưa cấp quyền mic: báo background mở tab xin quyền. NotAllowedError = bị từ chối/bỏ qua.
      if (e && e.name === 'NotAllowedError') return { ok: false, needMic: true };
      return { ok: false, error: `Không mở được micro: ${e && e.message || e}` };
    }
  }

  ctx = new AudioContext({ sampleRate: RATE });   // Chrome tự hạ 48k -> 16k
  await ctx.audioWorklet.addModule('pcm-worklet.js');
  pipe(tabStream, 'tab');                         // tab TRƯỚC: isEcho() cần đã có lịch sử đường bao của tab
  if (micStream) pipe(micStream, 'mic');
  if (bd) openLink('bd', (m) => { if (/^bd_/.test(String(m.type || ''))) tell({ cmd: 'bd:event', ev: m }); });
  return { ok: true, micOn: !!micStream };
}

function stop() {
  stopping = true;
  for (const k of Object.keys(links)) closeLink(links[k]);
  links = {};
  nodes.forEach(n => { try { n.disconnect(); } catch (e) {} });
  nodes = [];
  tracks.forEach(t => { try { t.stop(); } catch (e) {} });
  tracks = [];
  if (ctx) { try { ctx.close(); } catch (e) {} ctx = null; }
  const d = document.getElementById('playback'); if (d) d.srcObject = null;
  env = { mic: [], tab: [] };
  meetMuted = false;
  session = null;
  stats = freshStats();
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.cmd === 'offscreen:start') {
    echoGuard = msg.echo !== false;
    meetMuted = false;
    start(msg).then(reply).catch(e => { stop(); reply({ ok: false, error: String(e && e.message || e) }); });
    return true;
  }
  if (msg.cmd === 'offscreen:stop') { stop(); reply({ ok: true }); return true; }
  if (msg.cmd === 'offscreen:stats') { reply(stats); return true; }
  // mute-watch.js (content script trên tab họp) báo trạng thái nút tắt mic của app. Rẻ, bắn cả khi không thu.
  if (msg.cmd === 'meet:mute') { meetMuted = !!msg.muted; return; }
});
