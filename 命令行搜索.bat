@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 影盘搜 · 命令行

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

set "NAME=%~1"
if "%NAME%"=="" set /p NAME=请输入影视名称（可带年份/季数）：
if "%NAME%"=="" goto :end

echo.
%PYCMD% -u -m pansou "%NAME%" --save
echo.
echo 结果已保存到 out 目录（没有结果时不会生成文件）。

:end
echo.
pause >nul
