@echo off
REM 한국투자증권 모의투자 스케줄러 [미검증]. 환경변수 KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO 필요.
REM 실계좌 모드는 코드에서 차단되어 있음.
cd /d "%~dp0\.."
set PYTHONPATH=%CD%
python -m krflow.cli --mode kis_vts kis-check
python -m krflow.cli --mode kis_vts serve
pause
