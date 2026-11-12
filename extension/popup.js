// Popup: hai việc TÁCH BẠCH. "Tiếp tục" nối vào một cuộc họp đang mở (không bao giờ tạo mới); "Tạo cuộc họp mới" luôn tạo
// (không bao giờ chạy vào cuộc họp cũ). Bản 0.3 chỉ có một nút "Bắt đầu nghe" lấy cuộc họp đầu tiên trong ô chọn - ô này
// tải chậm và tự chọn, nên lúc thì tạo mới dù muốn nối tiếp, lúc lại chạy vào cuộc họp cũ dù đã gõ tên.
// Mọi việc nặng (lấy streamId, mở offscreen, gọi API) do background làm; popup chỉ ra lệnh và hiển thị trạng thái.
const $ = (id) => document.getElementById(id);
const send = (msg) => chrome.runtime.sendMessage(msg);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const BD_ON = 'Ẩn bảng gợi ý BD trên tab họp', BD_OFF = 'Hiện bảng gợi ý BD trên tab họp';
let statsTimer = null;
let liveList = [];      // cuộc họp chưa kết thúc của người dùng (server lọc ?live=1; lọc lại ở đây cho server cũ)
let last = null;        // cuộc họp extension nghe lần gần nhất trên server đang dùng ({mid, title, at})
let base = '';

function setMsg(text, cls) { const m = $('msg'); m.textContent = text || ''; m.className = 'row ' + (cls || ''); }
function ago(ts) {
  const m = Math.round((Date.now() - ts) / 60000);
  return m < 1 ? 'vừa xong' : m < 60 ? `${m} phút trước` : m < 1440 ? `${Math.round(m / 60)} giờ trước` : `${Math.round(m / 1440)} ngày trước`;
}

async function showAuth() {
  const a = await send({ cmd: 'bg:auth' });
  base = (a && a.base) || '';
  if (a && a.token) {
    $('auth').innerHTML = `<span class="ok">Đã lấy quyền từ ${esc(base.replace(/^https?:\/\//, ''))}</span>`;
    return true;
  }
  $('auth').innerHTML = '<span class="err">Chưa có quyền. Mở và đăng nhập Meeting Copilot trong một tab rồi bấm </span>'
    + '<button type="button" class="link" id="refresh">Lấy lại quyền đăng nhập</button>';
  $('refresh').onclick = async () => {
    setMsg('Đang lấy lại quyền...', 'muted');
    const r = await send({ cmd: 'bg:refresh' });
    if (!r || !r.ok) { setMsg(r && r.error || 'Chưa lấy được quyền', 'err'); return; }
    setMsg('');
    if (await showAuth()) await loadLists();
  };
  return false;
}

async function loadLists() {
  last = await send({ cmd: 'bg:last' });
  if (last && !last.mid) last = null;
  await Promise.all([loadMeetings(), loadGroups()]);
}

async function loadMeetings() {
  const sel = $('meeting');
  sel.disabled = true; $('resume').disabled = true;
  sel.innerHTML = '<option value="">Đang tải danh sách...</option>';
  const r = await send({ cmd: 'bg:meetings' });
  if (!r || !r.ok) {
    sel.innerHTML = '<option value="">(không tải được danh sách)</option>';
    setMsg(r && r.error || 'Không tải được danh sách cuộc họp', 'err');
    return;
  }
  liveList = (r.data.meetings || []).filter(m => m.status !== 'ended');
  renderResume();
}

function renderResume() {
  const sel = $('meeting'), note = $('lastNote'), btn = $('resume');
  const lastTxt = last ? `#${last.mid} "${last.title || 'Cuộc họp'}" (${ago(last.at)})` : '';
  if (!liveList.length) {
    sel.innerHTML = '<option value="">(không có cuộc họp nào đang mở)</option>';
    sel.disabled = true; btn.disabled = true; btn.textContent = 'Tiếp tục nghe';
    note.textContent = last ? `Cuộc họp nghe lần trước ${lastTxt} đã kết thúc. Tạo cuộc họp mới bên dưới.`
      : 'Chưa có cuộc họp nào đang mở: tạo mới bên dưới.';
    return;
  }
  sel.innerHTML = liveList.map(m => `<option value="${m.id}">#${m.id} · ${esc(m.title || 'Cuộc họp')}</option>`).join('');
  const lastLive = last && liveList.find(m => Number(m.id) === Number(last.mid));
  sel.value = String(lastLive ? lastLive.id : liveList[0].id);
  sel.disabled = false;
  note.textContent = lastLive ? `Lần trước extension nghe ${lastTxt}: đã chọn sẵn.`
    : last ? `Cuộc họp nghe lần trước ${lastTxt} đã kết thúc.` : '';
  updateResume();
}

function updateResume() {
  const m = liveList.find(x => String(x.id) === $('meeting').value);
  $('resume').disabled = !m;
  $('resume').textContent = m ? `Tiếp tục nghe #${m.id} · ${(m.title || 'Cuộc họp').slice(0, 36)}` : 'Tiếp tục nghe';
}

async function loadGroups() {
  const sel = $('group');
  const r = await send({ cmd: 'bg:groups' });
  const gs = (r && r.ok && r.data && r.data.groups) || [];
  sel.innerHTML = '<option value="">Không thuộc nhóm</option>'
    + gs.map(g => `<option value="${g.id}">${esc(g.name)}${g.bd_mode ? ' · nhóm BD (có bảng gợi ý)' : ''}</option>`).join('');
}

function busy(on, text) {
  ['resume', 'create', 'reload'].forEach(id => { $(id).disabled = on; });
  if (!on) { updateResume(); $('create').disabled = !$('newTitle').value.trim(); }
  if (text !== undefined) setMsg(text, 'muted');
}

async function startInto(mid) {
  const r = await send({ cmd: 'bg:start', mid: Number(mid), micOn: $('micOn').checked });
  if (!r || !r.ok) { setMsg(r && r.error || 'Không bắt đầu được', 'err'); return false; }
  setMsg('');
  await refreshRunningView();
  return true;
}

async function doResume() {
  const m = liveList.find(x => String(x.id) === $('meeting').value);
  if (!m) { setMsg('Chọn một cuộc họp đang mở để tiếp tục.', 'err'); return; }
  busy(true, `Đang nối vào cuộc họp #${m.id}...`);
  await startInto(m.id);
  busy(false);
}

async function doCreate() {
  const title = $('newTitle').value.trim();
  if (!title) { setMsg('Nhập tên cuộc họp mới.', 'err'); $('newTitle').focus(); return; }
  const gid = $('group').value ? Number($('group').value) : null;
  busy(true, 'Đang tạo cuộc họp mới...');
  const c = await send({ cmd: 'bg:create', title, group_id: gid });
  if (!c || !c.ok) { setMsg(c && c.error || 'Không tạo được cuộc họp', 'err'); busy(false); return; }
  const mid = c.data.meeting_id || c.data.id;
  $('newTitle').value = '';                       // đã tạo rồi: không để bấm lần nữa tạo thêm một cuộc họp trùng tên
  setMsg(`Đã tạo cuộc họp #${mid}, đang bắt đầu nghe...`, 'muted');
  if (!(await startInto(mid))) await loadMeetings();   // bắt đầu không được thì cuộc họp vẫn mở: lần sau bấm "Tiếp tục"
  busy(false);
}

/** Dòng trạng thái kết nối từ các luồng (offscreen báo qua stats; chưa có stats thì theo trạng thái background). */
function connText(run, s) {
  const L = (s && s.links) || (run && run.links) || {};
  const names = { tab: 'tiếng tab', mic: 'mic', bd: 'bảng BD' };
  const bad = Object.keys(L).filter(k => L[k] && L[k].conn && L[k].conn !== 'ok');
  if (bad.some(k => L[k].conn === 'auth')) {
    return ['Phiên đăng nhập hết hạn. Mở Meeting Copilot và đăng nhập lại; extension tự nối tiếp vào cuộc họp này.', 'err'];
  }
  const taken = bad.find(k => L[k].conn === 'taken');
  if (taken) return [`Một máy khác vừa bật ${names[taken] || taken} cho cuộc họp này, luồng ${names[taken] || taken} ở máy này đã dừng.`, 'err'];
  const re = bad.filter(k => L[k].conn === 'reconnecting');
  if (re.length) {
    return [`Mất kết nối ${re.map(k => names[k] || k).join(', ')}, đang nối lại (lần ${L[re[0]].tries || 1})... `
      + `Lời thoại sẽ nối tiếp vào cuộc họp #${run ? run.mid : ''}, không tạo cuộc họp mới.`, 'warn'];
  }
  return ['Kết nối ổn định.', 'ok'];
}

async function refreshRunningView() {
  const st = await send({ cmd: 'bg:state' });
  const run = st && st.running;
  const live = !!(run && run.mid);
  $('pick').style.display = live ? 'none' : '';
  $('live').style.display = live ? '' : 'none';
  if (live) {
    $('liveInfo').textContent = `Đang nghe ${run.micOn ? 'tab + mic' : 'tab'}, gửi về cuộc họp #${run.mid}${run.title ? ` "${run.title}"` : ''}.`;
    const [t, cls] = connText(run, null);
    $('liveConn').textContent = t; $('liveConn').className = 'row ' + cls;
    $('bdRow').style.display = run.bd ? '' : 'none';
    $('bdToggle').textContent = run.bdOpen ? BD_ON : BD_OFF;
    if (!statsTimer) statsTimer = setInterval(pollStats, 1000);
  } else {
    if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
    if (st && st.note) setMsg(st.note, 'warn');
  }
  return live;
}

async function pollStats() {
  let s = null, st = null;
  try { s = await send({ cmd: 'offscreen:stats' }); st = await send({ cmd: 'bg:state' }); } catch (e) { return; }
  const run = st && st.running;
  if (!run) { await refreshRunningView(); return; }
  if (!s || typeof s.tabSent !== 'number') return;
  const [t, cls] = connText(run, s);
  $('liveConn').textContent = t; $('liveConn').className = 'row ' + cls;
  const tabLoud = s.tabSent ? Math.round(100 * s.tabLoud / s.tabSent) : 0;
  const micNote = s.meet ? `, im theo app ${s.meet}` : (s.muted ? `, chặn vọng âm ${s.muted}` : '');
  $('liveStats').textContent = `Tab: gửi ${s.tabSent} khối, có tiếng ${tabLoud}% · Mic: gửi ${s.sent}${micNote}`
    + (s.wait ? ` · chờ server ${s.wait}` : '');
  if (s.tabSent > 40 && tabLoud === 0) setMsg('Tab không phát ra tiếng nào - kiểm tra tab có bị tắt tiếng không, hoặc đã bấm trên đúng tab họp chưa.', 'err');
}

async function init() {
  // Chrome nạp lại background.js chỉ khi bấm Reload ở chrome://extensions, còn popup đọc mới từ đĩa: kéo code mới về mà
  // chưa Reload thì popup mới nói chuyện với phần nền cũ (không biết bg:groups, bg:state trả dạng cũ) -> báo rõ, không chạy.
  let pong = null;
  try { pong = await send({ cmd: 'bg:ping' }); } catch (e) { pong = null; }
  if (!pong || pong.proto !== 2) {
    $('auth').innerHTML = '<span class="err">Extension chưa nạp bản mới: phần nền vẫn chạy bản cũ. Mở <b>chrome://extensions</b>, bấm '
      + '<b>Reload</b> (biểu tượng ⟳) ở thẻ "UrBox Meeting Copilot - thu tiếng tab" (số bản phải là 0.4.0), rồi mở lại popup này.</span>';
    $('pick').style.display = 'none';
    $('live').style.display = 'none';
    return;
  }
  $('resume').onclick = doResume;
  $('create').onclick = doCreate;
  $('reload').onclick = () => loadMeetings();
  $('meeting').onchange = updateResume;
  $('newTitle').oninput = () => { $('create').disabled = !$('newTitle').value.trim(); };
  $('newTitle').onkeydown = (e) => { if (e.key === 'Enter' && $('newTitle').value.trim() && !$('create').disabled) doCreate(); };
  $('stop').onclick = async () => {
    $('stop').disabled = true;
    await send({ cmd: 'bg:stop' });
    $('stop').disabled = false;
    setMsg('');
    await refreshRunningView();
    await loadLists();
  };
  $('openApp').onclick = async () => {
    const st = await send({ cmd: 'bg:state' });
    const run = st && st.running;
    if (run && base) chrome.tabs.create({ url: `${base}/#/m/${run.mid}` });
  };
  $('bdToggle').onclick = async () => {
    const r = await send({ cmd: 'bg:bdToggle' });
    $('bdToggle').textContent = r && r.open ? BD_ON : BD_OFF;
  };
  $('muteSync').onclick = async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url) { setMsg('Không đọc được tab đang mở.', 'err'); return; }
    let origin;
    try { origin = new URL(tab.url).origin + '/*'; } catch (e) { setMsg('Tab này không đồng bộ được.', 'err'); return; }
    let granted = false;
    try { granted = await chrome.permissions.request({ origins: [origin] }); } catch (e) { granted = false; }
    if (!granted) { setMsg('Chưa được cấp quyền đọc trang này nên chưa đồng bộ mute được (bấm lại và chọn Cho phép).', 'err'); return; }
    const r = await send({ cmd: 'bg:injectMute', tabId: tab.id });
    setMsg(r && r.ok ? 'Đã bật: mute trong app họp thì extension cũng im.' : (r && r.error || 'Không bật được'), r && r.ok ? 'ok' : 'err');
  };

  const running = await refreshRunningView();
  let authed = await showAuth();
  if (!authed) { await send({ cmd: 'bg:refresh' }); authed = await showAuth(); }   // thử đọc token từ tab app đang mở
  if (!running && authed) await loadLists();
}

init();
