#!/bin/bash
# AgentFlow Linux/Mac 一键启动：接口服务（FastAPI，8000）+ 界面（React 构建产物，vite preview，默认 8080）
# 前后端分开两个进程；界面把 /api 开头的请求转给接口服务。改前端代码时用 cd web && npm run dev（热更新，5173）

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

# 检查 Node.js：界面要 Node 20.19 或 22.12 以上
if ! command -v node >/dev/null 2>&1; then
    echo "[!] 没有找到 Node.js，界面需要它：https://nodejs.org/"
    exit 1
fi

# 设置 HuggingFace 镜像
export HF_ENDPOINT=https://hf-mirror.com

# 加载环境变量
set -a
source .env
set +a
export WEB_PORT=${WEB_PORT:-8080}

# 第一次运行：装前端依赖（国内用 npmmirror 镜像）、构建界面。改了前端代码后运行 npm run build 重新构建
if [ ! -d web/node_modules ]; then
    echo "[准备] 安装前端依赖..."
    (cd web && npm ci --registry=https://registry.npmmirror.com)
fi
if [ ! -f web/dist/index.html ]; then
    echo "[准备] 构建界面..."
    (cd web && npm run build)
fi

echo "[1/2] 启动接口服务 (http://localhost:8000)..."
uvicorn main:app --host 0.0.0.0 --port 8000 &
API_PID=$!

echo "[2/2] 启动界面 (http://localhost:${WEB_PORT})..."
(cd web && npm run preview -- --host 0.0.0.0) &
WEB_PID=$!

echo ""
echo "========================================"
echo "  AgentFlow 已启动！"
echo "  界面:     http://localhost:${WEB_PORT}"
echo "  接口文档: http://localhost:8000/docs"
echo "  接口刚启动时在后台加载模型，第一问可能要多等一会儿"
echo "  还没导入元数据的话，先运行 python -m metadata.importer"
echo "========================================"
echo ""
echo "按 Ctrl+C 停止所有服务"

# 捕获退出信号，清理子进程
trap "kill $API_PID $WEB_PID 2>/dev/null; exit" INT TERM
wait
