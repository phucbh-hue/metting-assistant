// Nơi thật sự bắt audio (MV3 cấm service worker dùng getUserMedia / AudioContext). Thu HAI nguồn vật lý riêng:
//
//   tab = chrome.tabCapture  -> tiếng người online phát ra từ tab (Google Meet, Teams, Zoom...) -> luồng "tab"
//   mic = getUserMedia       -> tiếng chính anh chị nói                                            -> luồng "mic"
//
// Hai nguồn riêng, hai WebSocket riêng (/tab và /audio), nên server gán đúng luồng. Một AudioContext 16kHz cho cả hai;
// KHÔNG nối node nào vào ctx.destination - tiếng tab được phát lại bằng thẻ <audio>, nối mic vào destination là vọng ngay.
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

let ctx = null;
let ws = {};          // {mic, tab} -> WebSocket
let nodes = [];       // giữ tham chiếu, không thì bị GC giữa chừng
let tracks = [];
let echoGuard = true;
let env = { mic: [], tab: [] };
let silence = null;
let stats = { sent: 0, muted: 0, tabSent: 0, tabLoud: 0, wait: 0, corr: 0, lag: 0 };

function wsUrl(base, mid, token, stream) {
  const path = stream === 'tab' ? `/ws/meeting/${mid}/tab` : `/ws/meeting/${mid}/audio`;
  const u = new URL(path, base);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  u.searchParams.set('token', token);            // WebSocket không gắn header được: token đi theo query (AuthMiddleware đọc)
  return u.toString();
}
function report(error) {
  try { const p = chrome.runtime.sendMessage({ cmd: 'bg:report', error }); if (p && p.catch) p.catch(() => {}); } catch (e) {}
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

function pipe(stream, name, base, mid, token) {
  const sock = new WebSocket(wsUrl(base, mid, token, name));
  sock.binaryType = 'arraybuffer';
  ws[name] = sock;
  let ready = false;
  sock.onmessage = (e) => {
    try { const m = JSON.parse(e.data); if (m.type === 'ready') ready = true; else if (m.type === 'error') report(m.text); }
    catch (err) { /* frame lạ */ }
  };
  sock.onclose = (e) => report(`Luồng ${name} đóng (${e.code})`);
  sock.onerror = () => report(`Luồng ${name} lỗi kết nối`);

  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, 'pcm', { processorOptions: { size: RATE / 10 } });
  node.port.onmessage = (ev) => {
    const { pcm, env: e } = ev.data;
    const buf = env[name];
    for (const v of e) buf.push(v);
    if (buf.length > HIST) buf.splice(0, buf.length - HIST);
    // Gửi sớm khi server chưa "ready" thì server dồn lại rồi đẩy một lèo cho Soniox, lệch nhịp realtime.
    if (sock.readyState !== WebSocket.OPEN || !ready) { stats.wait++; return; }
    if (name === 'tab') { stats.tabSent++; if (Math.max(...e) >= ECHO.floor) stats.tabLoud++; }
    if (name === 'mic' && isEcho()) {
      // Gửi IM LẶNG chứ không bỏ khối: bỏ khối làm dòng thời gian của luồng mic co lại, mốc giờ lệch dần so với tab.
      if (!silence || silence.byteLength !== pcm.byteLength) silence = new ArrayBuffer(pcm.byteLength);
      sock.send(silence);
      stats.muted++;
      return;
    }
    sock.send(pcm);
    if (name === 'mic') stats.sent++;
  };
  src.connect(node);            // KHÔNG nối ra ctx.destination
  nodes.push(src, node);
  stream.getTracks().forEach(t => tracks.push(t));
}

async function start({ base, mid, token, streamId, micOn }) {
  if (ctx || Object.keys(ws).length) stop();      // không bao giờ chạy hai bộ thu
  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId } }
  });
  document.getElementById('playback').srcObject = tabStream;   // trả tiếng lại cho người dùng

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
  pipe(tabStream, 'tab', base, mid, token);       // tab TRƯỚC: isEcho() cần đã có lịch sử đường bao của tab
  if (micStream) pipe(micStream, 'mic', base, mid, token);
  return { ok: true, micOn: !!micStream };
}

function stop() {
  for (const k of Object.keys(ws)) {
    ws[k].onclose = ws[k].onerror = ws[k].onmessage = null;   // onclose của chính cú đóng này không báo lỗi ngược lên
    try { if (ws[k].readyState === WebSocket.OPEN) ws[k].send('stop'); } catch (e) {}
    try { ws[k].close(); } catch (e) {}
  }
  ws = {};
  nodes.forEach(n => { try { n.disconnect(); } catch (e) {} });
  nodes = [];
  tracks.forEach(t => { try { t.stop(); } catch (e) {} });
  tracks = [];
  if (ctx) { try { ctx.close(); } catch (e) {} ctx = null; }
  const d = document.getElementById('playback'); if (d) d.srcObject = null;
  env = { mic: [], tab: [] };
  stats = { sent: 0, muted: 0, tabSent: 0, tabLoud: 0, wait: 0, corr: 0, lag: 0 };
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.cmd === 'offscreen:start') {
    echoGuard = msg.echo !== false;
    start(msg).then(reply).catch(e => { stop(); reply({ ok: false, error: String(e && e.message || e) }); });
    return true;
  }
  if (msg.cmd === 'offscreen:stop') { stop(); reply({ ok: true }); return true; }
  if (msg.cmd === 'offscreen:stats') { reply(stats); return true; }
});
