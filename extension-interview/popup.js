// Popup extension phỏng vấn: dán mã kết nối buổi (base|sid|token) rồi thu tiếng tab + mic. Việc nặng do background làm.
const $ = (id) => document.getElementById(id);
const send = (msg) => chrome.runtime.sendMessage(msg);
let statsTimer = null;

function setMsg(text, cls) { const m = $('msg'); m.textContent = text || ''; m.className = 'row ' + (cls || ''); }
function show(id, on) { $(id).style.display = on ? '' : 'none'; }

async function refreshRunningView() {
  const run = await send({ cmd: 'bg:state' });
  const live = !!(run && run.sid);
  show('live', live);
  if (live) {
    show('setup', false); show('pick', false);
    $('liveInfo').textContent = `Đang thu ${run.micOn ? 'mic + tab' : 'tab'} cho buổi #${run.sid}.`;
    if (run.error) setMsg(run.error, 'err');
    if (!statsTimer) statsTimer = setInterval(pollStats, 1000);
  } else if (statsTimer) {
    clearInterval(statsTimer); statsTimer = null;
  }
  return live;
}

async function showConn() {
  const c = await send({ cmd: 'iv:conn' });
  if (c && c.sid && c.token) {
    const host = (c.base || '').replace(/^https?:\/\//, '');
    $('auth').innerHTML = `<span class="ok">Buổi #${c.sid} · ${host}</span>`;
    show('setup', false); show('pick', true);
    return true;
  }
  $('auth').innerHTML = '<span class="muted">Chưa nối buổi nào.</span>';
  show('setup', true); show('pick', false);
  return false;
}

async function pollStats() {
  let s = null;
  try { s = await send({ cmd: 'offscreen:stats' }); } catch (e) { return; }
  if (!s) return;
  const tabLoud = s.tabSent ? Math.round(100 * s.tabLoud / s.tabSent) : 0;
  const micNote = s.meet ? `, im theo app ${s.meet}` : (s.muted ? `, chặn vọng âm ${s.muted}` : '');
  $('liveStats').textContent = `Ứng viên (tab): ${s.tabSent} khối, có tiếng ${tabLoud}% · NPV (mic): ${s.sent}${micNote}`
    + (s.wait ? ` · chờ server ${s.wait}` : '');
  if (s.tabSent > 40 && tabLoud === 0) setMsg('Tab không phát ra tiếng nào — kiểm tra đã bấm trên đúng tab phỏng vấn chưa.', 'err');
}

async function doStart() {
  setMsg('');
  $('start').disabled = true;
  const r = await send({ cmd: 'bg:start', micOn: $('micOn').checked });
  $('start').disabled = false;
  if (!r || !r.ok) { setMsg(r && r.error || 'Không bắt đầu được', 'err'); return; }
  await refreshRunningView();
}

async function init() {
  $('save').onclick = async () => {
    setMsg('');
    const r = await send({ cmd: 'iv:save', code: $('code').value });
    if (!r || !r.ok) { setMsg(r && r.error || 'Mã không hợp lệ', 'err'); return; }
    setMsg('Đã lưu mã kết nối.', 'ok');
    await showConn();
  };
  $('start').onclick = doStart;
  $('change').onclick = () => { show('pick', false); show('setup', true); $('auth').innerHTML = '<span class="muted">Dán mã kết nối buổi khác.</span>'; };
  $('stop').onclick = async () => { await send({ cmd: 'bg:stop' }); setMsg(''); await refreshRunningView(); await showConn(); };
  $('muteSync').onclick = async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url) { setMsg('Không đọc được tab đang mở.', 'err'); return; }
    let origin; try { origin = new URL(tab.url).origin + '/*'; } catch (e) { setMsg('Tab này không đồng bộ được.', 'err'); return; }
    let granted = false;
    try { granted = await chrome.permissions.request({ origins: [origin] }); } catch (e) { granted = false; }
    if (!granted) { setMsg('Chưa được cấp quyền đọc trang này (bấm lại và chọn Cho phép).', 'err'); return; }
    const r = await send({ cmd: 'bg:injectMute', tabId: tab.id });
    setMsg(r && r.ok ? 'Đã bật: mute trong app thì extension cũng im.' : (r && r.error || 'Không bật được'), r && r.ok ? 'ok' : 'err');
  };

  const running = await refreshRunningView();
  if (!running) await showConn();
}

init();
