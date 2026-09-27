@echo off
cd /d "%~dp0\.."
set PYTHONPATH=%CD%
if not exist config.json copy config.example.json config.json
python -m krflow.cli dashboard --out results
pause
