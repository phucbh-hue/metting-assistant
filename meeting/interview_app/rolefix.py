"""Vai của TỪNG CÂU trên mic phòng = so giọng + đọc nội dung.

Mic phòng chỉ có một luồng: Soniox tách giọng ẩn danh và hay tráo, gộp nhãn; so giọng (voice.Speakers)
chỉ quyết được câu thuộc cụm đủ chắc. Bản trước đoán vai theo NHÃN ở vài mốc cố định - nhãn đã sai thì
không sửa được câu nào, và tắt hẳn khi đang so giọng. Ở đây một model nhanh (llm.ROLE_MODEL) đọc cửa sổ
hội thoại và gán vai cho từng câu mới, fuse() gộp với kết quả so giọng. Chỉnh ngưỡng bằng bench/role_fix.py.
"""
import asyncio
import logging
import time

from meeting.interview_app import llm

log = logging.getLogger("interview.rolefix")

ROLES = ("interviewer", "candidate")
ACCEPT = 0.6         # nội dung chắc ngần này thì quyết câu mà giọng chưa chắc
OVERRIDE = 0.9       # ...ngần này thì thắng cả cụm giọng đã chắc
BATCH = 3            # đủ ngần này câu chưa gán thì gọi model ngay
MAX_WAIT_S = 12.0    # có câu chưa gán mà chưa đủ lô: chờ tối đa ngần này
EARLY = 6            # mấy câu đầu buổi: có câu mới là gọi - vai sai lúc đầu thì cả buổi khó theo
TICK_S = 1.5
ERROR_WAIT_S = 30.0  # model lỗi thì nghỉ ngần này rồi mới thử lại, khỏi dội API mỗi nhịp
CONTEXT = 20         # số câu trước câu cần gán đem theo làm ngữ cảnh
HEAD = 4             # vài câu đầu buổi (chào hỏi, giới thiệu) luôn đi kèm
MAX_ROWS = 45
MAX_TARGETS = 12
AFTER = 2            # câu gán lúc sau nó chưa có ngần này câu thì gán lại một lần khi đã có
TIMEOUT_S = 25.0
FINAL_CALLS = 6      # cuối buổi: tối đa ngần này lượt để gán nốt


VETO_CONF = 0.85     # chỉ câu mà CHÍNH nội dung cho thấy vai (độ tin ~0.9) mới được bỏ phiếu cho cả cụm
VETO_MIN = 3         # nội dung bác vai giọng gán cho CẢ cụm khi có từ ngần này câu nói ngược...
VETO_SHARE = 0.6     # ...và chúng chiếm từ ngần này trong số câu bỏ phiếu của cụm
CONFIRM_MIN = 2      # giọng gọi tên người phỏng vấn CÙNG chỉ thành bằng chứng mạnh khi cụm có từ ngần này phiếu
                     # nội dung chắc là người phỏng vấn (và nhiều hơn phiếu ứng viên) - họ có thể vắng mặt


def veto(against: int, total: int) -> bool:
    """Số đông nội dung của một cụm giọng nói ngược vai mà giọng gán cho cụm đó.

    Học từ meeting-assistant: giọng giỏi GOM câu của cùng một người, kém ở chỗ GỌI TÊN (giọng lưu thu ở chỗ
    khác, hay thu cùng kênh với phòng nên giống mọi người). Cụm 18 câu toàn kể kinh nghiệm bản thân mà giọng
    gọi là "anh Trí" thì cái sai là cái tên, không phải 18 câu kia."""
    return against >= VETO_MIN and against >= VETO_SHARE * total


def fuse(base, acoustic, content):
    """Vai cuối của một câu. acoustic = (vai, cụm giọng chắc?), content = (vai, độ tin) từ model."""
    c_role, c_conf = content if content and content[0] in ROLES else (None, 0.0)
    a_role, a_strong = acoustic if acoustic else (None, False)
    if c_role and c_conf >= OVERRIDE:
        return c_role
    if a_role and a_strong:
        return a_role
    if c_role and c_conf >= ACCEPT:
        return c_role
    return a_role or base


def due(n_segments: int, todo: int, waited_s: float) -> bool:
    return todo > 0 and (n_segments <= EARLY or todo >= BATCH or waited_s >= MAX_WAIT_S)


def targets(state: list, final: bool = False) -> list:
    """state[i] = (số lần đã gán, số câu đi sau lúc gán) -> vị trí các câu cần gán lần này."""
    n = len(state)
    new = [i for i, (k, _) in enumerate(state) if k == 0][:MAX_TARGETS]
    again = [i for i, (k, after) in enumerate(state)
             if k == 1 and after < AFTER and n - 1 - i > after and (final or n - 1 - i >= AFTER)]
    room = MAX_TARGETS - len(new)
    return sorted(new + (again[-room:] if room > 0 else []))


def window(tg: list, n: int) -> tuple:
    """-> (vị trí các câu đem cho model đọc, các câu cần gán nằm trong đó)."""
    start = max(0, tg[0] - CONTEXT)
    end = min(n, start + MAX_ROWS)
    return list(range(min(HEAD, start))) + list(range(start, end)), [i for i in tg if i < end]


def _hints(ls) -> list:
    if not ls._manual:
        return []
    return ["Câu ghi 'NGƯỜI DÙNG ĐÃ XÁC NHẬN' là vai người đang phỏng vấn tự sửa: chắc chắn đúng, "
            "dùng làm mốc để hiểu ai xưng hô thế nào trong buổi."]
# Không còn dòng "Cột 'giọng' so với giọng đã lưu của <tên>": tên người phỏng vấn khác không đưa cho model nữa
# (live.voice_hint), và cột giọng đã được giải thích trong llm._ROLE_RULES.


async def fix_once(ls, cand: dict, final: bool = False) -> int:
    """Một lượt gán vai. -> số câu đã gán, 0 khi không còn gì để gán, -1 khi model lỗi."""
    async with ls.content_lock:
        if ls.roles_locked:
            return 0
        tg = targets(ls.content_state(), final)
        if not tg:
            return 0
        idx, tg = window(tg, len(ls.segments))
        rows = [{"i": i, "t": ls.segments[i]["t"], "voice": ls.voice_hint(ls.segments[i]),
                 "text": ls.segments[i]["text"]} for i in idx]
        t0 = time.monotonic()
        try:
            res = await asyncio.wait_for(llm.label_roles(rows, tg, cand, _hints(ls)), TIMEOUT_S)
        except Exception as e:
            ls.role_stats["errors"] += 1
            log.warning("session %s: gán vai theo nội dung lỗi (%s: %s)", ls.id, type(e).__name__, str(e)[:160])
            return -1
        took = time.monotonic() - t0
        want = set(tg)
        got = {x.i: (x.role, min(max(float(x.confidence), 0.0), 1.0)) for x in res.segments if x.i in want}
        # Câu model bỏ sót cũng tính là đã gán (không chắc), khỏi bị đem đi gán mãi.
        changed = ls.apply_content({i: (*got.get(i, ("unclear", 0.0)), idx[-1] - i) for i in tg})
        st = ls.role_stats
        st["calls"] += 1
        st["seconds"] = round(st["seconds"] + took, 1)
        log.info("session %s: gán vai theo nội dung %d câu (#%d-#%d), đổi %d, %.1fs",
                 ls.id, len(tg), tg[0], tg[-1], changed, took)
        return len(tg)


async def run(ls, get_cand):
    """Chạy nền suốt buổi mic phòng: gom câu mới thành lô (due()) rồi gán vai."""
    cand, since = None, None
    try:
        while not ls.ended and not ls.roles_locked:
            await asyncio.sleep(TICK_S)
            state = ls.content_state()
            todo = sum(1 for k, _ in state if k == 0)
            if not todo:
                since = None
                continue
            now = time.monotonic()
            since = since or now
            if not due(len(state), todo, now - since):
                continue
            if cand is None:
                cand = await get_cand()
            if await fix_once(ls, cand) < 0:
                await asyncio.sleep(ERROR_WAIT_S)
            since = None
    except Exception:
        log.exception("session %s: vòng gán vai theo nội dung dừng", ls.id)


async def final_pass(ls, cand: dict):
    """Cuối buổi, trước khi chấm: gán nốt các câu còn lại để bản chấm dùng đúng vai."""
    for _ in range(FINAL_CALLS):
        if await fix_once(ls, cand, final=True) <= 0:
            return
