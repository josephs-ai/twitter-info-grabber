@echo off
REM Windows entry point. Mirrors ./run on Unix.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m tracker %*
) else (
  python -m tracker %*
)
