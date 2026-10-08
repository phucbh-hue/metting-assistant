# Server Meeting Copilot (FastAPI) cho bản web: chạy ở máy luôn bật (Render, Railway, Fly.io, máy chủ công ty...).
# Giao diện chạy riêng trên Vercel (vercel.json, scripts/build-web.mjs). Xem README mục "Chạy trên web".
#
#   docker build -t meeting-copilot .
#   docker run -p 8080:8080 -v meeting-data:/app/data --env-file prod.env meeting-copilot
#
# Chỉ chạy MỘT bản server: phòng họp đang diễn ra nằm trong bộ nhớ của server.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Tra cứu web bằng trình duyệt thật (Playwright, thêm khoảng 500 MB). Mặc định tắt: tra cứu qua Claude.
ARG WITH_BROWSER=0
RUN if [ "$WITH_BROWSER" = "1" ]; then python -m playwright install --with-deps chromium; fi

# Model nhận diện giọng ERes2NetV2 (71 MB, kiểm tra sha256 giống scripts/mst-urbox.mjs)
RUN mkdir -p models \
 && curl -fsSL -o models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx \
 && echo "bf1a75b9930474cf3389ef415e6e5d38ca96fea4a3a00f7e301d080a58ee2239  models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx" | sha256sum -c -

# Giọng đọc tiếng Việt chạy trên server (67 MB), dùng khi chưa có Soniox TTS. Đặt 0 để bỏ.
ARG WITH_LOCAL_TTS=1
COPY scripts/download_tts.py scripts/download_tts.py
RUN if [ "$WITH_LOCAL_TTS" = "1" ]; then python scripts/download_tts.py; fi

COPY meeting meeting
COPY static static
COPY mcp_server mcp_server
COPY slides slides
COPY vesper.html vesper.html

# Người dùng không có quyền root; data/ (nội dung họp lưu trên máy, ghi âm, ảnh tài liệu, khóa nhập trong Cài đặt)
# gắn ổ lưu lâu dài vào /app/data
RUN useradd --create-home --uid 10001 app && mkdir -p data && chown -R app:app data
USER app
ENV MEETING_ENV_FILE=/app/data/.env AUTH_REQUIRED=1 PORT=8080
VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1
CMD ["sh", "-c", "exec uvicorn meeting.app:app --host 0.0.0.0 --port ${PORT}"]
