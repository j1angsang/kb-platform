@echo off
REM 一键启动：迁移 head 后以 8005 端口起服务
cd /d %~dp0
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\python -m uvicorn main:app --reload --port 8005
pause
