@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto noenv

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 18765); $c.Close(); exit 0 } catch { exit 1 }"
if not errorlevel 1 goto guicheck

echo 正在启动翻译服务，首次加载模型约需十几秒...
start "llama-server" /min powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_llama.ps1"

:guicheck
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 7860); $c.Close(); exit 0 } catch { exit 1 }"
if not errorlevel 1 goto already

:startapp
echo 正在打开字幕生成界面...
start "字幕工具" /min "%PY%" "%~dp0app.py"
if errorlevel 1 pause
exit /b 0

:already
echo 界面已经在运行，直接打开已有页面...
start "" "http://127.0.0.1:7860"
exit /b 0

:noenv
echo [错误] 找不到虚拟环境 .venv
echo 请先阅读 readme.txt 完成环境搭建。
pause
exit /b 1