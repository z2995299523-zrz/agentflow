# AgentFlow — Project Context for Claude Code

## Project Overview
AgentFlow 是一个企业级 AI 智能助手平台（个人作品项目），用于面试展示。包含三个核心模块：
- RAG 知识库问答（已完成）
- Text-to-SQL 数据分析（待开发）
- LangGraph 多 Agent 工作流（待开发）

## Tech Stack
- Python 3.12+
- LangChain 1.x (LCEL 链式调用)
- 向量模型: BAAI/bge-small-zh-v1.5 (本地, 通过 sentence-transformers)
- 向量数据库: ChromaDB
- LLM: DeepSeek v4-pro (通过 OpenAI 兼容 API)
- WebUI: Streamlit

## Project Structure
```
C:\Users\DELL\agentflow\
├── config.py           # 全局配置 (API keys, 模型参数, 路径)
├── rag/                # RAG 模块 (已完成)
│   ├── loader.py       # 文档加载 (PDF, TXT, Markdown)
│   ├── vectorstore.py  # 向量化存储 (ChromaDB + BGE)
│   ├── chain.py        # LCEL 问答链
│   └── __init__.py
├── sql/                # Text-to-SQL 模块 (待开发)
│   ├── agent.py        # SQL Agent 骨架
│   ├── visualizer.py   # 结果可视化骨架
│   └── __init__.py
├── test_rag.py         # RAG 测试
├── Data/               # 示例文档
├── Dockerfile
└── requirements.txt
```

## Key Config (config.py)
- DEEPSEEK_API_KEY — 从环境变量读取
- DEEPSEEK_BASE_URL — https://api.deepseek.com/v1
- 默认模型: deepseek-chat
- ChromaDB 本地持久化路径: ./chroma_db

## Coding Standards
- 所有公开函数必须写中文 docstring
- 使用 type hints
- 4 空格缩进
- 文件用 UTF-8 编码
- 代码注释用中文

## Important Notes
- 用户是国内环境，某些 Python 包下载可能慢，优先用清华/阿里镜像
- 不允许修改 config.py 中的 API key（从环境变量读取）
- LLM 调用必须通过 LangChain 的 ChatOpenAI 兼容接口
- 该项目的目标是面试展示，代码要有清晰的架构和完整的注释
