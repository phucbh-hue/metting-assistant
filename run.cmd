@echo off
REM Khoi dong Meeting Copilot: bam dup file nay, hoac go "run" trong terminal.
REM Tuy chon: "run demo" -> server demo du lieu gia lap o cong 8090, khong can mic / Atlas.
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

set "PY=C:\work\cralwer with ai\ASR\interviewer-assistant-AI-circle\.venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto :nopy
if /i "%~1"=="demo" goto :demo

if not exist ".env" goto :noenv
if not exist "models\speaker.onnx" echo [CANH BAO] Thieu models\speaker.onnx: se khong nhan dien duoc nguoi noi bang giong.
if not exist "models\tts" call :gettts

echo Dang khoi dong Meeting Copilot tai http://127.0.0.1:8080 - bam Ctrl+C de dung.
start "" /b cmd /c "timeout /t 4 >nul & start "" http://127.0.0.1:8080"
"%PY%" -m uvicorn meeting.app:app --host 127.0.0.1 --port 8080
echo.
echo Server da dung.
pause
exit /b 0

:demo
echo Dang chay server DEMO du lieu gia lap tai http://127.0.0.1:8090 ...
start "" /b cmd /c "timeout /t 4 >nul & start "" http://127.0.0.1:8090"
"%PY%" scripts\demo_replay.py --port 8090 --loop
pause
exit /b 0

:gettts
echo Chua co giong doc tieng Viet. Dang tai khoang 67 MB, chi mot lan...
"%PY%" scripts\download_tts.py
exit /b 0

:nopy
echo [LOI] Khong tim thay Python cua venv: %PY%
echo       Sua duong dan PY trong run.cmd, hoac tao venv rieng: python -m venv .venv  roi  .venv\Scripts\pip install -r requirements.txt
pause
exit /b 1

:noenv
echo [LOI] Chua co file .env. Sao chep .env.example thanh .env roi dien API key.
pause
exit /b 1
