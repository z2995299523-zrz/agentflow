#!/bin/bash
# AgentFlow Linux/Mac 一键启动脚本

set -e

echo "========================================"
echo "  AgentFlow 启动中..."
echo "========================================"

# 检查 .env
if [ ! -f .env ]; then
    echo "[!] 未找到 .env 文件"
    echo "    请复制 .env.example 为 .env 并填入 DeepSeek API Key"
    exit 1
fi

# 设置 HuggingFace 镜像
export HF_ENDPOINT=https://hf-mirror.com

# 加载环境变量
set -a
source .env
set +a

echo "[1/2] 启动 FastAPI 后端 (http://localhost:8000)..."
uvicorn main:app --host 0.0.0.0 --port 8000 &
API_PID=$!

sleep 5

echo "[2/2] 启动 Streamlit 前端 (http://localhost:8501)..."
streamlit run app.py --server.port 8501 --server.address 0.0.0.0 &
UI_PID=$!

echo ""
echo "========================================"
echo "  AgentFlow 已启动！"
echo "  API 文档: http://localhost:8000/docs"
echo "  Web 界面: http://localhost:8501"
echo "========================================"
echo ""
echo "按 Ctrl+C 停止所有服务"

# 捕获退出信号，清理子进程
trap "kill $API_PID $UI_PID 2>/dev/null; exit" INT TERM
wait
