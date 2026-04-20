@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 启动 Fund Insight 服务器
:: 使用 python -m src 作为统一入口，修复模块导入问题

python start.py
pause
