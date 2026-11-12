// Bảng gợi ý BD nổi ngay trên tab họp (Google Meet / Teams / Zoom...), để đội BD không phải chuyển sang tab Copilot.
// Dữ liệu: offscreen giữ WebSocket /ws/meeting/<id>/bd, background chuyển sự kiện (bd_turn, bd_status) sang đây; hỏi thêm
// và "Tra thêm trên mạng" gửi ngược về background gọi API. Chỉ hiện khi cuộc họp thuộc nhóm bật chế độ BD.
// Vẽ trong shadow DOM (CSS của Meet không ảnh hưởng); phím gõ trong bảng không lọt tới trang (phím tắt của Meet).
// CẨN THẬN: chia sẻ CẢ MÀN HÌNH thì khách thấy bảng này; chia sẻ một cửa sổ / tab khác thì không.
(function () {
  if (window.__maBdPanel) { window.__maBdPanel.hello(); return; }     // manifest + executeScript có thể tiêm hai lần
  const alive = () => !!(chrome.runtime && chrome.runtime.id);
  const send = (msg) => {
    try { const p = chrome.runtime.sendMessage(msg); return p && p.catch ? p.catch(() => null) : Promise.resolve(null); }
    catch (e) { return Promise.resolve(null); }
  };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const st = { mid: 0, title: '', base: '', turns: [], busy: 0, open: false, min: false, unread: 0, fresh: null, pos: null };
  let host = null, root = null, list = null;

  const CSS = `
    :host{all:initial}
    .p{position:fixed;right:16px;bottom:88px;width:360px;max-height:70vh;display:flex;flex-direction:column;background:#17181c;
      color:#e8e8ea;border:1px solid #3a3b42;border-radius:12px;box-shadow:0 12px 40px #0008;font:13px/1.45 system-ui,"Segoe UI",sans-serif;
      z-index:2147483647;overflow:hidden}
    .p.min .b,.p.min .f{display:none}
    .h{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#23242a;cursor:move;user-select:none}
    .h b{font-size:13px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .h .s{font-size:11.5px;color:#a0a1a8;white-space:nowrap;max-width:150px;overflow:hidden;text-overflow:ellipsis}
    .h button{background:none;border:none;color:#c9cad1;font-size:16px;line-height:1;cursor:pointer;padding:2px 6px;border-radius:6px}
    .h button:hover{background:#ffffff1a}
    .badge{background:#7c3aed;color:#fff;border-radius:999px;font-size:11px;padding:1px 7px}
    .b{overflow:auto;padding:8px 10px;flex:1;min-height:0;display:flex;flex-direction:column;gap:10px}
    .tn{display:flex;flex-direction:column;gap:4px}
    .q{font-weight:600}
    .m{font-size:11.5px;color:#a0a1a8}
    .a{background:#23242a;border-radius:10px;padding:8px 10px;white-space:pre-wrap;word-break:break-word}
    .a.fresh{outline:2px solid #7c3aed}
    .a.web{border:1px dashed #b7791f;background:#2a2518}
    .a.err{color:#f87171}
    .n{margin:4px 0 0 16px;padding:0}
    .n li{margin:2px 0}
    .src{margin-top:6px;font-size:11.5px;color:#a0a1a8;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
    .src a{color:#c4b5fd}
    .chip{border:1px solid #3a3b42;border-radius:999px;padding:0 7px;font-size:10.5px}
    .lnk{background:none;border:none;color:#c4b5fd;cursor:pointer;font-size:11.5px;padding:0;text-decoration:underline;font-family:inherit}
    .lnk:disabled{opacity:.5;cursor:default}
    .f{display:flex;gap:6px;padding:8px 10px;border-top:1px solid #3a3b42;background:#1c1d22}
    .f input{flex:1;min-width:0;padding:7px 9px;border-radius:8px;border:1px solid #3a3b42;background:#111216;color:#e8e8ea;font:13px system-ui,"Segoe UI",sans-serif}
    .f button{padding:7px 10px;border-radius:8px;border:none;background:#7c3aed;color:#fff;font-weight:600;cursor:pointer;font-family:inherit}
    .f button:disabled{opacity:.5}
    .empty{color:#a0a1a8;font-size:12.5px;padding:4px 0}
    .warn{font-size:11px;color:#fbbf24;margin-bottom:4px}`;

  function build() {
    host = document.createElement('div');
    host.id = 'ma-bd-panel-host';
    root = host.attachShadow({ mode: 'closed' });
    root.innerHTML = `<style>${CSS}</style><div class="p" id="p">
      <div class="h" id="h"><b id="t">Gợi ý BD</b><span class="s" id="s"></span><span class="badge" id="u" style="display:none"></span>
        <button id="min" title="Thu gọn / mở ra">–</button><button id="x" title="Ẩn (hiện lại bằng nút trong popup extension)">×</button></div>
      <div class="b" id="b"></div>
      <div class="f"><input id="q" placeholder="Hỏi trợ lý BD..." maxlength="500"><button id="ask">Hỏi</button></div></div>`;
    // Phím gõ trong bảng không tới trang họp (Meet có phím tắt ctrl+d, ctrl+e... và bắt phím trên document).
    ['keydown', 'keyup', 'keypress'].forEach(ev => host.addEventListener(ev, e => e.stopPropagation()));
    (document.body || document.documentElement).appendChild(host);
    list = root.getElementById('b');
    root.getElementById('x').onclick = () => { hide(); send({ cmd: 'bdpanel:hide' }); };
    root.getElementById('min').onclick = () => {
      st.min = !st.min;
      root.getElementById('p').classList.toggle('min', st.min);
      if (!st.min) { st.unread = 0; renderHead(); scroll(); }
    };
    root.getElementById('ask').onclick = ask;
    root.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') ask(); });
    list.addEventListener('click', onClick);
    drag(root.getElementById('h'), root.getElementById('p'));
  }

  function drag(handle, panel) {
    let sx = 0, sy = 0, ox = 0, oy = 0, moving = false;
    handle.addEventListener('mousedown', e => {
      if (e.target.tagName === 'BUTTON') return;
      const r = panel.getBoundingClientRect();
      moving = true; sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
      e.preventDefault();
    });
    window.addEventListener('mousemove', e => { if (moving) place(panel, ox + e.clientX - sx, oy + e.clientY - sy); });
    window.addEventListener('mouseup', () => {
      if (!moving) return;
      moving = false;
      const r = panel.getBoundingClientRect();
      st.pos = { left: r.left, top: r.top };
      try { chrome.storage.local.set({ bdPos: st.pos }); } catch (e) {}
    });
  }
  function place(panel, left, top) {
    const w = panel.offsetWidth, h = panel.offsetHeight;
    left = Math.max(0, Math.min(window.innerWidth - w, left));
    top = Math.max(0, Math.min(window.innerHeight - h, top));
    panel.style.left = left + 'px'; panel.style.top = top + 'px'; panel.style.right = 'auto'; panel.style.bottom = 'auto';
  }

  const who = t => t.source === 'auto' ? `Khách hỏi${t.speaker ? ` (${esc(t.speaker)})` : ''} - trợ lý tự nhận ra`
    : t.source === 'marked' ? 'Hỏi về câu trong lời nói' : `${t.by ? esc(String(t.by).split('@')[0]) : 'Đội BD'} hỏi`;
  function at(t) {
    try { return new Date((t.at || 0) * 1000).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' }); } catch (e) { return ''; }
  }
  const legend = srcs => (srcs || []).map(x => `<span><b>${esc(x.code)}</b> ${x.url && /^https?:\/\//i.test(x.url)
    ? `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.domain || x.title || 'trang web')}</a>`
    : esc(x.title || '')}${x.ref ? `, ${esc(x.ref)}` : ''}</span>`).join('');
  function webHtml(t) {
    if (t.status !== 'done' && t.status !== 'error') return '';
    const w = t.web;
    const btn = label => `<button class="lnk" data-web="${esc(t.id)}" title="Tìm thêm trên internet cho câu hỏi này: khoảng 15-30 giây, có tính phí">${label}</button>`;
    if (!w) return `<div class="src">${btn('Tra thêm trên mạng')}</div>`;
    if (w.status === 'pending') return '<div class="a web m">Đang tra trên mạng, khoảng 15-30 giây...</div>';
    if (w.status === 'error') return `<div class="src"><span style="color:#f87171">Chưa tra được trên mạng: ${esc(w.error || '')}</span>${btn('Tra lại')}</div>`;
    return `<div class="a web"><div class="warn">Trên mạng: thông tin công khai, chưa kiểm chứng. Kiểm tra lại trước khi nói với khách.</div>${esc(w.answer || '')}`
      + `${(w.notes || []).length ? `<ul class="n">${w.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}<div class="src">${legend(w.sources)}</div></div>`;
  }
  function turnHtml(t) {
    let a;
    if (t.status === 'pending') a = '<div class="a m">Đang tìm trong tài liệu, các buổi trước và kho tri thức...</div>';
    else if (t.status === 'error') a = `<div class="a err">${esc(t.answer || 'Chưa trả lời được')}</div>`;
    else {
      a = `<div class="a ${t.id === st.fresh ? 'fresh' : ''}">${esc(t.answer || '')}`
        + `${(t.notes || []).length ? `<div class="m" style="margin-top:6px">Ghi chú cho đội mình</div><ul class="n">${t.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}`
        + `<div class="src">${t.confidence ? `<span class="chip">độ chắc: ${esc(t.confidence)}</span>` : ''}${legend(t.sources)}`
        + `<button class="lnk" data-copy="${esc(t.id)}" style="margin-left:auto">Chép</button></div></div>`;
    }
    return `<div class="tn"><div class="m">${who(t)}, ${at(t)}</div><div class="q">${esc(t.question || t.asked || '')}</div>${a}${webHtml(t)}</div>`;
  }
  function scroll() { if (list) list.scrollTop = list.scrollHeight; }
  function renderHead() {
    if (!root) return;
    root.getElementById('t').textContent = `Gợi ý BD · ${st.title || ('#' + st.mid)}`;
    root.getElementById('s').textContent = st.busy ? `đang trả lời ${st.busy} câu...` : 'trợ lý đang nghe';
    const u = root.getElementById('u');
    u.style.display = st.min && st.unread ? '' : 'none';
    u.textContent = st.unread ? `${st.unread} mới` : '';
  }
  function render() {
    if (!host) return;
    renderHead();
    const near = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    list.innerHTML = st.turns.length ? st.turns.map(turnHtml).join('')
      : '<div class="empty">Khi khách hỏi hoặc băn khoăn, trợ lý tự nhận ra và trả lời ở đây từ tài liệu của nhóm, các cuộc họp trước và kho tri thức '
        + 'WikiBox (K: tài liệu, M: cuộc họp trước, U: WikiBox, W: trang web). Hoặc gõ câu hỏi bên dưới.</div>';
    st.fresh = null;
    if (near || st.turns.length <= 1) scroll();
  }
  function flash(text) { if (!root) return; root.getElementById('s').textContent = text; setTimeout(renderHead, 4000); }

  function upsert(t) {
    const i = st.turns.findIndex(x => x.id === t.id);
    const prev = i >= 0 ? st.turns[i].status : null;
    if (t.status === 'none') { if (i >= 0) st.turns.splice(i, 1); }       // trợ lý thấy không phải câu hỏi: gỡ khỏi bảng
    else if (i >= 0) st.turns[i] = t;
    else st.turns.push(t);
    if (t.status === 'done' && prev !== 'done') { st.fresh = t.id; if (st.min) st.unread++; }
    render();
  }

  async function onClick(e) {
    const c = e.target.closest('[data-copy]');
    if (c) {
      const t = st.turns.find(x => x.id === c.dataset.copy);
      if (!t) return;
      try {
        await navigator.clipboard.writeText((t.answer || '').replace(/\s*\[[KMUW]\d{1,2}\]/g, ''));
        c.textContent = 'Đã chép'; setTimeout(() => { c.textContent = 'Chép'; }, 1500);
      } catch (err) { flash('Không chép được'); }
      return;
    }
    const w = e.target.closest('[data-web]');
    if (w) {
      w.disabled = true;
      const r = await send({ cmd: 'bdpanel:web', turn_id: w.dataset.web });
      if (r && r.ok && r.data && r.data.turn) upsert(r.data.turn);
      else { w.disabled = false; flash((r && r.error) || 'Chưa tra được'); }
    }
  }
  async function ask() {
    const inp = root.getElementById('q'), btn = root.getElementById('ask');
    const q = inp.value.trim();
    if (!q) return;
    btn.disabled = true;
    const r = await send({ cmd: 'bdpanel:ask', question: q });
    btn.disabled = false;
    if (r && r.ok) { inp.value = ''; if (r.data && r.data.turn) upsert(r.data.turn); }
    else flash((r && r.error) || 'Chưa gửi được câu hỏi');
  }

  function show(state) {
    if (state) { st.mid = state.mid; st.title = state.title || ''; st.base = state.base || ''; st.turns = (state.turns || []).slice(); }
    if (!host) build();
    st.open = true;
    host.style.display = '';
    if (st.pos) place(root.getElementById('p'), st.pos.left, st.pos.top);
    render();
  }
  function hide() { st.open = false; if (host) host.style.display = 'none'; }
  function destroy() { if (host) host.remove(); host = root = list = null; st.open = false; st.turns = []; }

  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || !alive()) return;
    if (msg.cmd === 'bdpanel:show') show(msg.state);
    else if (msg.cmd === 'bdpanel:hide') hide();
    else if (msg.cmd === 'bdpanel:stop') destroy();
    else if (msg.cmd === 'bdpanel:event' && st.open) {
      const ev = msg.ev || {};
      if (ev.type === 'bd_init') { st.turns = ev.turns || []; st.busy = ev.busy || 0; render(); }
      else if (ev.type === 'bd_turn' && ev.turn) upsert(ev.turn);
      else if (ev.type === 'bd_status') { st.busy = ev.busy || 0; renderHead(); }
    }
  });

  async function hello() {
    if (!alive()) return;
    try { const { bdPos } = await chrome.storage.local.get('bdPos'); if (bdPos) st.pos = bdPos; } catch (e) {}
    const state = await send({ cmd: 'bdpanel:hello' });     // background: cuộc họp đang nghe có bảng BD không, các lượt đã có
    if (state && state.mid && state.open) show(state); else hide();
  }
  window.__maBdPanel = { hello };
  hello();
})();
