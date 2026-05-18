@echo off
REM AgentFlow Windows 一键启动脚本
REM 同时启动 FastAPI 后端 + Streamlit 前端

echo ========================================
echo   AgentFlow 启动中...
echo ========================================

REM 检查 .env
if not exist .env (
    echo [!] 未找到 .env 文件
    echo     请复制 .env.example 为 .env 并填入 DeepSeek API Key
    pause
    exit /b 1
)

REM 设置 HuggingFace 镜像（国内加速 BGE 模型下载）
set HF_ENDPOINT=https://hf-mirror.com

echo [1/2] 启动 FastAPI 后端 (http://localhost:8000)...
start "AgentFlow API" cmd /c "uvicorn main:app --host 0.0.0.0 --port 8000"

REM 等待 FastAPI 启动
timeout /t 5 /nobreak >nul

echo [2/2] 启动 Streamlit 前端 (http://localhost:8501)...
start "AgentFlow WebUI" cmd /c "streamlit run app.py --server.port 8501"

echo.
echo ========================================
echo   AgentFlow 已启动！
echo   API 文档: http://localhost:8000/docs
echo   Web 界面: http://localhost:8501
echo ========================================
echo.
echo 关闭窗口即可停止所有服务。
pause
