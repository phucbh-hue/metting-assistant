"""Bộ slide demo "UrBox Meeting Copilot": HTML -> ảnh (Chromium) -> PPTX và PDF.

- PPTX (slides/demo/): mỗi slide một ảnh phủ kín + ghi chú là lời Jarvis trình bày + chữ nằm dưới ảnh để ứng dụng tìm
  kiếm và nhắc bài. Ứng dụng hiện cả hình (kể cả máy không có LibreOffice) và đọc ghi chú khi chọn "Theo ghi chú
  trong tệp".
- PDF (docs/demo/): cùng nội dung, chữ chọn được, để gửi người tham dự.
- docs/demo/kich-ban-demo.md: mục "Lời Jarvis trình bày" được viết lại từ ghi chú bên dưới.

Chạy: python docs/demo/build_deck.py   (cần playwright + chromium, python-pptx, Pillow; tải font Be Vietnam Pro từ
Google Fonts). Sửa lời trình bày: sửa "notes" trong SLIDES rồi chạy lại.
"""
import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
IMG = (REPO / "docs" / "huong-dan" / "img").as_uri()
BD = (HERE / "img" / "bd-panel.png").as_uri()
PANEL = (HERE / "img").as_uri()
SCRIPT_MD = HERE / "kich-ban-demo.md"
MARK = "<!-- LOI_TRINH_BAY -->"
MASCOT = (REPO / "static" / "mascot" / "urbox-mascot.svg").as_uri()
NAME = "UrBox Meeting Copilot - Demo 10.2026"

I = {  # biểu tượng nét (24x24)
    "mic": '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
    "ask": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6"/><path d="M12 16h.01"/>',
    "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "cloud": '<path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>',
    "spark": '<path d="M12 3l1.9 5.8L20 10l-6.1 1.2L12 17l-1.9-5.8L4 10l6.1-1.2z"/>',
    "eyeoff": '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M1 1l22 22"/>',
    "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "brief": '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "video": '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="M16 10l6-4v12l-6-4z"/>',
    "doc": '<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M8 7h8M8 11h8M8 15h5"/>',
}


def ic(name, size=34, color="#B98AFF"):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{I[name]}</svg>')


def shot(src, w, h, pos="50% 0%", extra=""):
    return f'<div class="shot" style="width:{w}px;height:{h}px;{extra}"><img src="{src}" style="object-position:{pos}"></div>'


def row(icon, head, text, color="#B98AFF"):
    return (f'<div class="row"><div class="ic">{ic(icon, 30, color)}</div><div><div class="rh">{head}</div>'
            f'<div class="rt">{text}</div></div></div>')


SLIDES = [
    dict(title="UrBox Meeting Copilot", plain=True, body=f"""
      <div class="cover">
        <div class="cl">
          <span class="pill">Demo nội bộ, tháng 10/2026</span>
          <h1>UrBox Meeting Copilot</h1>
          <p class="sub">Trợ lý cuộc họp AI:<br>nghe, ghi nhớ và gợi ý ngay trong buổi họp</p>
          <p class="gold">Bộ slide này do trợ lý Jarvis tự mở và trình bày</p>
          <p class="who">Phụ trách: phuc.bh@urbox.vn</p>
        </div>
        <div class="cr"><div class="halo"></div><img class="mascot" src="{MASCOT}"></div>
      </div>""",
         hidden=["Trợ lý cuộc họp AI: nghe, ghi nhớ và gợi ý ngay trong buổi họp", "Demo nội bộ tháng 10/2026",
                 "Bộ slide do Jarvis tự trình bày"],
         notes="Dạ em chào anh chị. Em là Jarvis, trợ lý họp của UrBox. Hôm nay em xin phép tự giới thiệu về chính mình: "
               "UrBox Meeting Copilot. Bộ slide anh chị đang xem cũng do em mở và trình bày. Em sẽ nói khoảng mười phút "
               "về những việc em làm được trong một cuộc họp. Sau đó anh Phúc sẽ demo trực tiếp trên hệ thống để anh chị "
               "thấy em làm việc thật."),
    dict(title="Mỗi cuộc họp đều để lại việc phải làm thêm", body=f"""
      <div class="grid2">
        <div class="card"><div class="ic big">{ic('doc', 40)}</div><h3>Ghi chép tay</h3><p>Người ghi vừa nghe vừa chép, dễ sót ý và nhầm ai nói gì.</p></div>
        <div class="card"><div class="ic big">{ic('clock', 40)}</div><h3>Biên bản đến muộn</h3><p>Tổng hợp quyết định, người phụ trách, hạn chót mất thêm thời gian sau buổi họp.</p></div>
        <div class="card"><div class="ic big">{ic('history', 40)}</div><h3>Quên điều đã chốt</h3><p>Buổi sau nói khác buổi trước mà không ai nhận ra.</p></div>
        <div class="card"><div class="ic big">{ic('ask', 40)}</div><h3>Khách hỏi, phải tra lại</h3><p>Đội kinh doanh mở bảng giá, tìm lại cuộc họp cũ ngay trước mặt khách.</p></div>
      </div>""",
         hidden=["Ghi chép tay dễ sót ý", "Biên bản đến muộn", "Quên điều đã chốt ở buổi trước",
                 "Khách hỏi, đội kinh doanh phải tra lại"],
         notes="Trước hết là chuyện quen thuộc. Trong mỗi cuộc họp luôn có một người vừa nghe vừa chép, nên rất dễ sót ý "
               "hoặc nhầm ai nói gì. Họp xong lại mất thêm thời gian để viết biên bản, liệt kê quyết định, người phụ trách "
               "và hạn chót. Sang buổi sau, có khi mình nói khác điều đã chốt ở buổi trước mà không ai nhận ra. Còn với đội "
               "kinh doanh, khi khách hỏi giá hay chính sách, anh chị phải mở tài liệu, tìm lại cuộc họp cũ ngay trước mặt khách."),
    dict(title="Một trợ lý ngồi trong mọi cuộc họp", body=f"""
      <div class="flow">
        <div class="step"><div class="num">1</div>{ic('mic', 52)}<h3>Nghe</h3><p>Chép lời theo thời gian thực, nhận ra từng người nói</p></div>
        <div class="arrow">›</div>
        <div class="step"><div class="num">2</div>{ic('spark', 52)}<h3>Hiểu và làm</h3><p>Gọi Jarvis bằng giọng nói: tóm tắt, soạn slide, dashboard, tra cứu</p></div>
        <div class="arrow">›</div>
        <div class="step"><div class="num">3</div>{ic('file', 52)}<h3>Ghi nhớ</h3><p>Biên bản tự động, lưu theo nhóm, so với các buổi trước</p></div>
        <div class="arrow">›</div>
        <div class="step"><div class="num">4</div>{ic('brief', 52, '#FFB700')}<h3>Hỗ trợ bán hàng</h3><p>Bảng BD gợi ý câu trả lời khi khách hỏi</p></div>
      </div>
      <p class="note">Dùng ngay trên trình duyệt, đăng nhập bằng tài khoản Google @urbox.vn</p>""",
         hidden=["Nghe: chép lời theo thời gian thực, nhận ra từng người nói", "Hiểu và làm: gọi Jarvis bằng giọng nói",
                 "Ghi nhớ: biên bản tự động, lưu theo nhóm", "Hỗ trợ bán hàng: bảng BD"],
         notes="Meeting Copilot giải quyết bằng một trợ lý ngồi trong mọi cuộc họp, làm bốn việc. Một, em nghe và chép lời "
               "theo thời gian thực, nhận ra từng người nói. Hai, anh chị gọi em bằng giọng nói để tóm tắt, soạn slide, vẽ "
               "dashboard hay tra cứu. Ba, em ghi nhớ: tự lập biên bản, lưu theo nhóm, và so với các buổi trước. Bốn, với đội "
               "kinh doanh, em gợi ý câu trả lời khi khách hỏi. Anh chị dùng ngay trên trình duyệt, đăng nhập bằng tài khoản "
               "Google của công ty."),
    dict(title="Chép lời và nhận ra từng người nói", body=f"""
      <div class="split">
        <div class="rows">
          {row('mic', 'Chữ hiện ngay khi đang nói', 'Mỗi người nói một màu, mất mạng tự nối lại không mất chữ.')}
          {row('users', 'Tự nhận ra người quen', 'Ai đã lưu mẫu giọng thì hiện đúng tên ở mọi cuộc họp.')}
          {row('spark', 'AI đoán tên', 'Khi có người tự giới thiệu hoặc được gọi tên; chưa chắc thì hỏi lại.')}
          {row('check', 'Sửa trong một cú bấm', 'Bấm vào câu bị gán nhầm, chọn đúng người nói.')}
        </div>
        {shot(IMG + '/06-phong-hop-chep-loi.jpg', 1000, 625)}
      </div>""",
         hidden=["Chữ hiện ngay khi đang nói", "Tự nhận ra người đã lưu mẫu giọng", "AI đoán tên người nói",
                 "Sửa người nói trong một cú bấm"],
         notes="Đầu tiên là chép lời. Khi mọi người nói, chữ hiện ra ngay, mỗi người một màu. Ai đã lưu mẫu giọng thì em tự "
               "nhận ra và hiện đúng tên. Khi có người tự giới thiệu hoặc được gọi tên, em đoán tên và hỏi lại nếu chưa chắc. "
               "Nếu em gán nhầm người nói, anh chị chỉ cần bấm vào câu đó để sửa. Mẫu giọng chỉ được lưu khi người đó đồng ý."),
    dict(title="Gọi Jarvis bằng giọng nói, như gọi một đồng nghiệp", body=f"""
      <div class="split rev">
        {shot(IMG + '/08-tro-ly-tom-tat.jpg', 1000, 625)}
        <div class="bubbles">
          <div class="bub">"Jarvis ơi, tóm tắt các quyết định từ đầu buổi"</div>
          <div class="bub">"Liệt kê việc cần làm, người phụ trách và hạn chót"</div>
          <div class="bub">"Vẽ dashboard tiến độ sprint"</div>
          <div class="bub">"Tra cứu trên mạng tỷ giá USD hôm nay"</div>
          <p class="cap">Không tiện nói thì gõ vào khung chat, không cần gọi tên.</p>
        </div>
      </div>""",
         hidden=["Jarvis ơi, tóm tắt các quyết định từ đầu buổi", "Liệt kê việc cần làm, người phụ trách và hạn chót",
                 "Vẽ dashboard tiến độ sprint", "Tra cứu trên mạng tỷ giá USD hôm nay"],
         notes="Trong lúc họp, anh chị gọi em như gọi một đồng nghiệp: Jarvis ơi, tóm tắt các quyết định từ đầu buổi. Hoặc: "
               "liệt kê việc cần làm, người phụ trách và hạn chót. Em trả lời ngay, vừa làm vừa báo em đang làm gì. Em cũng "
               "tra được thông tin trên mạng, ví dụ tỷ giá hôm nay. Nếu không tiện nói, anh chị gõ vào khung chat, không cần gọi tên."),
    dict(title="Nói một câu, có ngay slide, dashboard, sơ đồ", body=f"""
      <div class="three">
        <figure>{shot(PANEL + '/p-slide.png', 440, 620)}<figcaption>Slide</figcaption></figure>
        <figure>{shot(PANEL + '/p-dash.png', 440, 620)}<figcaption>Dashboard</figcaption></figure>
        <figure>{shot(PANEL + '/p-mindmap.png', 440, 620)}<figcaption>Sơ đồ tư duy</figcaption></figure>
      </div>
      <p class="note">Sửa bằng lời: "đổi biểu đồ cột thành đường". Mỗi lần sửa là một phiên bản mới.</p>""",
         hidden=["Slide", "Dashboard", "Sơ đồ tư duy", "Sửa bằng lời, mỗi lần sửa là một phiên bản mới"],
         notes="Chỉ với một câu nói, em soạn được bộ slide, dựng dashboard có biểu đồ, hoặc vẽ sơ đồ tư duy các ý chính của "
               "cuộc họp. Muốn sửa, anh chị cũng chỉ cần nói, ví dụ: đổi biểu đồ cột thành biểu đồ đường. Mỗi lần sửa là một "
               "phiên bản mới, nên anh chị luôn quay lại được bản cũ. Trên sơ đồ tư duy, bấm vào ý nào là em giải thích ý đó."),
    dict(title="Chiếu lên màn hình chung và tự thuyết trình", body=f"""
      <div class="split">
        <div class="rows">
          {row('video', 'Thuyết trình', 'Em đọc lời trình bày từng slide, hiện phụ đề.')}
          {row('mic', 'Tự chuyển slide', 'Nghe người đang trình bày và chuyển slide theo lời nói.')}
          {row('folder', 'Mở tài liệu trên máy', 'PDF, PowerPoint, Word: theo ghi chú trong tệp hoặc theo kịch bản.')}
          <div class="callout">Anh chị đang xem chính tính năng này</div>
        </div>
        {shot(IMG + '/12-man-hinh-trinh-bay.jpg', 1000, 625)}
      </div>""",
         hidden=["Thuyết trình: đọc lời trình bày từng slide", "Tự chuyển slide theo lời nói",
                 "Mở PDF, PowerPoint trên máy để trình bày"],
         notes="Mọi thứ em soạn đều chiếu được lên màn hình chung. Em có thể tự thuyết trình từng slide, hoặc lắng nghe người "
               "đang trình bày và tự chuyển slide theo lời nói. Em cũng mở được tệp PDF, PowerPoint trên máy để trình bày, "
               "theo ghi chú trong tệp hoặc theo kịch bản anh chị đưa. Và thật ra, anh chị đang xem chính tính năng này: bộ "
               "slide hôm nay em mở từ tệp PowerPoint và đọc theo ghi chú trong tệp."),
    dict(title="Họp xong là có biên bản, kèm điều nói khác buổi trước", body=f"""
      <div class="split rev">
        {shot(IMG + '/14-bien-ban-thay-doi.jpg', 1000, 625)}
        <div class="rows">
          <div class="quote">"Dạ thưa anh chị, theo như mình đã bàn ở buổi 25/09/2026 thì là 15/12/2026 chứ không phải 20/12/2026, anh chị có thể xem xét lại nha ạ."</div>
          {row('file', 'Đủ các mục', 'Tóm tắt, quyết định, bảng phân công, rủi ro.')}
          {row('history', 'Nhớ xuyên cuộc họp', 'So với điều nhóm đã chốt ở tối đa 12 buổi trước.')}
          {row('cloud', 'Lưu ở đâu cũng được', 'Tải Word, lưu Google Drive của nhóm hoặc thư mục trên máy.')}
        </div>
      </div>""",
         hidden=["Biên bản tự động: tóm tắt, quyết định, phân công, rủi ro", "So với tối đa 12 buổi trước của nhóm",
                 "Tải Word, lưu Google Drive hoặc thư mục trên máy"],
         notes="Khi kết thúc cuộc họp, em lập biên bản gồm tóm tắt, các quyết định, bảng phân công và rủi ro. Với cuộc họp "
               "trong nhóm, em còn so với những điều nhóm đã chốt ở tối đa mười hai buổi trước. Có điểm nào nói khác, em ghi "
               "rõ trong biên bản, như câu nhắc trên slide về ngày phát hành voucher. Biên bản tải được dạng Word, lưu lên "
               "Google Drive của nhóm hoặc vào thư mục trên máy."),
    dict(title="Nhóm cuộc họp, Google Drive và bot vào Google Meet", body=f"""
      <div class="split">
        <div class="rows">
          {row('users', 'Nhóm theo chủ đề, phòng ban', 'Thành viên xem chung cuộc họp và biên bản của nhóm.')}
          {row('folder', 'Thư mục nhóm trên Google Drive', 'Biên bản và ghi âm tự lưu vào đúng thư mục của buổi họp.')}
          {row('calendar', 'Bot theo lịch Google', 'Tự vào cuộc họp Google Meet, chép lời từng người đúng tên trong Meet.')}
          {row('video', 'Họp nửa online, nửa trực tiếp', 'Mic trong phòng và bot Meet cùng chép, tự bỏ câu trùng.')}
        </div>
        {shot(IMG + '/04-cai-dat-nhom.jpg', 1000, 625, '50% 0%')}
      </div>""",
         hidden=["Nhóm theo chủ đề, phòng ban", "Thư mục nhóm trên Google Drive", "Bot theo lịch Google vào Google Meet",
                 "Họp nửa online nửa trực tiếp"],
         notes="Cuộc họp được gom theo nhóm, theo chủ đề hay phòng ban. Thành viên nhóm xem chung cuộc họp và biên bản, và "
               "biên bản tự lưu vào thư mục của nhóm trên Google Drive. Nếu anh chị kết nối lịch Google, bot của em tự vào "
               "các cuộc họp Google Meet trên lịch, chép lời từng người online đúng tên trong Meet. Họp nửa online nửa trực "
               "tiếp vẫn chép đủ cả hai phía."),
    dict(title="Chế độ BD: khách hỏi, trợ lý gợi ý câu trả lời", body=f"""
      <div class="split rev wide">
        {shot(BD, 1100, 657)}
        <div class="rows">
          {row('ask', 'Tự nhận ra câu hỏi của khách', 'Bỏ qua lời chào, câu của đội mình.', '#FFB700')}
          {row('file', 'Trả lời có nguồn', 'Tài liệu nhóm (K) và các buổi trước (M), kèm ghi chú cho đội.', '#FFB700')}
          {row('search', 'Tra thêm trên mạng (W)', 'Chỉ khi bấm, ghi rõ chưa kiểm chứng.', '#FFB700')}
          {row('eyeoff', 'Chỉ đội mình thấy', 'Không lên màn hình chung, không đọc thành tiếng.', '#FFB700')}
        </div>
      </div>""",
         hidden=["Tự nhận ra câu hỏi của khách", "Trả lời từ tài liệu nhóm và các buổi trước, có nguồn",
                 "Tra thêm trên mạng khi cần", "Bảng BD chỉ đội mình thấy", "Chủ nhóm hỏi báo cáo trên toàn bộ cuộc họp"],
         notes="Với đội kinh doanh, có chế độ BD. Khi khách hỏi, em tự nhận ra câu hỏi và gợi ý câu trả lời trên một bảng "
               "riêng chỉ đội mình thấy, không lên màn hình chung và em không đọc thành tiếng. Câu trả lời lấy từ tài liệu "
               "của nhóm như bảng giá, chính sách, và từ các buổi họp trước, có đánh dấu nguồn. Em cũng ghi chú cho đội mình, "
               "ví dụ lần trước đã báo giá khác. Cần thông tin bên ngoài thì bấm tra thêm trên mạng, em ghi rõ đây là thông "
               "tin chưa kiểm chứng. Chủ nhóm còn hỏi được báo cáo trên toàn bộ cuộc họp, ví dụ các câu phàn nàn của khách."),
    dict(title="Dữ liệu cuộc họp ở trong phạm vi công ty", body=f"""
      <div class="grid3">
        <div class="tile">{ic('lock', 40)}<p>Chỉ tài khoản Google @urbox.vn đăng nhập được</p></div>
        <div class="tile">{ic('users', 40)}<p>Mỗi cuộc họp chỉ người tạo và thành viên nhóm xem được</p></div>
        <div class="tile">{ic('mic', 40)}<p>Ghi âm chỉ bật khi mọi người đồng ý, tự xóa sau 30 ngày</p></div>
        <div class="tile">{ic('shield', 40)}<p>Mẫu giọng chỉ lưu khi người đó đồng ý, theo Nghị định 13/2023</p></div>
        <div class="tile">{ic('eyeoff', 40)}<p>Bảng BD không lên màn hình chung, không đọc thành tiếng</p></div>
        <div class="tile">{ic('file', 40)}<p>Tài liệu nhóm chỉ giữ phần chữ, tệp gốc không lưu trên máy chủ</p></div>
      </div>""",
         hidden=["Chỉ tài khoản @urbox.vn đăng nhập", "Chỉ người tạo và thành viên nhóm xem cuộc họp",
                 "Ghi âm cần mọi người đồng ý, tự xóa sau 30 ngày", "Mẫu giọng cần người đó đồng ý",
                 "Bảng BD không lên màn hình chung", "Tài liệu nhóm chỉ giữ phần chữ"],
         notes="Về dữ liệu: chỉ tài khoản Google của công ty mới đăng nhập được. Mỗi cuộc họp chỉ người tạo và thành viên "
               "trong nhóm xem được. Ghi âm mặc định tắt, chỉ bật khi mọi người trong phòng đồng ý, và tự xóa sau ba mươi "
               "ngày. Mẫu giọng là dữ liệu sinh trắc học theo Nghị định mười ba, nên chỉ lưu khi người đó đồng ý. Tài liệu "
               "của nhóm chỉ giữ phần chữ, tệp gốc không lưu trên máy chủ."),
    dict(title="Sẵn sàng dùng thử, cần thêm cuộc họp thật", body=f"""
      <div class="cols2">
        <div class="card big"><h3 class="ok">{ic('check', 40, '#7CE3A4')} Đã có trên bản web</h3>
          <ul><li>Đăng nhập Google, chép lời và nhận ra người nói</li><li>Trợ lý Jarvis: tóm tắt, slide, dashboard, tra cứu</li>
          <li>Biên bản tự động, nhóm cuộc họp</li><li>Chế độ BD: bảng gợi ý, tra thêm trên mạng</li></ul></div>
        <div class="card big"><h3 class="todo">{ic('clock', 40, '#FFB700')} Cần làm trước khi mở rộng</h3>
          <ul><li>Thử với cuộc họp thật ở vài phòng ban</li><li>Kiểm tra lưu Google Drive, lịch Google và bot Meet với tài khoản thật</li>
          <li>Thống nhất với IT, Pháp chế về ghi âm và gói AI cá nhân</li><li>Nhận góp ý của anh chị sau buổi hôm nay</li></ul></div>
      </div>""",
         hidden=["Đã có trên bản web: chép lời, trợ lý, biên bản, nhóm, chế độ BD",
                 "Cần làm: thử cuộc họp thật, kiểm tra Google Drive và bot Meet, thống nhất với IT và Pháp chế"],
         notes="Hiện trên bản web đã có đăng nhập Google, chép lời và nhận ra người nói, trợ lý Jarvis, biên bản tự động, "
               "nhóm cuộc họp và chế độ BD. Trước khi mở rộng, nhóm cần thử với cuộc họp thật ở vài phòng ban, kiểm tra lưu "
               "Google Drive, lịch Google và bot Meet với tài khoản thật, và thống nhất cùng IT, Pháp chế về ghi âm và việc "
               "dùng gói AI cá nhân. Em rất mong nhận được góp ý của anh chị sau buổi hôm nay."),
    dict(title="Demo trực tiếp", plain=True, body=f"""
      <div class="cover">
        <div class="cl">
          <span class="pill">Phần tiếp theo</span>
          <h1>Demo trực tiếp</h1>
          <ol class="agenda"><li>Tạo cuộc họp, bật mic, chép lời</li><li>Gọi Jarvis tóm tắt và vẽ dashboard</li>
          <li>Kết thúc họp, xem biên bản</li><li>Bảng BD: khách hỏi giá, tra thêm trên mạng</li></ol>
          <p class="gold">Sau đó là phần hỏi đáp</p>
        </div>
        <div class="cr"><div class="halo"></div><img class="mascot" src="{MASCOT}"></div>
      </div>""",
         hidden=["Tạo cuộc họp, bật mic, chép lời", "Gọi Jarvis tóm tắt và vẽ dashboard", "Kết thúc họp, xem biên bản",
                 "Bảng BD: khách hỏi giá, tra thêm trên mạng", "Hỏi đáp"],
         notes="Phần trình bày của em đến đây là hết. Bây giờ em xin mời anh Phúc demo trực tiếp trên hệ thống: tạo cuộc họp, "
               "gọi em tóm tắt và vẽ dashboard, kết thúc để xem biên bản, và thử bảng BD khi khách hỏi giá. Em cảm ơn anh chị "
               "đã lắng nghe."),
]

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:wght@400;500;600;700;800&display=swap');
@page { size: 1920px 1080px; margin: 0; }
* { box-sizing: border-box; }
body { margin: 0; background: #0F0B1A; }
.slide { width: 1920px; height: 1080px; position: relative; overflow: hidden; font-family: 'Be Vietnam Pro', sans-serif;
  color: #F1EDFA; background-color: #0F0B1A; page-break-after: always;
  background-image: radial-gradient(1500px 800px at 92% -12%, rgba(130,53,228,.30), transparent 62%),
                    radial-gradient(1100px 650px at -8% 112%, rgba(239,101,197,.11), transparent 60%); }
.title { position: absolute; left: 120px; top: 84px; width: 1680px; font-size: 58px; font-weight: 700; line-height: 1.15;
  letter-spacing: -0.5px; margin: 0; }
.content { position: absolute; left: 120px; right: 120px; top: 230px; bottom: 120px; }
.foot { position: absolute; left: 120px; right: 120px; bottom: 46px; display: flex; justify-content: space-between;
  font-size: 20px; color: #8D82A8; }
.shot { border-radius: 22px; overflow: hidden; flex: none; background: #1B142E;
  box-shadow: 0 30px 90px rgba(130,53,228,.32), 0 0 0 1px rgba(214,196,255,.16); }
.shot img { display: block; width: 100%; height: 100%; object-fit: cover; }
.split { display: flex; align-items: center; gap: 80px; height: 100%; }
.split .rows { flex: 1; }
.rows { display: flex; flex-direction: column; gap: 34px; }
.row { display: flex; gap: 24px; align-items: flex-start; }
.ic { width: 64px; height: 64px; border-radius: 50%; background: rgba(130,53,228,.22); display: grid; place-items: center; flex: none; }
.rh { font-size: 30px; font-weight: 600; line-height: 1.25; }
.rt { font-size: 24px; color: #B5ABCB; line-height: 1.4; margin-top: 6px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 40px; height: calc(100% - 30px); }
.card { background: rgba(255,255,255,.045); border: 1px solid rgba(214,196,255,.12); border-radius: 26px; padding: 40px 44px; }
.card h3 { font-size: 42px; margin: 30px 0 14px; font-weight: 700; }
.card p { font-size: 30px; color: #B5ABCB; margin: 0; line-height: 1.45; }
.grid2 .card { padding: 48px 52px; }
.flow { display: flex; align-items: stretch; gap: 18px; margin-top: 40px; height: 540px; }
.step { flex: 1; background: rgba(255,255,255,.045); border: 1px solid rgba(214,196,255,.12); border-radius: 26px; padding: 40px 34px;
  position: relative; }
.step .num { position: absolute; top: 28px; right: 32px; font-size: 64px; font-weight: 800; color: rgba(185,138,255,.22); }
.step h3 { font-size: 42px; margin: 40px 0 18px; }
.step p { font-size: 29px; color: #B5ABCB; margin: 0; line-height: 1.45; }
.step { padding: 52px 40px; }
.arrow { align-self: center; font-size: 64px; color: #8235E4; font-weight: 300; }
.note { position: absolute; left: 0; bottom: 0; font-size: 26px; color: #B5ABCB; margin: 0; }
.bubbles { flex: 1; display: flex; flex-direction: column; gap: 22px; }
.bub { font-size: 28px; line-height: 1.35; padding: 20px 28px; border-radius: 22px 22px 22px 6px; background: rgba(130,53,228,.20);
  border: 1px solid rgba(185,138,255,.35); }
.cap { font-size: 24px; color: #B5ABCB; margin: 10px 0 0; }
.three { display: flex; gap: 80px; justify-content: center; }
.three figure { margin: 0; }
.three figcaption { font-size: 28px; font-weight: 600; margin-top: 22px; text-align: center; }
.callout { font-size: 28px; font-weight: 600; color: #FFB700; background: rgba(255,183,0,.12); border-radius: 18px; padding: 18px 26px;
  align-self: flex-start; }
.quote { font-size: 27px; line-height: 1.45; font-style: italic; color: #F1EDFA; background: rgba(255,183,0,.10);
  border: 1px solid rgba(255,183,0,.35); border-radius: 22px; padding: 26px 30px; }
.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 34px; margin-top: 10px; }
.tile { background: rgba(255,255,255,.045); border: 1px solid rgba(214,196,255,.12); border-radius: 26px; padding: 36px 36px 40px;
  min-height: 330px; }
.tile p { font-size: 31px; line-height: 1.4; margin: 26px 0 0; font-weight: 500; }
.cols2 { display: grid; grid-template-columns: 1fr 1fr; gap: 44px; margin-top: 10px; }
.cols2 .card.big { min-height: 560px; padding: 52px 56px; }
.card.big h3 { display: flex; gap: 16px; align-items: center; font-size: 34px; margin: 0 0 26px; }
.card.big ul { margin: 0; padding-left: 30px; }
.card.big li { font-size: 31px; line-height: 1.4; margin: 0 0 26px; color: #E4DDF3; }
.cover { position: absolute; inset: 0; display: flex; align-items: center; padding: 0 120px; }
.cl { flex: 1; }
.cover h1 { font-size: 112px; line-height: 1.02; margin: 34px 0 26px; font-weight: 800; letter-spacing: -2px; }
.sub { font-size: 38px; line-height: 1.35; color: #D9D0EC; margin: 0 0 40px; max-width: 980px; }
.gold { font-size: 30px; color: #FFB700; font-weight: 600; margin: 0 0 18px; }
.who { font-size: 26px; color: #8D82A8; margin: 0; }
.pill { display: inline-block; padding: 12px 26px; border-radius: 999px; background: rgba(255,183,0,.14); color: #FFB700;
  font-weight: 600; font-size: 26px; }
.cr { width: 640px; height: 640px; position: relative; flex: none; }
.halo { position: absolute; inset: 20px; border-radius: 50%; background: radial-gradient(circle, rgba(130,53,228,.55), rgba(130,53,228,0) 70%); }
.mascot { position: absolute; inset: 60px; width: 520px; height: 520px; object-fit: contain; }
.agenda { font-size: 36px; line-height: 1.5; margin: 0 0 34px; padding-left: 48px; color: #E4DDF3; }
.agenda li { padding-left: 8px; }
.wide { gap: 60px; }
.ic.big { width: 84px; height: 84px; }
"""


def page_html(only=None):
    out = []
    for i, s in enumerate(SLIDES, 1):
        if only is not None and i != only:
            continue
        head = "" if s.get("plain") else f'<h2 class="title">{s["title"]}</h2>'
        foot = "" if s.get("plain") else '<div class="foot"><span>UrBox Meeting Copilot</span></div>'   # số trang: ứng dụng tự hiện
        body = s["body"] if s.get("plain") else f'<div class="content">{s["body"]}</div>'
        out.append(f'<section class="slide" id="s{i}">{head}{body}{foot}</section>')
    return f'<!doctype html><html lang="vi"><head><meta charset="utf-8"><style>{CSS}</style></head><body>{"".join(out)}</body></html>'


def build():
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Emu, Pt
    work = Path(tempfile.mkdtemp(prefix="deck-"))
    html = work / "deck.html"
    html.write_text(page_html(), encoding="utf-8")
    pngs = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(html.as_uri())
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        page.wait_for_timeout(800)
        for i in range(1, len(SLIDES) + 1):
            png = work / f"slide-{i:02d}.png"
            page.locator(f"#s{i}").screenshot(path=str(png))
            pngs.append(png)
        page.pdf(path=str(HERE / f"{NAME}.pdf"), width="1920px", height="1080px", print_background=True)
        b.close()
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    for s, png in zip(SLIDES, pngs):
        jpg = png.with_suffix(".jpg")
        Image.open(png).convert("RGB").save(jpg, quality=88, optimize=True)
        sl = prs.slides.add_slide(prs.slide_layouts[5])                      # Title Only: tiêu đề cho dàn ý, trình đọc màn hình
        sl.shapes.title.text = s["title"]
        tb = sl.shapes.add_textbox(Emu(600000), Emu(1500000), Emu(10900000), Emu(4500000))  # chữ ẩn dưới ảnh: tìm kiếm, nhắc bài
        tf = tb.text_frame
        tf.word_wrap = True
        for k, line in enumerate(s["hidden"]):
            para = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
            para.text = line
            para.runs[0].font.size = Pt(14)
        pic = sl.shapes.add_picture(str(jpg), 0, 0, prs.slide_width, prs.slide_height)
        pic._element.nvPicPr.cNvPr.set("descr", s["title"] + ": " + "; ".join(s["hidden"]))
        sl.notes_slide.notes_text_frame.text = s["notes"]
    prs.core_properties.title = "UrBox Meeting Copilot - Demo"
    prs.core_properties.author = "phuc.bh@urbox.vn"
    prs.save(str(REPO / "slides" / "demo" / f"{NAME}.pptx"))
    write_notes_md()
    words = sum(len(s["notes"].split()) for s in SLIDES)
    print(json.dumps({"slides": len(SLIDES), "notes_words": words, "est_minutes": round(words / 140, 1)},
                     ensure_ascii=False))


def write_notes_md(width: int = 110):
    """Mục cuối của kịch bản demo: lời Jarvis đọc trên từng slide (đúng ghi chú trong tệp), dòng tối đa 110 ký tự."""
    def wrap(text: str) -> str:
        lines, cur = [], ""
        for w in text.split():
            if cur and len(cur) + 1 + len(w) > width:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        return "\n".join(lines + [cur])
    parts = [f"### Slide {i}. {s['title']}\n\n{wrap(s['notes'])}" for i, s in enumerate(SLIDES, 1)]
    head = SCRIPT_MD.read_text(encoding="utf-8").split(MARK)[0]
    SCRIPT_MD.write_text(head + MARK + "\n\n" + "\n\n".join(parts) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
