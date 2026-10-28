@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set UV_CACHE_DIR=%CD%\.uv-cache
uv run python -m guikerong_finance
