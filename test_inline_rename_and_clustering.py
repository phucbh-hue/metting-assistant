"""Test inline speaker renaming and voice override."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient
from meeting import app, db, live, voice
import numpy as np

def test_manual_override():
    db.init()
    client = TestClient(app.app)

    # 1. Test MeetingSpeakers manual override
    speakers = voice.MeetingSpeakers(anchors={})
    v1 = voice.unit(np.random.randn(192).astype(np.float32))
    speakers.add(key=1, v=v1, raw_label="0", t=1.0, text="Xin chào")
    dec1 = speakers.decided.get(1)
    print("Initial label:", dec1)

    # Override speaker manually
    speakers.override_speaker(dec1, "Nguyễn Hoàng Nam", new_vid=99)
    assert speakers.decided.get(1) == "Nguyễn Hoàng Nam"
    print("After override:", speakers.decided.get(1))

    # Add another utterance with same cluster or no vector
    speakers.add(key=2, v=v1, raw_label="0", t=3.0, text="Tôi đồng ý")
    assert speakers.decided.get(2) == "Nguyễn Hoàng Nam"
    print("Subsequent utterance correctly inherited override:", speakers.decided.get(2))

    # 2. Test confirm-identity endpoint on API
    mid = db.create_meeting(title="Test Rename API")
    db.add_segment(meeting_id=mid, t_start=1.0, t_end=3.0, speaker_label="Người nói #1", text="Em chào anh Phúc")
    
    res = client.post(f"/api/meetings/{mid}/confirm-identity", json={
        "unknown_label": "Người nói #1",
        "confirmed_name": "Phạm Thùy Linh",
        "role": "Lead Frontend UI/UX",
        "email": "linh.pt@urbox.vn"
    })
    data = res.json()
    print("API confirm-identity result:", data)
    assert data["success"] is True or data.get("updated") is True

    # Verify segments in DB are updated
    segs = db.get_segments(mid)
    assert segs[0]["speaker_label"] == "Phạm Thùy Linh"
    print("DB segment successfully updated to:", segs[0]["speaker_label"])

    # Clean up
    db.delete_meeting(mid)
    print("Cleaned up test meeting.")
    print("ALL TESTS PASSED!")

if __name__ == "__main__":
    test_manual_override()
