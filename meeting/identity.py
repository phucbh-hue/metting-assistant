"""Semantic Identity Inference Engine (AI Suy Luận Danh Tính Người Lạ Từ Ngữ Cảnh Hội Thoại).

Giải quyết bài toán:
Trong cuộc họp, khi có một "Người lạ" (chưa có mẫu giọng trong Voice Registry),
AI sẽ lắng nghe nội dung trò chuyện, phân tích các tín hiệu ngữ nghĩa (xưng hô,
tự giới thiệu, câu hỏi hướng đích, đối chiếu chéo công việc qua Mock Database MCP),
từ đó:
1. Dự đoán chính xác người đó là ai, tên gì, đảm nhiệm vai trò/phòng ban nào.
2. Tự động cập nhật lùi (retroactive update) toàn bộ các câu thoại trước đó của người này.
3. Tự động tính toán vector đặc trưng giọng nói (Centroid Embedding) và lưu trữ vào
   Voice Registry (PostgreSQL). Trong các cuộc họp sau này, người này cất giọng lên là
   hệ thống nhận diện được ngay lập tức!
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
from dotenv import load_dotenv
from pydantic import AliasChoices, BaseModel, Field

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting import db, live, mcp, voice

log = logging.getLogger("meeting.identity")

AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.6-flash")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
PROVIDER = (os.getenv("LLM_PROVIDER") or "claude").strip().lower()


class IdentityClue(BaseModel):
    clue_type: str = Field(default="context", validation_alias=AliasChoices("clue_type", "type"))
    quote: str = Field(default="", validation_alias=AliasChoices("quote", "evidence_quote", "text"))
    speaker_involved: str = Field(default="", validation_alias=AliasChoices("speaker_involved", "speaker"))


class IdentityPrediction(BaseModel):
    unknown_label: str = Field(default="", validation_alias=AliasChoices("unknown_label", "speaker_label", "label"))
    predicted_name: str = Field(default="", validation_alias=AliasChoices("predicted_name", "name"))
    predicted_role: str = Field(default="", validation_alias=AliasChoices("predicted_role", "role"))
    predicted_email: str = Field(default="", validation_alias=AliasChoices("predicted_email", "email"))
    confidence: float = Field(default=0.5)
    reasoning: str = Field(default="")
    evidence: List[Union[IdentityClue, str, Dict[str, Any]]] = Field(default_factory=list)


class MeetingIdentityAnalysis(BaseModel):
    predictions: List[IdentityPrediction]


# ==============================================================================
# PROMPT XÂY DỰNG NGỮ CẢNH SUY LUẬN DANH TÍNH
# ==============================================================================
SYSTEM_PROMPT = """Bạn là chuyên gia AI phân tích ngôn ngữ và suy luận danh tính người nói qua đối thoại tiếng Việt.
Nhiệm vụ: Dựa vào transcript cuộc đối thoại và ngữ cảnh xung quanh, hãy tìm ra TÊN THỰC SỰ và VAI TRÒ của các người nói đang mang nhãn tạm (ví dụ: 'Người lạ #1', 'Người lạ #2', 'Người nói #1', 'Speaker_X').

NGUYÊN TẮC SUY LUẬN TÊN TRONG HỘI THOẠI TIẾNG VIỆT:
1. Lời chào và xưng hô trực tiếp (Direct Address & Greeting):
   - Nếu A nói: "Dạ, em chào chị Hương. Chị có khỏe không chị?" và B trả lời: "Chào em, chị khỏe. Còn em?" -> B CHẮC CHẮN là "Hương" (hoặc "Chị Hương").
   - Nếu ai đó nói: "First, Nam greets Hương by saying: Dạ em chào chị Hương..." -> Người mở đầu chào chính là "Nam", và người được chào là "Hương".
   - Nếu A nói: "Tuấn ơi, xong chưa?" và B đáp: "Dạ em xong rồi" -> B chính là "Tuấn".
2. Tự giới thiệu (Self-Introduction):
   - "Chào bạn, mình tên là Lan Anh..." -> Người đó tên là "Lan Anh".
   - "Em là Tuấn bên DevOps..." -> Người đó tên là "Lê Văn Tuấn" (DevOps).
   - "Em là Quân bên Payment Core..." -> Người đó tên là "Đỗ Minh Quân" (Backend).
3. Người thứ ba nhắc đến hoặc giới thiệu:
   - "Hôm nay có Hương và Nam tham gia cùng chúng ta..." -> Các lượt lời tiếp theo tương ứng với các nhân vật này.
4. Đối chiếu với Danh bạ nhân sự / Dự án MCP:
   - Nếu tên hoặc công việc người đó nói khớp với nhân viên nội bộ (ví dụ: Phúc, Nam, Tuấn, Quân, Hằng, Linh, Trang...) thì lấy đầy đủ Họ tên, chức vụ, email từ danh bạ.
   - NẾU KHÔNG CÓ TRONG DANH BẠ NỘI BỘ (ví dụ khách mời, đối tác, phỏng vấn, podcast, nhân vật hội thoại như 'Hương', 'Nam', 'Lan Anh'): BẮT BUỘC VẪN PHẢI TRÍCH XUẤT VÀ DỰ ĐOÁN ĐÚNG TÊN CỦA HỌ (ví dụ: predicted_name = 'Hương', predicted_role = 'Người tham gia / Giáo viên') với độ tin cậy cao (confidence >= 0.85) khi hội thoại đã nêu rõ tên! TUYỆT ĐỐI KHÔNG ĐỂ TRỐNG TÊN CHỈ VÌ HỌ KHÔNG CÓ TRONG DANH BẠ CÔNG TY!

Thang điểm độ tin cậy (confidence):
- 0.85 - 1.00: Có lời chào gọi đích danh ("chào chị Hương", "Nam ơi"), hoặc có người nhắc tên ("Nam greets Hương"), hoặc tự xưng tên ("mình tên là Lan Anh", "em là Tuấn"). Đủ điều kiện tự động đổi tên ngay!
- 0.65 - 0.84: Có manh mối tên từ ngữ cảnh nhưng còn phân vân vai trò.
- < 0.50: Người đó chỉ nói vài từ ậm ừ ngắn ("dạ", "ừ"), chưa có ai gọi tên và chưa từng xưng tên.

Hãy trả về kết quả định dạng JSON chuẩn theo schema:
{"predictions": [{"unknown_label": "...", "predicted_name": "...", "predicted_role": "...", "predicted_email": "...", "confidence": 0.95, "reasoning": "...", "evidence": [...]}]}"""


def _format_context(segments: List[Dict[str, Any]], target_label: str) -> str:
    lines = []
    for s in segments[-40:]:
        spk = s.get("speaker_label") or f"Speaker_{s.get('speaker_id', '?')}"
        txt = s.get("text", "")
        mark = " >>> [ĐANG CẦN ĐOÁN] " if spk == target_label else ""
        lines.append(f"{mark}{spk}: {txt}")
    return "\n".join(lines)


async def infer_unknown_speaker(meeting_id: int, unknown_label: str,
                                segments: List[Dict[str, Any]]) -> Optional[IdentityPrediction]:
    """Phân tích các câu thoại để suy luận danh tính của một Unknown Speaker."""
    # Lấy dữ liệu MCP bổ trợ (danh bạ nhân viên & jira)
    emp_res = mcp.call_tool("query_employee_directory", {"query": ""})
    jira_res = mcp.call_tool("query_jira_issues", {})
    known_voices = db.list_voices()

    transcript_text = _format_context(segments, unknown_label)

    prompt = f"""
## Danh bạ nhân sự công ty (từ Database MCP):
{json.dumps(emp_res.get('employees', []), ensure_ascii=False, indent=2)}

## Các công việc / tickets Jira đang thực hiện:
{json.dumps(jira_res.get('issues', []), ensure_ascii=False, indent=2)}

## Danh sách người quen đã có mẫu giọng:
{json.dumps([v['name'] for v in known_voices], ensure_ascii=False)}

## Đối tượng cần suy luận danh tính:
Nhãn: "{unknown_label}"

## Đoạn hội thoại cuộc họp gần nhất:
{transcript_text}

Hãy phân tích và trả về thông tin suy luận danh tính cho nhãn "{unknown_label}".
Nếu không có bất kỳ manh mối nào hoặc người đó chưa nói gì đáng kể, đặt confidence = 0.1 và predicted_name = "".
"""

    try:
        if PROVIDER == "claude" and os.getenv("ANTHROPIC_API_KEY"):
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            resp = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt + "\n\nTrả về DUY NHẤT một JSON hợp lệ dạng: {\"predictions\": [...]}"}]
            )
            texts = [b.text for b in resp.content if getattr(b, "type", None) == "text" or hasattr(b, "text")]
            raw_text = "\n".join(texts)
        else:
            from google import genai
            g_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            # Run via asyncio.to_thread
            resp = await asyncio.to_thread(
                g_client.interactions.create,
                model=AGENT_MODEL,
                system_instruction=SYSTEM_PROMPT,
                input=prompt + "\n\nTrả về DUY NHẤT một JSON hợp lệ dạng: {\"predictions\": [...]}"
            )
            raw_text = resp.output_text

        # Extract JSON
        m = re.search(r"(\{.*\})", raw_text, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(1))
        preds = data.get("predictions", [])
        if not preds:
            return None

        # Parse first prediction
        pred = IdentityPrediction(**preds[0])
        return pred

    except Exception as e:
        log.error("meeting.identity inference error for %s: %s", unknown_label, e)
        return None


async def process_unknown_speakers(meeting_id: int, meeting_speakers: voice.MeetingSpeakers,
                                   segments: List[Dict[str, Any]],
                                   on_renamed: Optional[Callable[[Dict[str, Any]], Any]] = None,
                                   on_suggest: Optional[Callable[[Dict[str, Any]], Any]] = None):
    """Quét tất cả các nhãn 'Người lạ' trong cuộc họp và tiến hành suy luận."""
    # Tìm các nhãn chưa xác định danh tính (bao gồm Người lạ, Người nói, Speaker, Unknown)
    labels = {s.get("speaker_label") for s in segments}
    unknown_labels = []
    for lbl in labels:
        if not lbl:
            continue
        l_lower = lbl.lower()
        if any(prefix in l_lower for prefix in ["người lạ", "người nói", "speaker", "unknown"]):
            unknown_labels.append(lbl)

    for u_label in unknown_labels:
        # Đếm số câu và kiểm tra xem có đủ dữ liệu chưa
        u_segs = [s for s in segments if s.get("speaker_label") == u_label]
        if len(u_segs) < 1:
            continue

        pred = await infer_unknown_speaker(meeting_id, u_label, segments)
        if not pred or not pred.predicted_name or pred.confidence < 0.50:
            continue

        evidence_dicts = [
            c.model_dump() if hasattr(c, "model_dump") else
            ({"clue_type": "context", "quote": str(c), "speaker_involved": ""} if isinstance(c, str) else dict(c))
            for c in pred.evidence
        ]

        # ----------------------------------------------------------------------
        # MỨC 1: CAO (Confidence >= 0.85) -> TỰ ĐỘNG CẬP NHẬT LÙI & LƯU GIỌNG
        # ----------------------------------------------------------------------
        if pred.confidence >= 0.85:
            log.info("meeting.identity: TỰ ĐỘNG nhận diện %s -> %s (conf=%.2f)",
                     u_label, pred.predicted_name, pred.confidence)

            # 1. Lưu bản ghi suy luận vào database
            db.save_identity_inference(
                meeting_id=meeting_id,
                unknown_label=u_label,
                predicted_name=pred.predicted_name,
                predicted_role=pred.predicted_role,
                confidence=pred.confidence,
                reasoning=pred.reasoning,
                evidence=evidence_dicts,
                status="auto_applied"
            )

            # 2. Cập nhật lùi toàn bộ segments cũ trong database
            updated_count = db.update_segments_speaker(
                meeting_id=meeting_id,
                old_label=u_label,
                new_name=pred.predicted_name,
                is_inferred=True
            )

            # 3. Thu thập các vector âm thanh sạch để tính centroid và đăng ký vào Voice Registry
            vectors = meeting_speakers.get_cluster_vectors(u_label) if meeting_speakers else []
            if not vectors:
                db_segs = db.get_segments(meeting_id)
                for s in db_segs:
                    if (s.get("speaker_label") == u_label or s.get("speaker_label") == pred.predicted_name) and s.get("raw_embedding"):
                        vectors.append(np.asarray(s["raw_embedding"], dtype=np.float32))

            new_voice_id = None
            if vectors:
                centroid_vec = voice.merge_vectors(vectors)
                new_voice_id = db.save_voice(
                    name=pred.predicted_name,
                    embedding=centroid_vec.tolist(),
                    role=pred.predicted_role,
                    email=pred.predicted_email,
                    consent_by="meeting_host",
                    auto_learned=True,
                    n_samples=len(vectors)
                )
                log.info("meeting.identity: Đã tự động lưu giọng cho '%s' vào DB (voice_id=%s, %d vectors)",
                         pred.predicted_name, new_voice_id, len(vectors))

                if meeting_speakers:
                    meeting_speakers.update_anchors({
                        new_voice_id: {
                            "name": pred.predicted_name,
                            "vector": centroid_vec,
                            "role": pred.predicted_role,
                            "department": ""
                        }
                    })

            # 4. Bắn sự kiện cập nhật ra WebSocket cho giao diện
            event_payload = {
                "type": "speaker_renamed",
                "meeting_id": meeting_id,
                "old_label": u_label,
                "new_name": pred.predicted_name,
                "role": pred.predicted_role,
                "confidence": pred.confidence,
                "reasoning": pred.reasoning,
                "voice_id": new_voice_id,
                "updated_segments": updated_count
            }
            if on_renamed:
                await on_renamed(event_payload)

        # ----------------------------------------------------------------------
        # MỨC 2: TRUNG BÌNH (0.50 <= Confidence < 0.85) -> GỢI Ý XÁC NHẬN
        # ----------------------------------------------------------------------
        elif pred.confidence >= 0.50:
            log.info("meeting.identity: GỢI Ý nhận diện %s -> %s (conf=%.2f)",
                     u_label, pred.predicted_name, pred.confidence)

            db.save_identity_inference(
                meeting_id=meeting_id,
                unknown_label=u_label,
                predicted_name=pred.predicted_name,
                predicted_role=pred.predicted_role,
                confidence=pred.confidence,
                reasoning=pred.reasoning,
                evidence=evidence_dicts,
                status="pending"
            )

            suggest_payload = {
                "type": "identity_suggestion",
                "meeting_id": meeting_id,
                "unknown_label": u_label,
                "suggested_name": pred.predicted_name,
                "role": pred.predicted_role,
                "confidence": pred.confidence,
                "reasoning": pred.reasoning,
                "evidence": evidence_dicts
            }
            if on_suggest:
                await on_suggest(suggest_payload)


async def confirm_speaker_identity(meeting_id: int, unknown_label: str, confirmed_name: str,
                                   meeting_speakers: Optional[voice.MeetingSpeakers] = None,
                                   role: str = "", email: str = "") -> Dict[str, Any]:
    """Người dùng bấm nút xác nhận danh tính người lạ trên Web UI."""
    updated_count = db.update_segments_speaker(
        meeting_id=meeting_id,
        old_label=unknown_label,
        new_name=confirmed_name,
        is_inferred=False
    )

    vectors = meeting_speakers.get_cluster_vectors(unknown_label) if meeting_speakers else []
    if not vectors:
        db_segs = db.get_segments(meeting_id)
        for s in db_segs:
            if (s.get("speaker_label") == unknown_label or s.get("speaker_label") == confirmed_name) and s.get("raw_embedding"):
                vectors.append(np.asarray(s["raw_embedding"], dtype=np.float32))

    new_voice_id = None
    if vectors:
        centroid_vec = voice.merge_vectors(vectors)
        new_voice_id = db.save_voice(
            name=confirmed_name,
            embedding=centroid_vec.tolist(),
            role=role,
            email=email,
            consent_by="user_manual_confirm",
            auto_learned=True,
            n_samples=len(vectors)
        )
        if meeting_speakers:
            meeting_speakers.update_anchors({
                new_voice_id: {
                    "name": confirmed_name,
                    "vector": centroid_vec,
                    "role": role,
                    "department": ""
                }
            })

    # Ghi đè vào bộ nhớ MeetingSpeakers để thắng tuyệt đối mọi câu tiếp theo
    if meeting_speakers:
        meeting_speakers.override_speaker(unknown_label, confirmed_name, new_voice_id)

    return {
        "success": True,
        "old_label": unknown_label,
        "new_name": confirmed_name,
        "voice_id": new_voice_id,
        "updated_segments": updated_count
    }


async def infer_all_speakers(meeting_id: int, segments: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Phân tích toàn diện hội thoại cuộc họp và suy luận danh tính cho TẤT CẢ các người nói chưa định danh."""
    if segments is None:
        segments = db.get_segments(meeting_id)
    if not segments:
        return []

    # Tìm tất cả nhãn chưa xác định danh tính
    labels = {s.get("speaker_label") for s in segments}
    unidentified_labels = []
    for lbl in labels:
        if not lbl:
            continue
        l_lower = lbl.lower()
        if any(prefix in l_lower for prefix in ["người lạ", "người nói", "speaker", "unknown"]):
            unidentified_labels.append(lbl)

    if not unidentified_labels:
        return []

    # Lấy thông tin MCP và người quen
    emp_res = mcp.call_tool("query_employee_directory", {"query": ""})
    jira_res = mcp.call_tool("query_jira_issues", {})
    known_voices = db.list_voices()

    # Format toàn bộ transcript
    lines = []
    for s in segments[-60:]:
        spk = s.get("speaker_label", "Unknown")
        txt = s.get("text", "")
        lines.append(f"[{spk}]: {txt}")
    full_transcript = "\n".join(lines)

    prompt = f"""
## Danh bạ nhân sự nội bộ UrBox (nếu khớp):
{json.dumps(emp_res.get('employees', []), ensure_ascii=False, indent=2)}

## Các nhãn người nói cần suy luận danh tính:
{json.dumps(unidentified_labels, ensure_ascii=False)}

## Toàn bộ đối thoại cuộc họp:
{full_transcript}

Hãy đọc kỹ toàn bộ đối thoại, chú ý lời chào ("chào chị Hương", "chào em..."), tên được nhắc ("Nam greets Hương", "chị Lan Anh"), cách xưng hô, hoặc tự giới thiệu ("mình tên là...", "em là...").
Với MỖI nhãn trong danh sách {json.dumps(unidentified_labels, ensure_ascii=False)}, hãy xác định xem nhãn đó tương ứng với ai (ví dụ: 'Hương', 'Nam', 'Lan Anh', hoặc nhân viên UrBox nếu khớp).
Nếu hội thoại có nhắc tên rõ ràng, hãy dự đoán đúng tên và đặt confidence >= 0.85!
Trả về DUY NHẤT một JSON hợp lệ:
{{"predictions": [
  {{
    "unknown_label": "Nhãn cần đoán",
    "predicted_name": "Tên suy luận được",
    "predicted_role": "Vai trò (ví dụ: Người đàm thoại / Giáo viên / Khách mời / DevOps...)",
    "predicted_email": "Email nếu có",
    "confidence": 0.95,
    "reasoning": "Căn cứ suy luận...",
    "evidence": [{{"clue_type": "greeting", "quote": "trích dẫn..."}}]
  }}
]}}
"""
    raw_text = ""
    try:
        if PROVIDER == "claude" and os.getenv("ANTHROPIC_API_KEY"):
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            resp = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            texts = [b.text for b in resp.content if getattr(b, "type", None) == "text" or hasattr(b, "text")]
            raw_text = "\n".join(texts)
        else:
            from google import genai
            g_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            resp = await asyncio.to_thread(
                g_client.interactions.create,
                model=AGENT_MODEL,
                system_instruction=SYSTEM_PROMPT,
                input=prompt
            )
            raw_text = resp.output_text
    except Exception as e:
        log.error("infer_all_speakers LLM error: %s", e)
        return []

    # Parse JSON
    from meeting import llm
    data = llm._extract_json(raw_text)
    if not data or not isinstance(data, dict):
        try:
            cleaned = re.sub(r'\\"', '"', raw_text)
            cleaned = re.sub(r'```(?:json)?', '', cleaned)
            m = re.search(r"(\{.*\})", cleaned, re.DOTALL)
            if m:
                data = json.loads(m.group(1))
        except Exception:
            pass

    preds = data.get("predictions", []) if isinstance(data, dict) else []

    applied_results = []
    ls = live.LIVE_MEETINGS.get(meeting_id)

    for item in preds:
        try:
            pred = IdentityPrediction(**item)
            u_lbl = pred.unknown_label
            pred_name = pred.predicted_name.strip()
            if not u_lbl or not pred_name or pred.confidence < 0.60:
                continue

            evidence_dicts = [
                c.model_dump() if hasattr(c, "model_dump") else
                ({"clue_type": "context", "quote": str(c), "speaker_involved": ""} if isinstance(c, str) else dict(c))
                for c in pred.evidence
            ]

            # 1. Lưu suy luận vào DB
            db.save_identity_inference(
                meeting_id=meeting_id,
                unknown_label=u_lbl,
                predicted_name=pred_name,
                predicted_role=pred.predicted_role,
                confidence=pred.confidence,
                reasoning=pred.reasoning,
                evidence=evidence_dicts,
                status="auto_applied"
            )

            # 2. Cập nhật lùi toàn bộ segments cũ trong database
            updated_count = db.update_segments_speaker(
                meeting_id=meeting_id,
                old_label=u_lbl,
                new_name=pred_name,
                is_inferred=True
            )

            # 3. Thu thập vectors và tự động lưu vào Voice Registry (MongoDB Atlas)
            vectors = []
            if ls:
                vectors = ls.speakers.get_cluster_vectors(u_lbl)
            if not vectors:
                db_segs = db.get_segments(meeting_id)
                for s in db_segs:
                    if (s.get("speaker_label") == u_lbl or s.get("speaker_label") == pred_name) and s.get("raw_embedding"):
                        vectors.append(np.asarray(s["raw_embedding"], dtype=np.float32))

            new_voice_id = None
            if vectors:
                centroid_vec = voice.merge_vectors(vectors)
                new_voice_id = db.save_voice(
                    name=pred_name,
                    embedding=centroid_vec.tolist(),
                    role=pred.predicted_role or "Người tham gia",
                    email=pred.predicted_email,
                    consent_by="ai_identity_inference",
                    auto_learned=True,
                    n_samples=len(vectors)
                )
                log.info("infer_all_speakers: Đã lưu vector giọng cho %s vào voices DB (id=%s, %d mẫu)",
                         pred_name, new_voice_id, len(vectors))

            if ls:
                if new_voice_id:
                    ls.speakers.update_anchors({
                        new_voice_id: {
                            "name": pred_name,
                            "vector": centroid_vec,
                            "role": pred.predicted_role,
                            "department": ""
                        }
                    })
                ls.speakers.override_speaker(u_lbl, pred_name, new_voice_id)
                for s in ls.segments:
                    if s.get("speaker_label") == u_lbl:
                        s["speaker_label"] = pred_name
                        if new_voice_id:
                            s["speaker_id"] = new_voice_id

                # Bắn WebSocket event
                asyncio.create_task(ls.emit({
                    "type": "speaker_renamed",
                    "meeting_id": meeting_id,
                    "old_label": u_lbl,
                    "new_name": pred_name,
                    "role": pred.predicted_role,
                    "confidence": pred.confidence,
                    "reasoning": pred.reasoning,
                    "voice_id": new_voice_id,
                    "updated_segments": updated_count
                }))

            applied_results.append({
                "old_label": u_lbl,
                "new_name": pred_name,
                "role": pred.predicted_role,
                "confidence": pred.confidence,
                "reasoning": pred.reasoning,
                "updated_segments": updated_count
            })
            log.info("infer_all_speakers: Renamed %s -> %s (conf=%.2f, %d segs)",
                     u_lbl, pred_name, pred.confidence, updated_count)
        except Exception as ex:
            log.warning("infer_all_speakers item error: %s", ex)

    return applied_results
