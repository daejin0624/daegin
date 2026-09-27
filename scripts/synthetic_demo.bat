@echo off
REM 합성 데이터로 전체 흐름 검증 (결과는 실제 투자 성과가 아님)
cd /d "%~dp0\.."
set PYTHONPATH=%CD%
if not exist config.json copy config.example.json config.json
python -m krflow.cli --db data\synthetic.db synth --start 2024-01-01 --end 2025-12-31 --codes 40 --seed 7 --inject-issues
python -m krflow.cli --db data\synthetic.db compare --start 2024-02-01 --end 2025-12-31 --split 2025-01-01 --out results
python -m krflow.cli --db data\synthetic.db paper --start 2025-06-01 --end 2025-12-31
pause
