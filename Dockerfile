# AgentFlow 接口服务镜像（界面的镜像在 web/Dockerfile，docker-compose 把两个一起起）
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目代码（web/ 在 .dockerignore 里排除了）
COPY . .

# 导入模拟专题库的元数据（不算向量，几秒钟；召回只走关键字。要向量就在容器里去掉 --no-index 重新导入）
RUN python -m metadata.importer data/mock_mart --no-index

# 暴露端口
EXPOSE 8000

# 启动接口服务：启动后在后台预热流水线，第一问不用等冷启动
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
