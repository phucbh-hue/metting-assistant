"""Test Soniox Keepalive & Feed Resiliency."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from meeting import db, live

async def test_stream():
    db.init()
    session = live.MeetingSession(meeting_id=999, title="Test Stream Resiliency")
    stream = live.MeetingStream("mic", session, diarize=True)
    await stream.open()
    assert stream.ws is not None
    print("Soniox WebSocket opened successfully!")
    
    # Send audio chunks
    dummy_pcm = b"\x00" * 3200
    for i in range(5):
        await stream.feed(dummy_pcm)
        await asyncio.sleep(0.1)
    print("5 audio chunks fed cleanly!")
    
    # Wait for keepalive ping (5 seconds)
    print("Waiting 5s for keepalive pings...")
    await asyncio.sleep(5.0)
    assert not stream.down
    print("Keepalive succeeded, stream is still alive!")
    
    # Close cleanly
    await stream.close()
    print("Stream closed cleanly!")

if __name__ == "__main__":
    asyncio.run(test_stream())
