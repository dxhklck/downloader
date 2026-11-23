@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 视频下载助手

where python >nul 2>&1
if %errorlevel%==0 (
    python main.py
) else (
    where py >nul 2>&1
    if %errorlevel%==0 (
        py -3 main.py
    ) else (
        echo 未找到 Python，请先安装 Python 3.10 及以上版本。
        echo 安装后请勾选 "Add Python to PATH"。
    )
)
if errorlevel 1 pause
