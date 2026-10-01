"""Kiểm thử tích hợp với LLM THẬT: AI đoán tên người nói, trợ lý Jarvis + MCP, co-design.

    python scripts/integration/llm_identity_check.py

- Dùng ANTHROPIC_API_KEY / GEMINI_API_KEY trong .env (có tính phí).
- Luôn dùng DB in-memory, vector giọng tổng hợp: không ghi gì vào MongoDB thật.
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["MEETING_DB"] = "mock"
os.environ["SONIOX_API_KEY"] = ""

import numpy as np  # noqa: E402

from meeting import artifacts, db, live, llm, mcp  # noqa: E402

CONVERSATION = [
    (0, "1", "Chào anh em, sáng nay mình họp nhanh về Kubernetes và chiến dịch Mega Sale 10.10 nhé."),
    (1, "2", "Chào anh Phúc và cả nhà, em là Tuấn bên DevOps mới sang hỗ trợ team."),
    (0, "1", "Tuấn ơi, cụm staging em đã migrate sang Postgres 18 xong chưa?"),
    (1, "2", "Dạ em kiểm tra kết nối rồi, connection pool chạy ổn anh ạ."),
    (2, "3", "Bên payment em vừa tìm ra lỗi memory leak của worker webhook."),
    (0, "1", "Tốt, Quân gửi bản fix để Hằng test luôn nhé."),
    (2, "3", "Dạ, chiều nay em đẩy lên staging."),
]


def unit(x):
    return (x / np.linalg.norm(x)).astype(np.float32)


async def main() -> int:
    if not artifacts.llm_available():
        print("Thiếu ANTHROPIC_API_KEY / GEMINI_API_KEY trong .env")
        return 2
    db.init()
    mcp.seed_mock_data()
    rng = np.random.default_rng(3)
    ch = unit(rng.standard_normal(192))
    base = [unit(rng.standard_normal(192)) for _ in range(3)]
    mid = db.create_meeting("Họp Review Sprint 38", expected_attendees=["Lê Văn Tuấn", "Đỗ Minh Quân"])
    s = await live.get_session(mid)
    t = 1.0
    for k, raw, text in CONVERSATION:
        v = unit(0.7 * ch + base[k] + 0.85 * unit(rng.standard_normal(192)))
        await s.on_segment_finalized(t, t + 3.0, raw, text, vector=v, voiced=3.0)
        t += 4.0
    await s.drain()
    ok = True

    print("\n[1] AI đoán tên người nói")
    results = await s.identity.run(force=True)
    await s.drain()
    for r in results:
        print(f"    {r['old_label']} -> {r['new_name']} ({r['confidence']:.0%}, {r['action']}): {r['reasoning'][:90]}")
    names = {p.label for p in s.speakers.visible_profiles()}
    print("    Người nói sau khi đoán:", ", ".join(sorted(names)))
    if not any("Tuấn" in n for n in names):
        print("    KHÔNG ĐẠT: chưa nhận ra Tuấn")
        ok = False

    print("\n[2] Jarvis tra cứu Jira qua MCP rồi vẽ sơ đồ")
    res = await llm.think_and_act(mid, "tra cứu ticket của Tuấn trên Jira rồi vẽ sơ đồ luồng migrate", s.segments)
    print("    Tool:", [c["tool"] for c in res["tool_calls"]], "| Artifact:", (res.get("artifact") or {}).get("kind"))
    print("    Trả lời:", (res.get("chat_response") or "")[:160])

    art = res.get("artifact")
    if art:
        print("\n[3] Co-design chỉnh sửa sản phẩm")
        ref = await artifacts.co_design_refine(mid, art["id"], "thêm bước kiểm thử trên staging trước khi chạy production")
        print(f"    v{ref['version']} (parent {ref['parent_id']}): {ref['chat_message'][:120]}")
    print("\nĐẠT" if ok else "\nKHÔNG ĐẠT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
