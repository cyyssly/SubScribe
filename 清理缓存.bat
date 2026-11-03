@echo off
setlocal
cd /d "%~dp0"

echo 正在清理 Gradio 上传缓存...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\clean_gradio_cache.ps1"
if errorlevel 1 echo [提示] 未能清理，请先关闭字幕工具窗口。

echo.
pause
