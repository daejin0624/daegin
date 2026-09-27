@echo off
REM KRX 날짜별 전종목 시세·수급 수집 [미검증]. data.krx.co.kr 계정 필요.
REM 먼저 이 창에서:  set KRX_ID=아이디   set KRX_PW=비밀번호
REM 2년치(약 490거래일)는 날짜당 4~8회 호출이라 1시간 안팎 걸린다. 중단 후 다시 실행하면 빠진 날만 받는다.
cd /d "%~dp0\.."
set PYTHONPATH=%CD%
if "%KRX_ID%"=="" echo KRX_ID 환경변수가 없습니다. & pause & exit /b 1
python -m krflow.cli --db data\real.db collect-pykrx --start 2024-01-01 --end 2025-12-31
python -m krflow.cli --db data\real.db quality
python -m krflow.cli --db data\real.db compare --start 2024-02-01 --end 2025-12-31 --split 2025-01-01 --kind real_backtest --out results
pause
