@echo off
chcp 65001 >nul
title LectureScribe 课堂智能双语速记与排版工作台
echo ===================================================
echo   LectureScribe 课堂智能双语速记与排版工作台
echo ===================================================
echo 正在启动本地服务...
cd /d "%~dp0"
start http://localhost:8000
python server.py
pause
