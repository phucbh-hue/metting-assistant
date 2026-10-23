// Popup: chọn/tạo cuộc họp rồi bắt đầu thu tiếng tab đang mở. Mọi việc nặng (lấy streamId, mở offscreen, gọi API) do
// background làm; popup chỉ ra lệnh và hiển thị trạng thái.
const $ = (id) => document.getElementById(id);
const send = (msg) => chrome.runtime.sendMessage(msg);
let statsTimer = null;

function setMsg(text, cls) { const m = $('msg'); m.textContent = text || ''; m.className = 'row ' + (cls || ''); }

async function showAuth() {
  const a = await send({ cmd: 'bg:auth' });
  if (a && a.token) {
    const host = (a.base || '').replace(/^https?:\/\//, '');
    $('auth').innerHTML = `<span class="ok">Đã lấy quyền từ ${host}</span>`;
    return true;
  }
  $('auth').innerHTML = '<span class="err">Chưa có quyền. Mở và đăng nhập Meeting Copilot trong một tab rồi bấm "Lấy lại quyền".</span>';
  return false;
}

async function loadMeetings() {
  const sel = $('meeting');
  const r = await send({ cmd: 'bg:meetings' });
  if (!r || !r.ok) { sel.innerHTML = '<option value="">(không tải được)</option>'; setMsg(r && r.error || 'Không tải được danh sách cuộc họp', 'err'); return; }
  const live = (r.data.meetings || []).filter(m => m.status !== 'ended');
  if (!live.length) { sel.innerHTML = '<option value="">(chưa có cuộc họp nào đang mở - hãy tạo mới)</option>'; return; }
  sel.innerHTML = live.map(m => `<option value="${m.id}">${(m.title || 'Cuộc họp').replace(/</g, '&lt;')} · #${m.id}</option>`).join('');
}

async function refreshRunningView() {
  const run = await send({ cmd: 'bg:state' });
  const live = !!(run && run.mid);
  $('pick').style.display = live ? 'none' : '';
  $('live').style.display = live ? '' : 'none';
  if (live) {
    $('liveInfo').textContent = `Đang nghe ${run.micOn ? 'tab + mic' : 'tab'} và gửi về cuộc họp #${run.mid}.`;
    if (run.error) setMsg(run.error, 'err');
    if (!statsTimer) statsTimer = setInterval(pollStats, 1000);
  } else if (statsTimer) {
    clearInterval(statsTimer); statsTimer = null;
  }
  return live;
}

async function pollStats() {
  let s = null;
  try { s = await send({ cmd: 'offscreen:stats' }); } catch (e) { return; }
  if (!s) return;
  const tabLoud = s.tabSent ? Math.round(100 * s.tabLoud / s.tabSent) : 0;
  const micNote = s.meet ? `, im theo app ${s.meet}` : (s.muted ? `, chặn vọng âm ${s.muted}` : '');
  $('liveStats').textContent = `Tab: gửi ${s.tabSent} khối, có tiếng ${tabLoud}% · Mic: gửi ${s.sent}${micNote}` +
    (s.wait ? ` · chờ server ${s.wait}` : '');
  if (s.tabSent > 40 && tabLoud === 0) setMsg('Tab không phát ra tiếng nào - kiểm tra tab có bị tắt tiếng không, hoặc đã bấm trên đúng tab họp chưa.', 'err');
}

async function doStart() {
  setMsg('');
  const sel = $('meeting').value;
  const title = $('newTitle').value.trim();
  $('start').disabled = true;
  let mid = sel;
  if (!mid && title) {
    const c = await send({ cmd: 'bg:create', title });
    if (!c || !c.ok) { setMsg(c && c.error || 'Không tạo được cuộc họp', 'err'); $('start').disabled = false; return; }
    mid = c.data.id || c.data.meeting_id;
  }
  if (!mid) { setMsg('Chọn một cuộc họp hoặc nhập tên để tạo mới.', 'err'); $('start').disabled = false; return; }
  const r = await send({ cmd: 'bg:start', mid: Number(mid), micOn: $('micOn').checked });
  $('start').disabled = false;
  if (!r || !r.ok) { setMsg(r && r.error || 'Không bắt đầu được', 'err'); return; }
  await refreshRunningView();
}

async function init() {
  $('refresh').onclick = async () => {
    setMsg('Đang lấy lại quyền...', 'muted');
    const r = await send({ cmd: 'bg:refresh' });
    if (!r || !r.ok) { setMsg(r && r.error || 'Chưa lấy được quyền', 'err'); return; }
    setMsg('');
    if (await showAuth()) await loadMeetings();
  };
  $('start').onclick = doStart;
  $('stop').onclick = async () => { await send({ cmd: 'bg:stop' }); setMsg(''); await refreshRunningView(); };
  $('muteSync').onclick = async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url) { setMsg('Không đọc được tab đang mở.', 'err'); return; }
    let origin; try { origin = new URL(tab.url).origin + '/*'; } catch (e) { setMsg('Tab này không đồng bộ được.', 'err'); return; }
    let granted = false;
    try { granted = await chrome.permissions.request({ origins: [origin] }); } catch (e) { granted = false; }
    if (!granted) { setMsg('Chưa được cấp quyền đọc trang này nên chưa đồng bộ mute được (bấm lại và chọn Cho phép).', 'err'); return; }
    const r = await send({ cmd: 'bg:injectMute', tabId: tab.id });
    setMsg(r && r.ok ? 'Đã bật: mute trong app họp thì extension cũng im.' : (r && r.error || 'Không bật được'), r && r.ok ? 'ok' : 'err');
  };

  const running = await refreshRunningView();
  const authed = await showAuth();
  if (!authed) { await send({ cmd: 'bg:refresh' }); await showAuth(); }   // thử đọc token từ tab app đang mở
  if (!running && (await send({ cmd: 'bg:auth' })).token) await loadMeetings();
}

init();
