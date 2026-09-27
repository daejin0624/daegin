@echo off
REM 한국투자증권 모의투자 스케줄러 [미검증]. KIS_* 와 KRX_ID/KRX_PW 환경변수 필요.
REM 먼저 kis_vts_verify.bat 로 기능별 검증을 통과시킬 것. 실계좌 모드는 코드에서 차단되어 있음.
REM 일정: 08:50 매수(장전 동시호가) / 09:05 체결 반영·미체결 취소 / 15:21 매도(종가 동시호가) / 15:40 체결 반영 / 18:20 수집 / 18:30 후보 산출
cd /d "%~dp0\.."
set PYTHONPATH=%CD%
python -m krflow.cli --mode kis_vts --db data\kis.db serve
pause
