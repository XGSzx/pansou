@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 影盘搜 · 网页版

set "PYCMD="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"

if not defined PYCMD (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    echo %%i | findstr /i "WindowsApps" >nul
    if errorlevel 1 set "PYCMD=%%i"
  )
)

if not defined PYCMD (
  echo.
  echo [错误] 没有找到可用的 Python 3。
  echo 请先安装 Python 3.9 或更高版本，安装时务必勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo 正在启动影盘搜，浏览器会自动打开 http://127.0.0.1:8765 ...
echo 关闭本窗口即停止服务。
echo.
%PYCMD% -u -m pansou web

echo.
echo 服务已退出，按任意键关闭窗口。
pause >nul
