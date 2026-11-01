// Trang xin quyền mic. Popup KHÔNG xin được: hộp thoại quyền làm popup mất focus -> popup đóng -> promise reject
// "Permission dismissed". Tab thường thì không bị, và quyền cấp cho origin chrome-extension:// dùng chung cho offscreen.
const out = document.getElementById('out');
function show(text, cls) { out.className = cls || ''; out.textContent = text; }

async function refresh() {
  try {
    const st = await navigator.permissions.query({ name: 'microphone' });
    if (st.state === 'granted') show('Đã có quyền micro. Đóng tab này và bấm lại vào extension.', 'ok');
  } catch (e) { /* trình duyệt cũ không hỗ trợ */ }
}

document.getElementById('ask').onclick = async () => {
  show('Đang chờ anh chị chọn Allow...', '');
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach(t => t.stop());          // chỉ xin quyền, không giữ mic
    show('Xong. Đóng tab này và bấm lại vào extension.', 'ok');
  } catch (e) {
    show(e && e.name === 'NotAllowedError'
      ? 'Anh chị đã từ chối hoặc bỏ qua. Bấm lại nút trên, hoặc mở ổ khóa trên thanh địa chỉ để bật Microphone.'
      : String((e && e.message) || e), 'err');
  }
};
refresh();
