@echo off
chcp 65001 >nul
REM AgentFlow Windows 一键启动：接口服务（FastAPI，8000）+ 界面（React 构建产物，vite preview，默认 8080）
REM 前后端分开两个进程；界面把 /api 开头的请求转给接口服务。改前端代码时进 web 目录运行 npm run dev（热更新，5173）

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

REM 检查 Node.js：界面要 Node 20.19 或 22.12 以上
where node >nul 2>nul
if errorlevel 1 (
    echo [!] 没有找到 Node.js，界面需要它：https://nodejs.org/
    pause
    exit /b 1
)

REM 设置 HuggingFace 镜像（国内加速 BGE 模型下载）
set HF_ENDPOINT=https://hf-mirror.com
if "%WEB_PORT%"=="" set WEB_PORT=8080

REM 第一次运行：装前端依赖（国内用 npmmirror 镜像）、构建界面。改了前端代码后删掉 web\dist 或运行 npm run build 重新构建
if not exist web\node_modules (
    echo [准备] 安装前端依赖...
    pushd web
    call npm ci --registry=https://registry.npmmirror.com
    popd
)
if not exist web\dist\index.html (
    echo [准备] 构建界面...
    pushd web
    call npm run build
    popd
)

echo [1/2] 启动接口服务 (http://localhost:8000)...
start "AgentFlow API" cmd /c "uvicorn main:app --host 0.0.0.0 --port 8000"

echo [2/2] 启动界面 (http://localhost:%WEB_PORT%)...
start "AgentFlow Web" cmd /c "cd web && npm run preview -- --host 0.0.0.0"

echo.
echo ========================================
echo   AgentFlow 已启动！
echo   界面:     http://localhost:%WEB_PORT%
echo   接口文档: http://localhost:8000/docs
echo   接口刚启动时在后台加载模型，第一问可能要多等一会儿
echo   还没导入元数据的话，先运行 python -m metadata.importer
echo ========================================
echo.
echo 关闭两个服务窗口即可停止。
pause
