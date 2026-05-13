# AgentFlow

企业级 AI 智能助手平台 — 支持文档知识库问答（RAG）和自然语言数据分析（Text-to-SQL）

## 这是什么？

一个开箱即用的 AI 助手平台。你可以：

- 📄 **上传文档**（PDF/Word/TXT/Markdown），用自然语言提问，AI 基于文档内容回答
- 📊 **用自然语言查数据库**，不用写 SQL 也能做数据分析，结果自动可视化
- 🔌 **一个 API 搞定**，RAG 和 SQL 分析通过统一的 FastAPI 接口调用
- 🐳 **一键部署**，Docker Compose 启动，改个配置就能跑

## 技术栈

| 层 | 技术 |
|----|------|
| LLM | OpenAI API / DeepSeek / 通义千问（兼容 OpenAI 格式） |
| AI 框架 | LangChain |
| 向量数据库 | ChromaDB |
| 后端 | FastAPI（异步） |
| 前端 | Streamlit |
| 部署 | Docker + docker-compose |

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/z2995299523-zrz/agentflow.git
cd agentflow

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 API Key

# 3. 启动
docker-compose up -d

# 4. 打开浏览器
# Web UI: http://localhost:8501
# API 文档: http://localhost:8000/docs
```

## 项目结构

```
agentflow/
├── app.py              # Streamlit Web 界面
├── main.py             # FastAPI 后端入口
├── config.py           # 配置管理
├── rag/                # RAG 知识库模块
│   ├── loader.py       # 文档加载器
│   ├── vectorstore.py  # 向量化存储
│   └── chain.py        # 问答链
├── sql/                # SQL Agent 模块
│   ├── agent.py        # Text-to-SQL Agent
│   └── visualizer.py   # 查询结果可视化
├── data/               # 示例数据
│   └── sample_db.sql   # 示例数据库
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 开发状态

🚧 v1.0 开发中（2026年5月启动）

- [ ] RAG 知识库问答
- [ ] Text-to-SQL 数据分析
- [ ] FastAPI 统一接口
- [ ] Streamlit Web 界面
- [ ] Docker 一键部署

## 作者

张润泽 — 6年银行数据开发经验，正在转型 AI 应用开发

GitHub: [@z2995299523-zrz](https://github.com/z2995299523-zrz)
