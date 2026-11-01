"""Panel gợi ý câu hỏi: nhiều model sinh song song, một model tổng hợp.

Vì sao: mỗi model "nhìn" câu trả lời của ứng viên một kiểu, câu hỏi hay nhất hay nằm ở model khác nhau.
Giá phải trả là độ trễ, nên có 2 pha:
  1. draft  : model nào về trước thì đẩy ra màn hình ngay (thường GPT ~8s)
  2. final  : chờ cả panel (trần PANEL_TIMEOUT) rồi model tổng hợp chọn/ghép 3-4 câu tốt nhất, ghi rõ nguồn

Model đi qua LiteLLM (LITELLM_KEY + LITELLM_BASE_URL), trừ "gemini-direct" dùng Gemini API sẵn có (llm._json).
JSON ép bằng response_format json_schema; parse thất bại thì bỏ model đó, không làm hỏng cả panel.
"""
import asyncio
import json
import logging
import os
import time

import httpx

from meeting.interview_app import llm

log = logging.getLogger("interview.panel")

BASE = (os.getenv("LITELLM_BASE_URL") or "https://litellm.athena.tools/v1").rstrip("/")
PANEL_TIMEOUT = 35          # giây chờ các model sinh (Opus 4.6 với prompt thật hay vượt -> không nằm trong mặc định)
SYNTH_TIMEOUT = 45

# Model khả dụng cho panel (probe 04/09/2026: giây / $ mỗi lần gọi ngắn).
# 13/09: bỏ claude-opus-4-6 -> Opus 5, vì LiteLLM gỡ toàn bộ Claude 4.* tuần sau.
# Tên route PHẢI có đuôi "@default" - đó là tên thật trên proxy, thiếu là 403.
# Opus 5 đo 13/09 với prompt thật 9.6k ký tự: 48.5s > PANEL_TIMEOUT 35s -> chọn
# nó thì gần như chắc chắn bị loại khỏi panel. Giữ trong danh mục để ai cần vẫn
# thấy, nhưng đừng đưa vào DEFAULT_MODELS.
# (Opus 4.6 trước đây cũng cùng bệnh: "~16s, hay quá 35s với prompt dài".)
CATALOG = {
    "gpt-5.5":                   "GPT-5.5 (~8s)",
    "gpt-5.4":                   "GPT-5.4 (~8s, rẻ hơn)",
    "vertex_ai/claude-sonnet-5": "Claude Sonnet 5 (~12s)",
    "vertex_ai/claude-opus-5@default": "Claude Opus 5 (~48s — VƯỢT timeout 35s, sẽ bị bỏ)",
    "claude-fable-5":            "Claude Fable 5 (~13s, đắt 4x)",
    "openrouter/deepseek/deepseek-v4-pro-0813": "DeepSeek V4 Pro (~5s, rẻ nhất)",
    "gemini-direct":             f"Gemini {llm.MODEL} (API trực tiếp, ~6s)",
}
DEFAULT_MODELS = ["gpt-5.5", "vertex_ai/claude-sonnet-5", "gemini-direct"]
DEFAULT_SYNTH = "vertex_ai/claude-sonnet-5"


def key():
    return os.getenv("LITELLM_KEY") or None


# Đã chuyển sang llm.flat_schema khi đường Claude trực tiếp cũng cần nó; giữ
# tên cũ ở đây để phần còn lại của file không phải đổi.
_schema = llm.flat_schema


async def chat_json(model: str, system: str, prompt: str, schema_cls, temperature=0.5, timeout=PANEL_TIMEOUT):
    """Gọi 1 model qua LiteLLM, trả instance pydantic. Ném lỗi nếu hỏng (caller quyết)."""
    if model == "gemini-direct":
        return await asyncio.wait_for(llm._json(schema_cls, system, prompt, temperature), timeout)
    # Không gửi temperature: gpt-5.x chỉ nhận mặc định. Thử json_schema trước; provider nào từ chối (400) thì
    # lùi về json_object + nhét schema vào system prompt, rồi pydantic kiểm.
    schema = _schema(schema_cls)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    attempts = [
        {"response_format": {"type": "json_schema", "json_schema": {"name": schema_cls.__name__, "schema": schema}}},
        {"response_format": {"type": "json_object"},
         "messages": [{"role": "system", "content": system + "\n\nTrả về DUY NHẤT một JSON hợp lệ theo schema sau, không markdown:\n" + json.dumps(schema, ensure_ascii=False)},
                      {"role": "user", "content": prompt}]},
    ]
    last = None
    async with httpx.AsyncClient(timeout=timeout) as c:
        for extra in attempts:
            body = {"model": model, "max_tokens": 3000, "messages": msgs, **extra}
            r = await c.post(f"{BASE}/chat/completions", headers={"Authorization": f"Bearer {key()}"}, json=body)
            if r.status_code == 200:
                txt = r.json()["choices"][0]["message"]["content"] or ""
                txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                return schema_cls.model_validate_json(txt)
            last = f"{model}: HTTP {r.status_code} {r.text[:200]}"
            if r.status_code != 400:
                break
    raise RuntimeError(last)


async def assist_panel(p: dict, cand: dict, segments: list, trigger_text: str | None,
                       models: list[str], synth: str, on_draft=None) -> tuple[llm.Assist, dict]:
    """Trả (Assist cuối, meta). meta = {models: {name: {ok, seconds, n_questions, error}}, synth, seconds}"""
    system, prompt = llm.assist_prompt(p, cand, segments, trigger_text)
    models = [m for m in models if m in CATALOG] or DEFAULT_MODELS
    t0 = time.monotonic()
    meta = {"models": {}, "synth": synth, "draft_from": None}
    results: dict[str, llm.Assist] = {}

    async def one(m):
        ts = time.monotonic()
        try:
            a = await chat_json(m, system, prompt, llm.Assist)
            meta["models"][m] = {"ok": True, "seconds": round(time.monotonic() - ts, 1), "n_questions": len(a.follow_ups)}
            return m, a
        except Exception as e:
            meta["models"][m] = {"ok": False, "seconds": round(time.monotonic() - ts, 1), "error": f"{type(e).__name__}: {str(e)[:120]}"}
            log.warning("panel %s: %s", m, e)
            return m, None

    tasks = [asyncio.create_task(one(m)) for m in models]
    try:
        for fut in asyncio.as_completed(tasks, timeout=PANEL_TIMEOUT):
            m, a = await fut
            if a is None:
                continue
            results[m] = a
            if on_draft and meta["draft_from"] is None:
                meta["draft_from"] = m
                await on_draft(a, m)
    except asyncio.TimeoutError:
        for t in tasks:
            if not t.done():
                t.cancel()
        for m in models:
            meta["models"].setdefault(m, {"ok": False, "seconds": PANEL_TIMEOUT, "error": "timeout"})

    if not results:
        raise RuntimeError("Không model nào trong panel trả kết quả: " + json.dumps(meta["models"], ensure_ascii=False)[:300])
    if len(results) == 1 or not synth:
        (m, a), = results.items() if len(results) == 1 else [next(iter(results.items()))]
        for q in a.follow_ups:
            q.source = q.source or m
        meta["seconds"] = round(time.monotonic() - t0, 1)
        return a, meta

    # --- tổng hợp ---
    parts = []
    for m, a in results.items():
        parts.append(f"### Đề xuất từ {m}\nTóm tắt: {a.last_answer_summary}\nChỗ chưa rõ: {'; '.join(a.gaps)}\n" +
                     "\n".join(f"- [{q.kind} / {q.competency}] {q.question}\n"
                               f"    bám vào: \u201c{q.based_on}\u201d  (vì: {q.why})" for q in a.follow_ups) +
                     (f"\nRed flags: {'; '.join(a.red_flags)}" if a.red_flags else "") +
                     (f"\nChưa chạm: {', '.join(a.not_yet_covered)}" if a.not_yet_covered else ""))
    synth_system = (system + "\n\nBạn đang làm TỔNG HỢP: nhận đề xuất của nhiều chuyên gia (mỗi model một mục). "
                    "Chọn 3-4 câu hỏi TỐT NHẤT, không trùng ý; ghép hai câu cùng ý thành một câu sắc hơn; ưu tiên câu bóc được số liệu, "
                    "quyết định cụ thể, hoặc mâu thuẫn. Giữ nguyên văn phong để CEO đọc ra được. Điền `source` là tên model gốc "
                    "(hoặc 'ghép: a+b'). Red flag chỉ giữ khi ít nhất một mục nêu có bằng chứng trong transcript.\n\n"
                    "LỌC BỊA - việc chính của bạn ở bước này. Model trong panel có thể gài dữ kiện không có "
                    "trong transcript vào câu hỏi (số năm kinh nghiệm, tên công ty, con số vòng vốn). Với mỗi câu "
                    "định giữ, soi `based_on` của nó và đối chiếu với transcript ở dưới: nếu trích dẫn không tìm "
                    "được nguyên văn trong transcript, hoặc câu hỏi áp đặt dữ kiện không có trong trích dẫn, thì "
                    "SỬA thành dạng mở (hỏi con số thay vì giả định con số) hoặc BỎ hẳn câu đó. "
                    "Thà trả 2 câu sạch hơn 4 câu có một câu bịa.")
    synth_prompt = ("## Transcript tới giờ (rút gọn 40 dòng cuối)\n" + llm.fmt_transcript(segments[-40:]) +
                    "\n\n## Đề xuất của panel\n" + "\n\n".join(parts) + "\n\nHãy tổng hợp thành một bản duy nhất.")
    try:
        final = await chat_json(synth, synth_system, synth_prompt, llm.Assist, temperature=0.3, timeout=SYNTH_TIMEOUT)
        meta["synth_ok"] = True
    except Exception as e:
        log.warning("synth %s: %s", synth, e)
        meta["synth_ok"] = False; meta["synth_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        # rơi về: nối câu của các model, cắt 4
        m0, final = next(iter(results.items()))
        seen, merged = set(), []
        for m, a in results.items():
            for q in a.follow_ups:
                k = q.question.strip().lower()[:60]
                if k not in seen:
                    seen.add(k); q.source = q.source or m; merged.append(q)
        final = final.model_copy(update={"follow_ups": merged[:4]})
    meta["seconds"] = round(time.monotonic() - t0, 1)
    return final, meta
