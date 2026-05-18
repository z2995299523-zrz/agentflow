# AgentFlow — 企业级 AI 智能助手平台

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![LangChain](https://img.shields.io/badge/LangChain-1.x-green.svg)](https://www.langchain.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.x-orange.svg)](https://langchain-ai.github.io/langgraph/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

开箱即用的 AI 助手平台，支持文档知识库问答（RAG）和自然语言数据分析（Text-to-SQL），多 Agent 协作，一键 Docker 部署。

##   Demo 预览

```
💬 智能对话                          📊 数据查询
┌─────────────────────────┐      ┌─────────────────────────┐
│ 用户：AgentFlow有哪些核 │      │ 用户：销售额最高的3个    │
│       心功能？          │      │       产品是哪些？       │
│                        │      │                         │
│ 🤖：根据文档，核心功能  │      │ 🔍 SQL:                 │
│   1. 文档知识库问答(RAG) │      │ SELECT ... ORDER BY     │
│   2. 自然语言数据分析    │      │    SUM(sales) DESC      │
│   3. 统一API接口         │      │    LIMIT 3              │
│   4. 一键部署            │      │                         │
│ 📖 来源：产品介绍.txt    │      │ 📊 ████████████ (图表)   │
└─────────────────────────┘      └─────────────────────────┘
```

## 🏗 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    Streamlit WebUI                       │
│         💬 Chat  │  📄 Upload  │  📊 Query              │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│                 LangGraph Supervisor                     │
│              🎯 意图识别 → 智能路由                       │
│         ┌────────┼────────┐         │                   │
│         ▼        ▼        ▼         ▼                   │
│     ┌──────┐ ┌──────┐ ┌──────┐ ┌────────┐              │
│     │ RAG  │ │ SQL  │ │Mixed │ │Fallback│              │
│     │ Agent│ │ Agent│ │双 路 │ │  兜底  │              │
│     └──┬───┘ └──┬───┘ └──┬───┘ └───┬────┘              │
│        │        │        │         │                     │
│        ▼        ▼        ▼         ▼                     │
│     ┌──────────────────────────────────┐                │
│     │         Finalize / Answer         │               │
│     └──────────────────────────────────┘                │
└─────────────────────────────────────────────────────────┘
         │              │              │
         ▼              ▼              ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │ ChromaDB │  │ SQLite / │  │  DeepSeek │
   │ (向量库) │  │PostgreSQL│  │   (LLM)   │
   └──────────┘  └──────────┘  └──────────┘
```

## ✨ 核心功能

| 功能 | 说明 | 状态 |
|------|------|:--:|
| 📚 知识库问答 | 上传 PDF/Word/TXT/MD，自然语言提问，AI 基于文档回答（带引用来源） | ✅ |
| 📊 自然语言查数据 | Text-to-SQL：用中文描述需求，自动生成 SQL 并执行 | ✅ |
| 🤖 多 Agent 协作 | LangGraph Supervisor 自动判断意图，路由到 RAG/SQL/Mixed | ✅ |
| 📈 自动可视化 | 查询结果自动选择柱状图/折线图/饼图展示 | ✅ |
| 🔌 REST API | FastAPI 统一接口，Swagger 自动文档 | ✅ |
| 💬 Web 界面 | Streamlit 三模块：Chat / Upload / Query，流式输出 | ✅ |
| 🐳 一键部署 | Docker Compose 启动，改个 .env 就能跑 | ✅ |
| 🧠 多轮对话 | MemorySaver 记忆上下文，支持追问 | ✅ |

## 🚀 快速开始

### 方式一：Docker（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/z2995299523-zrz/agentflow.git
cd agentflow

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY=你的Key

# 3. 一键启动
docker-compose up -d

# 4. 打开浏览器
# Web UI:  http://localhost:8501
# API文档: http://localhost:8000/docs
```

### 方式二：本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 国内加速：
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 3. 启动（Windows）
start.bat

# 3. 启动（Linux/Mac）
bash start.sh
```

## 📡 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `GET` | `/docs` | Swagger 自动文档 |
| `POST` | `/rag/upload` | 上传文档到知识库 |
| `POST` | `/rag/query` | 知识库问答（带来源引用） |
| `GET` | `/rag/documents` | 列出已上传文档 |
| `DELETE` | `/rag/documents/{name}` | 删除文档 |
| `POST` | `/sql/query` | 自然语言 → SQL 查询 |
| `POST` | `/sql/visualize` | 查询 + 自动生成图表（Base64 PNG） |
| `GET` | `/sql/schema` | 获取数据库表结构 |

## 🧪 测试

```bash
# 运行全部测试
python run_tests.py

# 快速模式（跳过 LLM 测试）
python run_tests.py --quick

# 运行质量评估
python run_eval.py
```

### 测试结果基线

| 套件 | 测试数 | 结果 |
|------|:--:|:--:|
| SQL 模块 | 18 | ✅ 全部通过 |
| RAG 模块 | 5 | ✅ 全部通过 |
| Function Calling | 9 | ✅ 全部通过 |
| LangGraph 工作流 | 17 | ✅ 全部通过 |

### 性能基线

| 指标 | 数值 |
|------|------|
| API P50 响应 | 7.4s |
| API P95 响应 | 15.8s |
| RAG 检索 | <0.1s（BGE GPU 加速） |
| 文档上传 | <0.1s（小文件） |

## 🛠 技术栈

| 层 | 技术 | 选型理由 |
|----|------|---------|
| LLM | DeepSeek Chat | 便宜、国内直连、中文好 |
| Embedding | BGE-small-zh-v1.5（本地） | 免费、离线、GPU 加速 |
| AI 框架 | LangChain 1.x + LangGraph | 生态最全、Agent 支持好 |
| 向量数据库 | ChromaDB | 轻量、零配置、持久化 |
| 后端 | FastAPI（异步） | 高性能、自动文档 |
| 前端 | Streamlit | 纯 Python、聊天组件原生 |
| 部署 | Docker Compose | 一键启动、环境隔离 |

## 📁 项目结构

```
agentflow/
├── app.py              # Streamlit WebUI（Chat+Upload+Query）
├── main.py             # FastAPI 入口（7 端点）
├── config.py           # 配置管理（多 Provider 切换）
├── run_tests.py        # 统一测试入口
├── run_eval.py         # 质量评估入口
│
├── rag/                # RAG 知识库模块
│   ├── loader.py       #   文档加载（PDF/Word/TXT/MD）
│   ├── vectorstore.py  #   ChromaDB + BGE 向量化
│   └── chain.py        #   LCEL 问答链（带来源引用）
│
├── sql/                # Text-to-SQL 模块
│   ├── agent.py        #   SQL Agent（安全封装，只读）
│   ├── visualizer.py   #   自动图表生成（bar/line/pie）
│   └── tools.py        #   Function Calling 工具封装
│
├── graph/              # LangGraph 多Agent 工作流
│   ├── state.py        #   共享 State 定义
│   ├── nodes.py        #   RAG + SQL 节点
│   ├── supervisor.py   #   意图识别路由器
│   ├── workflow.py     #   StateGraph 组装
│   └── memory.py       #   多轮对话记忆
│
├── prompts/            # Prompt 工程体系
├── eval/               # 评估模块（路由/端到端/LLM-as-Judge）
├── notes/              # 技术笔记库（24篇）
│
├── docker-compose.yml  # Docker 编排
├── Dockerfile          # Docker 镜像
├── start.bat           # Windows 一键启动
├── start.sh            # Linux/Mac 一键启动
├── .env.example        # 环境变量模板
└── requirements.txt    # Python 依赖
```

## 📝 开发进度

- [x] Week 1-2: RAG 知识库问答
- [x] Week 3-4: Text-to-SQL + Function Calling
- [x] Week 5: LangGraph 多Agent 工作流
- [x] Week 6: Streamlit WebUI + 流式输出 + 端到端测试
- [x] Week 7: Docker 部署 + 测试体系 + 文档
- [ ] Week 8: 面试冲刺

## 👤 作者

**张润泽** — 6 年银行对公数据开发经验，全职转型 AI 应用开发。

- GitHub: [@z2995299523-zrz](https://github.com/z2995299523-zrz)
- 邮箱: z2995299523@gmail.com
- 技术栈: Python / LangChain / RAG / Text-to-SQL / Agent

---

📌 **面试中怎么说**：这是一个从 0 到 1 独立开发的企业级 AI Agent 平台，覆盖 RAG + Text-to-SQL + 多 Agent 协作三大核心能力。LangGraph Supervisor 自动判断用户意图并路由到对应 Agent，支持 RAG 和 SQL 的串联分析。全栈自研（FastAPI + Streamlit + Docker），有完整的测试体系和性能基线（P50=7.4s）。
