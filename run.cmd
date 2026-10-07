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
if not exist "models\3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx" echo [CANH BAO] Chua co model ERes2NetV2 (pnpm mst-urbox build): dang dung CAM++.
if not exist "models\tts" call :gettts

call :freeport
if errorlevel 1 exit /b 0

echo Dang khoi dong Meeting Copilot tai http://127.0.0.1:8080 - bam Ctrl+C de dung.
echo Neu khong ket noi duoc MongoDB Atlas, du lieu se luu tren may tai data\local_db (khong mat khi tat server).
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

:freeport
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if not errorlevel 1 exit /b 0
echo.
echo Cong 8080 dang duoc dung: co the server cu van dang chay o mot cua so khac.
choice /C YN /T 20 /D N /M "Dung server cu va chay lai (Y), hay giu server cu va chi mo trinh duyet (N)? Tu chon N sau 20 giay"
if errorlevel 2 (
    start "" http://127.0.0.1:8080
    exit /b 1
)
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
timeout /t 2 >nul
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
