"""Recall.ai: cho bot vào Meet/Zoom/Teams, nhận audio RIÊNG từng người qua websocket.

Bot cần một URL wss PUBLIC để đẩy audio về (tailscale funnel / ngrok / domain thật).
Đặt PUBLIC_WS_BASE=wss://your-host để app tự ghép URL receiver.

Chưa test với cuộc họp thật (máy dev không có URL public lúc viết). Shape API lấy từ
https://docs.recall.ai/docs/how-to-get-separate-audio-per-participant-realtime
"""
import os

import httpx


def key():
    return os.getenv("RECALLAI_API_KEY")


def base(region: str) -> str:
    return f"https://{region}.recall.ai/api/v1"


async def create_bot(meeting_url: str, receiver_ws_url: str, region: str, bot_name="Interview Assistant") -> dict:
    body = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "recording_config": {
            "audio_separate_raw": {},
            "realtime_endpoints": [{
                "type": "websocket",
                "url": receiver_ws_url,
                "events": ["audio_separate_raw.data"],
            }],
        },
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{base(region)}/bot", json=body,
                         headers={"authorization": key(), "content-type": "application/json"})
        if r.status_code >= 300:
            raise RuntimeError(f"Recall {r.status_code}: {r.text[:300]}")
        return r.json()


async def leave_bot(bot_id: str, region: str):
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(f"{base(region)}/bot/{bot_id}/leave_call", headers={"authorization": key()})


async def bot_status(bot_id: str, region: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{base(region)}/bot/{bot_id}", headers={"authorization": key()})
        return r.json()
