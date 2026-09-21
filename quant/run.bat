@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ============================================================
REM  JEV 量化交易系统 - Windows 一键启动
REM    run.bat          构建前端并由后端单端口托管 -> http://localhost:8000
REM    run.bat dev      仅启动后端(带自动重载)，前端另开 npm run dev
REM    run.bat test     跑后端测试
REM  端口可改：set PORT=8010 && run.bat
REM ============================================================

if "%PORT%"=="" set PORT=8000
set MODE=%~1
if "%MODE%"=="" set MODE=all

where python >nul 2>nul
if errorlevel 1 (
    echo [jev] 未找到 python，请先安装 Python 3.11+ 并勾选 "Add to PATH"
    pause & exit /b 1
)

if not exist ".venv" (
    echo [jev] 创建虚拟环境 .venv ...
    python -m venv .venv || (echo [jev] 创建虚拟环境失败 & pause & exit /b 1)
)

echo [jev] 安装后端依赖 ...
call ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
call ".venv\Scripts\python.exe" -m pip install -q -r backend\requirements.txt || (echo [jev] 依赖安装失败 & pause & exit /b 1)

if /i "%MODE%"=="test" (
    call ".venv\Scripts\python.exe" -m pip install -q -r backend\requirements-dev.txt
    pushd backend
    call "..\.venv\Scripts\python.exe" -m pytest -q
    popd
    pause & exit /b 0
)

if /i "%MODE%"=="all" (
    where npm >nul 2>nul
    if errorlevel 1 (
        echo [jev] 未找到 npm，跳过前端构建；启动后只提供 API 接口 ^(/docs^)
        echo [jev] 需要大屏请安装 Node 18+ 后重新运行本脚本
    ) else (
        if not exist "frontend\node_modules" (
            echo [jev] 安装前端依赖（首次约 1 分钟）...
            pushd frontend & call npm install --no-audit --no-fund & popd
        )
        echo [jev] 构建前端 ...
        pushd frontend & call npm run build & popd
    )
)

echo.
echo [jev] 启动服务: http://localhost:%PORT%    ^(API 文档: /docs，Ctrl+C 停止^)
echo.
pushd backend
if /i "%MODE%"=="dev" (
    call "..\.venv\Scripts\python.exe" -m uvicorn app.server:app --host 127.0.0.1 --port %PORT% --reload
) else (
    call "..\.venv\Scripts\python.exe" -m uvicorn app.server:app --host 127.0.0.1 --port %PORT%
)
popd
pause
