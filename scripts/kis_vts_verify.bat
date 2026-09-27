@echo off
REM 한국투자증권 모의투자 연동 검증. 환경변수 KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO 필요.
REM 장중(09:00~15:15)에 실행하면 체결되지 않을 가격으로 1주 주문 -> 조회 -> 취소까지 검증한다.
cd /d "%~dp0\.."
set PYTHONPATH=%CD%
python -m krflow.cli --mode kis_vts --db data\kis.db kis-verify --with-order
pause
